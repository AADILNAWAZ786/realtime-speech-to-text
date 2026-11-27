"""Events emitted by a TranscriptionSession.

These are transport-neutral: the WebSocket layer (next step) maps them to protocol
messages. Only `FinalTranscript` represents committed text; `PartialTranscript` is
provisional and must never be persisted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from app.models.enums import SpeechMode
from app.realtime.endpointer import CommitReason
from app.speech.results import WordTiming


@dataclass(frozen=True, slots=True)
class SpeechStarted:
    type: ClassVar[str] = "speech_started"
    utterance_id: int
    start_ms: int


@dataclass(frozen=True, slots=True)
class PartialTranscript:
    """Provisional text of the open utterance. Replaces the previous partial; never saved."""

    type: ClassVar[str] = "partial"
    utterance_id: int
    version: int
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class FinalTranscript:
    """Committed text for one utterance. `seq` is gap-free and increasing per session."""

    type: ClassVar[str] = "final"
    seq: int
    utterance_id: int
    text: str
    start_ms: int
    end_ms: int
    language: str | None
    avg_logprob: float | None
    commit_reason: CommitReason
    decode_strategy: str
    words: tuple[WordTiming, ...] = field(default=())


@dataclass(frozen=True, slots=True)
class UtteranceDiscarded:
    """The utterance produced no usable text (noise, cough, filtered hallucination)."""

    type: ClassVar[str] = "utterance_discarded"
    utterance_id: int
    start_ms: int
    end_ms: int
    reason: str


@dataclass(frozen=True, slots=True)
class UtteranceFailed:
    """Final transcription failed after retries. Loud on purpose: audio is never dropped
    silently; the range is reported so it can be surfaced or re-processed."""

    type: ClassVar[str] = "utterance_failed"
    utterance_id: int
    start_ms: int
    end_ms: int
    error_code: str
    message: str


@dataclass(frozen=True, slots=True)
class LanguageLocked:
    type: ClassVar[str] = "language_locked"
    mode: SpeechMode
    detected_language: str
    probability: float
    reason: str


@dataclass(frozen=True, slots=True)
class PreviewPaused:
    type: ClassVar[str] = "preview_paused"
    backlog_ms: int


@dataclass(frozen=True, slots=True)
class PreviewResumed:
    type: ClassVar[str] = "preview_resumed"
    backlog_ms: int


@dataclass(frozen=True, slots=True)
class SessionEnded:
    type: ClassVar[str] = "session_ended"
    reason: CommitReason
    duration_ms: int
    final_count: int
    failed_count: int


SessionEvent = (
    SpeechStarted
    | PartialTranscript
    | FinalTranscript
    | UtteranceDiscarded
    | UtteranceFailed
    | LanguageLocked
    | PreviewPaused
    | PreviewResumed
    | SessionEnded
)
