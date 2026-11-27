from __future__ import annotations

from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr


def _strip_non_blank(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


def strip_optional(value: str | None) -> str | None:
    """None, "" and whitespace become None; anything else is stripped."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


NonBlankStr = Annotated[str, AfterValidator(_strip_non_blank)]

# Optional, stripped text without a length limit.
#
# For a LIMITED optional string, constrain the `str` member, not the `str | None` union:
#
#     Annotated[Annotated[str, Field(max_length=N)] | None, AfterValidator(strip_optional)]
#
# `Annotated[str | None, ..., Field(max_length=N)]` makes Pydantic apply max_length to None
# as well, which raises TypeError (HTTP 500, or a dropped WebSocket) instead of a
# validation error. See DisplayNameStr (schemas/auth.py) and OptionalTitleStr
# (ws/protocol.py).
OptionalStrippedStr = Annotated[str | None, AfterValidator(strip_optional)]
# Whole address lower-cased so lookups and the unique constraint are case-insensitive.
NormalizedEmail = Annotated[EmailStr, AfterValidator(lambda value: value.lower())]


class RequestModel(BaseModel):
    """Base for request bodies: unknown fields are rejected (422), not silently ignored."""

    model_config = ConfigDict(extra="forbid", str_max_length=200_000)


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorResponse(BaseModel):
    detail: str
    code: str
    errors: list[dict[str, Any]] | None = None
