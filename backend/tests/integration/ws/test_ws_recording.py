"""Recording over the WebSocket: protocol messages, persistence and limits."""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.integration.ws.conftest import db_rows, speech, two_sentences
from tests.unit.realtime.fakes import SpokenWord, TimelineEngine


def _types(messages: list[dict[str, Any]]) -> list[str]:
    return [m["type"] for m in messages]


# ---------------------------------------------------------------- happy path


def test_recording_produces_partials_finals_and_saved_record(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        started, messages = h.record(token, pcm, speech_mode="hi", title="Standup")
        tid = started["transcription_id"]

        partials = [m for m in messages if m["type"] == "partial"]
        finals = [m for m in messages if m["type"] == "final"]
        saved = messages[-1]
        assert partials, "expected at least one partial update"
        assert [f["seq"] for f in finals] == [0, 1]
        assert all(f["saved"] for f in finals)
        assert _types(messages).index("final") < _types(messages).index("session_saved")
        spoken = " ".join(w.text for w in words)
        assert " ".join(f["text"] for f in finals) == spoken
        assert saved == {
            "type": "session_saved",
            "transcription_id": tid,
            "status": "completed",
            "end_reason": "stopped",
            "duration_ms": 6900,
            "final_count": 2,
            "saved_segments": 2,
            "unsaved_segments": [],
            "failed_utterances": 0,
            "record_saved": True,
            "text": spoken,
        }

        row = h.transcription(tid)
        assert row["title"] == "Standup"
        assert (row["source"], row["status"], row["speech_mode"]) == ("live", "completed", "hi")
        assert row["text"] == spoken
        assert row["duration_ms"] == 6900
        assert row["model_name"] == "timeline-fake"
        assert row["is_edited"] is False
        assert row["detected_language"] == "hi"

        segments = h.segments(tid)
        assert [s["seq"] for s in segments] == [0, 1]
        assert [(s["start_ms"], s["end_ms"]) for s in segments] == [(0, 2100), (3300, 5400)]
        assert [s["raw_text"] for s in segments] == [f["raw_text"] for f in finals]


def test_partials_are_never_saved(harness_factory: Any) -> None:
    pcm, words = speech(("speech", 6000), ("silence", 1500))
    with harness_factory(TimelineEngine(words)) as h:
        started, messages = h.record(h.token(), pcm)
        partials = [m for m in messages if m["type"] == "partial"]
        finals = [m for m in messages if m["type"] == "final"]
        assert len(partials) >= 2 and len(finals) == 1
        segments = h.segments(started["transcription_id"])
        assert len(segments) == len(finals)  # one row per final, none for partials
        assert segments[0]["raw_text"] == finals[0]["raw_text"]


def test_script_preference_applies_to_displayed_text_only(harness_factory: Any) -> None:
    pcm, timeline = speech(("speech", 1800), ("silence", 1500))
    hindi = ["आज", "की", "बात", "कल", "शाम", "ठीक"]
    words = [SpokenWord(hindi[i], w.start_ms, w.end_ms) for i, w in enumerate(timeline)]
    with harness_factory(TimelineEngine(words)) as h:
        started, messages = h.record(h.token(), pcm, speech_mode="mixed", script_preference="roman")
        (final,) = [m for m in messages if m["type"] == "final"]
        assert final["raw_text"] == "आज की बात कल शाम ठीक"
        assert final["text"] == "aaj ki baat kal shaam thik"
        partial = next(m for m in messages if m["type"] == "partial")
        assert partial["text"].isascii() and not partial["raw_text"].isascii()

        row = h.transcription(started["transcription_id"])
        assert row["text"] == "aaj ki baat kal shaam thik"  # editable, rendered text
        assert row["script_preference"] == "roman"
        (segment,) = h.segments(started["transcription_id"])
        assert segment["raw_text"] == "आज की बात कल शाम ठीक"  # raw model output preserved


def test_long_recording_timestamps_and_sequence(harness_factory: Any) -> None:
    pcm, words = speech(("speech", 32_000), ("silence", 1500))
    with harness_factory(TimelineEngine(words)) as h:
        started, _messages = h.record(h.token(), pcm)
        segments = h.segments(started["transcription_id"])
        assert [s["seq"] for s in segments] == list(range(len(segments)))
        assert len(segments) >= 2
        joined = " ".join(s["raw_text"] for s in segments).split()
        assert joined == [w.text for w in words]  # every word once, in order
        for left, right in itertools.pairwise(segments):
            assert left["end_ms"] <= right["start_ms"]
        assert segments[0]["start_ms"] == words[0].start_ms
        assert segments[-1]["end_ms"] == words[-1].end_ms  # last whole word: 31 800 ms


def test_utterance_failures_are_reported_and_recording_still_saved(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words, always_fail_finals=True)) as h:
        started, messages = h.record(h.token(), pcm)
        failed = [m for m in messages if m["type"] == "utterance_failed"]
        assert len(failed) == 2
        assert set(failed[0]) == {"type", "utterance_id", "start_ms", "end_ms", "code"}
        assert messages[-1]["failed_utterances"] == 2
        assert h.segments(started["transcription_id"]) == []
        assert h.transcription(started["transcription_id"])["status"] == "completed"


