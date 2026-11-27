from __future__ import annotations

import secrets
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings

_DB = "postgresql+asyncpg://user:pw@localhost:5432/db"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"database_url": _DB, "jwt_secret_key": secrets.token_urlsafe(48)}
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_valid_settings() -> None:
    settings = _settings(app_env="production")
    assert settings.is_production
    assert settings.app_env is Environment.PRODUCTION
    assert settings.jwt_algorithm == "HS256"


def test_secrets_are_not_exposed_in_repr() -> None:
    settings = _settings()
    assert "pw@" not in repr(settings)
    assert settings.jwt_secret_key.get_secret_value() not in repr(settings)


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None, jwt_secret_key=secrets.token_urlsafe(48))  # type: ignore[call-arg]


def test_jwt_secret_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError, match="jwt_secret_key"):
        Settings(_env_file=None, database_url=_DB)  # type: ignore[call-arg]


@pytest.mark.parametrize("secret", ["short", "a" * 40, "changeme"])
def test_weak_jwt_secret_rejected(secret: str) -> None:
    with pytest.raises(ValidationError):
        _settings(jwt_secret_key=secret)


def test_validation_errors_do_not_echo_secret_values() -> None:
    almost_secret = "Zq8#mK2$vL9@pX4!wR7&nT1*"  # rejected: too short
    with pytest.raises(ValidationError) as exc_info:
        _settings(
            jwt_secret_key=almost_secret,
            database_url="postgresql://user:hunter2@db/x",
        )
    message = str(exc_info.value)
    assert almost_secret not in message
    assert "hunter2" not in message


def test_sync_driver_rejected() -> None:
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(database_url="postgresql://user:pw@localhost/db")


def test_cors_origins_parsed_from_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://a.test, https://b.test/ ,")
    assert _settings().cors_allowed_origins == ["http://a.test", "https://b.test"]
