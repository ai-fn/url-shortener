"""Validate user-supplied URLs before storage. Runs at link creation, never on the
redirect hot path — this does DNS resolution, which the hot path cannot afford.

We are running a public open redirect: this module is what stands between it and
phishing, SSRF, or an amplifier against our own infrastructure. See the `url-safety`
skill for the full threat model and the bypass corpus this is tested against.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 2048

# CR, LF, NUL: if one reaches the Location header, it splits the response and lets an
# attacker inject arbitrary headers or a body.
_FORBIDDEN_CHARS = ("\r", "\n", "\0")

Resolver = Callable[[str], Awaitable[list[tuple]]]  # type: ignore[type-arg]


class URLValidationError(ValueError):
    """A user-supplied URL failed validation. Message is safe to return to the client."""


async def _default_resolve(host: str) -> list[tuple]:  # type: ignore[type-arg]
    """`getaddrinfo` via the running loop's native resolver — off the event loop
    thread, unlike a bare `socket.getaddrinfo` call in an `async def`."""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)


# ipaddress's is_private does not cover this range on all supported Python versions
# (verified: 100.64.0.1 reports is_private=False), so it needs an explicit check —
# without it, a shared-address-space host sails through as "not private".
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # IPv4-mapped IPv6 (::ffff:a.b.c.d) must be unwrapped before classification.
    # is_private/is_loopback/etc. already understand the mapped form for most
    # ranges (::ffff:127.0.0.1 reports is_loopback=True), but the CGNAT check
    # below only runs for IPv4Address — unwrap first or ::ffff:100.64.0.1 sails
    # through as neither private nor CGNAT.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    # Named explicitly, not folded into is_private: this is the one address whose
    # compromise is credential theft, not just an internal-network probe.
    if str(ip) == "169.254.169.254":
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT:
        return True
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _reject_forbidden_chars(url: str) -> None:
    if any(ch in url for ch in _FORBIDDEN_CHARS):
        raise URLValidationError("URL contains a forbidden control character")


def assert_safe_for_header(url: str) -> None:
    """Re-checked at redirect time, on the value about to reach `Location`. Cheap,
    and the consequence of skipping it is a header-injection vuln — both ends check."""
    _reject_forbidden_chars(url)


async def validate_target_url(
    url: str,
    *,
    public_host: str,
    resolve: Resolver = _default_resolve,
) -> str:
    """Validate a user-supplied URL for storage. Returns the URL unchanged if safe.

    Raises URLValidationError with a message safe to surface to the client.
    """
    if len(url) > MAX_URL_LENGTH:
        raise URLValidationError(f"URL exceeds {MAX_URL_LENGTH} characters")

    _reject_forbidden_chars(url)

    parts = urlsplit(url)

    scheme = parts.scheme.strip().lower()
    if scheme not in ALLOWED_SCHEMES:
        raise URLValidationError(f"scheme {scheme!r} is not allowed")

    if parts.username or parts.password:
        raise URLValidationError("credentials in the URL authority are not allowed")

    host = parts.hostname
    if not host:
        raise URLValidationError("URL has no hostname")

    # IDN -> ASCII (punycode) before any comparison, or a homograph slips the guard.
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise URLValidationError("hostname could not be normalized") from exc

    if ascii_host.lower() == public_host.lower():
        raise URLValidationError("URL points back at this service (redirect loop)")

    # Resolve, then classify every returned address. Never branch on "is this a bare
    # IP?" first: ipaddress.ip_address() raises on 2130706433 / 0x7f.0x0.0x0.0x1 /
    # 0177.0.0.1 / 127.1, all of which getaddrinfo happily resolves to 127.0.0.1. A
    # try/except around ip_address() is the documented bypass, not a defence.
    try:
        addrinfo = await resolve(ascii_host)
    except socket.gaierror as exc:
        raise URLValidationError("hostname does not resolve") from exc

    if not addrinfo:
        raise URLValidationError("hostname does not resolve")

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        raw_ip = sockaddr[0]
        ip = ipaddress.ip_address(raw_ip)
        if _is_blocked_ip(ip):
            raise URLValidationError("URL resolves to a disallowed address")

    return url
