"""Test doubles and audio builders for the real-time pipeline (no model, no network)."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.models.enums import SpeechMode
from app.realtime.config import SAMPLE_RATE, RealtimeConfig
from app.realtime.events import SessionEvent
from app.realtime.scheduler import InferenceScheduler
from app.realtime.session import SessionResult, TranscriptionSession
from app.realtime.vad import FrameClassifier
from app.speech.audio import AudioBuffer
from app.speech.engine import (
    DecodeOptions,
    EngineInfo,
    RawSegment,
    RawTranscription,
    RawWord,
    SpeechEngine,
)
from app.speech.errors import InferenceError
from app.speech.language import DecodingPlan, LanguageDetection
from app.speech.results import TranscriptionResult

# ---------------------------------------------------------------- audio


def voiced(ms: int, *, start_sample: int = 0) -> np.ndarray:
    """Speech-like signal (harmonics + syllable-rate modulation): WebRTC VAD says speech."""
    n = SAMPLE_RATE * ms // 1000
    t = (np.arange(n) + start_sample) / SAMPLE_RATE
    harmonics = sum(0.3 / k * np.sin(2 * np.pi * 140 * k * t) for k in range(1, 12))
    return (harmonics * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))).astype(np.float64)


@dataclass
class Script:
    """Builds a PCM16 recording from ("speech" | "silence", ms) parts."""

    parts: list[tuple[str, int]]
    speech_regions: list[tuple[int, int]] = field(default_factory=list)

    def pcm(self) -> bytes:
        chunks, cursor_ms = [], 0
        self.speech_regions = []
        for kind, ms in self.parts:
            start_sample = SAMPLE_RATE * cursor_ms // 1000
            if kind == "speech":
                chunks.append(voiced(ms, start_sample=start_sample))
                self.speech_regions.append((cursor_ms, cursor_ms + ms))
            else:
                chunks.append(np.zeros(SAMPLE_RATE * ms // 1000))
            cursor_ms += ms
        signal = np.clip(np.concatenate(chunks), -1, 1)
        return bytes((signal * 32767).astype("<i2").tobytes())

    @property
    def duration_ms(self) -> int:
        return sum(ms for _, ms in self.parts)


@dataclass(frozen=True)
class SpokenWord:
    text: str
    start_ms: int
    end_ms: int

    @property
    def mid_ms(self) -> float:
        return (self.start_ms + self.end_ms) / 2


def words_in(regions: Sequence[tuple[int, int]], *, word_ms: int = 300) -> list[SpokenWord]:
    """Lay consecutive words (w0, w1, ...) inside each speech region."""
    words: list[SpokenWord] = []
    for start, end in regions:
        cursor = start
        while cursor + word_ms <= end:
            words.append(SpokenWord(f"w{len(words)}", cursor, cursor + word_ms))
            cursor += word_ms
    return words


# ---------------------------------------------------------------- engine


@dataclass
class Call:
    kind: str  # "partial" | "final"
    mode: SpeechMode
    offset_ms: int
    duration_ms: int
    word_timestamps: bool
    context: str | None


class TimelineEngine(SpeechEngine):
    """A 'perfect recogniser' over a known word timeline.

    A call over audio [offset, offset + duration) returns exactly the words whose midpoint
    lies in that range, with correct word timestamps. Lost or duplicated audio at a cut
    therefore shows up as missing or duplicated words.
    """

    def __init__(
        self,
        words: Sequence[SpokenWord],
        *,
        delay: float = 0.0,
        final_delay: float | None = None,
        fail_finals: int = 0,
        always_fail_finals: bool = False,
        echo_prompt_words: int = 0,
        detection: LanguageDetection | None = None,
        language: str = "hi",
    ) -> None:
        super().__init__()
        self.words = list(words)
        self.delay = delay
        self.final_delay = final_delay if final_delay is not None else delay
        self.fail_finals = fail_finals
        self.always_fail_finals = always_fail_finals
        self.echo_prompt_words = echo_prompt_words
        self.detection = detection or LanguageDetection("hi", 0.9, (("hi", 0.9), ("en", 0.05)))
        self.language = language
        self.calls: list[Call] = []
        self.detect_calls = 0
        self._lock = threading.Lock()
        self._local = threading.local()
        self._active = 0
        self.max_active = 0

    @property
    def info(self) -> EngineInfo:
        return EngineInfo("timeline-fake", "cpu", "int8", 0, 1, True, 0.0)

    def load(self) -> None:
        return None

    def transcribe(
        self,
        audio: AudioBuffer,
        *,
        mode: SpeechMode = SpeechMode.AUTO,
        options: DecodeOptions | None = None,
        offset_ms: int = 0,
    ) -> TranscriptionResult:
        self._local.offset_ms = offset_ms
        self._local.mode = mode
        return super().transcribe(audio, mode=mode, options=options, offset_ms=offset_ms)

    def _transcribe_raw(
        self, audio: AudioBuffer, plan: DecodingPlan, options: DecodeOptions
    ) -> RawTranscription:
        final = options.word_timestamps
        offset = self._local.offset_ms
        duration = audio.duration_ms
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.calls.append(
                Call(
                    "final" if final else "partial",
                    self._local.mode,
                    offset,
                    duration,
                    options.word_timestamps,
                    options.context_prompt,
                )
            )
            should_fail = final and (self.always_fail_finals or self.fail_finals > 0)
            if final and self.fail_finals > 0:
                self.fail_finals -= 1
        try:
            time.sleep(self.final_delay if final else self.delay)
            if should_fail:
                raise InferenceError("simulated decoder failure")
            words = [w for w in self.words if offset <= w.mid_ms < offset + duration]
            raw_words = [
                RawWord(
                    (max(w.start_ms, offset) - offset) / 1000,
                    (min(w.end_ms, offset + duration) - offset) / 1000,
                    f" {w.text}",
                    0.9,
                )
                for w in words
            ]
            if self.echo_prompt_words and options.context_prompt and final:
                echoed = options.context_prompt.split()[-self.echo_prompt_words :]
                raw_words = [RawWord(0.0, 0.0, f" {t}", 0.5) for t in echoed] + raw_words
            if not raw_words:
                return RawTranscription(iter(()), self.language, 1.0)
            segment = RawSegment(
                start=raw_words[0].start,
                end=raw_words[-1].end,
                text="".join(w.word for w in raw_words),
                avg_logprob=-0.2,
                no_speech_prob=0.01,
                compression_ratio=1.2,
                words=tuple(raw_words) if options.word_timestamps else (),
            )
            return RawTranscription(iter([segment]), self.language, 1.0)
        finally:
            with self._lock:
                self._active -= 1

    def _detect_language_raw(self, audio: AudioBuffer) -> LanguageDetection:
        with self._lock:
            self.detect_calls += 1
        return self.detection


# ---------------------------------------------------------------- running a session


@dataclass
class Run:
    session: TranscriptionSession
    result: SessionResult
    events: list[SessionEvent]
    scheduler: InferenceScheduler

    def of(self, kind: type) -> list[Any]:
        return [e for e in self.events if isinstance(e, kind)]


async def run_session(
    pcm: bytes,
    engine: SpeechEngine,
    *,
    config: RealtimeConfig | None = None,
    mode: SpeechMode = SpeechMode.HINDI,
    chunk_ms: int = 250,
    extra_bytes: bytes = b"",
    pace: bool = True,
    classifier: FrameClassifier | None = None,
    concurrency: int = 1,
) -> Run:
    """Feed `pcm` in chunks, then stop. `pace=True` lets inference catch up after each chunk
    (like a real-time stream on a fast enough machine), making runs deterministic."""
    scheduler = InferenceScheduler(concurrency)
    events: list[SessionEvent] = []
    session = TranscriptionSession(
        engine=engine,
        scheduler=scheduler,
        mode=mode,
        config=config,
        classifier=classifier,
        on_event=events.append,
    )
    step = SAMPLE_RATE * chunk_ms // 1000 * 2
    data = pcm + extra_bytes
    for offset in range(0, len(data), step):
        await session.feed(data[offset : offset + step])
        if pace:
            await scheduler.wait_idle()
    result = await session.stop()
    await scheduler.aclose()
    return Run(session, result, events, scheduler)


def final_words(result: SessionResult) -> list[str]:
    return [w for final in result.finals for w in final.text.split()]


async def wait_for(predicate: Any, limit_s: float = 2.0) -> None:
    deadline = time.monotonic() + limit_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)
