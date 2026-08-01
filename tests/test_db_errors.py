from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from app.core.db_errors import constraint_name


def _integrity_error(*, cause_has_constraint: bool) -> IntegrityError:
    class _RawAsyncpgError(Exception):
        if cause_has_constraint:
            constraint_name = "uq_example"

    wrapped = Exception("wrapped dbapi error")
    wrapped.__cause__ = _RawAsyncpgError()
    return IntegrityError("insert", {}, wrapped)


def test_extracts_constraint_name_from_chained_cause() -> None:
    exc = _integrity_error(cause_has_constraint=True)
    assert constraint_name(exc) == "uq_example"


def test_returns_none_when_cause_has_no_constraint_name() -> None:
    exc = _integrity_error(cause_has_constraint=False)
    assert constraint_name(exc) is None


def test_returns_none_when_orig_has_no_cause() -> None:
    exc = IntegrityError("insert", {}, Exception("no cause chained"))
    assert constraint_name(exc) is None
