"""Click event enrichment. Runs inline on the redirect path — see the
redirect-hot-path skill for the latency budget.

Nothing here does I/O. The GeoIP database is opened once at startup and read from
memory; the UA parse is cached. The plaintext IP is HMAC'd here specifically so it
never leaves the process: moving enrichment downstream would put raw IPs in a Kafka
topic for the whole retention window.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from urllib.parse import urlsplit

import geoip2.database
import geoip2.errors
import geoip2.models
import maxminddb
import ua_parser

logger = logging.getLogger(__name__)

# ua-parser reports 'Spider' for the crawlers it knows; this catches the rest, which
# mostly self-identify. Neither is authoritative, and that is fine — is_bot is a
# dimension, not an access control decision.
_BOT_PATTERN = re.compile(
    r"bot|spider|crawl|slurp|facebookexternalhit|preview|monitor|curl|wget|python-requests",
    re.IGNORECASE,
)
_TABLET_FAMILIES = ("iPad", "Tablet", "Kindle", "Nexus 7", "Nexus 10")

_UNKNOWN = ""


@dataclass(frozen=True)
class UserAgentInfo:
    device_type: str
    browser: str
    os: str
    is_bot: bool


@dataclass(frozen=True)
class GeoipReader:
    """A geoip2 Reader with its country-lookup method resolved once, at open time.

    `reader.metadata().database_type` costs ~2us per call with the C extension — it
    rebuilds the Metadata object rather than caching it — against a 5-20us budget for
    the whole GeoIP step (redirect-hot-path skill). Resolving `.city` vs `.country`
    here, instead of on every lookup, keeps that cost out of the hot path.
    """

    _reader: geoip2.database.Reader
    _lookup: Callable[[str], geoip2.models.City | geoip2.models.Country]

    def close(self) -> None:
        self._reader.close()


def open_geoip_reader(path: str) -> GeoipReader | None:
    """None on any failure: a missing database costs the `country` dimension, and
    that must never be worth failing startup or a redirect over.

    MODE_AUTO, not MODE_MEMORY: maxminddb documents MODE_MEMORY as pure Python, so
    asking for it is asking for the slow reader. AUTO takes the mmap'd C extension
    when the wheel has one and falls back only when it must.
    """
    try:
        reader = geoip2.database.Reader(path, mode=maxminddb.MODE_AUTO)
    except (OSError, maxminddb.InvalidDatabaseError) as exc:
        logger.warning("geoip database unavailable, country will be empty: %s", exc)
        return None

    # City and Country databases are the only two types `lookup_country` knows how to
    # read (via `.city()`/`.country()` respectively); anything else — an ASN database,
    # say — would raise TypeError on every single lookup instead of costing just the
    # country dimension.
    db_type = reader.metadata().database_type
    if "City" in db_type:
        lookup: Callable[[str], geoip2.models.City | geoip2.models.Country] = reader.city
    elif "Country" in db_type:
        lookup = reader.country
    else:
        logger.warning("geoip database type %r has no country data, country will be empty", db_type)
        reader.close()
        return None

    if not _uses_c_extension(reader):  # pragma: no cover - depends on the installed wheel
        logger.warning("maxminddb C extension missing; GeoIP lookups are ~10x slower")
    return GeoipReader(_reader=reader, _lookup=lookup)


def _uses_c_extension(reader: geoip2.database.Reader) -> bool:
    """A diagnostic only — must never be able to fail startup, so an AttributeError
    from a maxminddb internal renaming `_db_reader` is swallowed, not raised."""
    extension = getattr(maxminddb, "extension", None)
    if extension is None:
        return False
    try:
        return isinstance(reader._db_reader, extension.Reader)
    except AttributeError:
        return False


def hash_ip(ip: str, key: bytes) -> int:
    """Truncate to /24 (v4) or /48 (v6), then HMAC. Truncating *after* hashing would
    still produce a plausible UInt64 while silently turning "unique visitor" into
    "unique address"."""
    network = _truncate(ip)
    digest = hmac.new(key, network.encode(), sha256).digest()
    return int.from_bytes(digest[:8], "big")


def _truncate(ip: str) -> str:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{ip}/{prefix}", strict=False).network_address)


_MAX_CACHED_UA_LENGTH = 512


def parse_user_agent(user_agent: str) -> UserAgentInfo:
    """Truncated before the cached call, since lru_cache retains its key verbatim —
    otherwise a flood of unique multi-KB headers fills the cache at near-zero hit rate."""
    return _parse_user_agent_cached(user_agent[:_MAX_CACHED_UA_LENGTH])


@lru_cache(maxsize=4096)
def _parse_user_agent_cached(user_agent: str) -> UserAgentInfo:
    if not user_agent:
        return UserAgentInfo(_UNKNOWN, _UNKNOWN, _UNKNOWN, is_bot=True)

    result = ua_parser.parse(user_agent)
    device = result.device
    is_spider = device is not None and device.family == "Spider"

    return UserAgentInfo(
        device_type=_device_type(device.family if device is not None else None),
        browser=result.user_agent.family if result.user_agent is not None else _UNKNOWN,
        os=result.os.family if result.os is not None else _UNKNOWN,
        is_bot=is_spider or bool(_BOT_PATTERN.search(user_agent)),
    )


def _device_type(family: str | None) -> str:
    # ua-parser reports no device for desktop browsers, so absence is the signal.
    if family is None or family == "Other":
        return "desktop"
    if family == "Spider":
        return "bot"
    if any(tablet in family for tablet in _TABLET_FAMILIES):
        return "tablet"
    return "mobile"


def referrer_domain(referer: str | None) -> str:
    """Host only. The full referrer routinely carries session tokens and PII in its
    query string, and we have no reason to store it."""
    if not referer:
        return _UNKNOWN
    try:
        return (urlsplit(referer).hostname or _UNKNOWN).lower()
    except ValueError:
        return _UNKNOWN


def lookup_country(ip: str, reader: GeoipReader | None) -> str:
    """AddressNotFoundError is the normal case for private, loopback and CGNAT
    addresses, not a failure worth reporting."""
    if reader is None:
        return _UNKNOWN
    try:
        return reader._lookup(ip).country.iso_code or _UNKNOWN
    except (geoip2.errors.GeoIP2Error, ValueError):
        return _UNKNOWN
