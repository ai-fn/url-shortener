from __future__ import annotations

import uuid

import jwt
import pytest

from app.config import Settings
from app.core.security import (
    InvalidTokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

_SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    environment="test",
    database_url="postgresql+asyncpg://u:p@localhost:5432/db",
    redis_url="redis://localhost:6379/0",
    secret_key="test-only-insecure-key-000000000000000000",
    ip_hash_key="test-only-insecure-key-111111111111111111",
)


def test_hash_password_roundtrips() -> None:
    hashed = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", hashed)


def test_verify_password_rejects_wrong_password() -> None:
    hashed = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", hashed)


def test_hash_password_is_argon2id() -> None:
    assert hash_password("x").startswith("$argon2id$")


def test_access_token_roundtrips_to_the_same_user_id() -> None:
    user_id = uuid.uuid4()
    token = create_access_token(user_id, settings=_SETTINGS)
    assert decode_access_token(token, settings=_SETTINGS) == user_id


def test_expired_token_is_rejected() -> None:
    token = jwt.encode(
        {"sub": str(uuid.uuid4()), "exp": 0, "iat": 0},
        _SETTINGS.secret_key.get_secret_value(),
        algorithm="HS256",
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=_SETTINGS)


def test_tampered_signature_is_rejected() -> None:
    token = create_access_token(uuid.uuid4(), settings=_SETTINGS)
    # Not the last character of a segment: base64url padding bits there can flip
    # without changing the decoded byte, making the test flaky.
    index = len(token) // 2
    replacement = "A" if token[index] != "A" else "B"
    tampered = token[:index] + replacement + token[index + 1 :]
    with pytest.raises(InvalidTokenError):
        decode_access_token(tampered, settings=_SETTINGS)


def test_alg_none_token_is_rejected() -> None:
    # Classic algorithm-confusion attack: a token with no signature at all.
    token = jwt.encode({"sub": str(uuid.uuid4())}, "", algorithm="none")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=_SETTINGS)


def test_missing_sub_claim_is_rejected() -> None:
    token = jwt.encode({"iat": 0}, _SETTINGS.secret_key.get_secret_value(), algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=_SETTINGS)


def test_non_uuid_sub_claim_is_rejected() -> None:
    token = jwt.encode(
        {"sub": "not-a-uuid"}, _SETTINGS.secret_key.get_secret_value(), algorithm="HS256"
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=_SETTINGS)


def test_token_with_no_exp_claim_is_rejected() -> None:
    """PyJWT only validates exp when the claim is present — a signed token that
    omits it would otherwise never expire."""
    token = jwt.encode(
        {"sub": str(uuid.uuid4())}, _SETTINGS.secret_key.get_secret_value(), algorithm="HS256"
    )
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, settings=_SETTINGS)
