"""Handshake: Origin allowlist, JWT in the first message, deadlines, protocol violations."""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.security import create_access_token
from tests.integration.ws.conftest import db_rows


def _closed_with(ws: Any, code: int, error: str | None = None) -> None:
    if error is not None:
        message = ws.receive_json()
        assert message["type"] == "error" and message["code"] == error, message
        assert message["fatal"] is True
        assert set(message) == {"type", "code", "message", "fatal"}  # generic, nothing internal
    with pytest.raises(WebSocketDisconnect) as exc_info:
        ws.receive_json()
    assert exc_info.value.code == code


def test_valid_token_starts_a_session(harness_factory: Any) -> None:
    with harness_factory() as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token, speech_mode="mixed", script_preference="roman")
            assert started["speech_mode"] == "mixed"
            assert started["script_preference"] == "roman"
            assert started["model_name"] == "timeline-fake"
            assert started["audio"] == {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1}
            uuid.UUID(started["transcription_id"])
            ws.send_json({"type": "stop"})
            saved = h.receive_until(ws)[-1]
            assert saved["status"] == "completed"


def test_invalid_token(harness_factory: Any) -> None:
    with harness_factory() as h, h.connect() as ws:
        ws.send_json({"type": "start", "token": "not.a.jwt"})
        _closed_with(ws, 4401, "invalid_token")


def test_token_signed_with_another_secret(harness_factory: Any) -> None:
    with harness_factory() as h:
        h.token()
        now = datetime.now(UTC)
        forged = jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "type": "access",
                "iss": h.settings.jwt_issuer,
                "aud": h.settings.jwt_audience,
                "iat": now,
                "nbf": now,
                "exp": now + timedelta(minutes=5),
                "jti": "x",
            },
            "a-completely-different-secret-value-123456789",
            algorithm="HS256",
        )
        with h.connect() as ws:
            ws.send_json({"type": "start", "token": forged})
            _closed_with(ws, 4401, "invalid_token")


def test_expired_token(harness_factory: Any) -> None:
    with harness_factory() as h:
        h.token()
        (user,) = db_rows(h.db_url, "SELECT id FROM users")
        expired, _ = create_access_token(
            user["id"], h.settings, now=datetime.now(UTC) - timedelta(days=1)
        )
        with h.connect() as ws:
            ws.send_json({"type": "start", "token": expired})
            _closed_with(ws, 4401, "token_expired")


@pytest.mark.parametrize(
    "message",
    [{"type": "start"}, {"type": "start", "token": ""}, {"type": "stop"}],
    ids=["no-token", "empty-token", "stop-before-start"],
)
def test_missing_token(harness_factory: Any, message: dict[str, Any]) -> None:
    with harness_factory() as h, h.connect() as ws:
        ws.send_json(message)
        expected = "auth_required" if message["type"] == "stop" else "invalid_message"
        _closed_with(ws, 4401 if expected == "auth_required" else 4400, expected)


def test_audio_before_authentication_is_rejected(harness_factory: Any) -> None:
    with harness_factory() as h, h.connect() as ws:
        ws.send_bytes(b"\x00\x00" * 480)
        _closed_with(ws, 4401, "auth_required")
    assert db_rows(h.db_url, "SELECT id FROM transcriptions") == []


def test_authentication_deadline(harness_factory: Any) -> None:
    with harness_factory(ws_auth_timeout_seconds=0.2) as h, h.connect() as ws:
        started = time.monotonic()
        _closed_with(ws, 4408, "auth_timeout")
        assert time.monotonic() - started < 2


@pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost:5174", "null"])
def test_origin_rejected(harness_factory: Any, origin: str) -> None:
    with harness_factory() as h:
        with pytest.raises(WebSocketDisconnect) as exc_info, h.connect(origin=origin) as ws:
            ws.receive_json()
        assert exc_info.value.code == 1008


def test_missing_origin_rejected_by_default_and_configurable(harness_factory: Any) -> None:
    with harness_factory() as h:
        with pytest.raises(WebSocketDisconnect) as exc_info, h.connect(origin=None) as ws:
            ws.receive_json()
        assert exc_info.value.code == 1008
    with harness_factory(ws_allow_missing_origin=True) as h:
        token = h.token()
        with h.connect(origin=None) as ws:
            h.start(ws, token)
            ws.send_json({"type": "stop"})
            assert h.receive_until(ws)[-1]["status"] == "completed"


@pytest.mark.parametrize(
    "fields",
    [
        {"sample_rate": 44100},
        {"channels": 2},
        {"format": "opus"},
        {"speech_mode": "french"},
        {"unexpected": True},
        {"title": "x" * 201},
    ],
)
def test_invalid_start_message(harness_factory: Any, fields: dict[str, Any]) -> None:
    with harness_factory() as h:
        token = h.token()
        with h.connect() as ws:
            ws.send_json({"type": "start", "token": token, **fields})
            _closed_with(ws, 4400, "invalid_message")


_NO_TITLE = object()


@pytest.mark.parametrize(
    ("title", "stored"),
    [
        (_NO_TITLE, None),
        (None, None),
        ("", None),
        ("   ", None),
        ("  Daily standup  ", "Daily standup"),
        ("t" * 200, "t" * 200),
    ],
    ids=["missing", "null", "empty", "whitespace", "padded", "max-length"],
)
def test_start_optional_title(harness_factory: Any, title: object, stored: str | None) -> None:
    """Regression: null / "" / whitespace used to raise TypeError and drop the socket."""
    with harness_factory() as h:
        token = h.token()
        fields = {} if title is _NO_TITLE else {"title": title}
        with h.connect() as ws:
            started = h.start(ws, token, **fields)
            ws.send_json({"type": "stop"})
            assert h.receive_until(ws)[-1]["status"] == "completed"
        row = h.transcription(started["transcription_id"])
        if stored is None:
            assert row["title"].startswith("Recording - ")  # server default title
        else:
            assert row["title"] == stored


@pytest.mark.parametrize("title", ["t" * 201, 42, ["x"]], ids=["too-long", "number", "list"])
def test_start_invalid_title_closes_4400(harness_factory: Any, title: object) -> None:
    with harness_factory() as h:
        token = h.token()
        with h.connect() as ws:
            ws.send_json({"type": "start", "token": token, "title": title})
            _closed_with(ws, 4400, "invalid_message")
        assert db_rows(h.db_url, "SELECT id FROM transcriptions") == []  # nothing created


def test_non_json_first_message(harness_factory: Any) -> None:
    with harness_factory() as h, h.connect() as ws:
        ws.send_text("hello")
        _closed_with(ws, 4400, "invalid_message")


def test_token_never_logged(
    harness_factory: Any, app_logs: Callable[[], pytest.LogCaptureFixture]
) -> None:
    with harness_factory() as h:
        caplog = app_logs()
        token = h.token()
        with h.connect() as ws:
            h.start(ws, token)
            ws.send_json({"type": "stop"})
            h.receive_until(ws)
        with h.connect() as ws:
            ws.send_json({"type": "start", "token": token + "x"})
            _closed_with(ws, 4401, "invalid_token")
    assert "POST /api/v1/auth/login -> 200" in caplog.text  # capture really works
    assert token not in caplog.text
