"""Text byte limits, protocol-level frame limits (real Uvicorn) and auth database failures."""

from __future__ import annotations

import asyncio
import io
import json
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from app.core.config import Settings
from app.main import create_app
from app.repositories.user_repository import UserRepository
from app.ws.connection import text_exceeds_limit
from tests.conftest import DEFAULT_PASSWORD
from tests.integration.ws.conftest import (
    ORIGIN,
    _truncate,
    db_rows,
    live_uvicorn,
    two_sentences,
)
from tests.integration.ws.test_ws_client_script import _load_client
from tests.unit.realtime.fakes import TimelineEngine

LIMIT_LOG = "-byte limit"


# ---------------------------------------------------------------- 1. UTF-8 byte limit


@pytest.mark.parametrize(
    ("text", "limit", "too_large"),
    [
        ("a" * 300, 300, False),
        ("a" * 301, 300, True),
        ("आ" * 100, 300, False),  # 100 characters, 300 bytes
        ("आ" * 101, 300, True),  # 101 characters but 303 bytes
        ("आ" * 100, 299, True),
        ("🙂" * 75, 299, True),  # 4-byte characters
    ],
)
def test_text_limit_counts_utf8_bytes(text: str, limit: int, too_large: bool) -> None:
    assert text_exceeds_limit(text, limit) is too_large


def _start_text(token: str, title: str) -> str:
    return json.dumps({"type": "start", "token": token, "title": title}, ensure_ascii=False)


def test_unicode_start_message_over_byte_limit_is_rejected(
    harness_factory: Any, app_logs: Callable[[], pytest.LogCaptureFixture]
) -> None:
    with harness_factory() as h:
        caplog = app_logs()
        token = h.token()
        text = _start_text(token, "आज की meeting " * 14)  # valid title, mostly 3-byte chars
        chars, size = len(text), len(text.encode("utf-8"))
        assert chars < size
        h.settings.ws_max_text_bytes = chars + (size - chars) // 2  # chars fit, bytes do not

        with h.connect() as ws:
            ws.send_text(text)
            error = ws.receive_json()
            assert (error["code"], error["fatal"]) == ("invalid_message", True)
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4400
        assert f"{size} bytes exceeds the {h.settings.ws_max_text_bytes}{LIMIT_LOG}" in caplog.text
        assert token not in caplog.text
        assert db_rows(h.db_url, "SELECT id FROM transcriptions") == []

        h.settings.ws_max_text_bytes = size  # exactly at the limit: accepted
        with h.connect() as ws:
            started = h.start(ws, token, title="आज की meeting " * 14)
            ws.send_json({"type": "stop"})
            assert h.receive_until(ws)[-1]["status"] == "completed"
        assert (
            h.transcription(started["transcription_id"])["title"] == ("आज की meeting " * 14).strip()
        )


def test_unicode_text_frame_during_recording_is_limited_by_bytes(
    harness_factory: Any, app_logs: Callable[[], pytest.LogCaptureFixture]
) -> None:
    with harness_factory() as h:
        caplog = app_logs()
        token = h.token()
        h.settings.ws_max_text_bytes = 1000
        message = json.dumps({"type": "stop", "note": "ठीक" * 120}, ensure_ascii=False)
        assert len(message) < 1000 < len(message.encode("utf-8"))  # 386 chars, 1106 bytes

        with h.connect() as ws:
            h.start(ws, token)
            caplog.clear()
            ws.send_text(message)
            messages = h.receive_until(ws)
            assert messages[0]["code"] == "invalid_message"
            assert messages[-1]["status"] == "interrupted"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4400
        assert LIMIT_LOG in caplog.text  # rejected by the byte limit, not only by validation


# ---------------------------------------------------------------- 3. auth DB failures


