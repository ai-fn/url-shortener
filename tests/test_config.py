"""Settings validation.

`public_base_url` is a security control, not a display string: it feeds the loop guard
that stops us shortening links back to ourselves.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings

_REQUIRED = {
    "environment": "test",
    "database_url": "postgresql+asyncpg://u:p@localhost:5432/db",
    "redis_url": "redis://localhost:6379/0",
    "secret_key": "test-only-insecure-key-000000000000000000",
    "ip_hash_key": "test-only-insecure-key-111111111111111111",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})  # type: ignore[arg-type]


pytestmark = pytest.mark.usefixtures("hermetic_env")


@pytest.mark.parametrize(
    ("base_url", "expected_host"),
    [
        ("http://localhost:8000", "localhost"),
        ("https://short.example.com", "short.example.com"),
        ("https://SHORT.EXAMPLE.COM", "short.example.com"),
        ("https://short.example.com/base/path", "short.example.com"),
    ],
)
def test_public_host_is_the_lowercased_hostname(base_url: str, expected_host: str) -> None:
    assert _settings(public_base_url=base_url).public_host == expected_host


@pytest.mark.parametrize("base_url", ["short.example.com", "localhost:8000", "not a url", ""])
def test_scheme_less_public_base_url_is_rejected_at_startup(base_url: str) -> None:
    """As a bare `str` these all parsed: urlparse reads `short.example.com` as a path
    and `localhost` as a scheme, so `public_host` returned "" and the guard matched
    nothing — a self-referential open redirect with no error anywhere.
    """
    with pytest.raises(ValidationError):
        _settings(public_base_url=base_url)


def test_default_public_base_url_yields_a_usable_host() -> None:
    assert _settings().public_host == "localhost"
