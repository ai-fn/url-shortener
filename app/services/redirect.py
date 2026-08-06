"""Cache-aside resolution for GET /{code}. Never imports auth — see invariant 9,
enforced by scripts/check_invariants.py's auth-on-redirect-hot-path rule, which scopes
to this module as well as app/api/redirect.py and app/cache/.

A cache hit or a negative sentinel answers without ever opening a Postgres session.
Only a miss — or the cache itself being unavailable — reaches Postgres.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import link_cache
from app.cache.link_cache import CachedLink, Lookup
from app.core import metrics
from app.services import links as links_service
from app.services.links import LinkNotFoundError

logger = logging.getLogger(__name__)


async def resolve(
    *,
    redis: aioredis.Redis,
    sessionmaker: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    code: str,
    ttl_seconds: int,
    negative_ttl_seconds: int,
) -> CachedLink:
    """Raises LinkNotFoundError for absent, inactive or expired alike — callers must
    not tell those apart in the response, the same enumeration-safety reasoning
    links_service.is_redirectable's docstring gives."""
    cache_errored = False
    try:
        cached = await link_cache.get(redis, code)
    except link_cache.LinkCacheUnavailable:
        metrics.LINK_CACHE_LOOKUPS.labels(result="error").inc()
        cache_errored = True
        cached = Lookup.MISS

    if cached is Lookup.NEGATIVE:
        metrics.LINK_CACHE_LOOKUPS.labels(result="negative").inc()
        raise LinkNotFoundError(code)

    if isinstance(cached, CachedLink):
        metrics.LINK_CACHE_LOOKUPS.labels(result="hit").inc()
        if not links_service.is_redirectable(
            is_active=cached.is_active, expires_at=cached.expires_at
        ):
            raise LinkNotFoundError(code)
        return cached

    if not cache_errored:
        metrics.LINK_CACHE_LOOKUPS.labels(result="miss").inc()

    generation = await _safe_generation(redis, code)

    async with sessionmaker() as session:
        link = await links_service.get_by_code(session, code)

    if link is None:
        await _try_store_negative(redis, code, negative_ttl_seconds, generation)
        raise LinkNotFoundError(code)

    resolved = CachedLink(
        link_id=link.id,
        target_url=link.target_url,
        is_active=link.is_active,
        expires_at=link.expires_at,
    )
    await _try_store(redis, code, resolved, ttl_seconds, generation)

    if not links_service.is_redirectable(is_active=link.is_active, expires_at=link.expires_at):
        raise LinkNotFoundError(code)
    return resolved


async def _safe_generation(redis: aioredis.Redis, code: str) -> int | None:
    """None means the fencing check couldn't run — treat it as disabled (fail open),
    same as any other cache-unavailable path on the redirect route."""
    try:
        return await link_cache.get_generation(redis, code)
    except link_cache.LinkCacheUnavailable:
        return None


async def _try_store(
    redis: aioredis.Redis, code: str, link: CachedLink, ttl_seconds: int, generation: int | None
) -> None:
    """A cache that cannot be written to makes the redirect slow, not failed — the
    write failure is swallowed after being counted. The store itself is a single
    atomic check-and-set against `generation` — see
    link_cache.store_if_generation_unchanged — so a concurrent invalidate() can't land
    between a fencing check and this write and get overwritten by it."""
    try:
        await link_cache.store_if_generation_unchanged(
            redis, code, link, ttl_seconds=ttl_seconds, expected_generation=generation
        )
    except link_cache.LinkCacheUnavailable as exc:
        logger.warning("link cache populate failed", extra={"code": code, "error": str(exc)})


async def _try_store_negative(
    redis: aioredis.Redis, code: str, ttl_seconds: int, generation: int | None
) -> None:
    try:
        await link_cache.store_negative_if_generation_unchanged(
            redis, code, ttl_seconds=ttl_seconds, expected_generation=generation
        )
    except link_cache.LinkCacheUnavailable as exc:
        logger.warning(
            "link cache negative populate failed", extra={"code": code, "error": str(exc)}
        )
