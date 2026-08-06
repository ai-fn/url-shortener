"""No real Redis: a recording fake proves the DEL-not-SET contract and error
handling without a container. Real-Redis coverage lives in
tests/test_redirect_cache.py's integration cases."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from redis.exceptions import RedisError

from app.cache import link_cache
from app.cache.link_cache import CachedLink, Lookup
from app.services.links import is_redirectable


class _FakeRedis:
    """Records every command name so tests can assert DEL was used, never SET."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.calls: list[str] = []
        self.raise_on: str | None = None

    def _maybe_raise(self, command: str) -> None:
        self.calls.append(command)
        if self.raise_on == command:
            raise RedisError(f"simulated failure on {command}")

    async def get(self, key: str) -> bytes | None:
        self._maybe_raise("get")
        return self.store.get(key)

    async def set(self, key: str, value: bytes | str, *, ex: int) -> None:
        self._maybe_raise("set")
        self.store[key] = value if isinstance(value, bytes) else value.encode()

    async def delete(self, key: str) -> None:
        self._maybe_raise("delete")
        self.store.pop(key, None)

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)


class _FakePipeline:
    """Queues commands like a real redis-py pipeline: no I/O until execute()."""

    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, tuple[object, ...]]] = []

    async def __aenter__(self) -> _FakePipeline:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    def delete(self, key: str) -> _FakePipeline:
        self._ops.append(("delete", (key,)))
        return self

    def incr(self, key: str) -> _FakePipeline:
        self._ops.append(("incr", (key,)))
        return self

    def expire(self, key: str, seconds: int) -> _FakePipeline:
        self._ops.append(("expire", (key, seconds)))
        return self

    async def execute(self) -> list[object]:
        results: list[object] = []
        for command, args in self._ops:
            self._redis._maybe_raise(command)
            if command == "delete":
                self._redis.store.pop(args[0], None)
                results.append(1)
            elif command == "incr":
                current = int(self._redis.store.get(args[0], b"0")) + 1
                self._redis.store[args[0]] = str(current).encode()
                results.append(current)
            elif command == "expire":
                results.append(True)
        return results


def _link(**overrides: object) -> CachedLink:
    defaults: dict[str, object] = {
        "link_id": uuid.uuid4(),
        "target_url": "https://example.com/",
        "is_active": True,
        "expires_at": None,
    }
    defaults.update(overrides)
    return CachedLink(**defaults)  # type: ignore[arg-type]


async def test_store_then_get_round_trips() -> None:
    redis = _FakeRedis()
    link = _link(expires_at=datetime(2030, 1, 1, tzinfo=UTC))

    await link_cache.store(redis, "abc123", link, ttl_seconds=60)
    result = await link_cache.get(redis, "abc123")

    assert result == link


async def test_store_then_get_round_trips_with_no_expiry() -> None:
    redis = _FakeRedis()
    link = _link(expires_at=None)

    await link_cache.store(redis, "abc123", link, ttl_seconds=60)
    result = await link_cache.get(redis, "abc123")

    assert result == link


async def test_missing_key_is_miss() -> None:
    redis = _FakeRedis()

    assert await link_cache.get(redis, "nope") is Lookup.MISS


async def test_negative_sentinel_is_negative_never_a_payload() -> None:
    redis = _FakeRedis()

    await link_cache.store_negative(redis, "nope", ttl_seconds=60)

    assert await link_cache.get(redis, "nope") is Lookup.NEGATIVE


async def test_corrupt_payload_is_a_miss_not_an_exception() -> None:
    """A schema change between deploys must not 500 the redirect."""
    redis = _FakeRedis()
    redis.store["link:broken"] = b"{not json"

    assert await link_cache.get(redis, "broken") is Lookup.MISS


async def test_payload_missing_a_field_is_a_miss() -> None:
    redis = _FakeRedis()
    redis.store["link:broken"] = b'{"link_id": "not-a-uuid"}'

    assert await link_cache.get(redis, "broken") is Lookup.MISS


async def test_invalidate_issues_delete_never_set() -> None:
    redis = _FakeRedis()
    await link_cache.store(redis, "abc123", _link(), ttl_seconds=60)
    redis.calls.clear()

    await link_cache.invalidate(redis, "abc123")

    assert "set" not in redis.calls
    assert redis.calls == ["delete", "incr", "expire"]
    assert "link:abc123" not in redis.store


async def test_invalidate_bumps_the_fencing_generation() -> None:
    redis = _FakeRedis()

    assert await link_cache.get_generation(redis, "abc123") == 0

    await link_cache.invalidate(redis, "abc123")
    await link_cache.invalidate(redis, "abc123")

    assert await link_cache.get_generation(redis, "abc123") == 2


async def test_store_if_generation_unchanged_skips_rather_than_writing_unprotected() -> None:
    """expected_generation=None means the caller's own fencing read failed — there is
    nothing to check against, so this must skip the write rather than fall back to an
    unprotected SET, which would reopen the exact race fencing exists to close."""
    redis = _FakeRedis()

    stored = await link_cache.store_if_generation_unchanged(
        redis, "abc123", _link(), ttl_seconds=60, expected_generation=None
    )

    assert stored is False
    assert redis.calls == []
    assert await link_cache.get(redis, "abc123") is Lookup.MISS


async def test_store_negative_if_generation_unchanged_skips_when_expected_generation_is_none() -> (
    None
):
    redis = _FakeRedis()

    stored = await link_cache.store_negative_if_generation_unchanged(
        redis, "abc123", ttl_seconds=60, expected_generation=None
    )

    assert stored is False
    assert redis.calls == []


@pytest.mark.parametrize("op", ["get", "set", "delete"])
async def test_redis_error_surfaces_as_link_cache_unavailable(op: str) -> None:
    redis = _FakeRedis()
    redis.raise_on = op

    with pytest.raises(link_cache.LinkCacheUnavailable):
        if op == "get":
            await link_cache.get(redis, "abc123")
        elif op == "set":
            await link_cache.store(redis, "abc123", _link(), ttl_seconds=60)
        else:
            await link_cache.invalidate(redis, "abc123")


@pytest.mark.parametrize(
    ("is_active", "expires_at", "expected"),
    [
        (True, None, True),
        (False, None, False),
        (True, datetime.now(UTC) + timedelta(days=1), True),
        (True, datetime.now(UTC) - timedelta(seconds=1), False),
        (False, datetime.now(UTC) + timedelta(days=1), False),
    ],
)
def test_is_redirectable_truth_table(
    *, is_active: bool, expires_at: datetime | None, expected: bool
) -> None:
    assert is_redirectable(is_active=is_active, expires_at=expires_at) is expected
