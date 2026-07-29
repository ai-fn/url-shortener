"""FastAPI dependencies. Every resource here was opened once in lifespan; a handler
never builds its own client."""

from __future__ import annotations

from collections.abc import AsyncIterator

import redis.asyncio as aioredis
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> aioredis.Redis:
    redis_client: aioredis.Redis = request.app.state.redis
    return redis_client


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session


def client_ip(request: Request) -> str:
    """Best-effort client IP. No `X-Forwarded-For` trust here — this service has no
    configured trusted-proxy list yet, and trusting an unverified header lets a
    client spoof its way around both rate limiters."""
    if request.client is None:
        return "unknown"
    return request.client.host
