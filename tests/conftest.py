"""Shared fixtures.

Connection details are pinned here to compose-matching literals, so the same tests run
against the compose stack and CI `services:` without branching — and so an ambient
`DATABASE_URL` cannot retarget a run at a real database. Override with `TEST_*`.
Integration tests need the dependencies already up; nothing here starts a container.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

# Assigned, not `setdefault`: a developer exporting DATABASE_URL to run alembic against
# the compose stack otherwise had every integration test hit their real `shortener` db.
# `TEST_*` is the explicit knob, which no ordinary shell has set but CI does.
_DEFAULTS = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://shortener:shortener@localhost:5432/shortener_test",
    "REDIS_URL": "redis://localhost:6379/0",
    "SECRET_KEY": "test-only-insecure-key-000000000000000000",
    "IP_HASH_KEY": "test-only-insecure-key-111111111111111111",
}
TEST_ENV = {name: os.environ.get(f"TEST_{name}", default) for name, default in _DEFAULTS.items()}
os.environ.update(TEST_ENV)


@pytest.fixture(autouse=True)
def _isolate_process_state() -> Iterator[None]:
    """Undo the two process-global mutations building an app performs.

    `get_settings` is lru_cached, so the first call pins Settings for the session and
    `monkeypatch.setenv` cannot reach it; `configure_logging` forces the root handler
    and level. Autouse because the leak is invisible — results just stop depending on
    the test's own setup.
    """
    from app.config import get_settings

    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level

    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


@pytest.fixture
def hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every Settings-derived variable, so a test asserting a *default* does not
    depend on the developer's shell or their `.env`.

    Every casing is deleted, not just `FIELD.upper()`: pydantic-settings matches env
    vars case-insensitively. Pair with `_env_file=None` on the constructor.
    """
    from app.config import Settings

    for field in Settings.model_fields:
        for name in {field, field.upper(), field.lower()}:
            monkeypatch.delenv(name, raising=False)
        for existing in [k for k in os.environ if k.lower() == field.lower()]:
            monkeypatch.delenv(existing, raising=False)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """App under test WITHOUT lifespan — no Postgres/Redis needed.

    For anything that does not touch a dependency, and for readiness cases with
    substituted probes. Real dependencies live in test_readiness_integration.py.
    """
    from app.config import Settings
    from app.main import create_app

    # Explicit settings, not `get_settings()`: /docs is gated by environment, so an app
    # built from the ambient env would pass or fail on the developer's `.env`.
    settings = Settings(  # type: ignore[arg-type]
        _env_file=None,
        environment="local",
        database_url="postgresql+asyncpg://test:test@localhost:5432/unused",
        redis_url="redis://localhost:6379/0",
        secret_key="test-only-insecure-key-000000000000000000",
        ip_hash_key="test-only-insecure-key-111111111111111111",
    )
    app = create_app(settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def live_client() -> AsyncIterator[AsyncClient]:
    """App under test WITH the real lifespan — needs Postgres and Redis up.

    `integration` only. Failing outright when they are down is the intended signal.
    """
    from app.main import create_app

    app = create_app()
    # ASGITransport does not run lifespan events, so it is entered explicitly.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        yield ac