@pytest.mark.parametrize("where", ["user_lookup", "session_open"])
def test_database_failure_during_authentication_is_generic(
    harness_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    app_logs: Callable[[], pytest.LogCaptureFixture],
    where: str,
) -> None:
    secret_detail = "SELECT password_hash FROM users WHERE email='alice@example.com' pw=hunter2"
    with harness_factory() as h:
        caplog = app_logs()
        token = h.token()
        if where == "user_lookup":

            async def broken_lookup(self: UserRepository, user_id: Any) -> Any:
                raise OperationalError(secret_detail, {"pw": "hunter2"}, ConnectionError("down"))

            monkeypatch.setattr(UserRepository, "get_by_id", broken_lookup)
        else:

            def broken_factory() -> Any:
                raise OperationalError(secret_detail, {}, ConnectionError("refused"))

            monkeypatch.setattr(h.app.state, "session_factory", broken_factory)

        with h.connect() as ws:
            ws.send_json({"type": "start", "token": token})
            error = ws.receive_json()
            assert error == {
                "type": "error",
                "code": "internal_error",
                "message": "Internal server error",
                "fatal": True,
            }
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1011
        assert "WebSocket authentication failed: OperationalError" in caplog.text
        assert token not in caplog.text
        assert "hunter2" not in caplog.text and "password_hash" not in caplog.text
        assert h.app.state.speech.registry.active_count == 0  # no slot taken
        monkeypatch.undo()
        assert db_rows(h.db_url, "SELECT id FROM transcriptions") == []


# ---------------------------------------------------------------- 2. protocol-level frame limit


@pytest.fixture
def uvicorn_app(make_settings: Callable[..., Settings], migrated_database: str) -> Any:
    apps: list[Any] = []

    def build(**overrides: Any) -> Any:
        pcm, words = two_sentences()
        settings = make_settings(ws_audio_burst_seconds=600.0, **overrides)
        app = create_app(settings, speech_engine=TimelineEngine(words))
        apps.append(app)
        return app, pcm, words

    yield build
    asyncio.run(_truncate(migrated_database))


def _register(api: str, email: str = "frames@example.com") -> str:
    httpx.post(f"{api}/api/v1/auth/register", json={"email": email, "password": DEFAULT_PASSWORD})
    login = httpx.post(
        f"{api}/api/v1/auth/login", json={"email": email, "password": DEFAULT_PASSWORD}
    )
    token: str = login.json()["access_token"]
    return token


async def _record_with_frames(
    api: str,
    token: str,
    frames: list[bytes],
    *,
    then: list[bytes] | None = None,
    pause: float = 0.0,
) -> tuple[list[dict[str, Any]], int | None, str]:
    """Send `frames`, optionally wait `pause` s (so the server consumes them), then `then`."""
    url = api.replace("http://", "ws://") + "/ws/transcribe"
    received: list[dict[str, Any]] = []
    async with connect(url, origin=Origin(ORIGIN), max_size=2**20) as ws:
        await ws.send(json.dumps({"type": "start", "token": token}))
        started = json.loads(await ws.recv())

        async def reader() -> None:
            try:
                async for raw in ws:
                    received.append(json.loads(raw))
            except ConnectionClosed:
                pass

        reading = asyncio.create_task(reader())
        try:
            for frame in frames:
                await ws.send(frame)
            if pause:
                await asyncio.sleep(pause)
            for frame in then or []:
                await ws.send(frame)
            await ws.send(json.dumps({"type": "stop"}))
        except ConnectionClosed:
            pass
        await reading
        return received, ws.close_code, started["transcription_id"]


