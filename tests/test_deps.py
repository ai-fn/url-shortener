from __future__ import annotations

from fastapi import HTTPException

from app.api.deps import get_current_user


def _traceback_length(exc: BaseException) -> int:
    length = 0
    tb = exc.__traceback__
    while tb is not None:
        length += 1
        tb = tb.tb_next
    return length


async def test_repeated_unauthorized_calls_raise_distinct_exception_instances() -> None:
    """A shared exception instance across requests lets one request's traceback (and
    the local variables it references, e.g. a bearer token) leak into another's."""
    exceptions = []
    for _ in range(5):
        try:
            await get_current_user(credentials=None, session=None, settings=None)  # type: ignore[arg-type]
        except HTTPException as exc:
            exceptions.append(exc)

    assert len({id(exc) for exc in exceptions}) == len(exceptions)


async def test_unauthorized_exception_traceback_does_not_grow_across_calls() -> None:
    """Reusing one exception instance across raises makes __traceback__ accumulate a
    frame per raise, unbounded — this pins it to a constant depth instead."""
    lengths = []
    for _ in range(5):
        try:
            await get_current_user(credentials=None, session=None, settings=None)  # type: ignore[arg-type]
        except HTTPException as exc:
            lengths.append(_traceback_length(exc))

    assert len(set(lengths)) == 1
