"""Shared fixtures. Connection details are pinned to compose-matching literals so an
ambient DATABASE_URL cannot retarget a run; override with `TEST_*`."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

# Assigned, not `setdefault`: an ambient DATABASE_URL (e.g. for running alembic by
# hand) must not retarget tests onto the real `shortener` db.
_DEFAULTS = {
    "ENVIRONMENT": "test",
    "DATABASE_URL": "postgresql+asyncpg://shortener:shortener@localhost:5432/shortener_test",
    "REDIS_URL": "redis://localhost:6379/0",
    "SECRET_KEY": "test-only-insecure-key-000000000000000000",
    "IP_HASH_KEY": "test-only-insecure-key-111111111111111111",
    # The compose EXTERNAL listener and published ports: tests run on the host, not
    # inside the compose network where `kafka:9092` resolves.
    "KAFKA_BOOTSTRAP_SERVERS": "localhost:19092",
    "CLICKHOUSE_HOST": "localhost",
    "CLICKHOUSE_PORT": "8123",
    "CLICKHOUSE_DATABASE": "analytics",
    "CLICKHOUSE_USER": "analytics",
    "CLICKHOUSE_PASSWORD": "dev-only-insecure-clickhouse",
}
TEST_ENV = {name: os.environ.get(f"TEST_{name}", default) for name, default in _DEFAULTS.items()}
os.environ.update(TEST_ENV)

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_process_state() -> Iterator[None]:
    """Undoes two process-global mutations building an app performs: `get_settings`
    is lru_cached (monkeypatch.setenv can't reach it), and `configure_logging` forces
    the root handler/level. Autouse — the leak is otherwise invisible."""
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
    """Strips every Settings-derived env var, case-insensitively (pydantic-settings
    matches that way), so a test asserting a *default* can't depend on the shell.
    Pair with `_env_file=None` on the constructor."""
    from app.config import Settings

    for field in Settings.model_fields:
        for name in {field, field.upper(), field.lower()}:
            monkeypatch.delenv(name, raising=False)
        for existing in [k for k in os.environ if k.lower() == field.lower()]:
            monkeypatch.delenv(existing, raising=False)


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """App under test without lifespan — no Postgres/Redis needed. Real dependencies
    live in test_readiness_integration.py."""
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
    """App under test with the real lifespan — needs Postgres and Redis up.
    `integration` only; failing outright when they're down is the intended signal."""
    from app.main import create_app

    app = create_app()
    # ASGITransport does not run lifespan events, so it is entered explicitly.
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac,
    ):
        yield ac


@pytest.fixture(scope="session")
def _migrated_database() -> None:
    """Applies migrations once per session. Runs in a worker thread: this is pulled
    in via `getfixturevalue` from inside pytest-asyncio's running event loop, and
    `migrations/env.py`'s `asyncio.run()` raises if called from a thread that
    already has one — a separate thread has none."""
    from concurrent.futures import ThreadPoolExecutor

    from alembic import command
    from alembic.config import Config

    config = Config(str(_REPO_ROOT / "alembic.ini"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(command.upgrade, config, "head").result()


@pytest.fixture
async def db_engine(_migrated_database: None) -> AsyncIterator[AsyncEngine]:
    # NullPool: a short-lived test engine has no business holding pooled connections
    # open past the test.
    engine = create_async_engine(TEST_ENV["DATABASE_URL"], poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(TEST_ENV["REDIS_URL"])
    yield client
    await client.aclose()


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    sessionmaker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with sessionmaker() as session:
        yield session


@pytest.fixture(autouse=True)
async def _clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Truncates mutable tables before every integration test; a no-op for unit
    tests. `_migrated_database` is pulled in via `getfixturevalue`, not as a normal
    parameter — a normal parameter would run migrations against Postgres for every
    unit test too."""
    if request.node.get_closest_marker("integration") is None:
        yield
        return

    request.getfixturevalue("_migrated_database")

    engine = create_async_engine(TEST_ENV["DATABASE_URL"], poolclass=NullPool)
    async with engine.begin() as conn:
        # Both tables in one statement: links.owner_id is NOT NULL, so truncating
        # users alone (even with CASCADE) would still fail without links listed too.
        await conn.execute(text("TRUNCATE TABLE links, users RESTART IDENTITY CASCADE"))
    await engine.dispose()

    # ASGITransport gives every request the same fake client address, so tests
    # sharing `live_client`/`client` share one rate-limit bucket in real Redis —
    # clear it, or a later test starts seeing 429s that aren't its own doing.
    # link:* too: a cached entry would otherwise outlive the TRUNCATE above and
    # the next test would redirect to a row that no longer exists.
    redis_client = aioredis.from_url(TEST_ENV["REDIS_URL"])
    for pattern in ("rl:*", "link:*"):
        async for key in redis_client.scan_iter(match=pattern):
            await redis_client.delete(key)
    await redis_client.aclose()

    yield


_CLICKHOUSE_TABLES = ("clicks_raw", "clicks_dlq", "clicks_hourly", "clicks_daily_dims")


@pytest.fixture(scope="session")
def _migrated_clickhouse() -> None:
    """Applies clickhouse/migrations once per session. No worker thread needed, unlike
    `_migrated_database`: clickhouse-connect is a plain sync HTTP client with no event
    loop of its own."""
    from scripts.apply_clickhouse_migrations import apply, connect

    client = connect()
    try:
        apply(client)
    finally:
        client.close()


@pytest.fixture
def clickhouse_client(_migrated_clickhouse: None) -> Iterator[object]:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=TEST_ENV["CLICKHOUSE_HOST"],
        port=int(TEST_ENV["CLICKHOUSE_PORT"]),
        username=TEST_ENV["CLICKHOUSE_USER"],
        password=TEST_ENV["CLICKHOUSE_PASSWORD"],
        database=TEST_ENV["CLICKHOUSE_DATABASE"],
    )
    yield client
    client.close()


@pytest.fixture
async def kafka_producer() -> AsyncIterator[object]:
    """A raw producer, for publishing what the app never would — a malformed message."""
    from aiokafka import AIOKafkaProducer

    producer = AIOKafkaProducer(bootstrap_servers=TEST_ENV["KAFKA_BOOTSTRAP_SERVERS"])
    await producer.start()
    yield producer
    await producer.stop()


@pytest.fixture(autouse=True)
def _clean_clickhouse_tables(request: pytest.FixtureRequest) -> None:
    """Analytics tests assert on absolute counts, so they need an empty start. Gated on
    the marker: unit tests must never reach for ClickHouse."""
    if request.node.get_closest_marker("analytics_integration") is None:
        return

    client = request.getfixturevalue("clickhouse_client")
    for table in _CLICKHOUSE_TABLES:
        client.command(f"TRUNCATE TABLE IF EXISTS {table}")


async def register_and_login(
    live_client: AsyncClient, *, email: str | None = None
) -> dict[str, str]:
    """Registers a user and returns Authorization headers for its access token."""
    credentials = {
        "email": email or f"{uuid.uuid4()}@example.com",
        "password": "correct horse battery staple",
    }
    await live_client.post("/api/v1/auth/register", json=credentials)
    login = await live_client.post("/api/v1/auth/login", json=credentials)
    token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def insert_user(db_session: AsyncSession) -> uuid.UUID:
    """Inserts a user row directly, for tests needing an owner_id without a login."""
    from app.core.security import hash_password
    from app.models.user import User

    user = User(email=f"{uuid.uuid4()}@example.com", hashed_password=hash_password("x"))
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user.id
