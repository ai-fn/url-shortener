"""GET /{code} against real Postgres and Redis."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.link import Link

pytestmark = pytest.mark.integration


async def _create_link(live_client: AsyncClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"target_url": "https://example.com/"}
    payload.update(overrides)
    response = await live_client.post("/api/v1/links", json=payload)
    assert response.status_code == 201
    result: dict[str, object] = response.json()
    return result


async def test_redirect_follows_to_target_with_no_store(live_client: AsyncClient) -> None:
    link = await _create_link(live_client)

    response = await live_client.get(f"/{link['short_code']}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/"
    assert response.headers["cache-control"] == "no-store, no-cache, must-revalidate, max-age=0"


async def test_unknown_code_is_404(live_client: AsyncClient) -> None:
    response = await live_client.get("/does-not-exist", follow_redirects=False)
    assert response.status_code == 404


async def test_inactive_link_is_404(live_client: AsyncClient) -> None:
    link = await _create_link(live_client)
    delete_response = await live_client.delete(f"/api/v1/links/{link['id']}")
    assert delete_response.status_code == 204

    response = await live_client.get(f"/{link['short_code']}", follow_redirects=False)
    assert response.status_code == 404


async def test_expired_link_is_404(live_client: AsyncClient, db_session: AsyncSession) -> None:
    """Seeded directly via the DB: the API has no way to create an already-expired
    link, and that's fine — it shouldn't."""
    link = Link(
        short_code="expiredlink",
        target_url="https://example.com/",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    db_session.add(link)
    await db_session.commit()

    response = await live_client.get("/expiredlink", follow_redirects=False)
    assert response.status_code == 404


async def test_reserved_prefix_is_404_not_a_lookup(live_client: AsyncClient) -> None:
    response = await live_client.get("/healthz")
    assert response.status_code == 200  # served by the real route, not the catch-all

    # "metrics" is reserved (Prometheus lands later) but has no route yet, so a
    # code colliding with it must still 404 rather than resolve.
    response = await live_client.get("/metrics", follow_redirects=False)
    assert response.status_code == 404


async def test_repeated_misses_eventually_rate_limited(live_client: AsyncClient) -> None:
    # Default budget is 120/minute; comfortably exceed it in one burst.
    statuses = [
        (await live_client.get("/no-such-code-at-all", follow_redirects=False)).status_code
        for _ in range(150)
    ]
    assert 429 in statuses


async def test_valid_hits_are_never_throttled_by_the_miss_budget(live_client: AsyncClient) -> None:
    """The 404 budget must charge only actual misses. A shared rate limit that also
    counted hits would 429 a popular link once enough legitimate clicks came from
    one IP — exactly the case redirects must never lose to."""
    link = await _create_link(live_client)

    statuses = [
        (await live_client.get(f"/{link['short_code']}", follow_redirects=False)).status_code
        for _ in range(150)
    ]

    assert all(status == 302 for status in statuses)
