from __future__ import annotations

from httpx import AsyncClient


async def test_healthz_is_ok_without_any_dependency(client: AsyncClient) -> None:
    """Passes with no containers running at all, which is the actual assertion."""
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_openapi_is_served(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")

    assert response.status_code == 200
    assert "/healthz" in response.json()["paths"]
