from __future__ import annotations

import geoip2.errors
import pytest

from app.events import enrichment

_KEY = b"test-key-at-least-32-bytes-long-000000"

_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_IPAD = (
    "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)
_GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


class _FakeReader:
    """Stands in for `geoip2.database.Reader` when monkeypatched into
    `open_geoip_reader`. `.city()`/`.country()` raise TypeError against the *other*
    database's type, exactly like the real `geoip2.database.Reader._get` does."""

    def __init__(self, iso_code: str | None = "DE", database_type: str = "GeoLite2-City"):
        self._iso_code = iso_code
        self._database_type = database_type

    def metadata(self):
        return type("Metadata", (), {"database_type": self._database_type})()

    def _response(self, method: str, required: str):
        if required not in self._database_type:
            raise TypeError(
                f"The {method} method cannot be used with the {self._database_type} database"
            )
        return type(
            "Response", (), {"country": type("Country", (), {"iso_code": self._iso_code})()}
        )()

    def city(self, ip: str):
        return self._response("city", "City")

    def country(self, ip: str):
        return self._response("country", "Country")

    def close(self):
        pass


def _geoip_reader(
    iso_code: str | None = None, raises: Exception | None = None
) -> enrichment.GeoipReader:
    """A `GeoipReader` with its `_lookup` resolved directly — mirrors what
    `open_geoip_reader` hands `lookup_country` after picking `.city`/`.country`."""

    def lookup(ip: str):
        if raises is not None:
            raise raises
        return type("Response", (), {"country": type("Country", (), {"iso_code": iso_code})()})()

    return enrichment.GeoipReader(_reader=None, _lookup=lookup)  # type: ignore[arg-type]


def test_hash_ip_is_deterministic():
    assert enrichment.hash_ip("203.0.113.7", _KEY) == enrichment.hash_ip("203.0.113.7", _KEY)


def test_hash_ip_fits_uint64():
    assert 0 <= enrichment.hash_ip("203.0.113.7", _KEY) < 2**64


def test_hash_ip_collapses_addresses_in_the_same_v4_slash24():
    """Truncation must happen before hashing; hashing first would still yield a
    plausible UInt64 while silently making every address its own visitor."""
    assert enrichment.hash_ip("203.0.113.7", _KEY) == enrichment.hash_ip("203.0.113.250", _KEY)


def test_hash_ip_separates_different_v4_slash24s():
    assert enrichment.hash_ip("203.0.113.7", _KEY) != enrichment.hash_ip("203.0.114.7", _KEY)


def test_hash_ip_collapses_addresses_in_the_same_v6_slash48():
    same = enrichment.hash_ip("2001:db8:abcd:1::1", _KEY)
    assert same == enrichment.hash_ip("2001:db8:abcd:ffff::9999", _KEY)


def test_hash_ip_separates_different_v6_slash48s():
    assert enrichment.hash_ip("2001:db8:abcd::1", _KEY) != enrichment.hash_ip(
        "2001:db8:abce::1", _KEY
    )


def test_hash_ip_accepts_a_non_address_without_raising():
    """client_ip() returns "unknown" when the request has no client; the hot path
    must not raise on it."""
    assert 0 <= enrichment.hash_ip("unknown", _KEY) < 2**64


def test_hash_ip_changes_with_the_key():
    assert enrichment.hash_ip("203.0.113.7", _KEY) != enrichment.hash_ip("203.0.113.7", b"other")


@pytest.mark.parametrize(
    ("user_agent", "expected"),
    [
        (_CHROME, "desktop"),
        (_IPHONE, "mobile"),
        (_IPAD, "tablet"),
        (_GOOGLEBOT, "bot"),
    ],
)
def test_parse_user_agent_device_type(user_agent, expected):
    assert enrichment.parse_user_agent(user_agent).device_type == expected


def test_parse_user_agent_reads_browser_and_os():
    parsed = enrichment.parse_user_agent(_CHROME)
    assert parsed.browser == "Chrome"
    assert parsed.os == "Windows"


def test_parse_user_agent_flags_known_crawlers():
    assert enrichment.parse_user_agent(_GOOGLEBOT).is_bot is True


def test_parse_user_agent_flags_self_identifying_tools():
    assert enrichment.parse_user_agent("curl/8.4.0").is_bot is True


def test_parse_user_agent_does_not_flag_a_real_browser():
    assert enrichment.parse_user_agent(_CHROME).is_bot is False


def test_parse_user_agent_treats_a_missing_agent_as_a_bot():
    assert enrichment.parse_user_agent("").is_bot is True


def test_parse_user_agent_survives_garbage():
    parsed = enrichment.parse_user_agent("!!!not-a-user-agent!!!")
    assert parsed.device_type == "desktop"
    assert parsed.browser == ""


def test_parse_user_agent_truncates_before_caching():
    """Two agents differing only past the cutoff must share one cache entry —
    otherwise the cache key isn't actually the truncated string."""
    enrichment._parse_user_agent_cached.cache_clear()
    prefix = "A" * enrichment._MAX_CACHED_UA_LENGTH
    enrichment.parse_user_agent(prefix + "-suffix-one")
    enrichment.parse_user_agent(prefix + "-a-longer-suffix-two")

    assert enrichment._parse_user_agent_cached.cache_info().currsize == 1


def test_parse_user_agent_misses_a_bot_token_past_the_truncation_cutoff():
    """_BOT_PATTERN runs on the truncated string, so a marker beyond the cutoff is
    invisible — the accepted cost of capping cache memory."""
    padded = "A" * enrichment._MAX_CACHED_UA_LENGTH + "bot"
    assert enrichment.parse_user_agent(padded).is_bot is False


@pytest.mark.parametrize(
    ("referer", "expected"),
    [
        ("https://news.example.com/a/b?utm=x", "news.example.com"),
        ("https://NEWS.example.com/", "news.example.com"),
        ("", ""),
        (None, ""),
        ("not a url", ""),
    ],
)
def test_referrer_domain(referer, expected):
    assert enrichment.referrer_domain(referer) == expected


def test_lookup_country_returns_the_iso_code():
    assert enrichment.lookup_country("203.0.113.7", _geoip_reader(iso_code="DE")) == "DE"


def test_lookup_country_without_a_reader():
    assert enrichment.lookup_country("203.0.113.7", None) == ""


def test_lookup_country_for_an_address_not_in_the_database():
    reader = _geoip_reader(raises=geoip2.errors.AddressNotFoundError("10.0.0.1 not found"))
    assert enrichment.lookup_country("10.0.0.1", reader) == ""


def test_lookup_country_for_an_unparseable_address():
    assert enrichment.lookup_country("unknown", _geoip_reader(raises=ValueError("bad"))) == ""


def test_lookup_country_when_the_database_has_no_iso_code():
    assert enrichment.lookup_country("203.0.113.7", _geoip_reader(iso_code=None)) == ""


def test_open_geoip_reader_wires_a_city_database_to_the_city_method(monkeypatch, tmp_path):
    """geoip_database_path defaults to a City database; `.country()` raises TypeError
    against it, so open_geoip_reader must resolve `.city` instead."""
    monkeypatch.setattr(
        "geoip2.database.Reader",
        lambda *_a, **_kw: _FakeReader(iso_code="DE", database_type="GeoLite2-City"),
    )
    path = tmp_path / "city.mmdb"
    path.write_bytes(b"unused, Reader is stubbed")

    reader = enrichment.open_geoip_reader(str(path))

    assert reader is not None
    assert enrichment.lookup_country("203.0.113.7", reader) == "DE"


def test_open_geoip_reader_wires_a_country_database_to_the_country_method(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "geoip2.database.Reader",
        lambda *_a, **_kw: _FakeReader(iso_code="DE", database_type="GeoLite2-Country"),
    )
    path = tmp_path / "country.mmdb"
    path.write_bytes(b"unused, Reader is stubbed")

    reader = enrichment.open_geoip_reader(str(path))

    assert reader is not None
    assert enrichment.lookup_country("203.0.113.7", reader) == "DE"


def test_open_geoip_reader_returns_none_for_a_missing_file(tmp_path):
    assert enrichment.open_geoip_reader(str(tmp_path / "absent.mmdb")) is None


def test_open_geoip_reader_returns_none_for_a_corrupt_file(tmp_path):
    corrupt = tmp_path / "corrupt.mmdb"
    corrupt.write_bytes(b"definitely not a maxmind database")
    assert enrichment.open_geoip_reader(str(corrupt)) is None


def test_open_geoip_reader_rejects_a_database_with_no_country_data(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "geoip2.database.Reader", lambda *_a, **_kw: _FakeReader(database_type="GeoLite2-ASN")
    )
    path = tmp_path / "asn.mmdb"
    path.write_bytes(b"unused, Reader is stubbed")
    assert enrichment.open_geoip_reader(str(path)) is None
