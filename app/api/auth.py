"""Registration and login. Routes are thin adapters — validation and persistence
live in app/services/auth.py."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

from app.api.deps import SessionDep, SettingsDep, enforce_rate_limit, get_redis, rate_limit_guard
from app.core.security import create_access_token
from app.services import auth as auth_service

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

_PASSWORD_MAX_LENGTH = 128
RegisterPassword = Annotated[str, Field(min_length=12, max_length=_PASSWORD_MAX_LENGTH)]
# No min_length: at login a short password is a wrong credential (401), not a
# malformed request (422) — and a later floor increase on registration must not
# lock existing users out of login entirely.
LoginPassword = Annotated[str, Field(max_length=_PASSWORD_MAX_LENGTH)]


class RegisterRequest(BaseModel):
    email: EmailStr
    password: RegisterPassword


class LoginRequest(BaseModel):
    email: EmailStr
    password: LoginPassword


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105 — RFC 6750 scheme name, not a secret
    expires_in: int


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[
        Depends(
            rate_limit_guard(
                key_prefix="rl:register",
                capacity=lambda settings: settings.rate_limit_register_per_minute,
            )
        )
    ],
)
async def register(body: RegisterRequest, session: SessionDep) -> UserResponse:
    data = auth_service.UserCreate(email=body.email, password=body.password)
    try:
        user = await auth_service.register(session, data)
    except auth_service.EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="email already registered"
        ) from exc

    return UserResponse(id=user.id, email=user.email, created_at=user.created_at)


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[
        Depends(
            rate_limit_guard(
                key_prefix="rl:login:ip",
                capacity=lambda settings: settings.rate_limit_login_per_minute,
            )
        )
    ],
)
async def login(
    request: Request,
    body: LoginRequest,
    session: SessionDep,
    settings: SettingsDep,
    redis: Annotated[aioredis.Redis, Depends(get_redis)],
) -> TokenResponse:
    normalized_email = auth_service.normalize_email(body.email)

    try:
        user = await auth_service.authenticate(session, normalized_email, body.password)
    except auth_service.InvalidCredentialsError as exc:
        # Spent on failure only: a correct-password login must never draw from this
        # bucket, or anyone who knows a victim's email can lock them out by burning
        # it with wrong-password attempts from IPs other than their own.
        await enforce_rate_limit(
            request,
            redis,
            key_prefix="rl:login:email",
            capacity=settings.rate_limit_login_per_minute,
            identity=normalized_email,
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid email or password"
        ) from exc

    token = create_access_token(user.id, settings=settings)
    return TokenResponse(access_token=token, expires_in=settings.access_token_expire_minutes * 60)
