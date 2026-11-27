"""What a live recording needs from a speech session, whichever provider runs it.

`RecordingConnection` (app/ws/connection.py) drives a session through exactly this
interface. Two implementations exist:

- `TranscriptionSession` (app/realtime/session.py): local faster-whisper with VAD,
  endpointing and the inference scheduler.
- `GeminiLiveSession` (app/realtime/gemini_live.py): Google Gemini Live API.

Both report results through the same `SessionEvent`s (app/realtime/events.py), so the
connection's single event consumer persists finals, renders the output script and builds
`session_saved` identically for both.

Contract:
- `open()` connects whatever the session needs before audio flows (no-op for Whisper).
  Raises `SpeechProviderError`.
- `feed(pcm16)` accepts 16 kHz mono PCM16 chunks of any size. Raises `AudioValidationError`
  (bad chunk), `SessionOverloadedError` (cannot keep up), `SpeechProviderError` (the
  provider failed) or `SessionClosedError` (no longer accepting audio). After it returns,
  `state` is not OPEN once the recording limit is reached.
- `stop(reason)` commits the remaining audio, waits for every pending final and only then
  emits `SessionEnded` (always exactly one, even after a provider failure). Idempotent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from app.realtime.endpointer import CommitReason
from app.realtime.session import SessionClosedError, SessionState


class RecordingSession(Protocol):
    @property
    def state(self) -> SessionState: ...

    @property
    def received_ms(self) -> int: ...

    async def open(self) -> None: ...

    async def feed(self, pcm16: bytes | bytearray | memoryview) -> None: ...

    async def stop(self, reason: CommitReason = CommitReason.STOP) -> object: ...


class ProviderFailure(StrEnum):
    """Client-safe classification of a speech provider failure."""

    AUTH = "auth"  # missing / invalid / unauthorised API key
    QUOTA = "quota"  # rate limit or quota exhausted
    UNAVAILABLE = "unavailable"  # connection failed, timed out or dropped; server error


class SpeechProviderError(SessionClosedError):
    """The external speech provider failed. Carries only a classification: provider
    exception text can contain request details and is never shown to clients."""

    def __init__(self, failure: ProviderFailure) -> None:
        super().__init__(None, f"speech provider failed: {failure.value}")
        self.failure = failure
