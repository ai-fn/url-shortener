"""User registration and authentication. All business logic; routes stay thin.

Email uniqueness is enforced by the database constraint and handled via
IntegrityError, never a read-then-write precheck — same reasoning as
app/services/links.py's short-code allocation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db_errors import constraint_name
from app.core.security import hash_password, verify_password
from app.models.user import User

# Matches migrations/versions/0002_add_users_and_link_ownership.py.
_EMAIL_UNIQUE_CONSTRAINT = "uq_users_email"


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    """Lazy, not module-level: avoids paying the hash cost at every process boot."""
    return hash_password("dummy password for timing-safe unknown-email checks")


class EmailAlreadyRegisteredError(Exception):
    """The email is already in use. Never silently merged into the existing
    account — a caller who tried to register learns the address is taken."""


class InvalidCredentialsError(Exception):
    """Email not found or password mismatch — one exception for both, so a caller
    cannot distinguish "no such user" from "wrong password" by exception type."""


@dataclass(frozen=True)
class UserCreate:
    email: str
    password: str


def normalize_email(email: str) -> str:
    return email.lower()


async def register(session: AsyncSession, data: UserCreate) -> User:
    hashed_password = await asyncio.to_thread(hash_password, data.password)
    user = User(email=normalize_email(data.email), hashed_password=hashed_password)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if constraint_name(exc) != _EMAIL_UNIQUE_CONSTRAINT:
            raise
        raise EmailAlreadyRegisteredError(data.email) from exc
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    result = await session.execute(select(User).where(User.email == normalize_email(email)))
    user = result.scalar_one_or_none()

    if user is None:
        # _dummy_hash()'s first call also hashes, so it belongs in the thread too.
        await asyncio.to_thread(lambda: verify_password(password, _dummy_hash()))
        raise InvalidCredentialsError(email)
    if not await asyncio.to_thread(verify_password, password, user.hashed_password):
        raise InvalidCredentialsError(email)

    return user
