"""The hot path. GET /{code} -> Redis -> Postgres on a miss -> 302. Changes here
need unusual care — see the redirect-hot-path skill.

Resources are pulled off `request.app.state` directly rather than via `Depends()`:
FastAPI resolves every declared dependency before the handler body runs, which
would defeat the reserved-prefix short-circuit below — a request for /favicon.ico
would touch Redis before ever reaching the check that rejects it.

The click event is enqueued, never sent: `put_nowait` on a bounded queue, drained by
a background task that owns the Kafka producer. Awaiting `send()` here hangs the
redirect for ~40s with the broker down (ADR-0001).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.config import Settings
from app.core import metrics
from app.core.rate_limit import RateLimiter, RateLimitUnavailable, client_ip
from app.core.short_code import is_reserved, is_valid_shape
from app.core.url_validation import assert_safe_for_header
from app.events.schema import ClickEvent
from app.services import redirect as redirect_service
from app.services.links import LinkNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"

# Enrichment failures are systematic, not one-off — a bad GeoIP database or config
# error hits every request the same way. One `logger.exception` per interval, not
# per request, or a synchronous full traceback lands on the hot path continuously.
# The drop counter carries the signal either way.
_ENRICH_ERROR_LOG_INTERVAL_SECONDS = 60.0
_last_enrich_error_log = 0.0


async def _reject_if_over_miss_budget(request: Request) -> None:
    """Charges the 404 budget — only ever called for an actual miss. A hit must
    never be throttled by it: this codebase's one rule is that redirects win over
    everything else, abuse controls included, so a legitimate popular link shared
    behind one IP (a corporate NAT, say) must never see 429 on a real redirect.

    The trade-off this accepts: a sustained miss-flood keeps touching Postgres for
    every probe rather than being turned away before the query once over budget,
    since the miss can only be known after the lookup. Redis unavailable fails
    open, same as everywhere else on this path.
    """
    settings = request.app.state.settings
    limiter = RateLimiter(
        redis=request.app.state.redis,
        key_prefix="rl:404",
        capacity=settings.rate_limit_notfound_per_minute,
    )
    try:
        allowed = await limiter.allow(client_ip(request))
    except RateLimitUnavailable:
        return
    if not allowed:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)


@router.get(
    "/{code}",
    # No response_model: a redirect has no body. Status and headers are the contract.
    response_class=Response,
    status_code=status.HTTP_302_FOUND,
    responses={
        status.HTTP_302_FOUND: {"description": "Redirect to the link's target URL."},
        status.HTTP_404_NOT_FOUND: {"description": "Unknown, inactive or expired code."},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Too many misses from this client."},
    },
    include_in_schema=False,
)
async def redirect(code: str, request: Request) -> Response:
    if not is_valid_shape(code) or is_reserved(code):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    settings = request.app.state.settings
    try:
        with metrics.REDIRECT_DURATION.time():
            link = await redirect_service.resolve(
                redis=request.app.state.redis,
                sessionmaker=request.app.state.sessionmaker,
                code=code,
                ttl_seconds=settings.link_cache_ttl_seconds,
                negative_ttl_seconds=settings.link_cache_negative_ttl_seconds,
            )
            assert_safe_for_header(link.target_url)
            response = Response(
                status_code=status.HTTP_302_FOUND,
                headers={"Location": link.target_url, "Cache-Control": _NO_STORE},
            )
    except LinkNotFoundError as exc:
        await _reject_if_over_miss_budget(request)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc

    _enqueue_click(request, link.link_id, settings)
    return response


def _enqueue_click(request: Request, link_id: uuid.UUID, settings: Settings) -> None:
    """Synchronous by design. Enrichment is in-memory CPU work, and the IP is hashed
    before it leaves the process — moving it downstream would put raw IPs in Kafka
    for the whole retention window.

    Runs after the response is already built: nothing here may turn a redirect that
    already succeeded into a 500."""
    try:
        with metrics.ENRICH_DURATION.time():
            event = ClickEvent.build(
                link_id=link_id,
                ip=client_ip(request),
                user_agent=request.headers.get("user-agent", ""),
                referer=request.headers.get("referer"),
                ip_hash_key=settings.ip_hash_key.get_secret_value().encode(),
                geoip_reader=request.app.state.geoip_reader,
            )
        request.app.state.click_queue.put_nowait(event)
    except asyncio.QueueFull:
        metrics.CLICKS_DROPPED.labels(reason="queue_full").inc()
    except Exception:
        metrics.CLICKS_DROPPED.labels(reason="enrich_failed").inc()
        _log_enrich_failure_throttled()


def _log_enrich_failure_throttled() -> None:
    global _last_enrich_error_log
    now = time.monotonic()
    if now - _last_enrich_error_log < _ENRICH_ERROR_LOG_INTERVAL_SECONDS:
        return
    _last_enrich_error_log = now
    logger.exception("dropped click event")
