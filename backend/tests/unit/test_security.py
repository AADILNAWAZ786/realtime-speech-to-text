from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from argon2 import PasswordHasher

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)


@pytest.fixture
def jwt_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        jwt_secret_key=secrets.token_urlsafe(48),
    )


def _claims(settings: Settings, **overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "type": "access",
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(minutes=5),
        "jti": "x",
    }
    claims.update(overrides)
    return claims


def _encode(settings: Settings, claims: dict[str, object], **kwargs: object) -> str:
    key = kwargs.pop("key", settings.jwt_secret_key.get_secret_value())
    algorithm = kwargs.pop("algorithm", "HS256")
    return jwt.encode(claims, key, algorithm=algorithm)  # type: ignore[arg-type]


# ---------- Passwords ----------


def test_hash_is_argon2id_and_verifies() -> None:
    hashed = hash_password("s3cret-pass")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "s3cret-pass")
    assert not verify_password(hashed, "wrong-pass")


def test_hashes_are_salted() -> None:
    assert hash_password("same-password") != hash_password("same-password")


def test_verify_rejects_garbage_hash() -> None:
    assert not verify_password("not-a-hash", "whatever")


def test_rehash_detection() -> None:
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash("pw-123456")
    assert password_needs_rehash(weak)
    assert not password_needs_rehash(hash_password("pw-123456"))


# ---------- JWT ----------


def test_token_round_trip(jwt_settings: Settings) -> None:
    user_id = uuid.uuid4()
    token, expires_in = create_access_token(user_id, jwt_settings)
    assert expires_in == jwt_settings.access_token_expire_minutes * 60
    assert decode_access_token(token, jwt_settings).sub == user_id


def test_expired_token(jwt_settings: Settings) -> None:
    past = datetime.now(UTC) - timedelta(days=1)
    token, _ = create_access_token(uuid.uuid4(), jwt_settings, now=past)
    with pytest.raises(AuthenticationError) as exc_info:
        decode_access_token(token, jwt_settings)
    assert exc_info.value.code == "token_expired"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"iss": "someone-else"},
        {"type": "refresh"},
        {"sub": "not-a-uuid"},
    ],
)
def test_invalid_claims_rejected(jwt_settings: Settings, overrides: dict[str, object]) -> None:
    token = _encode(jwt_settings, _claims(jwt_settings, **overrides))
    with pytest.raises(AuthenticationError) as exc_info:
        decode_access_token(token, jwt_settings)
    assert exc_info.value.code == "invalid_token"


def test_missing_required_claim_rejected(jwt_settings: Settings) -> None:
    claims = _claims(jwt_settings)
    del claims["jti"]
    with pytest.raises(AuthenticationError):
        decode_access_token(_encode(jwt_settings, claims), jwt_settings)


def test_wrong_signing_key_rejected(jwt_settings: Settings) -> None:
    token = _encode(jwt_settings, _claims(jwt_settings), key=secrets.token_urlsafe(48))
    with pytest.raises(AuthenticationError):
        decode_access_token(token, jwt_settings)


def test_algorithm_is_pinned(jwt_settings: Settings) -> None:
    """A token signed with a different algorithm (even with the right key) is rejected."""
    token = _encode(jwt_settings, _claims(jwt_settings), algorithm="HS512")
    with pytest.raises(AuthenticationError):
        decode_access_token(token, jwt_settings)


def test_unsigned_token_rejected(jwt_settings: Settings) -> None:
    token = jwt.encode(_claims(jwt_settings), key=None, algorithm="none")  # type: ignore[arg-type]
    with pytest.raises(AuthenticationError):
        decode_access_token(token, jwt_settings)


def test_tampered_token_rejected(jwt_settings: Settings) -> None:
    token, _ = create_access_token(uuid.uuid4(), jwt_settings)
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.{signature[::-1]}"
    with pytest.raises(AuthenticationError):
        decode_access_token(tampered, jwt_settings)