def test_noise_utterance_is_discarded(harness_factory: Any) -> None:
    pcm, _ = speech(("speech", 2100), ("silence", 1500))
    with harness_factory(TimelineEngine([])) as h:  # the recogniser hears no words
        started, messages = h.record(h.token(), pcm)
        assert "utterance_discarded" in _types(messages)
        assert h.segments(started["transcription_id"]) == []
        assert h.transcription(started["transcription_id"])["text"] == ""


def test_default_title(harness_factory: Any) -> None:
    with harness_factory() as h:
        started, _ = h.record(h.token(), b"")
        assert h.transcription(started["transcription_id"])["title"].startswith("Recording - ")


# ---------------------------------------------------------------- frame validation & limits


@pytest.mark.parametrize(
    ("payload", "code", "error"),
    [
        (b"\x00\x01\x02", 4400, "invalid_audio"),
        (b"", 4400, "invalid_audio"),
        (b"\x00" * 4098, 1009, "frame_too_large"),
    ],
    ids=["odd-bytes", "empty", "oversized"],
)
def test_invalid_audio_frames(harness_factory: Any, payload: bytes, code: int, error: str) -> None:
    pcm, words = speech(("speech", 2100), ("silence", 1200))
    with harness_factory(TimelineEngine(words), ws_max_frame_bytes=4096) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            h.send_audio(ws, pcm, chunk_ms=100)
            ws.send_bytes(payload)
            messages = h.receive_until(ws)
            errors = [m for m in messages if m["type"] == "error"]
            assert errors[0]["code"] == error
            assert messages[-1]["status"] == "interrupted"
            assert messages[-1]["saved_segments"] == 1  # audio before the bad frame kept
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == code
        row = h.transcription(started["transcription_id"])
        assert row["status"] == "interrupted"


def test_unexpected_text_messages(harness_factory: Any) -> None:
    with harness_factory() as h:
        token = h.token()
        for message in ({"type": "start", "token": token}, {"type": "pause"}):
            with h.connect() as ws:
                h.start(ws, token)
                ws.send_json(message)
                messages = h.receive_until(ws)
                assert messages[0] == {
                    "type": "error",
                    "code": "invalid_message",
                    "message": "Invalid message",
                    "fatal": True,
                }
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    ws.receive_json()
                assert exc_info.value.code == 4400


