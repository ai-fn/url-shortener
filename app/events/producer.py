"""Kafka click producer and its drain task.

This module is the *only* place `producer.send()` may be awaited. `aiokafka`'s
`send()` awaits buffer space and topic metadata, so with the broker down it blocks
for `request_timeout_ms` (~40s) — survivable in a background task, an outage in the
redirect handler. The handler only ever calls `put_nowait` on the queue drained here
(ADR-0001); `scripts/check_invariants.py` exempts this directory for that reason.
"""

from __future__ import annotations

import asyncio
import logging
import time

from aiokafka import AIOKafkaProducer
from aiokafka.errors import KafkaError

from app.core import metrics
from app.events.schema import ClickEvent

logger = logging.getLogger(__name__)

_INITIAL_BACKOFF_SECONDS = 0.5
_MAX_BACKOFF_SECONDS = 30.0


class ClickProducerUnavailable(Exception):
    """Kafka could not accept the event. The drain task decides what that costs —
    here, always a counted drop; analytics is allowed to lose data."""


def build_producer(*, bootstrap_servers: str) -> AIOKafkaProducer:
    """`acks="all"` with idempotence costs the request nothing, because nothing in the
    request waits on it. `key=None` is deliberate: keying by code would pin one viral
    link to one partition and leave the rest idle."""
    return AIOKafkaProducer(
        bootstrap_servers=bootstrap_servers,
        acks="all",
        enable_idempotence=True,
        linger_ms=50,
        compression_type="lz4",
    )


async def send_event(producer: AIOKafkaProducer, topic: str, event: ClickEvent) -> None:
    """Awaits buffer space only. Delivery is counted later, off the returned future,
    so one slow broker ack cannot stall the whole queue behind it."""
    try:
        future = await producer.send(topic, event.to_kafka_payload())
    except (TimeoutError, KafkaError, OSError) as exc:
        raise ClickProducerUnavailable(str(exc)) from exc
    future.add_done_callback(_count_delivery)


def _count_delivery(future: asyncio.Future[object]) -> None:
    if future.cancelled() or future.exception() is not None:
        metrics.CLICKS_DROPPED.labels(reason="send_failed").inc()
        return
    metrics.CLICKS_PRODUCED.inc()


async def drain(producer: AIOKafkaProducer, queue: asyncio.Queue[ClickEvent], topic: str) -> None:
    """Owns the producer for the process lifetime. Cancelled by lifespan teardown."""
    await _start_with_backoff(producer)

    while True:
        event = await queue.get()
        try:
            await send_event(producer, topic, event)
        except ClickProducerUnavailable as exc:
            metrics.CLICKS_DROPPED.labels(reason="send_failed").inc()
            logger.warning("dropped click event: %s", exc)
        except Exception:
            # A dead drain task stops delivery until the process restarts, which is
            # far worse than losing the event that killed it.
            metrics.CLICKS_DROPPED.labels(reason="send_failed").inc()
            logger.exception("unexpected error draining click event")
        finally:
            queue.task_done()


async def _start_with_backoff(producer: AIOKafkaProducer) -> None:
    """Retries forever. The queue is bounded, so a broker that never comes back costs
    counted drops, not memory — and never delays startup."""
    delay = _INITIAL_BACKOFF_SECONDS
    while True:
        try:
            await producer.start()
            return
        except (KafkaError, OSError) as exc:
            logger.warning("kafka producer start failed, retrying in %.1fs: %s", delay, exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_BACKOFF_SECONDS)


async def flush_remaining(
    producer: AIOKafkaProducer,
    queue: asyncio.Queue[ClickEvent],
    topic: str,
    *,
    timeout_seconds: float,
) -> None:
    """Best-effort drain of what is still queued at shutdown. Call *after* the drain
    task is cancelled, so nothing else is consuming the queue concurrently."""
    deadline = time.monotonic() + timeout_seconds
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        try:
            await asyncio.wait_for(send_event(producer, topic, event), timeout=remaining)
        except (ClickProducerUnavailable, TimeoutError):
            metrics.CLICKS_DROPPED.labels(reason="send_failed").inc()
        finally:
            queue.task_done()

    if not queue.empty():
        metrics.CLICKS_DROPPED.labels(reason="send_failed").inc(queue.qsize())
        logger.warning("shutdown flush timed out with %d click event(s) queued", queue.qsize())
