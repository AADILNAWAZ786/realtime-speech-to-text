from __future__ import annotations

import logging
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ConflictError, InvalidCredentialsError
from app.core.rate_limit import LoginRateLimiter
from app.core.security import (
    burn_password_check,
    create_access_token,
    decode_access_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterRequest, TokenResponse

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)

    async def register(self, data: RegisterRequest) -> User:
        if await self._users.get_by_email(data.email) is not None:
            raise ConflictError("Email is already registered", code="email_already_registered")

        password_hash = await run_in_threadpool(hash_password, data.password)
        user = User(email=data.email, password_hash=password_hash, display_name=data.display_name)
        try:
            await self._users.add(user)
            await self._session.commit()
        except IntegrityError as exc:
            # Lost a race with a concurrent registration of the same email.
            await self._session.rollback()
            raise ConflictError(
                "Email is already registered", code="email_already_registered"
            ) from exc
        logger.info("User registered", extra={"user_id": str(user.id)})
        return user

    async def login(
        self, email: str, password: str, *, limiter: LoginRateLimiter, client_key: str
    ) -> TokenResponse:
        limiter.check(client_key)
        try:
            user = await self._authenticate(email, password)
        except InvalidCredentialsError:
            limiter.record_failure(client_key)
            logger.info("Failed login attempt")
            raise
        limiter.reset(client_key)
        token, expires_in = create_access_token(user.id, self._settings)
        return TokenResponse(access_token=token, expires_in=expires_in)

    async def _authenticate(self, email: str, password: str) -> User:
        user = await self._users.get_by_email(email)
        if user is None:
            await run_in_threadpool(burn_password_check, password)
            raise InvalidCredentialsError()

        if not await run_in_threadpool(verify_password, user.password_hash, password):
            raise InvalidCredentialsError()
        if not user.is_active:
            # Same response as a wrong password: do not reveal account state.
            raise InvalidCredentialsError()

        if password_needs_rehash(user.password_hash):
            user.password_hash = await run_in_threadpool(hash_password, password)
            await self._session.commit()
        return user

    async def get_user_from_token(self, token: str) -> User:
        payload = decode_access_token(token, self._settings)
        user = await self._users.get_by_id(payload.sub)
        if user is None or not user.is_active:
            raise AuthenticationError("Invalid token", code="invalid_token")
        return user

    async def get_user(self, user_id: uuid.UUID) -> User | None:
        return await self._users.get_by_id(user_id)