def test_audio_rate_limit(harness_factory: Any) -> None:
    pcm, words = speech(("speech", 2100), ("silence", 6000))
    with harness_factory(
        TimelineEngine(words), ws_audio_burst_seconds=3.0, ws_audio_rate_factor=1.5
    ) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            h.send_audio(ws, pcm, pause=0)  # ~8 s of audio in a few milliseconds
            messages = h.receive_until(ws)
            assert {"type": "error", "code": "rate_limited"}.items() <= next(
                m for m in messages if m["type"] == "error"
            ).items()
            assert messages[-1]["status"] == "interrupted"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4429
        # Audio accepted before the limit was still committed and saved.
        assert len(h.segments(started["transcription_id"])) == 1


def test_one_active_recording_per_user(harness_factory: Any) -> None:
    with harness_factory() as h:
        token = h.token()
        with h.connect() as first:
            h.start(first, token)
            with h.connect() as second:
                second.send_json({"type": "start", "token": token})
                assert second.receive_json()["code"] == "recording_in_progress"
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    second.receive_json()
                assert exc_info.value.code == 4409
            first.send_json({"type": "stop"})
            h.receive_until(first)
        assert len(db_rows(h.db_url, "SELECT id FROM transcriptions")) == 1
        # The slot is released: the same user can record again.
        _started, messages = h.record(token, b"")
        assert messages[-1]["status"] == "completed"


def test_global_session_limit(harness_factory: Any) -> None:
    with harness_factory(ws_max_active_sessions=1) as h:
        alice, bob = h.token("alice@example.com"), h.token("bob@example.com")
        with h.connect() as first:
            h.start(first, alice)
            with h.connect() as second:
                second.send_json({"type": "start", "token": bob})
                assert second.receive_json()["code"] == "server_busy"
                with pytest.raises(WebSocketDisconnect) as exc_info:
                    second.receive_json()
                assert exc_info.value.code == 1013
            first.send_json({"type": "stop"})
            h.receive_until(first)
        assert h.record(bob, b"")[1][-1]["status"] == "completed"


def test_maximum_recording_duration(harness_factory: Any) -> None:
    pcm, words = speech(("speech", 2100), ("silence", 1200), ("speech", 9000))
    with harness_factory(TimelineEngine(words), ws_max_recording_seconds=10) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            h.send_audio(ws, pcm)
            messages = h.receive_until(ws)
            saved = messages[-1]
            assert (saved["status"], saved["end_reason"]) == ("completed", "recording_limit")
            assert 10_000 <= saved["duration_ms"] <= 10_250
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1000
        assert h.transcription(started["transcription_id"])["status"] == "completed"


def test_idle_timeout(harness_factory: Any) -> None:
    with harness_factory(ws_idle_timeout_seconds=0.3) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token)
            messages = h.receive_until(ws)
            assert messages[0]["code"] == "idle_timeout"
            assert messages[-1]["status"] == "interrupted"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 4408
        assert h.transcription(started["transcription_id"])["status"] == "interrupted"


# ---------------------------------------------------------------- isolation


def test_live_recording_is_visible_only_to_its_owner(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        alice, bob = h.token("alice@example.com"), h.token("bob@example.com")
        with h.connect() as ws:
            started = h.start(ws, alice)
            tid = started["transcription_id"]
            own = h.client.get(f"/api/v1/transcriptions/{tid}", headers=h.auth(alice))
            assert own.status_code == 200 and own.json()["status"] == "recording"
            other = h.client.get(f"/api/v1/transcriptions/{tid}", headers=h.auth(bob))
            assert other.status_code == 404
            assert h.client.get("/api/v1/transcriptions", headers=h.auth(bob)).json()["total"] == 0
            h.send_audio(ws, pcm)
            ws.send_json({"type": "stop"})
            h.receive_until(ws)
        detail = h.client.get(
            f"/api/v1/transcriptions/{tid}?include_segments=true", headers=h.auth(alice)
        ).json()
        assert detail["source"] == "live" and len(detail["segments"]) == 2
        assert (
            h.client.delete(f"/api/v1/transcriptions/{tid}", headers=h.auth(bob)).status_code == 404
        )
