"""App factory and lifespan. Pools open once at startup; handlers never build clients."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth, health, links, meta, redirect
from app.config import Settings, get_settings
from app.core.logging import configure_logging

logger = logging.getLogger(__name__)


async def _close_all(*resources: tuple[str, Callable[[], Awaitable[object]]]) -> None:
    """Run every teardown step even if one fails; stop only if *this task* is cancelled.

    A callback raising CancelledError is an ordinary failed teardown — close the rest,
    re-raise at the end. Our own cancellation must stop immediately, or the shutdown
    deadline stops bounding anything. `Task.cancelling()` tells the two apart.
    """
    total = len(resources)
    deferred: BaseException | None = None

    for index, (name, close) in enumerate(resources):
        skipped = total - index - 1
        try:
            await close()
        except asyncio.CancelledError as exc:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                logger.warning(
                    "shutdown cancelled while closing %s; %d later step(s) skipped",
                    name,
                    skipped,
                    extra={"resource": name, "skipped": skipped},
                )
                raise
            logger.warning(
                "cancelled while closing %s during shutdown; continuing",
                name,
                extra={"resource": name},
            )
            if deferred is None:
                deferred = exc
        except Exception:
            logger.exception("failed to close %s during shutdown", name, extra={"resource": name})
        except BaseException:
            # KeyboardInterrupt / SystemExit: stop, and let SystemExit keep its code.
            logger.warning(
                "shutdown interrupted while closing %s; %d later step(s) skipped",
                name,
                skipped,
                extra={"resource": name, "skipped": skipped},
            )
            raise

    if deferred is not None:
        raise deferred


def _make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Bind to *these* settings; reading `get_settings()` here would ignore injection."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Appended as each resource opens, unwound LIFO, so a partial startup closes
        # exactly what exists and adding a resource is one edit.
        teardown: list[tuple[str, Callable[[], Awaitable[object]]]] = []

        try:
            engine = create_async_engine(
                str(settings.database_url),
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=5,
            )
            teardown.append(("postgres engine", engine.dispose))

            redis_client = aioredis.from_url(
                str(settings.redis_url),
                decode_responses=False,
                health_check_interval=30,
            )
            teardown.append(("redis", redis_client.aclose))
        except BaseException:
            await _close_all(*reversed(teardown))
            raise

        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.redis = redis_client

        logger.info("startup complete", extra={"environment": settings.environment})
        try:
            yield
        finally:
            # "complete" logs after the steps actually ran, not before them.
            logger.info("shutdown starting")
            await _close_all(*reversed(teardown))
            logger.info("shutdown complete")

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    configure_logging(environment=settings.environment, debug=settings.debug)
    # `debug` may open docs in staging; nothing opens them in production. A stray
    # ambient DEBUG=1 must not be enough.
    expose_docs = settings.environment != "production" and (
        settings.debug or settings.environment == "local"
    )

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        lifespan=_make_lifespan(settings),
        docs_url="/docs" if expose_docs else None,
        redoc_url="/redoc" if expose_docs else None,
        openapi_url="/openapi.json" if expose_docs else None,
    )

    # Set once, so routes and tests can read config without a running lifespan.
    app.state.settings = settings

    app.include_router(health.router, tags=["ops"])
    app.include_router(meta.router, tags=["ops"])
    app.include_router(auth.router)
    app.include_router(links.router)

    # Invariant 8: the catch-all `GET /{code}` registers LAST, after every other
    # router, or it eats /docs, /metrics and /favicon.ico.
    app.include_router(redirect.router)

    return app
