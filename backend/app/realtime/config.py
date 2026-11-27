"""Configuration for the real-time pipeline (VAD, endpointing, buffering, partials, limits).

All durations are milliseconds and are converted to whole 30 ms frames (rounded up) where
the state machine needs frame counts. Defaults follow the approved architecture (v2 §3-4)
and are starting values to tune in the evaluation.
"""

from __future__ import annotations

import math
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAMPLE_RATE = 16_000


class RealtimeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    frame_ms: Literal[10, 20, 30] = 30  # WebRTC VAD supports exactly these

    # --- Voice activity detection (layer 1, per frame) ---
    vad_aggressiveness: int = Field(default=2, ge=0, le=3)
    energy_threshold_dbfs: float = Field(
        default=-50.0, ge=-120.0, le=0.0, description="Quieter frames are never speech"
    )
    onset_window_frames: int = Field(default=10, ge=1, le=100)
    onset_min_speech_frames: int = Field(default=6, ge=1, le=100)
    resume_min_speech_frames: int = Field(default=2, ge=1, le=50)
    short_burst_min_frames: int = Field(
        default=5,
        ge=0,
        le=100,
        description="A run of this many consecutive speech frames that ends without reaching "
        "the onset threshold still opens an utterance, so very short words ('no', 'haan') "
        "are not lost; also applied when recording stops. 0 disables. With 30 ms frames, "
        "5 means: sounds <= 90 ms (clicks, knocks) can overlap at most 4 frames and are "
        "always ignored; words >= 150 ms always cover >= 5 frames and are kept.",
    )
    preroll_ms: int = Field(default=300, ge=0, le=2_000)

    # --- Endpointing ---
    pause_enter_ms: int = Field(default=90, ge=10, description="Silence before IN_PAUSE")
    end_silence_ms: int = Field(default=800, ge=100, description="Pause that ends an utterance")
    min_speech_ms: int = Field(
        default=1_000, ge=0, description="Shorter utterances need short_utterance_silence_ms"
    )
    short_utterance_silence_ms: int = Field(default=1_500, ge=100)
    soft_max_utterance_ms: int = Field(
        default=10_000, ge=1_000, description="After this, commit at the next soft pause"
    )
    soft_pause_ms: int = Field(default=300, ge=30)
    max_utterance_ms: int = Field(default=15_000, ge=2_000, le=30_000)
    trailing_silence_keep_ms: int = Field(
        default=200, ge=0, description="Trailing silence kept when an utterance ends"
    )

    # --- Forced (max-length) boundaries ---
    boundary_search_ms: int = Field(
        default=3_000, ge=100, description="Look for a silence cut in this final stretch"
    )
    min_boundary_silence_ms: int = Field(default=150, ge=30)
    forced_carry_ms: int = Field(
        default=1_000, ge=100, description="Words ending in this final stretch are carried"
    )

    # --- Partial (preview) transcription ---
    partials_enabled: bool = True
    partial_interval_ms: int = Field(default=1_000, ge=100)
    partial_min_new_audio_ms: int = Field(default=500, ge=0)
    partial_min_audio_ms: int = Field(default=300, ge=0)
    context_chars: int = Field(default=200, ge=0, le=1_000)

    # --- Language (auto mode) ---
    auto_detect_min_ms: int = Field(default=3_000, ge=500)
    auto_detect_max_attempts: int = Field(default=3, ge=1)

    # --- Final post-processing ---
    hallucination_no_speech_prob: float = Field(default=0.6, ge=0.0, le=1.0)
    hallucination_logprob: float = Field(default=-1.0, le=0.0)
    hallucination_compression_ratio: float = Field(default=2.4, gt=1.0)
    dedupe_max_words: int = Field(default=8, ge=0, le=50)
    dedupe_min_words: int = Field(default=2, ge=1, le=10)

    # --- Limits & backpressure ---
    final_retries: int = Field(default=1, ge=0, le=5)
    preview_pause_backlog_ms: int = Field(
        default=10_000, ge=0, description="Pause partials when this much audio awaits finals"
    )
    max_backlog_ms: int = Field(
        default=30_000, ge=1_000, description="Stop accepting audio beyond this backlog"
    )
    max_session_ms: int = Field(default=30 * 60 * 1_000, ge=1_000)
    max_chunk_bytes: int = Field(default=64 * 1024, ge=2)

    @model_validator(mode="after")
    def _check_relationships(self) -> Self:
        if self.onset_min_speech_frames > self.onset_window_frames:
            raise ValueError("onset_min_speech_frames cannot exceed onset_window_frames")
        if self.short_burst_min_frames > self.onset_min_speech_frames:
            raise ValueError("short_burst_min_frames cannot exceed onset_min_speech_frames")
        if not self.pause_enter_ms < self.end_silence_ms <= self.short_utterance_silence_ms:
            raise ValueError(
                "require pause_enter_ms < end_silence_ms <= short_utterance_silence_ms"
            )
        if not self.soft_pause_ms <= self.end_silence_ms:
            raise ValueError("soft_pause_ms must not exceed end_silence_ms")
        if not self.soft_max_utterance_ms < self.max_utterance_ms:
            raise ValueError("soft_max_utterance_ms must be below max_utterance_ms")
        if self.boundary_search_ms >= self.max_utterance_ms:
            raise ValueError("boundary_search_ms must be below max_utterance_ms")
        if self.forced_carry_ms >= self.max_utterance_ms // 2:
            raise ValueError("forced_carry_ms must be well below max_utterance_ms")
        if self.dedupe_min_words > max(self.dedupe_max_words, 1):
            raise ValueError("dedupe_min_words cannot exceed dedupe_max_words")
        return self

    # --- Derived values ---

    @property
    def sample_rate(self) -> int:
        return SAMPLE_RATE

    @property
    def frame_samples(self) -> int:
        return SAMPLE_RATE * self.frame_ms // 1000

    @property
    def frame_bytes(self) -> int:
        return self.frame_samples * 2

    def frames(self, ms: int) -> int:
        """Whole frames covering `ms` (rounded up)."""
        return math.ceil(ms / self.frame_ms)

    def samples(self, ms: int) -> int:
        return SAMPLE_RATE * ms // 1000
