"""UUIDv7 (RFC 9562). Stdlib gains `uuid.uuid7()` in 3.14; the image pins 3.12.

Time-ordered, so `event_id` sorts by creation and ClickHouse's sparse index stays
useful on it. Not a substitute for `ts` — see ADR-0003.
"""

from __future__ import annotations

import os
import time
import uuid

_TS_MASK = (1 << 48) - 1
_RAND_B_MASK = (1 << 62) - 1


def uuid7() -> uuid.UUID:
    """48-bit ms timestamp | version 7 | 12 random | variant | 62 random."""
    ts_ms = (time.time_ns() // 1_000_000) & _TS_MASK
    rand = int.from_bytes(os.urandom(10), "big")

    value = ts_ms << 80
    value |= 0x7 << 76
    value |= ((rand >> 62) & 0xFFF) << 64
    value |= 0b10 << 62
    value |= rand & _RAND_B_MASK

    return uuid.UUID(int=value)
