"""Redis token-bucket rate limiting.

A GET/INCR/EXPIRE sequence races under concurrency: two requests can both read the
same counter before either writes it back, undercounting the limit. The bucket state
is read, updated and written in one Lua script instead, so the whole check-and-spend
is atomic regardless of how many callers hit the same key at once.

Callers pick their own failure policy — there is no single right answer:
`POST /api/v1/links` fails **closed** (this endpoint is not the hot path, and without
auth an unthrottled create is an open-redirect factory); the redirect's 404 budget
fails **open** (redirects win over abuse controls too — a Redis blip must not stop
redirecting).
"""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as aioredis
from fastapi import Request
from redis.exceptions import RedisError


def client_ip(request: Request) -> str:
    """Best-effort client IP; no `X-Forwarded-For` trust without a configured
    trusted-proxy list, or a client could spoof past the limiter. Lives here, not
    app/api/deps.py, so redirect.py can use it without importing the auth stack
    that module also wires up (invariant 9)."""
    if request.client is None:
        return "unknown"
    return request.client.host


# Reads current tokens, refills for elapsed time, spends one if available — all in a
# single atomic step. Token count and timestamp are stored together so a bucket that
# has not been touched in a while refills correctly on its next request.
_BUCKET_LUA_SCRIPT = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_second = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

-- Server time, not a client-supplied timestamp: a caller's clock skew (or a
-- lagging replica in a multi-replica deployment) must never move a bucket's
-- stored 'ts' backward, which would let elapsed-time refill over-credit tokens.
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000

local tokens = tonumber(redis.call('HGET', key, 'tokens'))
local last_ts = tonumber(redis.call('HGET', key, 'ts'))

if tokens == nil or last_ts == nil then
    tokens = capacity
    last_ts = now
end

local elapsed = math.max(0, now - last_ts)
tokens = math.min(capacity, tokens + elapsed * refill_per_second)

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)

return allowed
"""


class RateLimitUnavailable(Exception):
    """Redis could not be reached to evaluate the limit. Callers decide open/closed."""


@dataclass(frozen=True)
class RateLimiter:
    redis: aioredis.Redis
    key_prefix: str
    capacity: int
    ttl_seconds: int = 120

    @property
    def _refill_per_second(self) -> float:
        return self.capacity / 60.0

    async def allow(self, identity: str) -> bool:
        """True if the request may proceed. Raises RateLimitUnavailable on a Redis
        error — never returns a default, so a caller cannot forget to choose a policy."""
        key = f"{self.key_prefix}:{identity}"
        try:
            result = await self.redis.eval(
                _BUCKET_LUA_SCRIPT,
                1,
                key,
                self.capacity,
                self._refill_per_second,
                self.ttl_seconds,
            )
        except RedisError as exc:
            raise RateLimitUnavailable(str(exc)) from exc
        return bool(result)
