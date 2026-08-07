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
    Gauge,
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


CLICKS_PRODUCED = Counter(
    "clicks_produced_total",
    "Click events acknowledged by Kafka.",
    registry=REGISTRY,
)

CLICKS_DROPPED = Counter(
    "clicks_dropped_total",
    "Click events discarded rather than delivered, by reason.",
    labelnames=["reason"],  # queue_full | send_failed | enrich_failed
    registry=REGISTRY,
)

# Set via set_function in lifespan: the queue does not exist at import time, and a
# depth sampled at scrape time is the only one worth reporting anyway.
CLICK_QUEUE_DEPTH = Gauge(
    "click_queue_depth",
    "Click events waiting in the in-process queue.",
    registry=REGISTRY,
)

ENRICH_DURATION = Histogram(
    "enrich_duration_seconds",
    "Time spent enriching a click event on the redirect path.",
    registry=REGISTRY,
)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
