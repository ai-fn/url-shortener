"""Short code generation, shape and reserved prefixes.

Single source of truth: the catch-all route exclusion, the custom-alias validator,
and the route-registration test all read `RESERVED_PREFIXES` from here rather than
keeping their own copy.
"""

from __future__ import annotations

import re
import secrets
from string import ascii_lowercase, ascii_uppercase, digits

ALPHABET = ascii_lowercase + ascii_uppercase + digits
DEFAULT_LENGTH = 7

# 4-32 chars: short enough to type, long enough that a custom alias isn't trivially
# guessable as an existing one. Case-sensitive, matching ALPHABET.
CODE_PATTERN = re.compile(r"^[0-9A-Za-z_-]{4,32}$")

# Invariant 8: GET /{code} is a catch-all registered last. Every one of these is a
# real route or a well-known path browsers request unprompted (favicon.ico,
# robots.txt) — without the exclusion the catch-all eats them as 404s.
RESERVED_PREFIXES = frozenset(
    {
        "api",
        "docs",
        "redoc",
        "openapi.json",
        "health",
        "healthz",
        "readyz",
        "metrics",
        "static",
        "favicon.ico",
        "robots.txt",
    }
)


def is_reserved(code: str) -> bool:
    return code in RESERVED_PREFIXES


def is_valid_shape(code: str) -> bool:
    return bool(CODE_PATTERN.match(code))


def generate(length: int = DEFAULT_LENGTH) -> str:
    """A random base62 code. Collisions are handled by the DB constraint on insert,
    not prevented here — ~3.5e12 combinations at the default length makes them rare."""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))
