"""Register/login against real Postgres and Redis."""

from __future__ import annotations

import time
import uuid

import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient

from tests.conftest import TEST_ENV

pytestmark = pytest.mark.integration

_CREDENTIALS = {"email": "user@example.com", "password": "correct horse battery staple"}


async def test_register_then_login(live_client: AsyncClient) -> None:
    register_response = await live_client.post("/api/v1/auth/register", json=_CREDENTIALS)
    assert register_response.status_code == 201
    body = register_response.json()
    assert body["email"] == _CREDENTIALS["email"]
    assert "hashed_password" not in body
    assert "password" not in body

    login_response = await live_client.post("/api/v1/auth/login", json=_CREDENTIALS)
    assert login_response.status_code == 200
    login_body = login_response.json()
    assert login_body["token_type"] == "bearer"  # noqa: S105 — RFC 6750 scheme name
    assert login_body["access_token"]
    assert login_body["expires_in"] == 60 * 60


async def test_register_lowercases_email(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/auth/register",
        json={"email": "MixedCase@Example.com", "password": "correct horse battery staple"},
    )
    assert response.status_code == 201
    assert response.json()["email"] == "mixedcase@example.com"

    login_response = await live_client.post(
        "/api/v1/auth/login",
        json={"email": "MIXEDCASE@EXAMPLE.COM", "password": "correct horse battery staple"},
    )
    assert login_response.status_code == 200


async def test_duplicate_email_registration_is_conflict(live_client: AsyncClient) -> None:
    first = await live_client.post("/api/v1/auth/register", json=_CREDENTIALS)
    assert first.status_code == 201

    second = await live_client.post("/api/v1/auth/register", json=_CREDENTIALS)
    assert second.status_code == 409


async def test_invalid_email_shape_is_rejected(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/auth/register", json={"email": "not-an-email", "password": "whatever12345"}
    )
    assert response.status_code == 422


async def test_login_with_wrong_password_is_401(live_client: AsyncClient) -> None:
    await live_client.post("/api/v1/auth/register", json=_CREDENTIALS)

    response = await live_client.post(
        "/api/v1/auth/login", json={"email": _CREDENTIALS["email"], "password": "wrong password"}
    )
    assert response.status_code == 401


async def test_login_with_unknown_email_is_401(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "whatever12345"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize("password", ["", "short12", "x" * 129])
async def test_register_rejects_out_of_bounds_password(
    live_client: AsyncClient, password: str
) -> None:
    response = await live_client.post(
        "/api/v1/auth/register", json={"email": "bounds@example.com", "password": password}
    )
    assert response.status_code == 422


async def test_login_with_short_password_is_401_not_422(live_client: AsyncClient) -> None:
    """A wrong credential (401), not malformed (422) — login must not carry
    register's min_length, or a later floor increase locks out existing users."""
    response = await live_client.post(
        "/api/v1/auth/login", json={"email": _CREDENTIALS["email"], "password": "short"}
    )
    assert response.status_code == 401


async def test_login_rejects_overlong_password(live_client: AsyncClient) -> None:
    response = await live_client.post(
        "/api/v1/auth/login", json={"email": _CREDENTIALS["email"], "password": "x" * 129}
    )
    assert response.status_code == 422


async def test_login_rate_limit_is_also_keyed_by_email(live_client: AsyncClient) -> None:
    """Per-IP throttling alone collapses to one shared bucket behind any proxy and
    misses credential stuffing spread across many IPs at one account."""
    email = f"{uuid.uuid4()}@example.com"
    await live_client.post("/api/v1/auth/login", json={"email": email, "password": "whatever12345"})

    redis_client = aioredis.from_url(TEST_ENV["REDIS_URL"])
    try:
        keys = [key async for key in redis_client.scan_iter(match=f"rl:login:email:{email}*")]
    finally:
        await redis_client.aclose()

    assert keys, "expected a rate-limit bucket keyed on the submitted email"


async def test_successful_login_ignores_an_exhausted_email_rate_limit_bucket(
    live_client: AsyncClient,
) -> None:
    """A correct-password login must never be blocked by the email bucket, or
    anyone who knows a victim's email could lock them out by draining it first."""
    email = f"{uuid.uuid4()}@example.com"
    credentials = {"email": email, "password": "correct horse battery staple"}
    await live_client.post("/api/v1/auth/register", json=credentials)

    redis_client = aioredis.from_url(TEST_ENV["REDIS_URL"])
    try:
        # Written directly, not via failed logins: that would also drain the shared
        # IP bucket every request in this process uses.
        await redis_client.hset(f"rl:login:email:{email}", mapping={"tokens": 0, "ts": time.time()})

        # Proves the write above actually drained the bucket the limiter reads —
        # otherwise the assertion below would pass whether or not the setup worked.
        drained = await live_client.post(
            "/api/v1/auth/login", json={"email": email, "password": "wrong password here"}
        )
        assert drained.status_code == 429

        response = await live_client.post("/api/v1/auth/login", json=credentials)
    finally:
        await redis_client.aclose()

    assert response.status_code == 200


async def test_login_rate_limit_trips(live_client: AsyncClient) -> None:
    await live_client.post("/api/v1/auth/register", json=_CREDENTIALS)

    statuses = [
        (
            await live_client.post(
                "/api/v1/auth/login",
                json={"email": _CREDENTIALS["email"], "password": "wrong password"},
            )
        ).status_code
        for _ in range(15)
    ]
    assert 429 in statuses


async def test_register_rate_limit_trips(live_client: AsyncClient) -> None:
    # A distinct email per call: registration's own uniqueness constraint would
    # otherwise 409 the second attempt before the rate limit had a chance to.
    statuses = [
        (
            await live_client.post(
                "/api/v1/auth/register",
                json={"email": f"{uuid.uuid4()}@example.com", "password": "whatever12345"},
            )
        ).status_code
        for _ in range(15)
    ]
    assert 429 in statuses
