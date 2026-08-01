"""FastAPI dependencies. Every resource here was opened once in lifespan; a handler
never builds its own client."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.rate_limit import RateLimiter, RateLimitUnavailable, client_ip
from app.core.security import InvalidTokenError, decode_access_token
from app.models.user import User

# auto_error=False, not the default True: HTTPBearer's default 403s a missing
# header. Every failure mode here must be a 401 with WWW-Authenticate, not a 403.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    # Fresh instance every call: a shared one would leak the rejected token through
    # an ever-growing __traceback__ and let concurrent requests clobber __cause__.
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


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


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings_dep)],
) -> uuid.UUID:
    if credentials is None:
        raise _unauthorized()
    try:
        user_id = decode_access_token(credentials.credentials, settings=settings)
    except InvalidTokenError as exc:
        raise _unauthorized() from exc

    # Existence check, not a row fetch: no caller needs more than the id, but a
    # deleted user's still-valid token must still be rejected.
    result = await session.execute(select(User.id).where(User.id == user_id))
    if result.scalar_one_or_none() is None:
        raise _unauthorized()
    return user_id


async def enforce_rate_limit(
    request: Request,
    redis: aioredis.Redis,
    *,
    key_prefix: str,
    capacity: int,
    identity: str | None = None,
) -> None:
    """Fails closed, unlike the redirect's 404 budget. `identity` defaults to the
    client IP; pass it explicitly for a second, differently-keyed bucket (e.g.
    login's per-email bucket)."""
    limiter = RateLimiter(redis=redis, key_prefix=key_prefix, capacity=capacity)
    try:
        allowed = await limiter.allow(identity if identity is not None else client_ip(request))
    except RateLimitUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="rate limiter unavailable"
        ) from exc
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded"
        )


def rate_limit_guard(
    *, key_prefix: str, capacity: Callable[[Settings], int]
) -> Callable[[Request, aioredis.Redis, Settings], Awaitable[None]]:
    """A route-level `dependencies=[...]` entry, not a body-level call: it resolves
    before the endpoint's own parameter dependencies, so it can reject abusive
    traffic before e.g. get_current_user's DB lookup ever runs."""

    async def _guard(
        request: Request,
        redis: Annotated[aioredis.Redis, Depends(get_redis)],
        settings: Annotated[Settings, Depends(get_settings_dep)],
    ) -> None:
        await enforce_rate_limit(request, redis, key_prefix=key_prefix, capacity=capacity(settings))

    return _guard


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
CurrentUserIdDep = Annotated[uuid.UUID, Depends(get_current_user)]
