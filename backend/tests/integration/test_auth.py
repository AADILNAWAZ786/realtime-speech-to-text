from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import create_access_token
from app.main import create_app
from app.models.user import User
from tests.conftest import DEFAULT_PASSWORD, truncate_all

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


# ---------- Registration ----------


async def test_register_success(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post(
        REGISTER,
        json={
            "email": "  Alice@Example.COM ",
            "password": DEFAULT_PASSWORD,
            "display_name": " Al ",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "email", "display_name", "created_at"}
    assert body["email"] == "alice@example.com"
    assert body["display_name"] == "Al"
    uuid.UUID(body["id"])

    async with session_factory() as session:
        user = (await session.execute(select(User))).scalar_one()
    assert user.password_hash.startswith("$argon2id$")
    assert DEFAULT_PASSWORD not in user.password_hash


_NO_NAME = object()


@pytest.mark.parametrize(
    ("display_name", "stored"),
    [
        (_NO_NAME, None),
        (None, None),
        ("", None),
        ("   ", None),
        ("  Asha Verma  ", "Asha Verma"),
        ("x" * 100, "x" * 100),
    ],
    ids=["missing", "null", "empty", "whitespace", "padded", "max-length"],
)
async def test_register_optional_display_name(
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    display_name: object,
    stored: str | None,
) -> None:
    """Regression: null / "" / whitespace used to raise TypeError -> HTTP 500."""
    payload: dict[str, Any] = {"email": "name@example.com", "password": DEFAULT_PASSWORD}
    if display_name is not _NO_NAME:
        payload["display_name"] = display_name
    response = await client.post(REGISTER, json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["display_name"] == stored
    async with session_factory() as session:
        user = (await session.execute(select(User))).scalar_one()
    assert user.display_name == stored


async def test_register_too_long_display_name_is_422_not_500(client: AsyncClient) -> None:
    response = await client.post(
        REGISTER,
        json={"email": "a@example.com", "password": DEFAULT_PASSWORD, "display_name": "x" * 101},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["loc"][:2] == ["body", "display_name"]


async def test_register_duplicate_email_case_insensitive(
    client: AsyncClient, register_user: Callable[..., Any]
) -> None:
    await register_user(email="bob@example.com")
    response = await client.post(
        REGISTER, json={"email": "BOB@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": "Email is already registered",
        "code": "email_already_registered",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": DEFAULT_PASSWORD},
        {"email": "a@example.com", "password": "short"},
        {"email": "a@example.com", "password": "        "},
        {"email": "a@example.com", "password": "x" * 129},
        {"email": "a@example.com"},
        {"email": "a@example.com", "password": DEFAULT_PASSWORD, "is_active": False},
        {"email": "a@example.com", "password": DEFAULT_PASSWORD, "display_name": "x" * 101},
    ],
    ids=[
        "bad-email",
        "short-pw",
        "blank-pw",
        "long-pw",
        "missing-pw",
        "unknown-field",
        "long-name",
    ],
)
async def test_register_validation_errors(client: AsyncClient, payload: dict[str, Any]) -> None:
    response = await client.post(REGISTER, json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"]


# ---------- Login ----------


async def test_login_success_and_me(
    client: AsyncClient, register_user: Callable[..., Any], settings: Settings
) -> None:
    user = await register_user(email="carol@example.com")
    response = await client.post(
        LOGIN, json={"email": "Carol@Example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == settings.access_token_expire_minutes * 60

    me = await client.get(ME, headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json() == user


async def test_login_wrong_password(client: AsyncClient, register_user: Callable[..., Any]) -> None:
    await register_user(email="dave@example.com")
    response = await client.post(LOGIN, json={"email": "dave@example.com", "password": "nope-nope"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "detail": "Incorrect email or password",
        "code": "invalid_credentials",
    }


async def test_login_unknown_email_same_response_as_wrong_password(
    client: AsyncClient, register_user: Callable[..., Any]
) -> None:
    await register_user(email="erin@example.com")
    wrong_pw = await client.post(LOGIN, json={"email": "erin@example.com", "password": "bad-pass"})
    unknown = await client.post(LOGIN, json={"email": "ghost@example.com", "password": "bad-pass"})
    assert unknown.status_code == wrong_pw.status_code == 401
    assert unknown.json() == wrong_pw.json()


async def test_login_inactive_user_rejected(
    client: AsyncClient,
    register_user: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await register_user(email="frank@example.com")
    async with session_factory() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()
    response = await client.post(
        LOGIN, json={"email": "frank@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_credentials"


async def test_login_validation_error(client: AsyncClient) -> None:
    response = await client.post(LOGIN, json={"email": "x@example.com"})
    assert response.status_code == 422


async def test_login_rate_limited_after_repeated_failures(
    make_settings: Callable[..., Settings],
) -> None:
    app = create_app(make_settings(login_rate_limit_enabled=True, login_rate_limit_attempts=3))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        try:
            await client.post(
                REGISTER, json={"email": "gina@example.com", "password": DEFAULT_PASSWORD}
            )
            for _ in range(3):
                bad = await client.post(
                    LOGIN, json={"email": "gina@example.com", "password": "wrong-pass"}
                )
                assert bad.status_code == 401

            blocked = await client.post(
                LOGIN, json={"email": "gina@example.com", "password": DEFAULT_PASSWORD}
            )
            assert blocked.status_code == 429
            assert blocked.json()["code"] == "rate_limited"
            assert int(blocked.headers["retry-after"]) > 0

            # The limit is per (client, email): another account is unaffected.
            other = await client.post(
                LOGIN, json={"email": "someone@example.com", "password": "wrong-pass"}
            )
            assert other.status_code == 401
        finally:
            await truncate_all(app.state.engine)


# ---------- Protected routes / token validation ----------


async def test_me_requires_token(client: AsyncClient) -> None:
    response = await client.get(ME)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Not authenticated", "code": "not_authenticated"}


@pytest.mark.parametrize(
    "header",
    ["Bearer not.a.jwt", "Bearer ", "Basic dXNlcjpwYXNz", "Token abc"],
    ids=["garbage", "empty", "basic-scheme", "unknown-scheme"],
)
async def test_me_rejects_bad_authorization_header(client: AsyncClient, header: str) -> None:
    response = await client.get(ME, headers={"Authorization": header})
    assert response.status_code == 401


async def test_me_rejects_expired_token(
    client: AsyncClient, register_user: Callable[..., Any], settings: Settings
) -> None:
    user = await register_user(email="hank@example.com")
    token, _ = create_access_token(
        uuid.UUID(user["id"]), settings, now=datetime.now(UTC) - timedelta(days=2)
    )
    response = await client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["code"] == "token_expired"


async def test_me_rejects_token_signed_with_other_secret(
    client: AsyncClient, register_user: Callable[..., Any], settings: Settings
) -> None:
    user = await register_user(email="ivy@example.com")
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "sub": user["id"],
            "type": "access",
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
            "iat": now,
            "nbf": now,
            "exp": now + timedelta(minutes=5),
            "jti": "forged",
        },
        secrets.token_urlsafe(48),
        algorithm="HS256",
    )
    response = await client.get(ME, headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_token_for_deleted_user_rejected(client: AsyncClient, settings: Settings) -> None:
    token, _ = create_access_token(uuid.uuid4(), settings)  # user does not exist
    response = await client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


async def test_token_for_deactivated_user_rejected(
    client: AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await session.execute(update(User).values(is_active=False))
        await session.commit()
    response = await client.get(ME, headers=auth_headers)
    assert response.status_code == 401
