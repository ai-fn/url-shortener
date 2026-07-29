"""Table-driven over the url-safety skill's bypass corpus.

An injected resolver stands in for DNS: unit tests must not touch the network,
and the whole point of this module is that the check runs on the *resolved*
address, never on a syntactic read of the host.
"""

from __future__ import annotations

import socket

import pytest

from app.core.url_validation import URLValidationError, validate_target_url

PUBLIC_HOST = "short.example.com"


def _resolver(*addresses: str) -> object:
    async def resolve(host: str) -> list[tuple]:  # type: ignore[type-arg]
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (addr, 0)) for addr in addresses]

    return resolve


def _unresolvable() -> object:
    async def resolve(host: str) -> list[tuple]:  # type: ignore[type-arg]
        raise socket.gaierror("no such host")

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "file:///etc/passwd",
        "vbscript:msgbox(1)",
        "blob:https://example.com/uuid",
    ],
)
async def test_rejects_disallowed_scheme(url: str) -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(url, public_host=PUBLIC_HOST, resolve=_resolver("93.184.216.34"))


@pytest.mark.parametrize(
    "resolved_ip",
    [
        "127.0.0.1",
        "0.0.0.0",
        "169.254.169.254",
        "169.254.1.1",
        "10.0.0.5",
        "172.16.0.5",
        "192.168.1.5",
        "100.64.0.1",
        "224.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "ff00::1",
        # IPv4-mapped IPv6 forms of the same blocked addresses. is_private/etc.
        # understand most mapped forms natively; ::ffff:100.64.0.1 does not,
        # since the CGNAT check is otherwise IPv4Address-only.
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "::ffff:10.0.0.5",
        "::ffff:100.64.0.1",
    ],
)
async def test_rejects_url_resolving_to_blocked_address(resolved_ip: str) -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(
            "https://evil.example.com/", public_host=PUBLIC_HOST, resolve=_resolver(resolved_ip)
        )


async def test_rejects_when_any_resolved_address_is_blocked() -> None:
    """A hostname with both a public and a loopback A record must be rejected —
    checking only the first address is the documented bypass."""
    with pytest.raises(URLValidationError):
        await validate_target_url(
            "https://multi.example.com/",
            public_host=PUBLIC_HOST,
            resolve=_resolver("93.184.216.34", "127.0.0.1"),
        )


async def test_accepts_url_resolving_to_public_address() -> None:
    result = await validate_target_url(
        "https://example.com/path", public_host=PUBLIC_HOST, resolve=_resolver("93.184.216.34")
    )
    assert result == "https://example.com/path"


async def test_rejects_unresolvable_host() -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(
            "https://does-not-exist.invalid/", public_host=PUBLIC_HOST, resolve=_unresolvable()
        )


@pytest.mark.parametrize(
    "url",
    [
        # Literal control characters only — a percent-encoded "%0d%0a" is inert here
        # since the stored string is never decoded before it reaches Location.
        "https://example.com/\r\nSet-Cookie: evil=1",
        "https://example.com/\nSet-Cookie: evil=1",
        "https://example.com/\x00null",
    ],
)
async def test_rejects_header_injection_characters(url: str) -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(url, public_host=PUBLIC_HOST, resolve=_resolver("93.184.216.34"))


async def test_rejects_url_over_max_length() -> None:
    url = "https://example.com/" + "a" * 2048
    with pytest.raises(URLValidationError):
        await validate_target_url(url, public_host=PUBLIC_HOST, resolve=_resolver("93.184.216.34"))


async def test_rejects_missing_hostname() -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(
            "https:///path", public_host=PUBLIC_HOST, resolve=_resolver("1.2.3.4")
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://evil.com@127.0.0.1/",
        "http://user:pass@example.com/",
    ],
)
async def test_rejects_embedded_credentials(url: str) -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(url, public_host=PUBLIC_HOST, resolve=_resolver("93.184.216.34"))


async def test_rejects_self_domain_loop() -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(
            f"http://{PUBLIC_HOST}/abc123",
            public_host=PUBLIC_HOST,
            resolve=_resolver("93.184.216.34"),
        )


async def test_rejects_self_domain_loop_case_insensitive() -> None:
    with pytest.raises(URLValidationError):
        await validate_target_url(
            f"http://{PUBLIC_HOST.upper()}/abc123",
            public_host=PUBLIC_HOST,
            resolve=_resolver("93.184.216.34"),
        )


async def test_rejects_homograph_self_domain() -> None:
    """Uses a Cyrillic look-alike of the Latin letter it replaces (U+0430) — must
    normalize via IDNA before the self-domain comparison, or this slips the loop
    guard."""
    homograph_host = "shortа.example.com"  # noqa: RUF001
    with pytest.raises(URLValidationError):
        await validate_target_url(
            f"http://{homograph_host}/abc123",
            public_host=homograph_host.encode("idna").decode("ascii"),
            resolve=_resolver("93.184.216.34"),
        )
