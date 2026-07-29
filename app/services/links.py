"""Link creation, lookup and lifecycle. All business logic; routes stay thin.

Short-code uniqueness is enforced by the database constraint and handled via
IntegrityError, never a read-then-write precheck, which races: two concurrent
requests can both see a code as free and both insert it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import short_code
from app.core.url_validation import validate_target_url
from app.models.link import Link

MAX_GENERATION_ATTEMPTS = 5

# Matches migrations/versions/0001_create_links.py. Checked before an IntegrityError
# is reinterpreted as a code collision, so an unrelated constraint violation surfaces
# instead of being silently retried into a misleading 503.
_SHORT_CODE_UNIQUE_CONSTRAINT = "uq_links_short_code"


class LinkNotFoundError(Exception):
    """No link matches the given id or code."""


class LinkAliasTakenError(Exception):
    """The requested custom alias is already in use. Never silently re-rolled —
    a caller who asked for a specific alias should learn if it's unavailable."""


class InvalidAliasError(ValueError):
    """The requested custom alias fails shape or reserved-word checks."""


class LinkCreationExhaustedError(Exception):
    """Every generated code collided MAX_GENERATION_ATTEMPTS times in a row."""


@dataclass(frozen=True)
class LinkCreate:
    target_url: str
    custom_alias: str | None = None
    title: str | None = None
    description: str | None = None
    expires_at: datetime | None = None


def _validate_alias(alias: str) -> None:
    if not short_code.is_valid_shape(alias):
        raise InvalidAliasError("alias does not match the required shape")
    if short_code.is_reserved(alias):
        raise InvalidAliasError("alias is a reserved word")


async def create_link(session: AsyncSession, data: LinkCreate, *, public_host: str) -> Link:
    """Validate the target URL, then insert with either the caller's alias or a
    generated code. Raises URLValidationError, InvalidAliasError, LinkAliasTakenError
    or LinkCreationExhaustedError — the route maps each to its HTTP status."""
    await validate_target_url(data.target_url, public_host=public_host)

    if data.custom_alias is not None:
        _validate_alias(data.custom_alias)
        codes_to_try = [data.custom_alias]
    else:
        # Astronomically unlikely at this length, but a generated code landing on
        # a reserved word (e.g. "healthz") would insert successfully — short_code
        # is only unique, not reserved-checked — and then be permanently
        # unreachable, since the real route always wins over the catch-all.
        codes_to_try = []
        while len(codes_to_try) < MAX_GENERATION_ATTEMPTS:
            candidate = short_code.generate()
            if not short_code.is_reserved(candidate):
                codes_to_try.append(candidate)

    last_error: IntegrityError | None = None
    for code in codes_to_try:
        link = Link(
            short_code=code,
            target_url=data.target_url,
            title=data.title,
            description=data.description,
            expires_at=data.expires_at,
        )
        session.add(link)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            # asyncpg's dbapi wrapper doesn't proxy `constraint_name` onto exc.orig
            # itself — it's on the raw asyncpg error, chained as __cause__ via
            # SQLAlchemy's `raise translated_error from error`.
            constraint_name = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
            if constraint_name != _SHORT_CODE_UNIQUE_CONSTRAINT:
                raise
            if data.custom_alias is not None:
                raise LinkAliasTakenError(data.custom_alias) from exc
            last_error = exc
            continue
        else:
            await session.refresh(link)
            return link

    raise LinkCreationExhaustedError(
        f"could not allocate a unique code after {MAX_GENERATION_ATTEMPTS} attempts"
    ) from last_error


async def get_link(session: AsyncSession, link_id: uuid.UUID) -> Link:
    link = await session.get(Link, link_id)
    if link is None:
        raise LinkNotFoundError(str(link_id))
    return link


async def list_links(session: AsyncSession, *, limit: int = 50, offset: int = 0) -> list[Link]:
    result = await session.execute(
        select(Link).order_by(Link.created_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars().all())


async def update_link(
    session: AsyncSession,
    link_id: uuid.UUID,
    *,
    target_url: str | None = None,
    title: str | object | None = ...,
    description: str | object | None = ...,
    expires_at: datetime | object | None = ...,
    is_active: bool | None = None,
    public_host: str,
) -> Link:
    """`...` (Ellipsis) means "leave unchanged" for nullable fields, since `None` is
    itself a valid value (clear the field) and can't double as "not provided"."""
    link = await get_link(session, link_id)

    if target_url is not None:
        await validate_target_url(target_url, public_host=public_host)
        link.target_url = target_url
    if title is not ...:
        link.title = title  # type: ignore[assignment]
    if description is not ...:
        link.description = description  # type: ignore[assignment]
    if expires_at is not ...:
        link.expires_at = expires_at  # type: ignore[assignment]
    if is_active is not None:
        link.is_active = is_active

    await session.commit()
    await session.refresh(link)
    return link


async def soft_delete(session: AsyncSession, link_id: uuid.UUID) -> None:
    """Flips is_active rather than deleting the row, so a click event recorded
    earlier keeps a valid link_id to join against."""
    link = await get_link(session, link_id)
    link.is_active = False
    await session.commit()


async def get_redirect_target(session: AsyncSession, code: str) -> Link:
    """Only returns a link that is active and unexpired. Callers must not
    distinguish "not found" from "inactive" from "expired" in the response — that
    lets an enumeration attacker map out which codes ever existed."""
    result = await session.execute(select(Link).where(Link.short_code == code))
    link = result.scalar_one_or_none()

    if link is None or not link.is_active:
        raise LinkNotFoundError(code)
    if link.expires_at is not None and link.expires_at <= datetime.now(UTC):
        raise LinkNotFoundError(code)

    return link
