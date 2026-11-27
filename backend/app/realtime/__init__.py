"""Real-time audio processing core: VAD, endpointing, utterance buffering, prioritised
inference scheduling and the per-recording TranscriptionSession. Transport-agnostic (no
WebSocket code) and independently testable."""

from app.realtime.config import RealtimeConfig
from app.realtime.endpointer import CommitReason, EndpointState
from app.realtime.events import (
    FinalTranscript,
    LanguageLocked,
    PartialTranscript,
    PreviewPaused,
    PreviewResumed,
    SessionEnded,
    SessionEvent,
    SpeechStarted,
    UtteranceDiscarded,
    UtteranceFailed,
)
from app.realtime.scheduler import InferenceScheduler, recommended_concurrency, validate_cpu_budget
from app.realtime.session import (
    SessionClosedError,
    SessionOverloadedError,
    SessionResult,
    TranscriptionSession,
)
from app.realtime.vad import EnergyVad, FrameClassifier, WebRtcVad

__all__ = [
    "CommitReason",
    "EndpointState",
    "EnergyVad",
    "FinalTranscript",
    "FrameClassifier",
    "InferenceScheduler",
    "LanguageLocked",
    "PartialTranscript",
    "PreviewPaused",
    "PreviewResumed",
    "RealtimeConfig",
    "SessionClosedError",
    "SessionEnded",
    "SessionEvent",
    "SessionOverloadedError",
    "SessionResult",
    "SpeechStarted",
    "TranscriptionSession",
    "UtteranceDiscarded",
    "UtteranceFailed",
    "WebRtcVad",
    "recommended_concurrency",
    "validate_cpu_budget",
]
