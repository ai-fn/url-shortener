"""Interpreting SQLAlchemy IntegrityError internals."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError


def constraint_name(exc: IntegrityError) -> str | None:
    """The violated constraint's name, or None if it can't be determined.

    asyncpg's dbapi wrapper doesn't proxy `constraint_name` onto exc.orig itself —
    it's on the raw asyncpg error, chained as __cause__ via SQLAlchemy's
    `raise translated_error from error`.
    """
    return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
