"""Link creation, lookup and lifecycle. All business logic; routes stay thin.

Short-code uniqueness is enforced by the database constraint and handled via
IntegrityError, never a read-then-write precheck, which races: two concurrent
requests can both see a code as free and both insert it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import link_cache
from app.core import short_code
from app.core.db_errors import constraint_name
from app.core.url_validation import validate_target_url
from app.models.link import Link

logger = logging.getLogger(__name__)

MAX_GENERATION_ATTEMPTS = 5

# Matches migrations/versions/0001_create_links.py. Checked before an IntegrityError
# is reinterpreted as a code collision, so an unrelated constraint violation surfaces
# instead of being silently retried into a misleading 503.
_SHORT_CODE_UNIQUE_CONSTRAINT = "uq_links_short_code"


class LinkNotFoundError(Exception):
    """No link matches the given id or code."""


class LinkAliasTakenError(Exception):
    """The requested custom alias is already in use. Never silently re-rolled —
    a caller who asked for a specific alias should learn if it's unavailable."""


class InvalidAliasError(ValueError):
    """The requested custom alias fails shape or reserved-word checks."""


class LinkCreationExhaustedError(Exception):
    """Every generated code collided MAX_GENERATION_ATTEMPTS times in a row."""


@dataclass(frozen=True)
class LinkCreate:
    target_url: str
    custom_alias: str | None = None
    title: str | None = None
    description: str | None = None
    expires_at: datetime | None = None


async def _invalidate_or_raise(redis: aioredis.Redis, code: str, *, message: str) -> None:
    """Fail-closed cache invalidation shared by update_link and soft_delete: a caller
    that can't invalidate must not report success on a mutation, or a stale/wrong
    value keeps resolving from the cache for up to link_cache_ttl_seconds."""
    try:
        await link_cache.invalidate(redis, code)
    except link_cache.LinkCacheUnavailable as exc:
        logger.warning(message, extra={"code": code, "error": str(exc)})
        raise


def _validate_alias(alias: str) -> None:
    if not short_code.is_valid_shape(alias):
        raise InvalidAliasError("alias does not match the required shape")
    if short_code.is_reserved(alias):
        raise InvalidAliasError("alias is a reserved word")


async def create_link(
    session: AsyncSession,
    data: LinkCreate,
    *,
    owner_id: uuid.UUID,
    public_host: str,
    redis: aioredis.Redis,
) -> Link:
    """Validate the target URL, then insert with either the caller's alias or a
    generated code. Raises URLValidationError, InvalidAliasError, LinkAliasTakenError
    or LinkCreationExhaustedError — the route maps each to its HTTP status.

    Unlike update_link/soft_delete, a cache-invalidation failure here is logged and
    swallowed rather than raised: the row is already committed, and POST is not safe
    to retry the way PATCH/DELETE are — a retry with the same custom_alias would
    collide with the row this call just created and see LinkAliasTakenError, turning
    a successful request into a confusing failure for the caller. There is also no
    *stale* value at risk the way there is for update/delete: at worst, a negative
    sentinel written by a probe that raced this commit outlives it, which self-heals
    within link_cache_negative_ttl_seconds and is additionally guarded by the fencing
    generation link_cache.invalidate() bumps — see app/services/redirect.py.
    """
    await validate_target_url(data.target_url, public_host=public_host)

    if data.custom_alias is not None:
        _validate_alias(data.custom_alias)
        codes_to_try = [data.custom_alias]
    else:
        # Astronomically unlikely at this length, but a generated code landing on
        # a reserved word (e.g. "healthz") would insert successfully — short_code
        # is only unique, not reserved-checked — and then be permanently
        # unreachable, since the real route always wins over the catch-all.
        codes_to_try = []
        while len(codes_to_try) < MAX_GENERATION_ATTEMPTS:
            candidate = short_code.generate()
            if not short_code.is_reserved(candidate):
                codes_to_try.append(candidate)

    last_error: IntegrityError | None = None
    for code in codes_to_try:
        link = Link(
            short_code=code,
            owner_id=owner_id,
            target_url=data.target_url,
            title=data.title,
            description=data.description,
            expires_at=data.expires_at,
        )
        session.add(link)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if constraint_name(exc) != _SHORT_CODE_UNIQUE_CONSTRAINT:
                raise
            if data.custom_alias is not None:
                raise LinkAliasTakenError(data.custom_alias) from exc
            last_error = exc
            continue
        else:
            await session.refresh(link)
            try:
                await link_cache.invalidate(redis, link.short_code)
            except link_cache.LinkCacheUnavailable as exc:
                logger.warning(
                    "post-create cache invalidation failed",
                    extra={"code": link.short_code, "error": str(exc)},
                )
            return link

    raise LinkCreationExhaustedError(
        f"could not allocate a unique code after {MAX_GENERATION_ATTEMPTS} attempts"
    ) from last_error


