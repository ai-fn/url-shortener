"""Password hashing and JWT access tokens. Pure and session-free, like
app/core/rate_limit.py and app/core/url_validation.py — no DB access here."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from app.config import Settings

_password_hash = PasswordHash.recommended()


class InvalidTokenError(Exception):
    """Folds expired, malformed, tampered, and claim-shape failures into one — a
    caller never needs (or should get) to distinguish which."""


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return _password_hash.verify(password, hashed)


def create_access_token(user_id: uuid.UUID, *, settings: Settings) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_expire_minutes),
    }
    return jwt.encode(
        payload, settings.secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )


def decode_access_token(token: str, *, settings: Settings) -> uuid.UUID:
    """Never passes a caller- or token-controlled algorithm list to jwt.decode —
    `algorithms=[settings.jwt_algorithm]` is the one line that closes the `alg: none`
    / algorithm-confusion class of JWT vulnerability."""
    try:
        payload = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
            # PyJWT only validates a claim when present, so a signed token that
            # simply omits `exp` would otherwise never expire.
            options={"require": ["exp", "sub"]},
        )
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError(str(exc)) from exc

    sub = payload.get("sub")
    if not isinstance(sub, str):
        raise InvalidTokenError("token has no 'sub' claim")
    try:
        return uuid.UUID(sub)
    except ValueError as exc:
        raise InvalidTokenError("token 'sub' claim is not a valid user id") from exc
