"""A malformed message must land in clicks_dlq without wedging the consumer.

Under the default kafka_handle_error_mode an unparseable message aborts the whole
block insert, so offsets never commit, the consumer re-reads the same block and loops
until retention deletes the topic. It presents as "analytics stopped" with no error
anyone notices, which is why the recovery half of this test matters more than the
DLQ half.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.analytics_integration]

_TIMEOUT_SECONDS = 45
_POLL_SECONDS = 1.0
_TOPIC = "clicks"


def _valid_payload(event_id: uuid.UUID, link_id: uuid.UUID) -> bytes:
    return json.dumps(
        {
            "event_id": str(event_id),
            "link_id": str(link_id),
            "ts": "2026-08-06 10:00:00.123",
            "ip_hash": 42,
            "country": "DE",
            "device_type": "desktop",
            "browser": "Chrome",
            "os": "Windows",
            "referer_domain": "news.example.com",
            "is_bot": 0,
        }
    ).encode()


def _wait_for(client, query: str, expected: int, **parameters: object) -> int:
    deadline = time.monotonic() + _TIMEOUT_SECONDS
    observed = -1
    while time.monotonic() < deadline:
        observed = int(client.query(query, parameters=parameters).result_rows[0][0])
        if observed >= expected:
            return observed
        time.sleep(_POLL_SECONDS)
    return observed


async def test_malformed_message_lands_in_the_dlq(kafka_producer, clickhouse_client) -> None:
    await kafka_producer.send_and_wait(_TOPIC, b"{ this is not valid json at all")

    found = _wait_for(clickhouse_client, "SELECT count() FROM clicks_dlq", expected=1)

    assert found >= 1


_NIL_LINK_ID = "00000000-0000-0000-0000-000000000000"


async def test_malformed_message_does_not_reach_clicks_raw(
    kafka_producer, clickhouse_client
) -> None:
    """Filters on the nil-UUID link_id (the Kafka engine's default for an
    unparseable row) instead of a global count(), so another test's delayed insert
    landing mid-poll can't cause a false failure."""
    await kafka_producer.send_and_wait(_TOPIC, b"{ this is not valid json at all")
    _wait_for(clickhouse_client, "SELECT count() FROM clicks_dlq", expected=1)

    leaked = int(
        clickhouse_client.query(
            "SELECT count() FROM clicks_raw WHERE link_id = {link_id:UUID}",
            parameters={"link_id": _NIL_LINK_ID},
        ).result_rows[0][0]
    )
    assert leaked == 0


async def test_a_good_message_after_a_bad_one_still_arrives(
    kafka_producer, clickhouse_client
) -> None:
    """The assertion that actually proves the consumer is not wedged, and the one it
    is easiest to leave out."""
    link_id = uuid.uuid4()

    await kafka_producer.send_and_wait(_TOPIC, b"not json")
    await kafka_producer.send_and_wait(_TOPIC, _valid_payload(uuid.uuid4(), link_id))

    delivered = _wait_for(
        clickhouse_client,
        "SELECT count() FROM clicks_raw WHERE link_id = {link_id:UUID}",
        expected=1,
        link_id=link_id,
    )
    assert delivered == 1


async def test_the_dlq_row_records_what_was_lost(kafka_producer, clickhouse_client) -> None:
    """kafka_skip_broken_messages would also avoid the wedge, but discards the payload
    with no record — the reason it is not used."""
    await kafka_producer.send_and_wait(_TOPIC, b"{ broken-payload-marker")
    _wait_for(clickhouse_client, "SELECT count() FROM clicks_dlq", expected=1)

    rows = clickhouse_client.query("SELECT raw, error, topic FROM clicks_dlq").result_rows

    assert any("broken-payload-marker" in row[0] for row in rows)
    assert all(row[1] for row in rows)
    assert all(row[2] == _TOPIC for row in rows)
