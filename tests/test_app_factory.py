"""App factory and lifespan ownership."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fastapi import FastAPI

from app.config import Settings
from app.main import _close_all, _stop_click_pipeline, create_app

_REQUIRED = {
    "secret_key": "test-only-insecure-key-000000000000000000",
    "ip_hash_key": "test-only-insecure-key-111111111111111111",
}


def _injected() -> Settings:
    # `_env_file=None` or this reads the developer's .env.
    return Settings(  # type: ignore[arg-type]
        _env_file=None,
        app_name="custom-app",
        environment="staging",
        database_url="postgresql+asyncpg://u:p@otherhost:5432/other",
        redis_url="redis://otherhost:6379/9",
        **_REQUIRED,
    )


def test_injected_settings_reach_app_state() -> None:
    """Injected Settings used to survive only as far as the OpenAPI title, while the
    pools opened against the process-global database."""
    settings = _injected()

    app = create_app(settings)

    assert app.title == "custom-app"
    assert app.state.settings is settings
    assert app.state.settings.environment == "staging"
    assert str(app.state.settings.database_url).endswith("/other")
    assert str(app.state.settings.redis_url).endswith("/9")


async def test_lifespan_opens_pools_from_the_injected_settings() -> None:
    """`create_async_engine` is lazy, so this asserts the wiring without a database."""
    settings = _injected()
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        assert app.state.engine.url.host == "otherhost"
        assert app.state.engine.url.database == "other"
        assert app.state.settings is settings


async def test_a_failing_teardown_does_not_strand_the_other_resource(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raising `aclose()` in a bare `finally` skipped `engine.dispose()`, leaking
    server-side Postgres connections across a rolling restart."""
    disposed = False

    async def boom() -> None:
        raise ConnectionError("redis already gone")

    async def dispose() -> None:
        nonlocal disposed
        disposed = True

    with caplog.at_level(logging.ERROR):
        await _close_all(("redis", boom), ("postgres engine", dispose))

    assert disposed, "a failing redis teardown must not skip engine.dispose()"
    assert "failed to close redis during shutdown" in caplog.text


async def test_cancellation_during_teardown_still_disposes_the_engine() -> None:
    """The likely shape of the leak: on SIGTERM with an in-flight command, `aclose()`
    raises CancelledError, which `except Exception` does not stop."""
    disposed = False

    async def cancelled() -> None:
        raise asyncio.CancelledError

    async def dispose() -> None:
        nonlocal disposed
        disposed = True

    with pytest.raises(asyncio.CancelledError):
        await _close_all(("redis", cancelled), ("postgres engine", dispose))

    assert disposed, "cancellation during redis teardown must not skip engine.dispose()"


async def test_stop_click_pipeline_stops_the_producer_when_drain_task_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drain_task that dies with a non-cancellation exception before stop_drain
    ever calls cancel() must not skip producer.stop() — that leaks the aiokafka
    client and its sender task."""
    stopped = False

    async def fake_flush(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr("app.events.producer.flush_remaining", fake_flush)

    class _FakeProducer:
        async def stop(self) -> None:
            nonlocal stopped
            stopped = True

    async def boom() -> None:
        raise RuntimeError("drain died")

    drain_task = asyncio.create_task(boom())
    with contextlib.suppress(RuntimeError):
        await drain_task

    with pytest.raises(RuntimeError):
        await _stop_click_pipeline(
            _FakeProducer(),  # type: ignore[arg-type]
            asyncio.Queue(),
            "clicks",
            drain_task=drain_task,
            timeout_seconds=0.1,
        )

    assert stopped, "producer.stop() must run even when drain_task failed"


async def test_stop_click_pipeline_stops_the_producer_when_flush_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown-deadline CancelledError raised inside flush_remaining (which only
    catches its own delivery errors) must not skip producer.stop() either."""
    stopped = False

    async def fake_flush(*_args: object, **_kwargs: object) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("app.events.producer.flush_remaining", fake_flush)

    class _FakeProducer:
        async def stop(self) -> None:
            nonlocal stopped
            stopped = True

    drain_task = asyncio.create_task(asyncio.sleep(3600))

    with pytest.raises(asyncio.CancelledError):
        await _stop_click_pipeline(
            _FakeProducer(),  # type: ignore[arg-type]
            asyncio.Queue(),
            "clicks",
            drain_task=drain_task,
            timeout_seconds=0.1,
        )

    assert stopped, "producer.stop() must run even when flush_remaining is cancelled"


async def test_close_all_logs_rather_than_raising_on_ordinary_errors() -> None:
    async def boom() -> None:
        raise OSError("broken pipe")

    await _close_all(("redis", boom))  # must not raise


def test_configure_logging_leaves_other_root_handlers_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Clearing every root handler removed pytest's, so `client` + `caplog` tests
    asserted against an empty string and passed for the wrong reason."""
    with caplog.at_level(logging.ERROR):
        create_app()
        logging.getLogger("app.probe").error("hello-from-probe")

    assert "hello-from-probe" in caplog.text


@pytest.mark.parametrize(
    ("environment", "debug", "exposed"),
    [
        ("local", False, True),
        ("staging", True, True),
        ("production", False, False),
        ("staging", False, False),
    ],
)
def test_interactive_docs_are_gated_by_environment(
    environment: str, *, debug: bool, exposed: bool
) -> None:
    """`/docs` in production enumerates every route and schema to the public."""
    app = create_app(
        Settings(  # type: ignore[arg-type]
            _env_file=None,
            environment=environment,
            debug=debug,
            database_url="postgresql+asyncpg://u:p@h:5432/d",
            redis_url="redis://h:6379/0",
            **_REQUIRED,
        )
    )

    assert (app.docs_url is not None) is exposed
    assert (app.redoc_url is not None) is exposed
    assert (app.openapi_url is not None) is exposed


def test_readyz_and_healthz_are_registered() -> None:
    app: FastAPI = create_app()

    assert {"/healthz", "/readyz"} <= set(app.openapi()["paths"])
