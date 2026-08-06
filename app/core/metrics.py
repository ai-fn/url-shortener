"""Prometheus collectors, defined once at import time — not via
prometheus-fastapi-instrumentator, whose .instrument() installs a global middleware
that would run on every redirect and trip the global-middleware-registration
invariant rule for no benefit the redirect path needs.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

LINK_CACHE_LOOKUPS = Counter(
    "link_cache_lookups_total",
    "Redirect-path link cache lookups by outcome.",
    labelnames=["result"],  # hit | negative | miss | error
    registry=REGISTRY,
)

REDIRECT_DURATION = Histogram(
    "redirect_duration_seconds",
    "Time spent resolving and serving GET /{code}.",
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
