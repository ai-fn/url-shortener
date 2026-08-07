from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from app.events.schema import ClickEvent

_KEY = b"test-key-at-least-32-bytes-long-000000"
_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# The kafka_clicks_queue column list. A field added on one side and not the other is
# the failure this guards: input_format_skip_unknown_fields makes it silent.
_COLUMNS = {
    "event_id",
    "link_id",
    "ts",
    "ip_hash",
    "country",
    "device_type",
    "browser",
    "os",
    "referer_domain",
    "is_bot",
}


def _build(**overrides) -> ClickEvent:
    kwargs = {
        "link_id": uuid.uuid4(),
        "ip": "203.0.113.7",
        "user_agent": _CHROME,
        "referer": "https://news.example.com/story?utm=x",
        "ip_hash_key": _KEY,
        "geoip_reader": None,
    }
    kwargs.update(overrides)
    return ClickEvent.build(**kwargs)


def test_build_assigns_a_uuid7_event_id():
    assert _build().event_id.version == 7


def test_build_assigns_a_utc_timestamp():
    event = _build()
    assert event.ts.tzinfo is not None
    assert abs(datetime.now(UTC) - event.ts) < timedelta(seconds=5)


def test_each_build_gets_its_own_event_id():
    assert _build().event_id != _build().event_id


def test_build_carries_the_link_id_through():
    link_id = uuid.uuid4()
    assert _build(link_id=link_id).link_id == link_id


def test_build_populates_enriched_dimensions():
    event = _build()
    assert event.browser == "Chrome"
    assert event.os == "Windows"
    assert event.device_type == "desktop"
    assert event.referer_domain == "news.example.com"
    assert event.is_bot is False


def test_payload_keys_match_the_clickhouse_columns_exactly():
    assert set(json.loads(_build().to_kafka_payload())) == _COLUMNS


def test_payload_serializes_uuids_as_strings():
    payload = json.loads(_build().to_kafka_payload())
    assert uuid.UUID(payload["event_id"])
    assert uuid.UUID(payload["link_id"])


def test_payload_serializes_ts_in_clickhouse_datetime64_form():
    event = _build()
    payload = json.loads(event.to_kafka_payload())
    assert datetime.strptime(payload["ts"], "%Y-%m-%d %H:%M:%S.%f")
    assert payload["ts"] == event.ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def test_payload_serializes_ts_with_millisecond_precision():
    """DateTime64(3) — sending microseconds would be silently truncated, and the
    truncated value is what dedup keys on."""
    payload = json.loads(_build().to_kafka_payload())
    assert len(payload["ts"].split(".")[1]) == 3


def test_payload_serializes_is_bot_as_uint8():
    payload = json.loads(_build(user_agent="curl/8.4.0").to_kafka_payload())
    assert payload["is_bot"] == 1


def test_payload_ip_hash_is_a_plain_uint64_integer():
    payload = json.loads(_build().to_kafka_payload())
    assert isinstance(payload["ip_hash"], int)
    assert 0 <= payload["ip_hash"] < 2**64


def test_payload_carries_no_raw_visitor_data():
    """Raw IP, UA and referrer must never reach the topic — they would sit there for
    the whole retention window."""
    raw = _build().to_kafka_payload().decode()
    assert "203.0.113.7" not in raw
    assert "Mozilla" not in raw
    assert "utm=x" not in raw


def test_payload_is_one_json_object_with_no_newline():
    """JSONEachRow delimits on newlines; an embedded one would split the record."""
    raw = _build().to_kafka_payload()
    assert b"\n" not in raw
    assert json.loads(raw)
