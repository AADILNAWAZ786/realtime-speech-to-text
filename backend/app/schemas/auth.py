from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field

from app.models.enums import DISPLAY_NAME_MAX_LENGTH
from app.schemas.common import NormalizedEmail, RequestModel, strip_optional

PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128  # bounds the cost of hashing attacker-supplied input

# Optional: missing, null, "" and whitespace all mean "no name". The length limit sits on
# the str member so None is never length-checked (see schemas/common.py).
DisplayNameStr = Annotated[
    Annotated[str, Field(max_length=DISPLAY_NAME_MAX_LENGTH)] | None,
    AfterValidator(strip_optional),
]


def _password_not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class RegisterRequest(RequestModel):
    email: NormalizedEmail
    password: Annotated[
        str,
        Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
        AfterValidator(_password_not_blank),
    ]
    display_name: DisplayNameStr = None


class LoginRequest(RequestModel):
    email: NormalizedEmail
    password: Annotated[str, Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)]


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - OAuth2 token type
    expires_in: int = Field(description="Token lifetime in seconds")
