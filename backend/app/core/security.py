"""Password hashing (Argon2id) and JWT access tokens.

Argon2 hashing is deliberately CPU-expensive; callers in async code should run these
functions in a thread pool (see AuthService) so the event loop is not blocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from pydantic import BaseModel

from app.core.config import Settings
from app.core.exceptions import AuthenticationError

# argon2-cffi defaults follow RFC 9106's recommended (low-memory) Argon2id profile.
_password_hasher = PasswordHasher()

TOKEN_TYPE_ACCESS = "access"  # noqa: S105 - claim label, not a secret


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True when the stored hash uses outdated parameters (upgrade it on next login)."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _password_hasher.hash("dummy-password-for-timing-equalisation")


def burn_password_check(password: str) -> None:
    """Spend the same time as a real verification (used when the email is unknown),
    so response timing does not reveal which emails are registered."""
    verify_password(_dummy_hash(), password)


class AccessTokenPayload(BaseModel):
    sub: uuid.UUID
    exp: datetime
    iat: datetime
    jti: str


def create_access_token(
    user_id: uuid.UUID, settings: Settings, *, now: datetime | None = None
) -> tuple[str, int]:
    """Return (encoded token, lifetime in seconds)."""
    issued_at = now or datetime.now(UTC)
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "type": TOKEN_TYPE_ACCESS,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "iat": issued_at,
        "nbf": issued_at,
        "exp": issued_at + lifetime,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(
        claims, settings.jwt_secret_key.get_secret_value(), algorithm=settings.jwt_algorithm
    )
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str, settings: Settings) -> AccessTokenPayload:
    """Validate signature, algorithm, expiry, issuer, audience and token type."""
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],  # pinned: never trust the token's header
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            leeway=timedelta(seconds=10),
            options={"require": ["exp", "iat", "nbf", "sub", "iss", "aud", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthenticationError("Token has expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise AuthenticationError("Invalid token", code="invalid_token") from exc

    if claims.get("type") != TOKEN_TYPE_ACCESS:
        raise AuthenticationError("Invalid token", code="invalid_token")
    try:
        return AccessTokenPayload.model_validate(claims)
    except ValueError as exc:  # e.g. `sub` is not a UUID
        raise AuthenticationError("Invalid token", code="invalid_token") from exc
