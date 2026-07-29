"""Unit test: no Redis needed, unlike test_rate_limit.py's atomicity tests."""

from __future__ import annotations

from typing import Any

from app.core.rate_limit import RateLimiter


class _FakeRedis:
    def __init__(self) -> None:
        self.script: str | None = None
        self.args: tuple[Any, ...] | None = None

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        self.script = script
        self.args = keys_and_args
        return 1


async def test_allow_does_not_pass_a_client_timestamp() -> None:
    """The bucket's clock comes from Redis TIME inside the script, not the
    caller's, so a lagging replica can no longer roll a bucket's stored 'ts'
    backward and over-refill it. Regression guard: catches a revert to a
    client-supplied `now` argument even though no real Redis is involved."""
    redis = _FakeRedis()
    limiter = RateLimiter(redis=redis, key_prefix="test", capacity=5)  # type: ignore[arg-type]

    await limiter.allow("identity")

    assert redis.script is not None
    assert "redis.call('TIME')" in redis.script
    # key, capacity, refill_per_second, ttl_seconds — nothing else.
    assert redis.args == ("test:identity", 5, 5 / 60.0, 120)
