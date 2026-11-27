"""Optional, stripped text fields with a length limit (regression: TypeError on None).

`display_name` (registration) and `title` (WebSocket start) used to raise TypeError for
null / "" / whitespace, because max_length was applied to the whole `str | None` union.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from app.schemas.auth import RegisterRequest
from app.ws.protocol import CloseCode, ErrorCode, ProtocolError, StartMessage, parse_client_message

MISSING = object()

NORMALISED = [
    (MISSING, None),
    (None, None),
    ("", None),
    ("   ", None),
    ("\t\n ", None),
    ("  Asha  ", "Asha"),
    ("आशा", "आशा"),
]
IDS = ["missing", "null", "empty", "spaces", "whitespace", "padded", "devanagari"]


def _register(value: object) -> RegisterRequest:
    fields: dict[str, Any] = {"email": "a@example.com", "password": "correct-horse"}
    if value is not MISSING:
        fields["display_name"] = value
    return RegisterRequest(**fields)


def _start(value: object) -> StartMessage:
    message: dict[str, Any] = {"type": "start", "token": "t"}
    if value is not MISSING:
        message["title"] = value
    parsed = parse_client_message(json.dumps(message))
    assert isinstance(parsed, StartMessage)
    return parsed


@pytest.mark.parametrize(("value", "expected"), NORMALISED, ids=IDS)
def test_display_name_is_optional_and_normalised(value: object, expected: str | None) -> None:
    assert _register(value).display_name == expected


@pytest.mark.parametrize(("value", "expected"), NORMALISED, ids=IDS)
def test_start_title_is_optional_and_normalised(value: object, expected: str | None) -> None:
    assert _start(value).title == expected


def test_display_name_limit_is_a_validation_error() -> None:
    assert _register("x" * 100).display_name == "x" * 100
    with pytest.raises(ValidationError) as exc_info:  # not TypeError
        _register("x" * 101)
    assert exc_info.value.errors()[0]["type"] == "string_too_long"
    assert exc_info.value.errors()[0]["loc"] == ("display_name",)  # maps to the form field


def test_start_title_limit_is_a_protocol_error() -> None:
    assert _start("t" * 200).title == "t" * 200
    with pytest.raises(ProtocolError) as exc_info:
        _start("t" * 201)
    assert exc_info.value.code is ErrorCode.INVALID_MESSAGE
    assert exc_info.value.close_code is CloseCode.BAD_REQUEST  # 4400


@pytest.mark.parametrize("value", [123, ["a"], {"a": 1}, True])
def test_wrong_types_are_validation_errors(value: object) -> None:
    with pytest.raises(ValidationError):
        _register(value)
    with pytest.raises(ProtocolError) as exc_info:
        _start(value)
    assert exc_info.value.close_code is CloseCode.BAD_REQUEST


def test_a_type_error_inside_validation_becomes_a_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: any TypeError while parsing ends as 4400, never an unhandled crash."""
    import app.ws.protocol as protocol

    class Broken:
        def validate_python(self, data: object) -> None:
            raise TypeError("validator bug")

    monkeypatch.setattr(protocol, "_client_message", Broken())
    with pytest.raises(ProtocolError) as exc_info:
        parse_client_message('{"type": "start", "token": "t"}')
    assert exc_info.value.close_code is CloseCode.BAD_REQUEST
