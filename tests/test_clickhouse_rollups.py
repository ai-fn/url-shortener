"""Duplicate deliveries must not inflate the rollups.

The Kafka engine is at-least-once, so this is not a hypothetical: a rebalance or a
failed offset commit redelivers a block. `countState()` here would over-count
permanently, because ReplacingMergeTree collapses the raw row during a later merge
and nothing rewinds an aggregate (ADR-0002).
"""

from __future__ import annotations

import time
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.analytics_integration]

# kafka_flush_interval_ms is 7500, so nothing lands sooner than that.
_TIMEOUT_SECONDS = 45
_POLL_SECONDS = 1.0


def _publish(client, event_id: uuid.UUID, link_id: uuid.UUID) -> None:
    """INSERT into a Kafka engine table *produces* to the topic. Only SELECT is
    forbidden — that would consume the messages out from under the ingest MV."""
    client.command(
        "INSERT INTO kafka_clicks_queue "
        "(event_id, link_id, ts, ip_hash, country, device_type, browser, os, "
        "referer_domain, is_bot) VALUES "
        "({event_id:UUID}, {link_id:UUID}, '2026-08-06 10:00:00.123', 42, 'DE', "
        "'desktop', 'Chrome', 'Windows', 'news.example.com', 0)",
        parameters={"event_id": event_id, "link_id": link_id},
    )


def _wait_for(client, query: str, link_id: uuid.UUID, expected: int) -> int:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    observed = -1
    while time.monotonic() < deadline:
        rows = client.query(query, parameters={"link_id": link_id}).result_rows
        observed = int(rows[0][0])
        if observed == expected:
            return observed
        time.sleep(_POLL_SECONDS)
    return observed


def test_duplicate_event_id_does_not_inflate_the_hourly_rollup(clickhouse_client) -> None:
    event_id, link_id = uuid.uuid4(), uuid.uuid4()

    _publish(clickhouse_client, event_id, link_id)
    _publish(clickhouse_client, event_id, link_id)

    # Asserted on the rollup, not on count() in clicks_raw: raw legitimately holds two
    # rows until a background merge collapses them, so a raw count would fail for a
    # reason that is not the defect under test.
    clicks = _wait_for(
        clickhouse_client,
        "SELECT uniqMerge(clicks_state) FROM clicks_hourly WHERE link_id = {link_id:UUID}",
        link_id,
        expected=1,
    )
    assert clicks == 1


def test_duplicate_event_id_does_not_inflate_the_daily_rollup(clickhouse_client) -> None:
    event_id, link_id = uuid.uuid4(), uuid.uuid4()

    _publish(clickhouse_client, event_id, link_id)
    _publish(clickhouse_client, event_id, link_id)

    clicks = _wait_for(
        clickhouse_client,
        "SELECT uniqMerge(clicks_state) FROM clicks_daily_dims WHERE link_id = {link_id:UUID}",
        link_id,
        expected=1,
    )
    assert clicks == 1


def test_distinct_event_ids_still_count_separately(clickhouse_client) -> None:
    """The other half of the guarantee: dedup must not swallow genuine clicks."""
    link_id = uuid.uuid4()

    _publish(clickhouse_client, uuid.uuid4(), link_id)
    _publish(clickhouse_client, uuid.uuid4(), link_id)

    clicks = _wait_for(
        clickhouse_client,
        "SELECT uniqMerge(clicks_state) FROM clicks_hourly WHERE link_id = {link_id:UUID}",
        link_id,
        expected=2,
    )
    assert clicks == 2


def test_rollup_buckets_by_utc(clickhouse_client) -> None:
    """Explicit 'UTC' in toStartOfHour/toDate — without it the container's TZ shifts
    bucket boundaries and daily totals stop lining up."""
    link_id = uuid.uuid4()
    _publish(clickhouse_client, uuid.uuid4(), link_id)

    _wait_for(
        clickhouse_client,
        "SELECT uniqMerge(clicks_state) FROM clicks_hourly WHERE link_id = {link_id:UUID}",
        link_id,
        expected=1,
    )
    rows = clickhouse_client.query(
        "SELECT toString(h) FROM clicks_hourly WHERE link_id = {link_id:UUID}",
        parameters={"link_id": link_id},
    ).result_rows

    assert rows[0][0] == "2026-08-06 10:00:00"
