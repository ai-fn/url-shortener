"""Behaviours that were previously pinned by nothing — each one a mutation that
survived the whole suite."""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import AsyncClient

from app.main import _close_all
from app.services import health as health_service


async def test_cancelling_the_teardown_stops_it_inside_the_deadline() -> None:
    """Swallowing the cancellation and awaiting every later step extended the deadline
    by however long those took — 3.2s against a 0.2s budget, i.e. SIGKILL mid-dispose.
    """
    reached_later_step = False

    async def hangs() -> None:
        await asyncio.sleep(5)

    async def later() -> None:
        nonlocal reached_later_step
        reached_later_step = True

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            _close_all(("redis", hangs), ("postgres engine", later)), timeout=0.2
        )
    elapsed = time.perf_counter() - started

    assert elapsed < 1.0, f"teardown overran its 0.2s deadline by {elapsed:.2f}s"
    assert not reached_later_step, "a cancelled teardown must not start further awaits"


async def test_readiness_probe_names_match_the_dependency_they_check() -> None:
    """Swapping the two probes left the suite green: every case failed both or passed
    both. A mislabelled report sends on-call to restart the wrong datastore.
    """
    calls: list[str] = []

    class _Conn:
        async def __aenter__(self) -> _Conn:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def execute(self, statement: object) -> None:
            calls.append("postgres")

    class _Engine:
        def connect(self) -> _Conn:
            return _Conn()

    class _Redis:
        async def ping(self) -> None:
            calls.append("redis")

    class _State:
        engine = _Engine()
        redis = _Redis()

    class _App:
        state = _State()

    await health_service.READINESS_PROBES["postgres"](_App())  # type: ignore[arg-type]
    assert calls == ["postgres"], "the 'postgres' probe must query Postgres"

    calls.clear()
    await health_service.READINESS_PROBES["redis"](_App())  # type: ignore[arg-type]
    assert calls == ["redis"], "the 'redis' probe must ping Redis"


async def test_probe_timeout_default_follows_the_module_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The knob has to be live, not frozen into the signature at import time."""

    async def slow(app: object) -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(health_service, "READY_CHECK_TIMEOUT_SECONDS", 0.01)

    report = await health_service.run_readiness_checks(
        None,  # type: ignore[arg-type]
        probes={"slow": slow},  # type: ignore[dict-item]
    )

    assert report.checks == {"slow": "timeout"}


def test_readiness_report_does_not_alias_its_caller_dict() -> None:
    """`frozen=True` freezes the binding, not the dict behind it."""
    source = {"postgres": "ok"}
    report = health_service.ReadinessReport(checks=source)

    source["postgres"] = "error"

    assert report.checks == {"postgres": "ok"}
    assert report.ok


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_health_endpoints_forbid_caching(client: AsyncClient, path: str) -> None:
    """An intermediary caching a 200 pins "ready" for a pod whose database has died."""
    response = await client.get(path)

    assert response.headers["cache-control"] == "no-store"


async def test_readyz_documents_its_503(client: AsyncClient) -> None:
    schema = await client.get("/openapi.json")

    assert "503" in schema.json()["paths"]["/readyz"]["get"]["responses"]
