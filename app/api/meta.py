"""robots.txt, favicon.ico and /metrics. All three must be registered before the
GET /{code} catch-all, and each path segment lives in
app.core.short_code.RESERVED_PREFIXES so the catch-all excludes it."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

from app.core import metrics

router = APIRouter()

_ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
_FAVICON_PATH = Path(__file__).resolve().parent.parent / "static" / "favicon.ico"


@router.get("/robots.txt", response_class=Response, include_in_schema=False)
async def robots_txt() -> Response:
    return Response(content=_ROBOTS_TXT, media_type="text/plain")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(_FAVICON_PATH, media_type="image/x-icon")


@router.get("/metrics", response_class=Response, include_in_schema=False)
async def metrics_endpoint() -> Response:
    body, content_type = metrics.render()
    return Response(content=body, media_type=content_type)
