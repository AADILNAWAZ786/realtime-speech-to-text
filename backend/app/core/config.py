"""Application settings, loaded from environment variables (and an optional `.env` file).

Nothing secret has a default value: the app refuses to start if `DATABASE_URL` or
`JWT_SECRET_KEY` is missing, and rejects weak or placeholder JWT secrets.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_MIN_SECRET_LENGTH = 32
_PLACEHOLDER_SECRETS = frozenset(
    {"changeme", "change-me", "secret", "your-secret-key", "replace-me", "jwt-secret"}
)


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Never echo rejected values (secrets, DB passwords) into error messages/logs.
        hide_input_in_errors=True,
    )

    # --- Application ---
    app_name: str = "Real-Time Speech to Text API"
    app_version: str = "0.1.0"
    app_env: Environment = Environment.DEVELOPMENT
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = False
    docs_enabled: bool = True
    api_v1_prefix: str = "/api/v1"

    # --- Database ---
    database_url: SecretStr
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_echo: bool = False

    # --- JWT ---
    jwt_secret_key: SecretStr
    jwt_algorithm: Literal["HS256", "HS384", "HS512"] = "HS256"
    access_token_expire_minutes: int = Field(default=60, ge=1, le=24 * 60)
    jwt_issuer: str = "stt-backend"
    jwt_audience: str = "stt-frontend"

    # --- HTTP ---
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Login rate limiting ---
    login_rate_limit_enabled: bool = True
    login_rate_limit_attempts: int = Field(default=5, ge=1)
    login_rate_limit_window_seconds: int = Field(default=60, ge=1)

    # --- Live transcription WebSocket (/ws/transcribe) ---
    ws_auth_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    ws_idle_timeout_seconds: float = Field(
        default=30.0, gt=0, description="Close a recording that sends nothing for this long"
    )
    ws_max_frame_bytes: int = Field(default=64 * 1024, ge=2, le=1024 * 1024)
    ws_max_text_bytes: int = Field(default=4096, ge=256, le=65536)
    ws_max_recording_seconds: int = Field(default=30 * 60, ge=10)
    ws_max_active_sessions: int = Field(default=3, ge=1, le=1000)
    ws_audio_rate_factor: float = Field(
        default=1.5, gt=1.0, description="Max audio speed relative to real time"
    )
    ws_audio_burst_seconds: float = Field(
        default=5.0, gt=0, description="Audio a client may send ahead of real time"
    )
    ws_allow_missing_origin: bool = Field(
        default=False, description="Accept clients without an Origin header (non-browser)"
    )
    ws_shutdown_timeout_seconds: float = Field(default=30.0, gt=0)

    # --- Inference runtime (engine settings come from WHISPER_* variables) ---
    inference_concurrency: int = Field(
        default=1, ge=1, le=64, description="Concurrent Whisper jobs (shared by all sessions)"
    )
    inference_allow_cpu_oversubscription: bool = False
    speech_preload_model: bool = Field(
        default=False, description="Load the Whisper model at startup instead of first use"
    )

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr) -> SecretStr:
        url = value.get_secret_value()
        if not url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the async driver: postgresql+asyncpg://...")
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def _validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if len(secret) < _MIN_SECRET_LENGTH:
            raise ValueError(f"JWT_SECRET_KEY must be at least {_MIN_SECRET_LENGTH} characters")
        if secret.lower() in _PLACEHOLDER_SECRETS or len(set(secret)) < 8:
            raise ValueError("JWT_SECRET_KEY looks like a placeholder; generate a random secret")
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    """Settings singleton for runtime use. Tests construct `Settings(...)` directly instead."""
    return Settings()  # required values come from the environment