@pytest.mark.parametrize("implementation", ["websockets", "websockets-sansio"])
def test_uvicorn_ws_max_size_rejects_oversized_frames_before_the_app(
    uvicorn_app: Any, migrated_database: str, implementation: str
) -> None:
    app, pcm, words = uvicorn_app()  # WS_MAX_FRAME_BYTES = 65536 (default)
    with live_uvicorn(app, ws=implementation, ws_max_size=65536) as api:
        token = _register(api)
        first_sentence = pcm[: 16_000 * 2 * 3300 // 1000]  # 2.1 s speech + 1.2 s pause
        speech_frames = [first_sentence[i : i + 8000] for i in range(0, len(first_sentence), 8000)]
        received, code, _tid = asyncio.run(
            _record_with_frames(api, token, speech_frames, then=[b"\x00" * 65538], pause=0.5)
        )
        assert code == 1009  # closed by Uvicorn at the protocol layer
        assert not any(m["type"] == "session_saved" for m in received)  # app never saw it
        for _ in range(200):
            (row,) = db_rows(migrated_database, "SELECT status, text FROM transcriptions")
            if row["status"] != "recording":
                break
            time.sleep(0.05)
        assert row["status"] == "interrupted"  # saved like a disconnect
        assert row["text"].split() == [w.text for w in words][:7]  # the first sentence kept


def test_frame_at_the_limit_is_accepted_by_uvicorn_and_app(
    uvicorn_app: Any, migrated_database: str
) -> None:
    app, pcm, words = uvicorn_app()
    with live_uvicorn(app) as api:
        token = _register(api)
        frames = [pcm[i : i + 65536] for i in range(0, len(pcm), 65536)]  # 64 KiB frames
        received, code, _ = asyncio.run(_record_with_frames(api, token, frames))
        assert code == 1000
        assert received[-1]["type"] == "session_saved" and received[-1]["status"] == "completed"
        assert received[-1]["text"] == " ".join(w.text for w in words)


def test_app_level_limit_when_uvicorn_limit_is_larger(uvicorn_app: Any) -> None:
    app, pcm, _ = uvicorn_app(ws_max_frame_bytes=16384)
    with live_uvicorn(app, ws_max_size=65536) as api:
        token = _register(api)
        received, code, _ = asyncio.run(_record_with_frames(api, token, [pcm[:32000]]))
        assert code == 1009
        assert received[0]["code"] == "frame_too_large"
        assert received[-1]["type"] == "session_saved" and received[-1]["status"] == "interrupted"


def _wav(path: Path, pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(pcm)
    return path


def test_sample_client_caps_frames_to_the_announced_limit(uvicorn_app: Any, tmp_path: Path) -> None:
    app, pcm, _ = uvicorn_app(ws_max_frame_bytes=8192)
    with live_uvicorn(app) as api:
        _register(api, "cap@example.com")
        out = io.StringIO()
        code = _load_client().main(
            [
                str(_wav(tmp_path / "c.wav", pcm)),
                "--api",
                api,
                "--email",
                "cap@example.com",
                "--password",
                DEFAULT_PASSWORD,
                "--chunk-ms",
                "1000",
                "--speed",
                "0",
            ],
            stdout=out,
        )
        assert code == 0, out.getvalue()
        assert "sending 8192-byte frames" in out.getvalue()
        assert "status=completed" in out.getvalue()


def test_sample_client_reports_protocol_level_rejection(uvicorn_app: Any, tmp_path: Path) -> None:
    # Misconfiguration: Uvicorn's limit (16 KiB) is below the announced 64 KiB frame size.
    app, pcm, _ = uvicorn_app()
    with live_uvicorn(app, ws_max_size=16384) as api:
        _register(api, "reject@example.com")
        out = io.StringIO()
        code = _load_client().main(
            [
                str(_wav(tmp_path / "r.wav", pcm)),
                "--api",
                api,
                "--email",
                "reject@example.com",
                "--password",
                DEFAULT_PASSWORD,
                "--chunk-ms",
                "1000",
                "--speed",
                "0",
            ],
            stdout=out,
        )
        assert code == 1
        assert "rejected an audio frame as too large (close 1009)" in out.getvalue()


def test_sample_client_reports_app_level_rejection(uvicorn_app: Any, tmp_path: Path) -> None:
    # The client caps to the announced limit, so force an oversized frame via a stale limit:
    # the app announces 64 KiB but the frame limit is lowered after the session started.
    app, pcm, _ = uvicorn_app()
    with live_uvicorn(app) as api:
        _register(api, "stale@example.com")
        original = app.state.settings.ws_max_frame_bytes
        client = _load_client()
        real_stream = client.stream

        async def stream_with_lowered_limit(*args: Any, **kwargs: Any) -> int:
            async def lower() -> None:
                await asyncio.sleep(0.2)
                app.state.settings.ws_max_frame_bytes = 4096

            lowering = asyncio.create_task(lower())
            try:
                return int(await real_stream(*args, **kwargs))
            finally:
                await lowering

        client.stream = stream_with_lowered_limit
        out = io.StringIO()
        try:
            code = client.main(
                [
                    str(_wav(tmp_path / "s.wav", pcm)),
                    "--api",
                    api,
                    "--email",
                    "stale@example.com",
                    "--password",
                    DEFAULT_PASSWORD,
                    "--chunk-ms",
                    "500",
                    "--speed",
                    "1",
                ],
                stdout=out,
            )
        finally:
            app.state.settings.ws_max_frame_bytes = original
        assert code == 1
        assert "frame_too_large" in out.getvalue()
        assert "close 1009" in out.getvalue()
