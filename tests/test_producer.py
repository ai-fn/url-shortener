from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from aiokafka.errors import KafkaConnectionError

from app.core import metrics
from app.events import producer as producer_mod
from app.events.producer import ClickProducerUnavailable
from app.events.schema import ClickEvent

pytestmark = pytest.mark.asyncio

_TOPIC = "clicks"
_KEY = b"test-key-at-least-32-bytes-long-000000"


def _event() -> ClickEvent:
    return ClickEvent.build(
        link_id=uuid.uuid4(),
        ip="203.0.113.7",
        user_agent="curl/8.4.0",
        referer=None,
        ip_hash_key=_KEY,
        geoip_reader=None,
    )


def _counter(name: str, **labels) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


def _fake_producer(*, send_effect=None) -> Mock:
    producer = Mock()
    delivered: asyncio.Future[object] = asyncio.get_event_loop().create_future()
    delivered.set_result(object())
    producer.send = AsyncMock(return_value=delivered, side_effect=send_effect)
    producer.start = AsyncMock()
    producer.stop = AsyncMock()
    return producer


async def test_send_event_publishes_the_serialized_payload():
    producer = _fake_producer()
    event = _event()

    await producer_mod.send_event(producer, _TOPIC, event)

    producer.send.assert_awaited_once_with(_TOPIC, event.to_kafka_payload())


async def test_send_event_wraps_kafka_errors():
    producer = _fake_producer(send_effect=KafkaConnectionError("no broker"))

    with pytest.raises(ClickProducerUnavailable):
        await producer_mod.send_event(producer, _TOPIC, _event())


async def test_send_event_counts_a_successful_delivery():
    before = _counter("clicks_produced_total")
    await producer_mod.send_event(_fake_producer(), _TOPIC, _event())
    await asyncio.sleep(0)

    assert _counter("clicks_produced_total") == before + 1


async def test_send_event_counts_a_failed_delivery():
    producer = _fake_producer()
    failed: asyncio.Future[object] = asyncio.get_event_loop().create_future()
    failed.set_exception(KafkaConnectionError("ack never arrived"))
    producer.send = AsyncMock(return_value=failed)

    before = _counter("clicks_dropped_total", reason="send_failed")
    await producer_mod.send_event(producer, _TOPIC, _event())
    await asyncio.sleep(0)

    assert _counter("clicks_dropped_total", reason="send_failed") == before + 1


async def test_drain_sends_queued_events():
    producer = _fake_producer()
    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    await queue.put(_event())

    task = asyncio.create_task(producer_mod.drain(producer, queue, _TOPIC))
    await asyncio.wait_for(queue.join(), timeout=1)
    task.cancel()

    producer.send.assert_awaited_once()


async def test_drain_survives_a_failing_send_and_keeps_going():
    """A dead drain task stops delivery until the process restarts — strictly worse
    than losing the one event that killed it."""
    producer = _fake_producer()
    delivered: asyncio.Future[object] = asyncio.get_event_loop().create_future()
    delivered.set_result(object())
    producer.send = AsyncMock(side_effect=[KafkaConnectionError("down"), delivered])

    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    await queue.put(_event())
    await queue.put(_event())

    task = asyncio.create_task(producer_mod.drain(producer, queue, _TOPIC))
    await asyncio.wait_for(queue.join(), timeout=1)
    task.cancel()

    assert producer.send.await_count == 2
    assert not task.done() or task.cancelled()


async def test_drain_survives_an_unexpected_error():
    producer = _fake_producer()
    delivered: asyncio.Future[object] = asyncio.get_event_loop().create_future()
    delivered.set_result(object())
    producer.send = AsyncMock(side_effect=[RuntimeError("something else"), delivered])

    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    await queue.put(_event())
    await queue.put(_event())

    task = asyncio.create_task(producer_mod.drain(producer, queue, _TOPIC))
    await asyncio.wait_for(queue.join(), timeout=1)
    task.cancel()

    assert producer.send.await_count == 2


async def test_drain_retries_a_failed_start_instead_of_giving_up():
    producer = _fake_producer()
    producer.start = AsyncMock(side_effect=[KafkaConnectionError("not up yet"), None])

    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    await queue.put(_event())

    task = asyncio.create_task(producer_mod.drain(producer, queue, _TOPIC))
    await asyncio.wait_for(queue.join(), timeout=5)
    task.cancel()

    assert producer.start.await_count == 2
    producer.send.assert_awaited_once()


async def test_flush_remaining_sends_what_is_still_queued():
    producer = _fake_producer()
    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    await queue.put(_event())
    await queue.put(_event())

    await producer_mod.flush_remaining(producer, queue, _TOPIC, timeout_seconds=1)

    assert producer.send.await_count == 2
    assert queue.empty()


async def test_flush_remaining_returns_immediately_on_an_empty_queue():
    producer = _fake_producer()
    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()

    await producer_mod.flush_remaining(producer, queue, _TOPIC, timeout_seconds=1)

    producer.send.assert_not_awaited()


async def test_flush_remaining_counts_drops_when_the_broker_is_gone():
    producer = _fake_producer(send_effect=KafkaConnectionError("down"))
    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    await queue.put(_event())

    before = _counter("clicks_dropped_total", reason="send_failed")
    await producer_mod.flush_remaining(producer, queue, _TOPIC, timeout_seconds=1)

    assert _counter("clicks_dropped_total", reason="send_failed") == before + 1


async def test_flush_remaining_gives_up_at_the_deadline():
    """A shutdown that waits on an unreachable broker is a hung deploy."""
    producer = _fake_producer()

    async def slow_send(*args, **kwargs):
        await asyncio.sleep(0.2)
        raise KafkaConnectionError("down")

    producer.send = AsyncMock(side_effect=slow_send)
    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    for _ in range(50):
        await queue.put(_event())

    await asyncio.wait_for(
        producer_mod.flush_remaining(producer, queue, _TOPIC, timeout_seconds=0.3),
        timeout=2,
    )

    assert not queue.empty()


async def test_flush_remaining_bounds_a_single_hanging_send():
    """A send() that never returns (e.g. broker unreachable, request_timeout_ms not
    yet elapsed) must not let one event blow past the shutdown deadline."""
    producer = _fake_producer()

    async def hanging_send(*args, **kwargs):
        await asyncio.sleep(60)

    producer.send = AsyncMock(side_effect=hanging_send)
    queue: asyncio.Queue[ClickEvent] = asyncio.Queue()
    await queue.put(_event())

    before = _counter("clicks_dropped_total", reason="send_failed")
    await asyncio.wait_for(
        producer_mod.flush_remaining(producer, queue, _TOPIC, timeout_seconds=0.3),
        timeout=1,
    )

    assert _counter("clicks_dropped_total", reason="send_failed") == before + 1
