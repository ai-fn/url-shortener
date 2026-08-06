"""Invariant 8: GET /{code} is a catch-all and must be the last route registered,
excluding every reserved prefix. Getting this wrong means /docs, /metrics or
favicon.ico silently start 404ing instead of resolving.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import FastAPI
from httpx import AsyncClient
from starlette.routing import BaseRoute

from app.config import Settings
from app.core.short_code import RESERVED_PREFIXES
from app.main import create_app


def _build_app() -> FastAPI:
    settings = Settings(  # type: ignore[arg-type]
        _env_file=None,
        environment="local",
        database_url="postgresql+asyncpg://test:test@localhost:5432/unused",
        redis_url="redis://localhost:6379/0",
        secret_key="test-only-insecure-key-000000000000000000",
        ip_hash_key="test-only-insecure-key-111111111111111111",
    )
    return create_app(settings)


def _flatten_paths(routes: Iterable[BaseRoute]) -> list[str]:
    """FastAPI 0.140 wraps each `include_router` call in an opaque `_IncludedRouter`
    rather than flattening it into `app.router.routes`; `original_router` is the
    only way back to the real Route objects. Private API — if this breaks on a
    FastAPI upgrade, the HTTP-behavior tests below are the ones that actually
    enforce the invariant."""
    paths: list[str] = []
    for route in routes:
        if hasattr(route, "original_router"):
            paths.extend(_flatten_paths(route.original_router.routes))  # type: ignore[attr-defined]
        elif hasattr(route, "path"):
            paths.append(route.path)  # type: ignore[attr-defined]
    return paths


async def test_docs_and_health_and_robots_are_not_eaten_by_the_catch_all(
    client: AsyncClient,
) -> None:
    for path in ("/docs", "/healthz", "/robots.txt", "/favicon.ico", "/openapi.json", "/metrics"):
        response = await client.get(path)
        assert response.status_code != 404, f"{path} was swallowed by the catch-all"


async def test_reserved_prefix_used_as_a_code_returns_404(client: AsyncClient) -> None:
    # "static" is reserved but has no route yet, so a code that collides with it
    # must still 404, not resolve.
    response = await client.get("/static")
    assert response.status_code == 404


def test_catch_all_code_route_is_registered_last() -> None:
    paths = _flatten_paths(_build_app().router.routes)
    assert paths[-1] == "/{code}", "GET /{code} must be the last registered route"


def test_every_other_route_is_covered_by_a_reserved_prefix() -> None:
    """The catch-all's exclusion list and the app's real routes must be the same
    set, or a new single-segment route added ahead of the catch-all silently
    becomes unreachable."""
    paths = _flatten_paths(_build_app().router.routes)

    first_segments = {p.strip("/").split("/")[0] for p in paths[:-1] if p not in ("/", "")}
    unreserved = first_segments - RESERVED_PREFIXES
    assert not unreserved, f"routes not covered by RESERVED_PREFIXES: {unreserved}"
