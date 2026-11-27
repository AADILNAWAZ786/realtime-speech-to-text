"""/ws/transcribe with SPEECH_PROVIDER=gemini_live: real app, PostgreSQL and protocol; only
the Google Gen AI SDK is mocked (tests/unit/realtime/gemini_fakes.py). No Gemini calls."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.ws.persistence import RecordingStore
from tests.integration.ws.conftest import Harness, _truncate, db_rows
from tests.unit.realtime.gemini_fakes import (
    FakeConnector,
    Reactor,
    api_error,
    final,
    interim,
    pcm,
    scripted,
)

SECRET = "fake-gemini-key-integration-never-sent-0001"
CHUNK = 3200


@pytest.fixture
def gemini_factory(
    make_settings: Callable[..., Settings], migrated_database: str
) -> Iterator[Callable[..., Any]]:
    @contextmanager
    def build(connector: FakeConnector, **overrides: Any) -> Iterator[Harness]:
        values: dict[str, Any] = {
            "speech_provider": "gemini_live",
            "gemini_api_key": SECRET,
            "gemini_finalize_timeout_seconds": 2.0,
            "ws_audio_burst_seconds": 600.0,
            "ws_auth_timeout_seconds": 2.0,
            "ws_idle_timeout_seconds": 10.0,
        }
        values.update(overrides)
        settings = make_settings(**values)
        app = create_app(settings, gemini_connector=connector)
        app.state.recording_store = RecordingStore(
            app.state.session_factory, retry_delays=(0.01, 0.02)
        )
        try:
            with TestClient(app) as client:
                yield Harness(client, app, settings, migrated_database)
        finally:
            asyncio.run(_truncate(migrated_database))

    yield build


def stream(ws: Any, audio: bytes, frame: int = 1_000) -> None:
    for offset in range(0, len(audio), frame):  # odd browser-sized frames
        ws.send_bytes(audio[offset : offset + frame])


def hinglish() -> Reactor:
    return scripted(
        [
            (CHUNK, interim("आज की")),
            (2 * CHUNK, interim("आज की मीटिंग")),
            (4 * CHUNK, final("आज की मीटिंग कैंसल हो गई", language_code="hi-IN")),
            (5 * CHUNK, interim("next week")),
            ("end", final("next week deployment plan karte hain", language_code="en-IN")),
        ]
    )


def test_records_with_gemini_and_saves_finals_through_the_existing_store(
    gemini_factory: Any,
) -> None:
    connector = FakeConnector(reactor=hinglish())
    with gemini_factory(connector) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token, speech_mode="mixed", script_preference="roman")
            assert started["model_name"] == "gemini:gemini-3.5-transcribe-live"
            assert started["max_duration_ms"] == 600_000  # Gemini's 10-minute session
            assert started["audio"] == {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1}
            audio = pcm(700)
            stream(ws, audio)
            ws.send_json({"type": "stop"})
            messages = h.receive_until(ws)
        tid = started["transcription_id"]
        row = h.wait_status(tid)
        segments = h.segments(tid)

    # Gemini received exactly the browser's PCM16, in 100 ms chunks, then stream end.
    live = connector.live
    assert bytes(live.audio) == audio
    assert [len(b.data or b"") for b in live.blobs] == [CHUNK] * 7
    assert live.calls[-1] == "audio_stream_end"

    kinds = [m["type"] for m in messages]
    assert kinds.count("partial") == 3
    assert kinds[-1] == "session_saved"
    partials = [m for m in messages if m["type"] == "partial"]
    assert [(p["utterance_id"], p["version"]) for p in partials] == [(0, 1), (0, 2), (1, 1)]
    assert partials[0]["raw_text"] == "आज की"
    assert partials[0]["text"].isascii()  # roman output script applied by the existing code
    finals = [m for m in messages if m["type"] == "final"]
    assert [f["seq"] for f in finals] == [0, 1]
    assert all(f["saved"] for f in finals)
    assert finals[0]["raw_text"] == "आज की मीटिंग कैंसल हो गई"
    assert finals[0]["text"].isascii()
    assert finals[0]["language"] == "hi"
    assert finals[0]["avg_logprob"] is None  # never invented
    assert finals[1]["commit_reason"] == "stop"  # arrived after audio_stream_end
    saved = messages[-1]
    assert saved["status"] == "completed"
    assert saved["end_reason"] == "stopped"
    assert saved["record_saved"] is True
    assert saved["saved_segments"] == 2 and saved["unsaved_segments"] == []
    assert saved["duration_ms"] == 700

    # Persisted by RecordingStore.append_final(): ordered, no duplicates, owned by the user.
    assert [s["seq"] for s in segments] == [0, 1]
    assert segments[0]["raw_text"] == "आज की मीटिंग कैंसल हो गई"
    assert segments[0]["avg_logprob"] is None
    assert row["status"] == "completed"
    assert row["source"] == "live"
    assert row["model_name"] == "gemini:gemini-3.5-transcribe-live"
    assert row["detected_language"] == "hi"
    assert row["duration_ms"] == 700
    assert "next week deployment plan karte hain" in row["text"]


def test_recording_appears_in_the_history_api(gemini_factory: Any) -> None:
    connector = FakeConnector(reactor=scripted([("end", final("history check"))]))
    with gemini_factory(connector) as h:
        token = h.token()
        started, messages = h.record(token, pcm(300))
        listing = h.client.get("/api/v1/transcriptions", headers=h.auth(token)).json()
        detail = h.client.get(
            f"/api/v1/transcriptions/{started['transcription_id']}?include_segments=true",
            headers=h.auth(token),
        ).json()
        other = h.token("mallory@example.com")
        foreign = h.client.get(
            f"/api/v1/transcriptions/{started['transcription_id']}", headers=h.auth(other)
        )
    assert messages[-1]["status"] == "completed"
    assert listing["total"] == 1 and listing["items"][0]["status"] == "completed"
    assert detail["text"] == "history check"
    assert [s["raw_text"] for s in detail["segments"]] == ["history check"]
    assert foreign.status_code == 404  # ownership unchanged


def test_browser_disconnect_saves_pending_finals_as_interrupted(gemini_factory: Any) -> None:
    connector = FakeConnector(
        reactor=scripted(
            [
                (CHUNK, final("first sentence")),
                ("end", final("second sentence, finalized after the disconnect")),
            ]
        ),
        latency=0.3,
    )
    with gemini_factory(connector) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            stream(ws, pcm(400))
        tid = started["transcription_id"]
        row = h.wait_status(tid)
        segments = h.segments(tid)
    assert row["status"] == "interrupted"
    assert [s["raw_text"] for s in segments] == [
        "first sentence",
        "second sentence, finalized after the disconnect",
    ]
    assert connector.closed == 1  # the Gemini session was closed


def test_server_shutdown_processes_pending_finals(gemini_factory: Any) -> None:
    connector = FakeConnector(
        reactor=scripted([("end", final("saved during shutdown"))]), latency=0.3
    )
    with gemini_factory(connector) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            stream(ws, pcm(300))
            shutdown = threading.Thread(
                target=h.client.portal.call,  # type: ignore[union-attr]
                args=(h.app.state.speech.registry.shutdown, 5.0),
            )
            shutdown.start()
            messages = h.receive_until(ws)
            shutdown.join(10)
        tid = started["transcription_id"]
        segments = h.segments(tid)
        row = h.wait_status(tid)
    errors = [m for m in messages if m["type"] == "error"]
    assert errors[0]["code"] == "server_shutdown"
    assert messages[-1]["status"] == "interrupted"
    assert messages[-1]["end_reason"] == "shutdown"
    assert [s["raw_text"] for s in segments] == ["saved during shutdown"]
    assert row["status"] == "interrupted"


@pytest.mark.parametrize(
    ("error", "code", "close"),
    [
        (
            api_error(401, "UNAUTHENTICATED", f"API key {SECRET} not valid"),
            "transcription_unavailable",
            1011,
        ),
        (
            api_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded"),
            "transcription_quota_exceeded",
            1013,
        ),
        (OSError("connection refused"), "transcription_unavailable", 1013),
    ],
    ids=["invalid-key", "quota", "unreachable"],
)
def test_gemini_connection_failure_is_rejected_cleanly(
    gemini_factory: Any, error: BaseException, code: str, close: int
) -> None:
    connector = FakeConnector(connect_error=error)
    with gemini_factory(connector) as h:
        token = h.token()
        with h.connect() as ws:
            ws.send_json({"type": "start", "token": token})
            message = ws.receive_json()
            closed = ws.receive()
        rows = db_rows(h.db_url, "SELECT id FROM transcriptions")
        active = h.app.state.speech.registry.active_count
    assert message["type"] == "error"
    assert message["code"] == code
    assert message["fatal"] is True
    assert SECRET not in str(message) and "API key" not in message["message"]
    assert closed["code"] == close
    assert rows == []  # the empty recording was discarded: nothing in the history
    assert active == 0  # slot released


@pytest.mark.parametrize(
    ("connector", "code", "close"),
    [
        (FakeConnector(setup="never"), "transcription_unavailable", 1013),
        (
            FakeConnector(setup="close", setup_error=api_error(1011, "", "quota exceeded")),
            "transcription_quota_exceeded",
            1013,
        ),
    ],
    ids=["setup-timeout", "closed-before-setup"],
)
def test_gemini_setup_failure_is_rejected_and_cleaned_up(
    gemini_factory: Any, connector: FakeConnector, code: str, close: int
) -> None:
    with gemini_factory(connector, gemini_connect_timeout_seconds=0.5) as h:
        token = h.token()
        with h.connect() as ws:
            ws.send_json({"type": "start", "token": token})
            message = ws.receive_json()
            closed = ws.receive()
        rows = db_rows(h.db_url, "SELECT id FROM transcriptions")
        active = h.app.state.speech.registry.active_count
    assert message["type"] == "error" and message["code"] == code
    assert closed["code"] == close
    assert rows == []  # no recording left behind
    assert active == 0  # slot released
    assert connector.closed == 1  # Gemini connection closed
    assert connector.live.blobs == []  # no audio ever reached Gemini


def test_with_the_sdk_style_handshake_audio_flows_normally(gemini_factory: Any) -> None:
    connector = FakeConnector(setup="sdk", reactor=scripted([("end", final("sdk handshake"))]))
    with gemini_factory(connector) as h:
        token = h.token()
        _, messages = h.record(token, pcm(300))
    assert messages[-1]["status"] == "completed"
    assert [m["raw_text"] for m in messages if m["type"] == "final"] == ["sdk handshake"]


def test_quota_error_mid_recording_keeps_saved_text_and_reports_it(gemini_factory: Any) -> None:
    connector = FakeConnector(
        reactor=scripted(
            [
                (CHUNK, final("said before the quota ran out")),
                (2 * CHUNK, api_error(429, "RESOURCE_EXHAUSTED", f"Quota exceeded {SECRET}")),
            ]
        )
    )
    with gemini_factory(connector) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            stream(ws, pcm(1_000))
            messages = h.receive_until(ws)
        tid = started["transcription_id"]
        row = h.wait_status(tid)
        segments = h.segments(tid)
    errors = [m for m in messages if m["type"] == "error"]
    assert errors[0]["code"] == "transcription_quota_exceeded"
    assert SECRET not in str(messages)
    saved = messages[-1]
    assert saved["status"] == "interrupted"
    assert saved["end_reason"] == "provider_error"
    assert saved["saved_segments"] == 1
    assert row["status"] == "interrupted"
    assert [s["raw_text"] for s in segments] == ["said before the quota ran out"]


def test_session_limit_ends_the_recording_as_completed(gemini_factory: Any) -> None:
    connector = FakeConnector(reactor=scripted([("end", final("ten seconds of talk"))]))
    with gemini_factory(connector, gemini_max_session_seconds=10) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            assert started["max_duration_ms"] == 10_000
            for second in range(12):  # more than the limit
                try:
                    stream(ws, pcm(1_000), frame=32_000)
                except Exception:  # the server may already have closed
                    break
                if second == 11:
                    break
            messages = h.receive_until(ws)
        tid = started["transcription_id"]
        row = h.wait_status(tid)
    saved = messages[-1]
    assert saved["status"] == "completed"
    assert saved["end_reason"] == "recording_limit"
    assert saved["duration_ms"] == 10_000
    assert row["status"] == "completed"
    assert [m["raw_text"] for m in messages if m["type"] == "final"] == ["ten seconds of talk"]


def test_whisper_remains_the_default_provider(harness_factory: Any) -> None:
    with harness_factory() as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            ws.send_json({"type": "stop"})
            h.receive_until(ws)
    assert not started["model_name"].startswith("gemini:")
    assert started["max_duration_ms"] == 1_800_000  # WS_MAX_RECORDING_SECONDS unchanged
