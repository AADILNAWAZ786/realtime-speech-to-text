"""Edge cases: unhandled errors, password rehashing, registration races, list filters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.user import User
from app.repositories.user_repository import UserRepository
from tests.conftest import DEFAULT_PASSWORD


async def test_unhandled_error_returns_generic_500(app: FastAPI) -> None:
    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password is hunter2")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error", "code": "internal_error"}
    assert "hunter2" not in response.text


async def test_login_upgrades_outdated_password_hash(
    client: AsyncClient,
    register_user: Callable[..., Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await register_user(email="old@example.com")
    weak_hash = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(DEFAULT_PASSWORD)
    async with session_factory() as session:
        await session.execute(update(User).values(password_hash=weak_hash))
        await session.commit()

    response = await client.post(
        "/api/v1/auth/login", json={"email": "old@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200

    async with session_factory() as session:
        stored = (await session.execute(select(User.password_hash))).scalar_one()
    assert stored != weak_hash
    assert stored.startswith("$argon2id$")


async def test_registration_race_returns_409(
    client: AsyncClient, register_user: Callable[..., Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a concurrent request inserts the same email between our check and our insert,
    the unique constraint fires and the API still answers 409 (not 500)."""
    await register_user(email="race@example.com")

    async def not_found(self: UserRepository, email: str) -> None:
        return None

    monkeypatch.setattr(UserRepository, "get_by_email", not_found)
    response = await client.post(
        "/api/v1/auth/register", json={"email": "race@example.com", "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 409
    assert response.json()["code"] == "email_already_registered"


async def test_list_filters_by_status_and_source(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    await client.post("/api/v1/transcriptions", json={"text": "x"}, headers=auth_headers)
    completed = await client.get(
        "/api/v1/transcriptions",
        params={"status": "completed", "source": "manual"},
        headers=auth_headers,
    )
    recording = await client.get(
        "/api/v1/transcriptions", params={"status": "recording"}, headers=auth_headers
    )
    live = await client.get(
        "/api/v1/transcriptions", params={"source": "live"}, headers=auth_headers
    )
    assert completed.json()["total"] == 1
    assert recording.json()["total"] == 0
    assert live.json()["total"] == 0
