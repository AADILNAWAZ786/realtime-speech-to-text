"""Reliability: disconnects, slow inference, database and socket failures, shutdown."""

from __future__ import annotations

import threading
import uuid
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from starlette.websockets import WebSocket, WebSocketDisconnect

from app.main import create_app
from app.models.enums import ScriptPreference, SpeechMode
from app.realtime.endpointer import CommitReason
from app.realtime.events import FinalTranscript
from app.ws.persistence import DisplayedFinal, RecordingStore, SaveOutcome
from tests.integration.ws.conftest import Harness, db_rows, speech, two_sentences
from tests.unit.realtime.fakes import TimelineEngine


class FlakyStore(RecordingStore):
    """append_final fails (after its own retries) for the first `failures` calls."""

    def __init__(self, *args: Any, failures: int | None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures = failures  # None = always fail
        self.calls = 0

    async def append_final(self, transcription_id: uuid.UUID, item: DisplayedFinal) -> SaveOutcome:
        self.calls += 1
        if self.failures is None or self.calls <= self.failures:
            raise OperationalError("INSERT", {}, ConnectionError("database unavailable"))
        return await super().append_final(transcription_id, item)


def _flaky(h: Harness, failures: int | None) -> FlakyStore:
    store = FlakyStore(h.app.state.session_factory, failures=failures, retry_delays=(0.01,))
    h.app.state.recording_store = store
    return store


# ---------------------------------------------------------------- sessions in sequence / parallel


def test_multiple_sequential_recordings(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        ids = [h.record(token, pcm)[0]["transcription_id"] for _ in range(3)]
        assert len(set(ids)) == 3
        for tid in ids:
            assert h.transcription(tid)["status"] == "completed"
            assert [s["seq"] for s in h.segments(tid)] == [0, 1]
        assert h.app.state.speech.registry.active_count == 0


def test_two_users_record_independently(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        alice, bob = h.token("alice@example.com"), h.token("bob@example.com")
        with h.connect() as wa, h.connect() as wb:
            a = h.start(wa, alice, script_preference="as_recognised")["transcription_id"]
            b = h.start(wb, bob, speech_mode="en")["transcription_id"]
            step = 8000
            for offset in range(0, len(pcm), step):  # interleave the two streams
                wa.send_bytes(pcm[offset : offset + step])
                wb.send_bytes(pcm[offset : offset + step])
            wa.send_json({"type": "stop"})
            wb.send_json({"type": "stop"})
            saved_a, saved_b = h.receive_until(wa)[-1], h.receive_until(wb)[-1]
        assert saved_a["transcription_id"] == a and saved_b["transcription_id"] == b
        users = {
            r["id"]: r["user_id"]
            for r in db_rows(h.db_url, "SELECT id, user_id FROM transcriptions")
        }
        assert len(set(users.values())) == 2
        for tid in (a, b):
            assert [s["seq"] for s in h.segments(tid)] == [0, 1]
            assert h.transcription(tid)["text"] == " ".join(w.text for w in words)
        assert h.transcription(b)["speech_mode"] == "en"


# ---------------------------------------------------------------- disconnects


def test_unexpected_disconnect_commits_pending_audio_and_marks_interrupted(
    harness_factory: Any,
) -> None:
    pcm, words = speech(("speech", 2100), ("silence", 1200), ("speech", 2400))  # 2nd unfinished
    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        with h.connect() as ws:
            tid = h.start(ws, token)["transcription_id"]
            h.send_audio(ws, pcm)
        # Client vanished without "stop": the open utterance is still committed.
        row = h.wait_status(tid)
        assert row["status"] == "interrupted"
        assert row["duration_ms"] == 5700
        segments = h.segments(tid)
        assert [s["seq"] for s in segments] == [0, 1]
        assert row["text"] == " ".join(w.text for w in words)
        assert h.app.state.speech.registry.active_count == 0


def test_disconnect_while_inference_is_running(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    engine = TimelineEngine(words, final_delay=0.4)
    with harness_factory(engine) as h:
        token = h.token()
        with h.connect() as ws:
            tid = h.start(ws, token)["transcription_id"]
            h.send_audio(ws, pcm, pause=0)
        row = h.wait_status(tid)
        assert row["status"] == "interrupted"
        assert len(h.segments(tid)) == 2  # finals finished after the client left were saved
        assert h.app.state.speech.scheduler.running == 0


def test_server_shutdown_saves_and_closes_active_recordings(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        with h.connect() as ws:
            tid = h.start(ws, token)["transcription_id"]
            h.send_audio(ws, pcm)
            shutdown = threading.Thread(
                target=h.client.portal.call,
                args=(h.app.state.speech.registry.shutdown, 5.0),
            )
            shutdown.start()
            messages = h.receive_until(ws)
            shutdown.join(5)
            errors = [m for m in messages if m["type"] == "error"]
            assert [e["code"] for e in errors] == ["server_shutdown"]
            assert messages[-1]["status"] == "interrupted"
            with pytest.raises(WebSocketDisconnect) as exc_info:
                ws.receive_json()
            assert exc_info.value.code == 1012
        assert h.transcription(tid)["status"] == "interrupted"
        assert len(h.segments(tid)) == 2
        with h.connect() as ws:  # no new recordings while shutting down
            ws.send_json({"type": "start", "token": token})
            assert ws.receive_json()["code"] == "server_shutdown"


# ---------------------------------------------------------------- persistence failures


def test_database_outage_mid_recording_recovers_in_order(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        _flaky(h, failures=1)  # the first final cannot be saved
        started, messages = h.record(token, pcm)
        finals = [m for m in messages if m["type"] == "final"]
        assert [f["saved"] for f in finals] == [False, False]  # 2nd waits to keep order
        warning = next(m for m in messages if m["type"] == "error")
        assert (warning["code"], warning["fatal"], warning["seq"]) == (
            "persistence_failed",
            False,
            0,
        )
        saved = messages[-1]
        assert (saved["saved_segments"], saved["unsaved_segments"]) == (2, [])
        tid = started["transcription_id"]
        assert [s["seq"] for s in h.segments(tid)] == [0, 1]  # flushed at the end, in order
        assert h.transcription(tid)["text"] == " ".join(w.text for w in words)


def test_permanent_database_outage_is_reported_not_silent(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        _flaky(h, failures=None)
        started, messages = h.record(token, pcm)
        finals = [m for m in messages if m["type"] == "final"]
        assert len(finals) == 2 and not any(f["saved"] for f in finals)  # still delivered
        errors = [m for m in messages if m["type"] == "error"]
        assert errors[-1]["unsaved_segments"] == [0, 1]
        saved = messages[-1]
        assert (saved["saved_segments"], saved["unsaved_segments"]) == (0, [0, 1])
        row = h.transcription(started["transcription_id"])
        assert row["status"] == "completed" and row["text"] == ""  # never marked recording


def test_websocket_sender_failure_does_not_stop_persistence(
    harness_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    pcm, words = two_sentences()
    original = WebSocket.send_json

    async def failing_send(self: WebSocket, data: Any, mode: str = "text") -> None:
        if data.get("type") != "session_started":
            raise RuntimeError("socket write failed")
        await original(self, data, mode)

    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        monkeypatch.setattr(WebSocket, "send_json", failing_send)
        with h.connect() as ws:
            tid = h.start(ws, token)["transcription_id"]
            h.send_audio(ws, pcm)
            ws.send_json({"type": "stop"})
        row = h.wait_status(tid)
        assert row["status"] == "completed"
        assert [s["seq"] for s in h.segments(tid)] == [0, 1]
        assert row["text"] == " ".join(w.text for w in words)


def test_no_duplicate_segments_even_with_retries(harness_factory: Any) -> None:
    pcm, words = speech(("speech", 32_000), ("silence", 1500))
    with harness_factory(TimelineEngine(words)) as h:
        _started, messages = h.record(h.token(), pcm)
        rows = db_rows(
            h.db_url,
            "SELECT seq, count(*) AS n FROM transcription_segments GROUP BY seq ORDER BY seq",
        )
        assert all(r["n"] == 1 for r in rows)
        assert len(rows) == len([m for m in messages if m["type"] == "final"])


# ---------------------------------------------------------------- store (direct)


def _final(seq: int, text: str, start: int, end: int) -> FinalTranscript:
    return FinalTranscript(seq, seq, text, start, end, "hi", -0.2, CommitReason.PAUSE, "hi")


async def _store_scenario(h: Harness, user_id: uuid.UUID) -> tuple[uuid.UUID, RecordingStore]:
    store = RecordingStore(h.app.state.session_factory, retry_delays=(0.01,))
    tid = await store.create_recording(
        user_id=user_id,
        title="t",
        speech_mode=SpeechMode.MIXED,
        script_preference=ScriptPreference.ROMAN,
        model_name="m",
    )
    return tid, store


def test_store_append_is_atomic_and_exactly_once(harness_factory: Any) -> None:
    with harness_factory() as h:
        h.token()

        (user,) = db_rows(h.db_url, "SELECT id FROM users")

        async def scenario() -> None:
            tid, store = await _store_scenario(h, user["id"])
            first = DisplayedFinal(_final(0, "आज", 0, 300), "aaj")
            assert await store.append_final(tid, first) is SaveOutcome.SAVED
            assert await store.append_final(tid, first) is SaveOutcome.ALREADY_SAVED
            await store.append_final(tid, DisplayedFinal(_final(1, "कल", 400, 700), "kal"))
            assert await store.saved_seqs(tid) == [0, 1]

        h.client.portal.call(scenario)
        (row,) = db_rows(h.db_url, "SELECT text FROM transcriptions")
        assert row["text"] == "aaj kal"  # the duplicate did not append twice
        assert [
            r["raw_text"]
            for r in db_rows(h.db_url, "SELECT raw_text FROM transcription_segments ORDER BY seq")
        ] == ["आज", "कल"]


def test_store_retries_transient_database_errors(harness_factory: Any) -> None:
    with harness_factory() as h:
        h.token()

        (user,) = db_rows(h.db_url, "SELECT id FROM users")

        async def scenario() -> None:
            tid, store = await _store_scenario(h, user["id"])
            real_factory = store._sessions
            calls = {"n": 0}

            def flaky_factory() -> Any:
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OperationalError("connect", {}, ConnectionError("reset"))
                return real_factory()

            store._sessions = flaky_factory  # type: ignore[assignment]
            outcome = await store.append_final(tid, DisplayedFinal(_final(0, "x", 0, 1), "x"))
            assert outcome is SaveOutcome.SAVED and calls["n"] == 2

        h.client.portal.call(scenario)
        assert len(db_rows(h.db_url, "SELECT id FROM transcription_segments")) == 1


def test_stale_recordings_marked_interrupted_at_startup(
    harness_factory: Any, make_settings: Any
) -> None:
    with harness_factory() as h:
        h.token()

        (user,) = db_rows(h.db_url, "SELECT id FROM users")

        async def create() -> uuid.UUID:
            tid, _ = await _store_scenario(h, user["id"])
            return tid

        tid = h.client.portal.call(create)
        assert h.transcription(tid)["status"] == "recording"
        restarted = create_app(h.settings, speech_engine=TimelineEngine([]))
        from starlette.testclient import TestClient

        with TestClient(restarted):  # a restart: no recording can still be live
            pass
        assert h.transcription(tid)["status"] == "interrupted"


def test_startup_fails_when_cpu_budget_cannot_be_met(
    make_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    monkeypatch.setattr("os.cpu_count", lambda: 2)
    app = create_app(make_settings(inference_concurrency=2), speech_engine=TimelineEngine([]))
    with pytest.raises(ValueError, match="exceeds the budget"), TestClient(app):
        pass
    allowed = create_app(
        make_settings(inference_concurrency=2, inference_allow_cpu_oversubscription=True),
        speech_engine=TimelineEngine([]),
    )
    with TestClient(allowed):
        assert allowed.state.speech.concurrency == 2


def test_asyncio_is_not_blocked_by_inference(harness_factory: Any) -> None:
    """A slow final must not stop the server from answering other requests."""
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words, final_delay=0.5)) as h:
        token = h.token()
        with h.connect() as ws:
            h.start(ws, token)
            h.send_audio(ws, pcm, pause=0)
            response = h.client.get("/api/v1/health")  # served while inference runs
            assert response.status_code == 200
            ws.send_json({"type": "stop"})
            assert h.receive_until(ws)[-1]["saved_segments"] == 2
