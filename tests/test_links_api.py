"""POST/GET/PATCH/DELETE against real Postgres and Redis."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import links as links_service
from tests.conftest import insert_user, register_and_login

pytestmark = pytest.mark.integration


async def test_create_link_without_auth_is_401(live_client: AsyncClient) -> None:
    """M2 advertised open, anonymous link creation; M3 closes that door — every
    mutation now requires a caller identity to own the link."""
    response = await live_client.post("/api/v1/links", json={"target_url": "https://example.com/"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_create_then_fetch_then_update_then_soft_delete(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)

    create_response = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "title": "demo"},
        headers=headers,
    )
    assert create_response.status_code == 201
    body = create_response.json()
    link_id = body["id"]
    assert body["target_url"] == "https://example.com/"
    assert body["title"] == "demo"
    assert body["is_active"] is True
    assert body["short_url"].endswith(f"/{body['short_code']}")

    get_response = await live_client.get(f"/api/v1/links/{link_id}", headers=headers)
    assert get_response.status_code == 200
    assert get_response.json()["short_code"] == body["short_code"]

    patch_response = await live_client.patch(
        f"/api/v1/links/{link_id}", json={"title": "renamed"}, headers=headers
    )
    assert patch_response.status_code == 200
    patched = patch_response.json()
    assert patched["title"] == "renamed"
    # Untouched fields survive a partial update.
    assert patched["target_url"] == "https://example.com/"

    delete_response = await live_client.delete(f"/api/v1/links/{link_id}", headers=headers)
    assert delete_response.status_code == 204

    after_delete = await live_client.get(f"/api/v1/links/{link_id}", headers=headers)
    assert after_delete.status_code == 200
    assert after_delete.json()["is_active"] is False


async def test_create_with_custom_alias(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)
    response = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "custom_alias": "my-alias"},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["short_code"] == "my-alias"


async def test_duplicate_custom_alias_is_conflict(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)
    first = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "custom_alias": "taken-alias"},
        headers=headers,
    )
    assert first.status_code == 201

    second = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.org/", "custom_alias": "taken-alias"},
        headers=headers,
    )
    assert second.status_code == 409


async def test_reserved_word_as_custom_alias_is_rejected(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)
    response = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "custom_alias": "healthz"},
        headers=headers,
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
    headers = await register_and_login(live_client)
    response = await live_client.post(
        "/api/v1/links", json={"target_url": target_url}, headers=headers
    )
    assert response.status_code == 422


async def test_get_unknown_link_is_404(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)
    response = await live_client.get(
        "/api/v1/links/00000000-0000-0000-0000-000000000000", headers=headers
    )
    assert response.status_code == 404


async def test_list_links_returns_created_links(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)
    await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/"}, headers=headers
    )

    response = await live_client.get("/api/v1/links", headers=headers)
    assert response.status_code == 200
    assert len(response.json()["items"]) >= 1


async def test_list_links_is_scoped_to_the_caller(live_client: AsyncClient) -> None:
    owner_headers = await register_and_login(live_client, email="owner-a@example.com")
    other_headers = await register_and_login(live_client, email="owner-b@example.com")

    await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/"}, headers=owner_headers
    )

    response = await live_client.get("/api/v1/links", headers=other_headers)
    assert response.status_code == 200
    assert response.json()["items"] == []


async def test_list_links_orders_by_id_as_a_tiebreak(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the compiled SQL, not row order: with no concurrent writes,
    Postgres already returns stable order regardless of the tiebreak."""
    owner_id = await insert_user(db_session)

    captured: list[object] = []
    original_execute = db_session.execute

    async def _capturing_execute(statement: object, *args: object, **kwargs: object) -> object:
        captured.append(statement)
        return await original_execute(statement, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(db_session, "execute", _capturing_execute)

    await links_service.list_links(db_session, owner_id=owner_id)

    compiled = str(captured[0].compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]
    order_by_clause = compiled.split("ORDER BY", 1)[1]
    assert "links.id" in order_by_clause


@pytest.mark.parametrize("method", ["get", "patch", "delete"])
async def test_cross_owner_access_is_404_not_403(live_client: AsyncClient, method: str) -> None:
    owner_headers = await register_and_login(live_client, email="owner-a@example.com")
    other_headers = await register_and_login(live_client, email="owner-b@example.com")

    create_response = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/"}, headers=owner_headers
    )
    link_id = create_response.json()["id"]

    kwargs = {"json": {"title": "hijacked"}} if method == "patch" else {}
    response = await getattr(live_client, method)(
        f"/api/v1/links/{link_id}", headers=other_headers, **kwargs
    )
    assert response.status_code == 404


async def test_generated_code_never_lands_on_a_reserved_word(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """short_code is unique but not reserved-checked, so a colliding code would
    insert fine and then be permanently unreachable behind the real route."""
    # "healthz" must be skipped; the generation loop then needs MAX_GENERATION_ATTEMPTS
    # more (non-reserved) candidates before it starts trying inserts.
    forced_codes = iter(["healthz", "abc1234", "abc1235", "abc1236", "abc1237", "abc1238"])
    monkeypatch.setattr(links_service.short_code, "generate", lambda: next(forced_codes))

    owner_id = await insert_user(db_session)
    data = links_service.LinkCreate(target_url="https://example.com/")
    link = await links_service.create_link(
        db_session, data, owner_id=owner_id, public_host="short.example.com"
    )

    assert link.short_code == "abc1234"


async def test_unrelated_integrity_error_is_not_treated_as_a_collision(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry loop must reinterpret only the short-code unique violation as a
    collision — any other IntegrityError has to surface, not become a misleading 503."""

    class _RawAsyncpgError(Exception):
        constraint_name = "some_other_constraint"

    async def _raise_integrity_error(*args: object, **kwargs: object) -> None:
        wrapped = Exception("wrapped dbapi error")
        wrapped.__cause__ = _RawAsyncpgError()
        raise IntegrityError("insert", {}, wrapped)

    owner_id = await insert_user(db_session)
    monkeypatch.setattr(db_session, "commit", _raise_integrity_error)

    data = links_service.LinkCreate(target_url="https://example.com/")
    with pytest.raises(IntegrityError):
        await links_service.create_link(
            db_session, data, owner_id=owner_id, public_host="short.example.com"
        )


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
    headers = await register_and_login(live_client)
    response = await live_client.post(
        "/api/v1/links",
        json={"target_url": "https://example.com/", "expires_at": expires_at},
        headers=headers,
    )
    assert response.status_code == expected_status


async def test_update_expires_at_requires_a_timezone(live_client: AsyncClient) -> None:
    headers = await register_and_login(live_client)
    create_response = await live_client.post(
        "/api/v1/links", json={"target_url": "https://example.com/"}, headers=headers
    )
    link_id = create_response.json()["id"]

    naive = await live_client.patch(
        f"/api/v1/links/{link_id}", json={"expires_at": "2026-08-01T00:00:00"}, headers=headers
    )
    assert naive.status_code == 422

    aware = await live_client.patch(
        f"/api/v1/links/{link_id}", json={"expires_at": "2026-08-01T00:00:00Z"}, headers=headers
    )
    assert aware.status_code == 200
