"""Shared pytest fixtures.

Tests run against a real PostgreSQL database given by TEST_DATABASE_URL (environment or
`.env`). As a safety guard, the database name must end with "_test" because the suite
drops and recreates its schema.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from alembic import command
from app.core.config import Environment, Settings
from app.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]

# One random secret per test run: never hardcoded, never reused.
_TEST_JWT_SECRET = secrets.token_urlsafe(48)


class _TestEnv(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")
    test_database_url: SecretStr | None = None


def _test_database_url() -> str:
    url = _TestEnv().test_database_url
    if url is None:
        pytest.exit("TEST_DATABASE_URL is not set (see .env.example)", returncode=2)
    raw = url.get_secret_value()
    db_name = make_url(raw).database or ""
    if not db_name.endswith("_test"):
        pytest.exit(
            f"Refusing to run: test database name {db_name!r} must end with '_test'",
            returncode=2,
        )
    return raw


@pytest.fixture(scope="session")
def test_database_url() -> str:
    return _test_database_url()


def alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    # ConfigParser interpolation: escape '%' in passwords.
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    return config


async def _reset_schema(database_url: str) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_database(test_database_url: str) -> str:
    """Once per run, on first use: wipe the test schema and build it with the real Alembic
    migrations. Opt-in (not autouse): only tests that need PostgreSQL request it, directly
    or through `make_settings` / `settings` / `app` / `client`, so unit tests run without
    a database."""
    asyncio.run(_reset_schema(test_database_url))
    command.upgrade(alembic_config(test_database_url), "head")
    return test_database_url


async def truncate_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("TRUNCATE transcription_segments, transcriptions, users RESTART IDENTITY CASCADE")
        )


@pytest.fixture
def make_settings(migrated_database: str) -> Callable[..., Settings]:
    """Settings pointing at the migrated test database. Depends on `migrated_database` so
    any app built from these settings (even outside the `app` fixture) has a schema."""

    def _make(**overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "app_env": Environment.TEST,
            "database_url": migrated_database,
            "jwt_secret_key": _TEST_JWT_SECRET,
            "log_level": "WARNING",
            "cors_allowed_origins": ["http://localhost:5173"],
            "login_rate_limit_enabled": False,
            "db_pool_size": 2,
            "db_max_overflow": 2,
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)

    return _make


@pytest.fixture
def settings(make_settings: Callable[..., Settings]) -> Settings:
    return make_settings()


@pytest.fixture
async def app(migrated_database: str, settings: Settings) -> AsyncIterator[FastAPI]:
    """Application wired to the migrated test database; tables are truncated afterwards."""
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        try:
            yield application
        finally:
            await truncate_all(application.state.engine)


@pytest.fixture
def session_factory(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    """Direct DB access for tests that need to set up or inspect rows."""
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    return factory


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


DEFAULT_PASSWORD = "correct-horse-battery"


@pytest.fixture
def register_user(client: AsyncClient) -> Callable[..., Any]:
    """Register a user through the API and return the response JSON."""

    async def _register(
        email: str = "alice@example.com", password: str = DEFAULT_PASSWORD, **extra: Any
    ) -> dict[str, Any]:
        response = await client.post(
            "/api/v1/auth/register", json={"email": email, "password": password, **extra}
        )
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    return _register


@pytest.fixture
def login(client: AsyncClient) -> Callable[..., Any]:
    """Log in through the API and return ready-to-use Authorization headers."""

    async def _login(email: str, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
        response = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": password}
        )
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    return _login


@pytest.fixture
def auth_headers_for(register_user: Callable[..., Any], login: Callable[..., Any]) -> Any:
    """Register + log in a user; returns Authorization headers."""

    async def _make(email: str) -> dict[str, str]:
        await register_user(email=email)
        headers: dict[str, str] = await login(email)
        return headers

    return _make


@pytest.fixture
async def auth_headers(auth_headers_for: Any) -> dict[str, str]:
    headers: dict[str, str] = await auth_headers_for("alice@example.com")
    return headers
