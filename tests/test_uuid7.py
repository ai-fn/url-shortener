from __future__ import annotations

import time
from datetime import UTC, datetime

from app.core.uuid7 import uuid7


def test_version_and_variant_bits_are_rfc_9562():
    value = uuid7()
    assert value.version == 7
    assert (value.int >> 62) & 0b11 == 0b10


def test_encodes_current_time_in_the_leading_48_bits():
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000

    assert before <= (value.int >> 80) <= after


def test_values_created_in_order_sort_in_order():
    """Time-ordering is the whole reason for v7 over v4; a broken layout still yields
    valid-looking UUIDs, so nothing else would catch it."""
    values = []
    for _ in range(5):
        values.append(uuid7())
        time.sleep(0.002)

    assert values == sorted(values)


def test_values_are_unique_within_the_same_millisecond():
    values = {uuid7() for _ in range(1000)}
    assert len(values) == 1000


def test_timestamp_is_interpretable_as_a_utc_datetime():
    value = uuid7()
    encoded = datetime.fromtimestamp((value.int >> 80) / 1000, tz=UTC)
    assert abs((datetime.now(UTC) - encoded).total_seconds()) < 5