async def get_link(session: AsyncSession, link_id: uuid.UUID, *, owner_id: uuid.UUID) -> Link:
    """Folds "no such link" and "not yours" into the same LinkNotFoundError — a
    caller must not learn a link exists by getting a different error for someone
    else's id, the same enumeration-safety reasoning is_redirectable's callers use."""
    link = await session.get(Link, link_id)
    if link is None or link.owner_id != owner_id:
        raise LinkNotFoundError(str(link_id))
    return link


async def list_links(
    session: AsyncSession, *, owner_id: uuid.UUID, limit: int = 50, offset: int = 0
) -> list[Link]:
    result = await session.execute(
        select(Link)
        .where(Link.owner_id == owner_id)
        # id as a secondary key: created_at alone ties for rows inserted in the
        # same transaction, and an untied ORDER BY can skip or repeat rows across
        # separately-fetched pages.
        .order_by(Link.created_at.desc(), Link.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


async def update_link(
    session: AsyncSession,
    link_id: uuid.UUID,
    *,
    owner_id: uuid.UUID,
    target_url: str | None = None,
    title: str | object | None = ...,
    description: str | object | None = ...,
    expires_at: datetime | object | None = ...,
    is_active: bool | None = None,
    public_host: str,
    redis: aioredis.Redis,
) -> Link:
    """`...` (Ellipsis) means "leave unchanged" for nullable fields, since `None` is
    itself a valid value (clear the field) and can't double as "not provided".

    Invalidates the cache after commit, never before: a DEL before commit lets a
    concurrent reader repopulate the still-old row. May raise LinkCacheUnavailable —
    unlike create_link, this must fail closed: a caller who can't invalidate must not
    report success, or a stale, wrong target_url keeps resolving from the cache for
    up to link_cache_ttl_seconds. PATCH is safe to retry (same link_id, no collision),
    which create_link's custom-alias case is not.
    """
    link = await get_link(session, link_id, owner_id=owner_id)

    if target_url is not None:
        await validate_target_url(target_url, public_host=public_host)
        link.target_url = target_url
    if title is not ...:
        link.title = title  # type: ignore[assignment]
    if description is not ...:
        link.description = description  # type: ignore[assignment]
    if expires_at is not ...:
        link.expires_at = expires_at  # type: ignore[assignment]
    if is_active is not None:
        link.is_active = is_active

    await session.commit()
    await session.refresh(link)
    await _invalidate_or_raise(
        redis, link.short_code, message="post-update cache invalidation failed"
    )
    return link


async def soft_delete(
    session: AsyncSession, link_id: uuid.UUID, *, owner_id: uuid.UUID, redis: aioredis.Redis
) -> None:
    """Flips is_active rather than deleting the row, so a click event recorded
    earlier keeps a valid link_id to join against. Invalidates after commit and fails
    closed on LinkCacheUnavailable, same reasoning as update_link: a takedown that
    isn't reflected in the cache is a wrong redirect kept alive, not a stale absence."""
    link = await get_link(session, link_id, owner_id=owner_id)
    link.is_active = False
    await session.commit()
    await _invalidate_or_raise(
        redis, link.short_code, message="post-delete cache invalidation failed"
    )


async def get_by_code(session: AsyncSession, code: str) -> Link | None:
    """The raw row, with no active/expiry judgment — get_redirect_target's old job is
    now split so the redirect cache can populate an entry for a dead link too. Use
    is_redirectable() to make the same judgment the old function made inline."""
    result = await session.execute(select(Link).where(Link.short_code == code))
    return result.scalar_one_or_none()


def is_redirectable(
    *, is_active: bool, expires_at: datetime | None, now: datetime | None = None
) -> bool:
    """Pure predicate, callable against either a Postgres row or a cached payload —
    callers must not distinguish "inactive" from "expired" from "not found" in their
    response, or an enumeration attacker can map out which codes ever existed."""
    if not is_active:
        return False
    effective_now = now if now is not None else datetime.now(UTC)
    return expires_at is None or expires_at > effective_now
