"""Structured, immutable transcription results.

Times are integer milliseconds. `offset_ms` lets a caller place a chunk inside a longer
recording (e.g. an utterance that starts 12.5 s into a live session), so every timestamp
is relative to the start of the whole recording.

`raw_text` is exactly what the model produced and is never modified; `text` is only a
whitespace-trimmed view of it. Script conversion lives in app.speech.script.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models.enums import SpeechMode


@dataclass(frozen=True, slots=True)
class WordTiming:
    start_ms: int
    end_ms: int
    word: str
    probability: float


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    index: int
    start_ms: int
    end_ms: int
    raw_text: str
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float
    words: tuple[WordTiming, ...] = ()

    @property
    def text(self) -> str:
        return self.raw_text.strip()

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    segments: tuple[TranscriptSegment, ...]
    language: str | None
    language_probability: float | None
    language_forced: bool  # True when the language was chosen by the mode, not detected
    speech_mode: SpeechMode
    decode_strategy: str
    model_name: str
    device: str
    compute_type: str
    audio_duration_ms: int
    processing_ms: float  # wall time including complete generator consumption
    offset_ms: int = 0
    all_language_probs: tuple[tuple[str, float], ...] = field(default=())

    @property
    def text(self) -> str:
        """Full transcription: trimmed segment texts joined by single spaces."""
        return " ".join(segment.text for segment in self.segments if segment.text)

    @property
    def is_empty(self) -> bool:
        return not self.text

    @property
    def real_time_factor(self) -> float | None:
        """Processing time / audio time (< 1.0 means faster than real time)."""
        if self.audio_duration_ms <= 0:
            return None
        return self.processing_ms / self.audio_duration_ms

    def to_dict(self) -> dict[str, Any]:
        rtf = self.real_time_factor
        return {
            "text": self.text,
            "language": self.language,
            "language_probability": self.language_probability,
            "language_forced": self.language_forced,
            "speech_mode": self.speech_mode.value,
            "decode_strategy": self.decode_strategy,
            "model_name": self.model_name,
            "device": self.device,
            "compute_type": self.compute_type,
            "audio_duration_ms": self.audio_duration_ms,
            "processing_ms": round(self.processing_ms, 2),
            "real_time_factor": round(rtf, 4) if rtf is not None else None,
            "offset_ms": self.offset_ms,
            "segments": [
                {
                    "index": seg.index,
                    "start_ms": seg.start_ms,
                    "end_ms": seg.end_ms,
                    "text": seg.text,
                    "raw_text": seg.raw_text,
                    "avg_logprob": seg.avg_logprob,
                    "no_speech_prob": seg.no_speech_prob,
                    "compression_ratio": seg.compression_ratio,
                    "words": [
                        {
                            "start_ms": w.start_ms,
                            "end_ms": w.end_ms,
                            "word": w.word,
                            "probability": w.probability,
                        }
                        for w in seg.words
                    ],
                }
                for seg in self.segments
            ],
        }


def seconds_to_ms(
    seconds: float, *, offset_ms: int = 0, audio_duration_ms: int | None = None
) -> int:
    """Convert a model timestamp (seconds, relative to the chunk) into absolute ms.

    Clamped to [0, audio duration]: Whisper's last timestamp can slightly overshoot the
    real end of the audio, and timestamps must never point outside the recording.
    """
    local_ms = round(max(seconds, 0.0) * 1000)
    if audio_duration_ms is not None:
        local_ms = min(local_ms, audio_duration_ms)
    return offset_ms + local_ms
