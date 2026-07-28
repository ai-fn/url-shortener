"""Readiness and lifespan against real Postgres and Redis.

test_readiness.py proves the *logic* with fakes; this proves the *probes* — that
SELECT 1 and PING are the right calls against the real clients. Connection details
are pinned by conftest.py, overridable through `TEST_DATABASE_URL` / `TEST_REDIS_URL`.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.main import create_app

pytestmark = pytest.mark.integration


async def test_readyz_is_ready_against_live_dependencies(live_client: AsyncClient) -> None:
    response = await live_client.get("/readyz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"] == {"postgres": "ok", "redis": "ok"}


async def _backend_count(observer: AsyncEngine) -> int:
    """Backends against the test database, excluding the one doing the counting."""
    async with observer.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
        return int(result.scalar_one())


async def _settled_backend_count(
    observer: AsyncEngine, target: int, timeout_seconds: float = 5.0
) -> int:
    """Poll until the count stops shrinking. Terminating backends linger briefly and
    the observer opens one per call under NullPool, so exact equality raced both ways.
    """
    deadline = time.monotonic() + timeout_seconds
    count = await _backend_count(observer)
    while count > target and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
        count = await _backend_count(observer)
    return count


async def test_lifespan_disposes_the_postgres_pool_on_shutdown() -> None:
    """Measured server-side via pg_stat_activity, not on SQLAlchemy's pool: the leak is
    backends outliving the process and accumulating against max_connections, and only
    the server can confirm they went away.
    """
    app = create_app()
    observer = create_async_engine(str(app.state.settings.database_url), poolclass=NullPool)

    try:
        baseline = await _backend_count(observer)

        async with app.router.lifespan_context(app):
            # create_async_engine is lazy — force a real backend open.
            async with app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            during = await _backend_count(observer)

        after = await _settled_backend_count(observer, baseline)
    finally:
        await observer.dispose()

    assert during > baseline, "expected the lifespan to hold at least one Postgres backend"
    assert after <= baseline, f"lifespan leaked {after - baseline} Postgres backend(s)"
