"""The browser client's contract against the real WebSocket server (fake speech engine).

The React client (frontend/src/realtime/transcriptionSocket.ts + public/pcm-worklet.js)
sends 100 ms frames of 3200 bytes, a smaller final frame on stop, and paces itself with a
token bucket of 4 s burst and 1.4x real time. These tests pin those numbers to the server's
DEFAULT limits (WS_AUDIO_BURST_SECONDS=5, WS_AUDIO_RATE_FACTOR=1.5), so a change on either
side that would get real users rate-limited fails here.
"""

from __future__ import annotations

import time
from typing import Any

from tests.integration.ws.conftest import two_sentences
from tests.unit.realtime.fakes import TimelineEngine

FRAME_BYTES = 3200  # frontend FRAME_SAMPLES (1600) * 2
CLIENT_BURST_S = 4.0  # frontend DEFAULT_LIMITS.burstMs
CLIENT_RATE = 1.4  # frontend DEFAULT_LIMITS.rateFactor
BYTES_PER_S = 32_000


def browser_frames(pcm: bytes) -> list[bytes]:
    """Split like the AudioWorklet framer: full 100 ms frames, then the remainder."""
    return [pcm[i : i + FRAME_BYTES] for i in range(0, len(pcm), FRAME_BYTES)]


def test_browser_framing_and_pacing_pass_the_default_rate_limit(harness_factory: Any) -> None:
    pcm, words = two_sentences()
    pcm += b"\x00\x00" * 777  # an odd number of samples: the last frame is not 3200 bytes
    frames = browser_frames(pcm)
    assert len(frames[-1]) not in (0, FRAME_BYTES) and len(frames[-1]) % 2 == 0

    # Production rate-limit defaults (the shared harness relaxes them for speed).
    overrides = {"ws_audio_burst_seconds": 5.0, "ws_audio_rate_factor": 1.5}
    with harness_factory(TimelineEngine(words), **overrides) as h:
        token = h.token()
        with h.connect() as ws:
            ws.send_json(
                {
                    "type": "start",
                    "token": token,
                    "speech_mode": "mixed",
                    "script_preference": "roman",
                    "format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "title": "Browser client",
                }
            )
            started = ws.receive_json()
            assert started["type"] == "session_started"
            assert started["max_frame_bytes"] >= FRAME_BYTES

            # Worst case the client allows itself: a full 4 s burst at once (after a
            # network stall), then 1.4x real time.
            start = time.monotonic()
            sent = 0
            for frame in frames:
                allowed = (
                    CLIENT_BURST_S * BYTES_PER_S
                    + (time.monotonic() - start) * CLIENT_RATE * BYTES_PER_S
                )
                while sent + len(frame) > allowed:
                    time.sleep(0.01)
                    allowed = (
                        CLIENT_BURST_S * BYTES_PER_S
                        + (time.monotonic() - start) * CLIENT_RATE * BYTES_PER_S
                    )
                ws.send_bytes(frame)
                sent += len(frame)
            ws.send_json({"type": "stop"})
            messages = h.receive_until(ws)

    errors = [m for m in messages if m["type"] == "error"]
    assert errors == [], errors  # in particular: no rate_limited
    saved = messages[-1]
    assert saved["status"] == "completed"
    assert saved["end_reason"] == "stopped"
    assert saved["duration_ms"] == len(pcm) // 32  # every byte arrived, tail included
    finals = [m for m in messages if m["type"] == "final"]
    assert [f["seq"] for f in finals] == list(range(len(finals))) and finals


def test_client_abort_after_failed_flush_is_stored_as_interrupted(harness_factory: Any) -> None:
    """When the browser cannot flush its last audio it must NOT send stop (that would store
    "completed"); it closes the socket with 4000. The server must then keep the recording
    as interrupted, with every final it produced from the audio that did arrive."""
    pcm, words = two_sentences()
    with harness_factory(TimelineEngine(words)) as h:
        token = h.token()
        with h.connect() as ws:
            started = h.start(ws, token, speech_mode="mixed")
            for frame in browser_frames(pcm):
                ws.send_bytes(frame)
            ws.close(code=4000)  # TranscriptionSocket.abort(): no {"type": "stop"}
        tid = started["transcription_id"]
        row = h.wait_status(tid)
        segments = h.segments(tid)

    assert row["status"] == "interrupted"
    assert row["duration_ms"] == len(pcm) // 32  # what was received is accounted for
    assert segments, "finals from the received audio are kept"
    assert [s["seq"] for s in segments] == list(range(len(segments)))
