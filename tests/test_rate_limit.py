"""Real Redis: proves the Lua script is atomic under concurrency, not just correct
in isolation. A GET/INCR/EXPIRE sequence would under-count exactly this case.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import redis.asyncio as aioredis

from app.core.rate_limit import RateLimiter, RateLimitUnavailable
from tests.conftest import TEST_ENV

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis_client() -> aioredis.Redis:  # type: ignore[misc]
    client = aioredis.from_url(TEST_ENV["REDIS_URL"])
    yield client
    await client.aclose()


def _identity() -> str:
    """A fresh identity per call: fixed strings like "client-a" leak bucket state
    into the *next* run against this real, un-truncated Redis, since the refill
    rate (capacity/60) is too slow to have emptied it by then."""
    return f"test-{uuid.uuid4()}"


async def test_allows_up_to_capacity_then_denies(redis_client: aioredis.Redis) -> None:
    limiter = RateLimiter(redis=redis_client, key_prefix="test:burst", capacity=5)
    identity = _identity()

    results = [await limiter.allow(identity) for _ in range(5)]
    assert all(results)

    assert await limiter.allow(identity) is False


async def test_concurrent_callers_cannot_exceed_capacity(redis_client: aioredis.Redis) -> None:
    """The atomicity claim: 40 concurrent requests against a bucket of 5 must
    produce exactly 5 allows, not more."""
    limiter = RateLimiter(redis=redis_client, key_prefix="test:concurrent", capacity=5)
    identity = _identity()

    results = await asyncio.gather(*(limiter.allow(identity) for _ in range(40)))

    assert sum(results) == 5


async def test_different_identities_have_independent_buckets(redis_client: aioredis.Redis) -> None:
    limiter = RateLimiter(redis=redis_client, key_prefix="test:independent", capacity=2)
    identity_a, identity_b = _identity(), _identity()

    assert await limiter.allow(identity_a) is True
    assert await limiter.allow(identity_a) is True
    assert await limiter.allow(identity_a) is False

    # A different identity has its own bucket, unaffected by the first one's exhaustion.
    assert await limiter.allow(identity_b) is True


async def test_unreachable_redis_raises_rate_limit_unavailable() -> None:
    # Port 1 is never a Redis server: connect_timeout keeps this test fast.
    client = aioredis.from_url("redis://localhost:1/0", socket_connect_timeout=0.5)
    limiter = RateLimiter(redis=client, key_prefix="test:down", capacity=5)

    with pytest.raises(RateLimitUnavailable):
        await limiter.allow(_identity())

    await client.aclose()
