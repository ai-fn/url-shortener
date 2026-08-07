"""App factory and lifespan. Pools open once at startup; handlers never build clients."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import redis.asyncio as aioredis
from aiokafka import AIOKafkaProducer
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import auth, health, links, meta, redirect
from app.config import Settings, get_settings
from app.core import metrics
from app.core.logging import configure_logging
from app.events import enrichment
from app.events import producer as producer_mod
from app.events.schema import ClickEvent

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


async def _stop_click_pipeline(
    producer: AIOKafkaProducer,
    queue: asyncio.Queue[ClickEvent],
    topic: str,
    *,
    drain_task: asyncio.Task[None],
    timeout_seconds: float,
) -> None:
    """Cancel first, then flush: with the drain task gone nothing else is consuming
    the queue, so the flush sees a stable snapshot.

    `producer.stop()` is in `finally`: a non-cancellation exception from `drain_task`,
    or a shutdown-deadline `CancelledError` escaping `flush_remaining` (which only
    catches its own delivery errors), must not skip it — that would leak the aiokafka
    client and sender task.
    """
    drain_task.cancel()
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await drain_task
        await producer_mod.flush_remaining(producer, queue, topic, timeout_seconds=timeout_seconds)
    finally:
        await producer.stop()


def _make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Bind to *these* settings; reading `get_settings()` here would ignore injection."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
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
                socket_connect_timeout=settings.redis_socket_connect_timeout_seconds,
                socket_timeout=settings.redis_socket_timeout_seconds,
            )
            teardown.append(("redis", redis_client.aclose))

            click_queue: asyncio.Queue[ClickEvent] = asyncio.Queue(
                maxsize=settings.click_queue_maxsize
            )
            kafka_producer = producer_mod.build_producer(
                bootstrap_servers=settings.kafka_bootstrap_servers
            )
            # Connecting happens inside the task, with backoff: a broker that is down
            # must not delay startup, and /readyz deliberately does not check Kafka.
            drain_task = asyncio.create_task(
                producer_mod.drain(kafka_producer, click_queue, settings.kafka_clicks_topic)
            )

            async def stop_drain() -> None:
                await _stop_click_pipeline(
                    kafka_producer,
                    click_queue,
                    settings.kafka_clicks_topic,
                    drain_task=drain_task,
                    timeout_seconds=settings.click_drain_shutdown_timeout_seconds,
                )

            teardown.append(("click drain task", stop_drain))

            # Best-effort: a latency optimization must not veto startup.
            try:
                enrichment.parse_user_agent("warmup/1.0")
            except Exception:
                logger.warning("ua_parser warmup failed", exc_info=True)

            # None when the database is missing or unreadable: that costs the country
            # dimension, which is never worth failing startup over.
            geoip_reader = enrichment.open_geoip_reader(settings.geoip_database_path)
            if geoip_reader is not None:

                async def close_geoip(reader: enrichment.GeoipReader = geoip_reader) -> None:
                    reader.close()

                teardown.append(("geoip reader", close_geoip))
        except BaseException:
            await _close_all(*reversed(teardown))
            raise

        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.redis = redis_client
        app.state.click_queue = click_queue
        app.state.click_producer = kafka_producer
        app.state.geoip_reader = geoip_reader
        # Sampled at scrape time, so metrics.py needs no reference to app state.
        metrics.CLICK_QUEUE_DEPTH.set_function(click_queue.qsize)

        logger.info("startup complete", extra={"environment": settings.environment})
        try:
            yield
        finally:
            # "complete" logs after the steps actually ran, not before them.
            logger.info("shutdown starting")
            # Ahead of _close_all: the gauge needs no live queue to report 0, and
            # _close_all can re-raise a deferred teardown exception.
            metrics.CLICK_QUEUE_DEPTH.set_function(lambda: 0)
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
