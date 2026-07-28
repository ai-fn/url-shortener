"""Readiness probing.

Here rather than in the route because the timeout policy, the error taxonomy and the
set of dependencies that gate traffic are decisions — reachable from a CLI or a worker,
and testable without a client.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from fastapi import FastAPI
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Without a bound, a hung dependency turns the probe into a hang.
READY_CHECK_TIMEOUT_SECONDS = 2.0

Probe = Callable[[FastAPI], Awaitable[None]]


async def check_postgres(app: FastAPI) -> None:
    async with app.state.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def check_redis(app: FastAPI) -> None:
    await app.state.redis.ping()


# Kafka and ClickHouse are deliberately absent: a redirect still succeeds with both
# down, so failing readiness on them would take the service offline to protect
# analytics — backwards.
READINESS_PROBES: Mapping[str, Probe] = {
    "postgres": check_postgres,
    "redis": check_redis,
}


@dataclass(frozen=True)
class ReadinessReport:
    """Outcome of one sweep. Values are "ok", "timeout" or "error"."""

    checks: Mapping[str, str]

    def __post_init__(self) -> None:
        # `frozen=True` freezes the binding, not the dict behind it: built from a
        # caller's live dict, `ok` could flip after the report was returned.
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))

    @property
    def ok(self) -> bool:
        """Every check passed *and* there was something to check."""
        return bool(self.checks) and all(status == "ok" for status in self.checks.values())


async def run_readiness_checks(
    app: FastAPI,
    probes: Mapping[str, Probe] | None = None,
    timeout_seconds: float | None = None,
) -> ReadinessReport:
    """Run every probe concurrently, bounded, and never raise.

    A probe that raises is a failed check, not a failed request: a 500 cannot say
    *which* dependency is down. Both defaults resolve here rather than in the signature,
    where they would freeze at import and silently ignore a reassigned constant.
    """
    selected = READINESS_PROBES if probes is None else probes
    timeout = READY_CHECK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    checks: dict[str, str] = {}

    async def probe(name: str, run: Probe) -> None:
        try:
            await asyncio.wait_for(run(app), timeout=timeout)
        except TimeoutError:
            logger.warning(
                "readiness check timed out",
                extra={"check": name, "timeout_seconds": timeout},
            )
            checks[name] = "timeout"
        except Exception as exc:
            logger.warning(
                "readiness check failed",
                extra={"check": name, "error": str(exc), "error_type": type(exc).__name__},
            )
            checks[name] = "error"
        else:
            checks[name] = "ok"

    await asyncio.gather(*(probe(name, run) for name, run in selected.items()))

    return ReadinessReport(checks=checks)
