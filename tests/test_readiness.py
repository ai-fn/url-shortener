"""Readiness logic, without containers.

A probe failure that stops returning 503 pulls a broken pod *into* the load balancer,
and the check names in the body are the only thing saying which dependency broke.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from app.services import health as health_service
from app.services.health import Probe, ReadinessReport, run_readiness_checks


async def _ok(app: FastAPI) -> None:
    return None


async def _boom(app: FastAPI) -> None:
    raise ConnectionRefusedError("connection refused")


async def _hang(app: FastAPI) -> None:
    await asyncio.sleep(60)


# --- the service ---------------------------------------------------------------


async def test_all_probes_passing_reports_ok() -> None:
    report = await run_readiness_checks(FastAPI(), probes={"postgres": _ok, "redis": _ok})

    assert report.ok
    assert report.checks == {"postgres": "ok", "redis": "ok"}


async def test_a_raising_probe_is_error_and_does_not_propagate() -> None:
    """A 500 could not say which check failed."""
    report = await run_readiness_checks(FastAPI(), probes={"postgres": _boom, "redis": _ok})

    assert not report.ok
    assert report.checks == {"postgres": "error", "redis": "ok"}


async def test_a_hanging_probe_times_out_rather_than_hanging() -> None:
    report = await run_readiness_checks(
        FastAPI(), probes={"postgres": _hang, "redis": _ok}, timeout_seconds=0.05
    )

    assert not report.ok
    assert report.checks == {"postgres": "timeout", "redis": "ok"}


async def test_probes_run_concurrently_not_serially() -> None:
    """A barrier, not a stopwatch: a serial implementation cannot satisfy it at all,
    while a wall-clock threshold makes correct code flaky on a loaded runner.
    """
    barrier = asyncio.Barrier(3)

    async def rendezvous(app: FastAPI) -> None:
        async with asyncio.timeout(2.0):
            await barrier.wait()

    probes: dict[str, Probe] = dict.fromkeys("abc", rendezvous)

    report = await run_readiness_checks(FastAPI(), probes=probes, timeout_seconds=5.0)

    assert report.ok, f"probes did not run concurrently: {report.checks}"


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        # `all({})` is True, so the empty case is what stops a fail-open regression.
        ({}, False),
        ({"postgres": "ok"}, True),
        ({"postgres": "ok", "redis": "error"}, False),
        ({"postgres": "timeout", "redis": "ok"}, False),
        ({"postgres": "error", "redis": "error"}, False),
    ],
)
def test_report_ok_requires_every_check(checks: dict[str, str], *, expected: bool) -> None:
    assert ReadinessReport(checks=checks).ok is expected


# --- the endpoint --------------------------------------------------------------


async def test_readyz_returns_503_when_a_dependency_is_down(client: AsyncClient) -> None:
    """No lifespan, so neither pool exists and both probes fail — an outage's shape."""
    response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"postgres": "error", "redis": "error"},
    }


async def test_readyz_returns_200_when_every_dependency_answers(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What is under test is the report→response mapping, which needs no database."""
    monkeypatch.setattr(health_service, "READINESS_PROBES", {"postgres": _ok, "redis": _ok})

    response = await client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {"postgres": "ok", "redis": "ok"},
    }


async def test_healthz_stays_ok_while_readyz_fails(client: AsyncClient) -> None:
    """If liveness tracked readiness, a Redis blip would roll every pod at once."""
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/readyz")).status_code == 503
