"""Shared FastAPI dependencies: settings, DB session, services and the current user."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.rate_limit import LoginRateLimiter
from app.db.session import get_db_session
from app.models.user import User
from app.services.auth_service import AuthService
from app.services.transcription_service import TranscriptionService

# auto_error=False so a missing header yields our own 401 error format.
_bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_login_rate_limiter(request: Request) -> LoginRateLimiter:
    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    return limiter


DbSession = Annotated[AsyncSession, Depends(get_db_session)]
AppSettings = Annotated[Settings, Depends(get_settings)]


def get_auth_service(session: DbSession, settings: AppSettings) -> AuthService:
    return AuthService(session, settings)


def get_transcription_service(session: DbSession) -> TranscriptionService:
    return TranscriptionService(session)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
TranscriptionServiceDep = Annotated[TranscriptionService, Depends(get_transcription_service)]
LoginRateLimiterDep = Annotated[LoginRateLimiter, Depends(get_login_rate_limiter)]


async def get_current_user(
    auth_service: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
) -> User:
    """Protects a route: requires `Authorization: Bearer <access token>`."""
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Not authenticated", code="not_authenticated")
    return await auth_service.get_user_from_token(credentials.credentials)


CurrentUser = Annotated[User, Depends(get_current_user)]
