"""The redirect's half of the click pipeline, against real Postgres and Redis but a
stopped drain task: the handler enqueues and returns, and every way the queue or the
broker can fail is a counted drop rather than a slow or failed redirect (ADR-0001).

Delivery into ClickHouse is tests/test_clickhouse_*.py, which need the full stack.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from app.core import metrics
from tests.conftest import register_and_login

pytestmark = pytest.mark.integration


async def _create_link(live_client: AsyncClient) -> str:
    headers = await register_and_login(live_client)
    response = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/"}, headers=headers
    )
    assert response.status_code == 201
    return str(response.json()["short_code"])


def _counter(name: str, **labels: str) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


async def test_redirect_enqueues_exactly_one_click_event(live_client: AsyncClient) -> None:
    code = await _create_link(live_client)
    queue: asyncio.Queue = live_client._transport.app.state.click_queue  # type: ignore[attr-defined]
    while not queue.empty():
        queue.get_nowait()

    response = await live_client.get(f"/{code}", follow_redirects=False)

    assert response.status_code == 302
    assert queue.qsize() == 1


async def test_enqueued_event_carries_the_resolved_link_id(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)
    created = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/"}, headers=headers
    )
    code = created.json()["short_code"]
    queue: asyncio.Queue = live_client._transport.app.state.click_queue  # type: ignore[attr-defined]
    while not queue.empty():
        queue.get_nowait()

    await live_client.get(f"/{code}", follow_redirects=False)

    event = queue.get_nowait()
    assert str(event.link_id) == created.json()["id"]
    assert json.loads(event.to_kafka_payload())["link_id"] == created.json()["id"]


async def test_a_missing_code_enqueues_nothing(live_client: AsyncClient) -> None:
    queue: asyncio.Queue = live_client._transport.app.state.click_queue  # type: ignore[attr-defined]
    while not queue.empty():
        queue.get_nowait()

    response = await live_client.get("/nosuchcode", follow_redirects=False)

    assert response.status_code == 404
    assert queue.empty()


async def test_redirect_still_works_with_the_queue_full(live_client: AsyncClient) -> None:
    """A full queue is the shape a stopped broker takes at the handler: the drain task
    stops consuming, the queue fills, and every further click is a counted drop. The
    redirect must not notice."""
    code = await _create_link(live_client)
    queue: asyncio.Queue = live_client._transport.app.state.click_queue  # type: ignore[attr-defined]
    with patch.object(type(queue), "put_nowait", side_effect=asyncio.QueueFull):
        before = _counter("clicks_dropped_total", reason="queue_full")
        response = await live_client.get(f"/{code}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/"
    assert _counter("clicks_dropped_total", reason="queue_full") == before + 1


async def test_redirect_survives_an_enrichment_failure(live_client: AsyncClient) -> None:
    """The response is already built by the time enrichment runs — an unexpected
    raise here must be a counted drop, not a 500 on a redirect that already
    succeeded."""
    code = await _create_link(live_client)

    with patch("app.api.redirect.ClickEvent.build", side_effect=RuntimeError("boom")):
        before = _counter("clicks_dropped_total", reason="enrich_failed")
        response = await live_client.get(f"/{code}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/"
    assert _counter("clicks_dropped_total", reason="enrich_failed") == before + 1


async def test_redirect_is_not_delayed_by_an_unreachable_broker(live_client: AsyncClient) -> None:
    """The invariant ADR-0001 exists for. `send()` hangs ~40s with the broker down, so
    a redirect that awaited it would too; this asserts on wall-clock, not on mocks."""
    code = await _create_link(live_client)

    async def never_returns(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(60)

    with patch("app.events.producer.send_event", new=AsyncMock(side_effect=never_returns)):
        started = time.monotonic()
        response = await live_client.get(f"/{code}", follow_redirects=False)
        elapsed = time.monotonic() - started

    assert response.status_code == 302
    assert elapsed < 1.0


async def test_metrics_endpoint_exposes_the_click_collectors(live_client: AsyncClient) -> None:
    code = await _create_link(live_client)
    await live_client.get(f"/{code}", follow_redirects=False)

    body = (await live_client.get("/metrics")).text

    assert "clicks_produced_total" in body
    assert "clicks_dropped_total" in body
    assert "click_queue_depth" in body
    assert "enrich_duration_seconds" in body


async def test_click_queue_depth_reports_the_live_queue(live_client: AsyncClient) -> None:
    queue: asyncio.Queue = live_client._transport.app.state.click_queue  # type: ignore[attr-defined]
    while not queue.empty():
        queue.get_nowait()
    code = await _create_link(live_client)

    async def never_returns(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(60)

    with patch("app.events.producer.send_event", new=AsyncMock(side_effect=never_returns)):
        await live_client.get(f"/{code}", follow_redirects=False)

    assert _counter("click_queue_depth") == queue.qsize()
