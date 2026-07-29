"""POST/GET/PATCH/DELETE against real Postgres and Redis."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import links as links_service

pytestmark = pytest.mark.integration


async def test_create_then_fetch_then_update_then_soft_delete(live_client: AsyncClient) -> None:
    create_response = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/", "title": "demo"}
    )
    assert create_response.status_code == 201
    body = create_response.json()
    link_id = body["id"]
    assert body["target_url"] == "https://example.com/"
    assert body["title"] == "demo"
    assert body["is_active"] is True
    assert body["short_url"].endswith(f"/{body['short_code']}")

    get_response = await live_client.get(f"/api/v1/links/{link_id}")
    assert get_response.status_code == 200
    assert get_response.json()["short_code"] == body["short_code"]

    patch_response = await live_client.patch(f"/api/v1/links/{link_id}", json={"title": "renamed"})
    assert patch_response.status_code == 200
    patched = patch_response.json()
    assert patched["title"] == "renamed"
    # Untouched fields survive a partial update.
    assert patched["target_url"] == "https://example.com/"

    delete_response = await live_client.delete(f"/api/v1/links/{link_id}")
    assert delete_response.status_code == 204

    after_delete = await live_client.get(f"/api/v1/links/{link_id}")
    assert after_delete.status_code == 200
    assert after_delete.json()["is_active"] is False


async def test_create_with_custom_alias(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/", "custom_alias": "my-alias"}
    )
    assert response.status_code == 201
    assert response.json()["short_code"] == "my-alias"


async def test_duplicate_custom_alias_is_conflict(live_client: AsyncClient) -> None:
    first = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/", "custom_alias": "taken-alias"}
    )
    assert first.status_code == 201

    second = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.org/", "custom_alias": "taken-alias"}
    )
    assert second.status_code == 409


async def test_reserved_word_as_custom_alias_is_rejected(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/", "custom_alias": "healthz"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "target_url",
    [
        "javascript:alert(1)",
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "ftp://example.com/",
    ],
)
async def test_unsafe_target_url_is_rejected(live_client: AsyncClient, target_url: str) -> None:
    response = await live_client.post("/api/v1/links", json={"target_url": target_url})
    assert response.status_code == 422


async def test_get_unknown_link_is_404(live_client: AsyncClient) -> None:
    response = await live_client.get("/api/v1/links/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404


async def test_list_links_returns_created_links(live_client: AsyncClient) -> None:
    await live_client.post("/api/v1/links", json={"target_url": "https://example.com/"})

    response = await live_client.get("/api/v1/links")
    assert response.status_code == 200
    assert len(response.json()["items"]) >= 1


async def test_generated_code_never_lands_on_a_reserved_word(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A generated code colliding with a reserved word would insert successfully
    — short_code is only unique, not reserved-checked — and then be permanently
    unreachable, since the real route always wins over the catch-all."""
    # "healthz" must be skipped; the generation loop then needs MAX_GENERATION_ATTEMPTS
    # more (non-reserved) candidates before it starts trying inserts.
    forced_codes = iter(["healthz", "abc1234", "abc1235", "abc1236", "abc1237", "abc1238"])
    monkeypatch.setattr(links_service.short_code, "generate", lambda: next(forced_codes))

    data = links_service.LinkCreate(target_url="https://example.com/")
    link = await links_service.create_link(db_session, data, public_host="short.example.com")

    assert link.short_code == "abc1234"


async def test_unrelated_integrity_error_is_not_treated_as_a_collision(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_link's retry loop must only reinterpret the short-code unique
    violation as a collision. A future constraint (or any other IntegrityError)
    has to surface, not be silently retried into a misleading 503."""

    class _RawAsyncpgError(Exception):
        constraint_name = "some_other_constraint"

    async def _raise_integrity_error(*args: object, **kwargs: object) -> None:
        wrapped = Exception("wrapped dbapi error")
        wrapped.__cause__ = _RawAsyncpgError()
        raise IntegrityError("insert", {}, wrapped)

    monkeypatch.setattr(db_session, "commit", _raise_integrity_error)

    data = links_service.LinkCreate(target_url="https://example.com/")
    with pytest.raises(IntegrityError):
        await links_service.create_link(db_session, data, public_host="short.example.com")


@pytest.mark.parametrize(
    ("expires_at", "expected_status"),
    [
        ("2026-08-01T00:00:00", 422),
        ("2026-08-01T00:00:00Z", 201),
        ("2026-08-01T00:00:00+02:00", 201),
    ],
)
async def test_create_expires_at_requires_a_timezone(
    live_client: AsyncClient, expires_at: str, expected_status: int
) -> None:
    response = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "expires_at": expires_at},
    )
    assert response.status_code == expected_status


async def test_update_expires_at_requires_a_timezone(live_client: AsyncClient) -> None:
    create_response = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/"}
    )
    link_id = create_response.json()["id"]

    naive = await live_client.patch(
        f"/api/v1/links/{link_id}", json={"expires_at": "2026-08-01T00:00:00"}
    )
    assert naive.status_code == 422

    aware = await live_client.patch(
        f"/api/v1/links/{link_id}", json={"expires_at": "2026-08-01T00:00:00Z"}
    )
    assert aware.status_code == 200
