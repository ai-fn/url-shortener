"""The hot path. GET /{code} -> Postgres -> 302. Changes here need unusual care —
see the redirect-hot-path skill.

Resources are pulled off `request.app.state` directly rather than via `Depends()`:
FastAPI resolves every declared dependency before the handler body runs, which
would defeat the reserved-prefix short-circuit below — a request for /favicon.ico
would open a DB session before ever reaching the check that rejects it.

No Redis link cache and no click event here: those arrive in milestone 4/5. This
route only does what milestone 2 needs — do not pre-build them.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.core.rate_limit import RateLimiter, RateLimitUnavailable, client_ip
from app.core.short_code import is_reserved, is_valid_shape
from app.core.url_validation import assert_safe_for_header
from app.services import links as links_service
from app.services.links import LinkNotFoundError

router = APIRouter()

_NO_STORE = "no-store, no-cache, must-revalidate, max-age=0"


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

    async with request.app.state.sessionmaker() as session:
        try:
            link = await links_service.get_redirect_target(session, code)
        except LinkNotFoundError as exc:
            await _reject_if_over_miss_budget(request)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from exc

    assert_safe_for_header(link.target_url)

    return Response(
        status_code=status.HTTP_302_FOUND,
        headers={"Location": link.target_url, "Cache-Control": _NO_STORE},
    )
