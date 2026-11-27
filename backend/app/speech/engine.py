"""The speech engine interface.

`SpeechEngine.transcribe()` is implemented once, here, for every backend:
validate input -> build the decoding plan -> run the backend -> **fully consume** the
backend's (possibly lazy) segment iterator -> normalise timestamps -> immutable result.

faster-whisper's `transcribe()` returns a *generator*: decoding happens while it is
iterated. Returning before it is exhausted would report a near-zero processing time and
defer the real work (and its errors) to whoever iterates later. The consumption therefore
happens inside the timed, error-wrapped block below.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.models.enums import SpeechMode
from app.speech.audio import AudioBuffer, validate_float_audio
from app.speech.config import MixedStrategy
from app.speech.errors import InferenceError, SpeechError
from app.speech.language import DecodingPlan, LanguageDetection, plan_for_mode
from app.speech.results import (
    TranscriptionResult,
    TranscriptSegment,
    WordTiming,
    seconds_to_ms,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DecodeOptions:
    """Per-call decoding settings. Use `partial()` / `final()` for the two live passes."""

    beam_size: int = 5
    temperature: tuple[float, ...] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)
    condition_on_previous_text: bool = False  # avoids error compounding / loops
    context_prompt: str | None = None  # previous final text, appended to the style prompt
    vad_filter: bool = False  # faster-whisper's built-in Silero VAD (offline, per call)
    vad_min_silence_ms: int = 500
    word_timestamps: bool = False
    no_speech_threshold: float = 0.6
    log_prob_threshold: float = -1.0
    compression_ratio_threshold: float = 2.4
    max_new_tokens: int | None = None
    hotwords: str | None = None

    def __post_init__(self) -> None:
        if self.beam_size < 1:
            raise ValueError("beam_size must be >= 1")
        if not self.temperature:
            raise ValueError("temperature must contain at least one value")
        if self.vad_min_silence_ms < 0:
            raise ValueError("vad_min_silence_ms must be >= 0")

    @classmethod
    def partial(cls, context_prompt: str | None = None) -> DecodeOptions:
        """Fast, provisional pass (live preview)."""
        return cls(
            beam_size=1,
            temperature=(0.0,),
            context_prompt=context_prompt,
            max_new_tokens=120,
        )

    @classmethod
    def final(cls, context_prompt: str | None = None) -> DecodeOptions:
        """Higher-quality pass for committed text, with word timings and VAD trimming."""
        return cls(
            beam_size=5,
            context_prompt=context_prompt,
            vad_filter=True,
            word_timestamps=True,
        )


@dataclass(frozen=True, slots=True)
class RawWord:
    start: float
    end: float
    word: str
    probability: float


@dataclass(frozen=True, slots=True)
class RawSegment:
    """Backend-neutral segment. Times are seconds relative to the audio passed in."""

    start: float
    end: float
    text: str
    avg_logprob: float
    no_speech_prob: float
    compression_ratio: float
    words: tuple[RawWord, ...] = ()


@dataclass(frozen=True, slots=True)
class RawTranscription:
    segments: Iterable[RawSegment]  # may be lazy: consumed by SpeechEngine.transcribe
    language: str | None
    language_probability: float | None
    all_language_probs: tuple[tuple[str, float], ...] = field(default=())


@dataclass(frozen=True, slots=True)
class EngineInfo:
    model_name: str
    device: str
    compute_type: str
    cpu_threads: int
    num_workers: int
    loaded: bool
    load_ms: float | None


class SpeechEngine(ABC):
    """Base class for speech-to-text backends."""

    def __init__(
        self,
        *,
        mixed_strategy: MixedStrategy = MixedStrategy.HINDI_PROMPTED,
        mixed_prompt: str | None = None,
        max_audio_seconds: float | None = None,
    ) -> None:
        self._mixed_strategy = mixed_strategy
        self._mixed_prompt = mixed_prompt
        self._max_audio_seconds = max_audio_seconds

    # ----- backend hooks -----

    @property
    @abstractmethod
    def info(self) -> EngineInfo: ...

    @abstractmethod
    def load(self) -> None:
        """Load the model (idempotent, thread-safe). Called lazily on first use."""

    @abstractmethod
    def _transcribe_raw(
        self, audio: AudioBuffer, plan: DecodingPlan, options: DecodeOptions
    ) -> RawTranscription: ...

    @abstractmethod
    def _detect_language_raw(self, audio: AudioBuffer) -> LanguageDetection: ...

    # ----- public API -----

    @property
    def model_name(self) -> str:
        return self.info.model_name

    def plan(self, mode: SpeechMode) -> DecodingPlan:
        return plan_for_mode(
            mode, mixed_strategy=self._mixed_strategy, mixed_prompt=self._mixed_prompt
        )

    def transcribe(
        self,
        audio: AudioBuffer,
        *,
        mode: SpeechMode = SpeechMode.AUTO,
        options: DecodeOptions | None = None,
        offset_ms: int = 0,
    ) -> TranscriptionResult:
        """Transcribe one audio chunk. Blocking and CPU-bound: call from a worker thread."""
        if offset_ms < 0:
            raise ValueError("offset_ms must be >= 0")
        audio = self._validated(audio)
        options = options or DecodeOptions()
        plan = self.plan(mode)
        self.load()

        started = time.perf_counter()
        try:
            raw = self._transcribe_raw(audio, plan, options)
            # Exhaust the (lazy) iterator here, inside the timer and the error wrapper.
            raw_segments = list(raw.segments)
        except SpeechError:
            raise
        except Exception as exc:
            raise InferenceError(f"Transcription failed: {type(exc).__name__}: {exc}") from exc
        processing_ms = (time.perf_counter() - started) * 1000

        duration = audio.duration_ms
        # Drop empty segments first, then number them, so indexes are contiguous.
        spoken = [seg for seg in raw_segments if seg.text.strip()]
        segments = tuple(
            self._normalise_segment(index, seg, offset_ms=offset_ms, audio_duration_ms=duration)
            for index, seg in enumerate(spoken)
        )
        info = self.info
        result = TranscriptionResult(
            segments=segments,
            language=raw.language,
            language_probability=raw.language_probability,
            language_forced=plan.language is not None,
            speech_mode=mode,
            decode_strategy=plan.strategy,
            model_name=info.model_name,
            device=info.device,
            compute_type=info.compute_type,
            audio_duration_ms=duration,
            processing_ms=processing_ms,
            offset_ms=offset_ms,
            all_language_probs=raw.all_language_probs,
        )
        logger.debug(
            "Transcribed audio",
            extra={
                "audio_ms": duration,
                "processing_ms": round(processing_ms, 1),
                "segments": len(result.segments),
                "strategy": plan.strategy,
            },
        )
        return result

    def detect_language(self, audio: AudioBuffer) -> LanguageDetection:
        audio = self._validated(audio)
        self.load()
        try:
            return self._detect_language_raw(audio)
        except SpeechError:
            raise
        except Exception as exc:
            raise InferenceError(f"Language detection failed: {type(exc).__name__}: {exc}") from exc

    # ----- helpers -----

    def _validated(self, audio: AudioBuffer) -> AudioBuffer:
        if not isinstance(audio, AudioBuffer):
            raise TypeError("audio must be an AudioBuffer (use decode_pcm16 / load_audio_file)")
        # Re-check: AudioBuffer is a plain dataclass and could be built by hand.
        return validate_float_audio(
            audio.samples,
            sample_rate=audio.sample_rate,
            max_duration_seconds=self._max_audio_seconds,
        )

    @staticmethod
    def _normalise_segment(
        index: int, seg: RawSegment, *, offset_ms: int, audio_duration_ms: int
    ) -> TranscriptSegment:
        start_ms = seconds_to_ms(
            seg.start, offset_ms=offset_ms, audio_duration_ms=audio_duration_ms
        )
        end_ms = seconds_to_ms(seg.end, offset_ms=offset_ms, audio_duration_ms=audio_duration_ms)
        words = []
        for word in seg.words:
            w_start = seconds_to_ms(
                word.start, offset_ms=offset_ms, audio_duration_ms=audio_duration_ms
            )
            w_end = seconds_to_ms(
                word.end, offset_ms=offset_ms, audio_duration_ms=audio_duration_ms
            )
            words.append(WordTiming(w_start, max(w_end, w_start), word.word, word.probability))
        return TranscriptSegment(
            index=index,
            start_ms=start_ms,
            end_ms=max(end_ms, start_ms),  # never negative duration
            raw_text=seg.text,
            avg_logprob=seg.avg_logprob,
            no_speech_prob=seg.no_speech_prob,
            compression_ratio=seg.compression_ratio,
            words=tuple(words),
        )
