"""The click event. Its field names are the ClickHouse column names — see
`clickhouse/migrations/`; a rename here silently misroutes data there.

Nothing raw about the visitor is carried: the IP arrives hashed, the user agent
arrives parsed, the referrer arrives reduced to its host. Adding a raw field would
put it in Kafka for the whole retention window.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from app.core.uuid7 import uuid7
from app.events import enrichment


@dataclass(frozen=True)
class ClickEvent:
    event_id: uuid.UUID
    link_id: uuid.UUID
    ts: datetime
    ip_hash: int
    country: str
    device_type: str
    browser: str
    os: str
    referer_domain: str
    is_bot: bool

    @classmethod
    def build(
        cls,
        *,
        link_id: uuid.UUID,
        ip: str,
        user_agent: str,
        referer: str | None,
        ip_hash_key: bytes,
        geoip_reader: enrichment.GeoipReader | None,
    ) -> ClickEvent:
        """`event_id` and `ts` are assigned here and never again — ClickHouse dedups
        on a tuple containing `ts`, so a value reassigned downstream or per delivery
        disables dedup without erroring (ADR-0003)."""
        parsed = enrichment.parse_user_agent(user_agent)
        return cls(
            event_id=uuid7(),
            link_id=link_id,
            ts=datetime.now(UTC),
            ip_hash=enrichment.hash_ip(ip, ip_hash_key),
            country=enrichment.lookup_country(ip, geoip_reader),
            device_type=parsed.device_type,
            browser=parsed.browser,
            os=parsed.os,
            referer_domain=enrichment.referrer_domain(referer),
            is_bot=parsed.is_bot,
        )

    def to_kafka_payload(self) -> bytes:
        """JSONEachRow, matching `kafka_clicks_queue` column for column."""
        payload = asdict(self)
        payload["event_id"] = str(self.event_id)
        payload["link_id"] = str(self.link_id)
        # ClickHouse's own DateTime64 text form. Its ISO-8601 parsing is looser and
        # varies by setting; this spelling does not.
        payload["ts"] = self.ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        payload["is_bot"] = int(self.is_bot)
        return json.dumps(payload, separators=(",", ":")).encode()
