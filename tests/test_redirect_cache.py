"""GET /{code} against real Postgres and Redis: the cache-aside contract itself —
hit avoids Postgres, invalidation is immediate, and Redis being down never breaks
a redirect. Basic 302/404 behavior is covered by tests/test_redirect.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient

from app.cache.link_cache import LinkCacheUnavailable
from app.services import links as links_service
from tests.conftest import register_and_login

pytestmark = pytest.mark.integration


async def _create_link(
    live_client: AsyncClient, **overrides: object
) -> tuple[dict[str, object], dict[str, str]]:
    payload: dict[str, object] = {"target_url": "https://example.com/"}
    payload.update(overrides)
    headers = await register_and_login(live_client)
    response = await live_client.post("/api/v1/links", json=payload, headers=headers)
    assert response.status_code == 201
    result: dict[str, object] = response.json()
    return result, headers


async def test_hit_populates_cache_and_avoids_postgres_on_the_next_request(
    live_client: AsyncClient, redis_client: aioredis.Redis
) -> None:
    link, _ = await _create_link(live_client)
    code = link["short_code"]

    first = await live_client.get(f"/{code}", follow_redirects=False)
    assert first.status_code == 302
    assert await redis_client.get(f"link:{code}") is not None

    with patch(
        "app.services.links.get_by_code", new=AsyncMock(side_effect=AssertionError("hit Postgres"))
    ):
        second = await live_client.get(f"/{code}", follow_redirects=False)

    assert second.status_code == 302
    assert second.headers["location"] == "https://example.com/"


async def test_update_is_visible_on_the_very_next_redirect(live_client: AsyncClient) -> None:
    link, headers = await _create_link(live_client)
    code = link["short_code"]

    await live_client.get(f"/{code}", follow_redirects=False)  # populate the cache

    patch_response = await live_client.patch(
        f"/api/v1/links/{link['id']}",
        json={"target_url": "https://example.org/"},
        headers=headers,
    )
    assert patch_response.status_code == 200

    response = await live_client.get(f"/{code}", follow_redirects=False)
    assert response.headers["location"] == "https://example.org/"


async def test_delete_is_visible_on_the_very_next_redirect(live_client: AsyncClient) -> None:
    link, headers = await _create_link(live_client)
    code = link["short_code"]

    await live_client.get(f"/{code}", follow_redirects=False)  # populate the cache

    delete_response = await live_client.delete(f"/api/v1/links/{link['id']}", headers=headers)
    assert delete_response.status_code == 204

    response = await live_client.get(f"/{code}", follow_redirects=False)
    assert response.status_code == 404


async def test_unknown_code_writes_negative_sentinel_and_stays_out_of_postgres(
    live_client: AsyncClient, redis_client: aioredis.Redis
) -> None:
    first = await live_client.get("/no-such-code-ever", follow_redirects=False)
    assert first.status_code == 404
    assert await redis_client.get("link:no-such-code-ever") == b"\x00"

    with patch(
        "app.services.links.get_by_code", new=AsyncMock(side_effect=AssertionError("hit Postgres"))
    ):
        second = await live_client.get("/no-such-code-ever", follow_redirects=False)

    assert second.status_code == 404


async def test_alias_created_after_being_probed_resolves_immediately(
    live_client: AsyncClient,
) -> None:
    """The negative-sentinel regression: creation must invalidate the cache too, or
    a probed-then-created alias 404s for up to link_cache_negative_ttl_seconds."""
    alias = "freshalias"
    probe = await live_client.get(f"/{alias}", follow_redirects=False)
    assert probe.status_code == 404

    headers = await register_and_login(live_client)
    create_response = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "custom_alias": alias},
        headers=headers,
    )
    assert create_response.status_code == 201

    response = await live_client.get(f"/{alias}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/"


async def test_repopulate_does_not_cache_stale_value_after_concurrent_update(
    live_client: AsyncClient,
) -> None:
    """Regression for the miss-then-repopulate race: a Postgres read that straddles a
    concurrent update must not overwrite that update's invalidation with the stale
    value it read."""
    link, headers = await _create_link(live_client)
    code = link["short_code"]
    original_get_by_code = links_service.get_by_code

    async def _read_then_concurrent_update(session: object, code_arg: str) -> object:
        result = await original_get_by_code(session, code_arg)  # type: ignore[arg-type]
        patch_response = await live_client.patch(
            f"/api/v1/links/{link['id']}",
            json={"target_url": "https://example.org/"},
            headers=headers,
        )
        assert patch_response.status_code == 200
        return result

    with patch(
        "app.services.redirect.links_service.get_by_code",
        new=AsyncMock(side_effect=_read_then_concurrent_update),
    ):
        await live_client.get(f"/{code}", follow_redirects=False)

    response = await live_client.get(f"/{code}", follow_redirects=False)
    assert response.headers["location"] == "https://example.org/"


async def test_repopulate_does_not_cache_negative_after_concurrent_create(
    live_client: AsyncClient,
) -> None:
    """Same race as above, negative-sentinel variant: a probe's Postgres read
    straddling a concurrent create must not cache a 404 for the new alias."""
    alias = "raceonlyalias"
    headers = await register_and_login(live_client)
    original_get_by_code = links_service.get_by_code

    async def _read_then_concurrent_create(session: object, code_arg: str) -> object:
        result = await original_get_by_code(session, code_arg)  # type: ignore[arg-type]
        create_response = await live_client.post(
            "/api/v1/links",
            json={"target_url": "https://example.com/", "custom_alias": alias},
            headers=headers,
        )
        assert create_response.status_code == 201
        return result

    with patch(
        "app.services.redirect.links_service.get_by_code",
        new=AsyncMock(side_effect=_read_then_concurrent_create),
    ):
        probe = await live_client.get(f"/{alias}", follow_redirects=False)
    assert probe.status_code == 404

    response = await live_client.get(f"/{alias}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/"


async def test_redirects_keep_working_with_the_cache_raising(live_client: AsyncClient) -> None:
    link, _ = await _create_link(live_client)
    code = link["short_code"]

    with patch(
        "app.cache.link_cache.get",
        new=AsyncMock(side_effect=LinkCacheUnavailable("simulated")),
    ):
        response = await live_client.get(f"/{code}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/"


async def test_mutation_returns_503_when_cache_invalidation_fails(
    live_client: AsyncClient,
) -> None:
    link, headers = await _create_link(live_client)

    with patch(
        "app.cache.link_cache.invalidate",
        new=AsyncMock(side_effect=LinkCacheUnavailable("simulated")),
    ):
        response = await live_client.patch(
            f"/api/v1/links/{link['id']}",
            json={"target_url": "https://example.org/"},
            headers=headers,
        )

    assert response.status_code == 503


async def test_create_succeeds_when_cache_invalidation_fails(live_client: AsyncClient) -> None:
    """Unlike update/delete, create must not 503 on a post-commit cache failure: the
    caller would reasonably retry the identical POST, and a retry with the same
    custom_alias would collide with the row this call already created."""
    headers = await register_and_login(live_client)

    with patch(
        "app.services.links.link_cache.invalidate",
        new=AsyncMock(side_effect=LinkCacheUnavailable("simulated")),
    ):
        response = await live_client.post(
            "/api/v1/links",
            json={"target_url": "https://example.com/", "custom_alias": "survivesoutage"},
            headers=headers,
        )

    assert response.status_code == 201
    assert response.json()["short_code"] == "survivesoutage"

    # The row exists — retrying the same request must see it as a real conflict.
    retry = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "custom_alias": "survivesoutage"},
        headers=headers,
    )
    assert retry.status_code == 409


async def test_metrics_endpoint_exposes_cache_lookup_counter(live_client: AsyncClient) -> None:
    await live_client.get("/no-such-code-for-metrics", follow_redirects=False)

    response = await live_client.get("/metrics")

    assert response.status_code == 200
    assert "link_cache_lookups_total" in response.text
