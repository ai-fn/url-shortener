"""robots.txt and favicon.ico. Both must be registered before the GET /{code}
catch-all, and both path segments live in app.core.short_code.RESERVED_PREFIXES
so the catch-all excludes them."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, Response

router = APIRouter()

_ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
_FAVICON_PATH = Path(__file__).resolve().parent.parent / "static" / "favicon.ico"


@router.get("/robots.txt", response_class=Response, include_in_schema=False)
async def robots_txt() -> Response:
    return Response(content=_ROBOTS_TXT, media_type="text/plain")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(_FAVICON_PATH, media_type="image/x-icon")
