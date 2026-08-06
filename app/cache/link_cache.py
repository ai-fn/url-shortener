"""Redis link cache. Cache-aside: populate on a Postgres read, invalidate with DEL.

Every value here is reconstructible from Postgres — SET is never used to invalidate,
only DEL. A SET races an in-flight reader that fetched the old row before the write
and writes it back after, installing the stale value with a fresh TTL.
"""

from __future__ import annotations

import enum
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

import redis.asyncio as aioredis
from redis.exceptions import RedisError

_KEY_PREFIX = "link:"
# Not valid JSON, so a payload and the negative sentinel can never be confused.
_NEGATIVE_SENTINEL = b"\x00"


class LinkCacheUnavailable(Exception):
    """Redis could not be reached. Callers decide open/closed — see app/core/rate_limit.py."""


class Lookup(enum.Enum):
    MISS = "miss"
    NEGATIVE = "negative"


@dataclass(frozen=True)
class CachedLink:
    link_id: uuid.UUID
    target_url: str
    is_active: bool
    expires_at: datetime | None


_GENERATION_TTL_SECONDS = 30

# Closes the gap a get-then-set fencing check leaves open: between reading the
# generation and issuing the SET, a concurrent invalidate() can still land, and the
# SET would reinstall the value it just invalidated. Checking and writing in one
# server-side step removes that window instead of narrowing it. Same pattern as
# app/core/rate_limit.py's bucket script.
_STORE_IF_GENERATION_UNCHANGED_LUA = """
local value_key = KEYS[1]
local gen_key = KEYS[2]
local payload = ARGV[1]
local ttl = ARGV[2]
local expected_generation = ARGV[3]

local current = redis.call('GET', gen_key)
if current and current ~= expected_generation then
    return 0
end

redis.call('SET', value_key, payload, 'EX', ttl)
return 1
"""


def _key(code: str) -> str:
    return f"{_KEY_PREFIX}{code}"


def _gen_key(code: str) -> str:
    return f"{_KEY_PREFIX}{code}:gen"


def _encode(link: CachedLink) -> str:
    return json.dumps(
        {
            "link_id": str(link.link_id),
            "target_url": link.target_url,
            "is_active": link.is_active,
            "expires_at": link.expires_at.isoformat() if link.expires_at is not None else None,
        }
    )


def _decode(raw: bytes | str) -> CachedLink | None:
    """None on anything unparseable — a schema change between deploys must not 500
    the redirect, it must just look like a cache miss."""
    try:
        data = json.loads(raw)
        return CachedLink(
            link_id=uuid.UUID(data["link_id"]),
            target_url=data["target_url"],
            is_active=data["is_active"],
            expires_at=datetime.fromisoformat(data["expires_at"]) if data["expires_at"] else None,
        )
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


async def get(redis: aioredis.Redis, code: str) -> CachedLink | Lookup:
    """Raises LinkCacheUnavailable on a Redis error — callers decide the fallback."""
    try:
        raw = await redis.get(_key(code))
    except RedisError as exc:
        raise LinkCacheUnavailable(str(exc)) from exc

    if raw is None:
        return Lookup.MISS
    if raw == _NEGATIVE_SENTINEL:
        return Lookup.NEGATIVE

    decoded = _decode(raw)
    return decoded if decoded is not None else Lookup.MISS


async def store(redis: aioredis.Redis, code: str, link: CachedLink, *, ttl_seconds: int) -> None:
    try:
        await redis.set(_key(code), _encode(link), ex=ttl_seconds)
    except RedisError as exc:
        raise LinkCacheUnavailable(str(exc)) from exc


async def store_negative(redis: aioredis.Redis, code: str, *, ttl_seconds: int) -> None:
    try:
        await redis.set(_key(code), _NEGATIVE_SENTINEL, ex=ttl_seconds)
    except RedisError as exc:
        raise LinkCacheUnavailable(str(exc)) from exc


async def store_if_generation_unchanged(
    redis: aioredis.Redis,
    code: str,
    link: CachedLink,
    *,
    ttl_seconds: int,
    expected_generation: int | None,
) -> bool:
    """Atomic counterpart to store(): populates the cache only if get_generation is
    still what the caller observed before its Postgres read, so a concurrent
    invalidate() landing after that read can't be overwritten by this store. Returns
    whether the store happened. expected_generation=None means the caller's earlier
    generation read itself failed, so there is nothing to fence against — this skips
    the write rather than falling back to an unprotected SET, which would reopen the
    exact race fencing exists to close. A skipped populate just costs one more
    Postgres read on the next request; that's the same cost every other
    cache-unavailable path here already accepts."""
    return await _store_if_generation_unchanged(
        redis, code, _encode(link), ttl_seconds, expected_generation
    )


async def store_negative_if_generation_unchanged(
    redis: aioredis.Redis,
    code: str,
    *,
    ttl_seconds: int,
    expected_generation: int | None,
) -> bool:
    """Negative-sentinel counterpart to store_if_generation_unchanged."""
    return await _store_if_generation_unchanged(
        redis, code, _NEGATIVE_SENTINEL, ttl_seconds, expected_generation
    )


async def _store_if_generation_unchanged(
    redis: aioredis.Redis,
    code: str,
    payload: bytes | str,
    ttl_seconds: int,
    expected_generation: int | None,
) -> bool:
    if expected_generation is None:
        return False
    try:
        result = await redis.eval(
            _STORE_IF_GENERATION_UNCHANGED_LUA,
            2,
            _key(code),
            _gen_key(code),
            payload,
            ttl_seconds,
            expected_generation,
        )
    except RedisError as exc:
        raise LinkCacheUnavailable(str(exc)) from exc
    return bool(result)


async def get_generation(redis: aioredis.Redis, code: str) -> int:
    """Fencing token for the miss-then-repopulate race: a caller reads this before its
    Postgres read and passes it to store_if_generation_unchanged, which re-checks it
    atomically against invalidate()'s bump at store time — otherwise a read that
    straddles a concurrent write can cache the value the write just made stale.
    Expires with the invalidation it belongs to, so it costs no unbounded Redis memory
    for links that are never updated again."""
    try:
        raw = await redis.get(_gen_key(code))
    except RedisError as exc:
        raise LinkCacheUnavailable(str(exc)) from exc
    return int(raw) if raw is not None else 0


async def invalidate(redis: aioredis.Redis, code: str) -> None:
    """DEL only for the value — see the module docstring for why SET is never used
    here. Also bumps the fencing counter get_generation reads."""
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.delete(_key(code))
            pipe.incr(_gen_key(code))
            pipe.expire(_gen_key(code), _GENERATION_TTL_SECONDS)
            await pipe.execute()
    except RedisError as exc:
        raise LinkCacheUnavailable(str(exc)) from exc
