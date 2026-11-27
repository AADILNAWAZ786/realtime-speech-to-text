"""Test doubles for the speech engine. No model weights, no network.

`FakeWhisperModel` mimics faster_whisper.WhisperModel's API closely - including returning
a *lazy generator* of segments - so FasterWhisperEngine's real code is exercised.
`FakeSpeechEngine` is a minimal SpeechEngine implementation driven by scripted output.
"""

from __future__ import annotations

import time
import wave
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.speech.audio import TARGET_SAMPLE_RATE, AudioBuffer, validate_float_audio
from app.speech.engine import (
    DecodeOptions,
    EngineInfo,
    RawSegment,
    RawTranscription,
    SpeechEngine,
)
from app.speech.language import DecodingPlan, LanguageDetection

# ---------- Canned "recognitions" for synthetic audio ----------

ENGLISH_SEGMENTS = [
    (0.0, 1.4, " Let's start the meeting."),
    (1.4, 2.9, " The report is ready."),
]
HINDI_SEGMENTS = [
    (0.0, 1.5, " आज मौसम बहुत अच्छा है।"),
    (1.5, 3.0, " हम शाम को मिलेंगे।"),
]
HINGLISH_SEGMENTS = [  # typical language="hi" output: Hindi in Devanagari, English in Latin
    (0.0, 1.6, " आज की meeting cancel हो गई।"),
    (1.6, 3.0, " कल फिर call करते हैं, okay?"),
]
CANNED_BY_LANGUAGE: dict[str | None, list[tuple[float, float, str]]] = {
    "en": ENGLISH_SEGMENTS,
    "hi": HINGLISH_SEGMENTS,
    None: ENGLISH_SEGMENTS,
}


def make_segment(start: float, end: float, text: str, **extra: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "start": start,
        "end": end,
        "text": text,
        "avg_logprob": -0.25,
        "no_speech_prob": 0.01,
        "compression_ratio": 1.3,
        "words": None,
    }
    values.update(extra)
    return SimpleNamespace(**values)


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel."""

    def __init__(
        self,
        *args: Any,
        segments: Sequence[SimpleNamespace] | None = None,
        by_language: dict[str | None, list[tuple[float, float, str]]] | None = None,
        detected_language: str = "en",
        detected_probability: float = 0.93,
        all_language_probs: list[tuple[str, float]] | None = None,
        multilingual: bool = True,
        fail_at_segment: int | None = None,
        segment_delay: float = 0.0,
        **kwargs: Any,
    ) -> None:
        self.init_args = args
        self.init_kwargs = kwargs
        self.model = SimpleNamespace(is_multilingual=multilingual)
        self._segments = segments
        self._by_language = by_language if by_language is not None else CANNED_BY_LANGUAGE
        self.detected_language = detected_language
        self.detected_probability = detected_probability
        self.all_language_probs = all_language_probs or [
            (detected_language, detected_probability),
            ("hi" if detected_language != "hi" else "en", 1 - detected_probability),
        ]
        self.fail_at_segment = fail_at_segment
        self.segment_delay = segment_delay
        self.transcribe_calls: list[dict[str, Any]] = []
        self.consumed = 0
        self.generator_finished = False

    def _segments_for(self, language: str | None) -> list[SimpleNamespace]:
        if self._segments is not None:
            return list(self._segments)
        return [make_segment(*seg) for seg in self._by_language.get(language, [])]

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterator[Any], Any]:
        self.transcribe_calls.append({"audio": audio, **kwargs})
        language = kwargs.get("language")
        forced = language is not None
        info = SimpleNamespace(
            language=language if forced else self.detected_language,
            language_probability=1.0 if forced else self.detected_probability,
            all_language_probs=None if forced else self.all_language_probs,
        )
        segments = self._segments_for(language)

        def generate() -> Iterator[Any]:
            # Like faster-whisper: decoding happens while the generator is iterated.
            for index, segment in enumerate(segments):
                if self.fail_at_segment == index:
                    raise RuntimeError("simulated decoder failure")
                if self.segment_delay:
                    time.sleep(self.segment_delay)
                self.consumed += 1
                yield segment
            self.generator_finished = True

        return generate(), info

    def detect_language(self, audio: Any = None, **kwargs: Any) -> tuple[str, float, Any]:
        return self.detected_language, self.detected_probability, list(self.all_language_probs)


@dataclass
class ModelFactorySpy:
    """Callable model factory that records calls and returns FakeWhisperModel instances."""

    model_kwargs: dict[str, Any] = field(default_factory=dict)
    error: Exception | None = None
    delay: float = 0.0
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    instances: list[FakeWhisperModel] = field(default_factory=list)

    def __call__(self, *args: Any, **kwargs: Any) -> FakeWhisperModel:
        self.calls.append((args, kwargs))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        model = FakeWhisperModel(*args, **{**kwargs, **self.model_kwargs})
        self.instances.append(model)
        return model


class FakeSpeechEngine(SpeechEngine):
    """Minimal SpeechEngine driven by a scripted RawTranscription."""

    def __init__(self, raw: RawTranscription, detection: LanguageDetection | None = None) -> None:
        super().__init__()
        self.raw = raw
        self.detection = detection or LanguageDetection("en", 0.9, (("en", 0.9),))
        self.loaded = False
        self.calls: list[tuple[DecodingPlan, DecodeOptions]] = []

    @property
    def info(self) -> EngineInfo:
        return EngineInfo("fake", "cpu", "int8", 0, 1, self.loaded, 0.0 if self.loaded else None)

    def load(self) -> None:
        self.loaded = True

    def _transcribe_raw(
        self, audio: AudioBuffer, plan: DecodingPlan, options: DecodeOptions
    ) -> RawTranscription:
        self.calls.append((plan, options))
        return self.raw

    def _detect_language_raw(self, audio: AudioBuffer) -> LanguageDetection:
        return self.detection


def raw_segments(*items: tuple[float, float, str]) -> list[RawSegment]:
    return [RawSegment(start, end, text, -0.2, 0.01, 1.2) for start, end, text in items]


# ---------- Synthetic audio ----------


def tone(seconds: float = 1.0, *, freq: float = 220.0, amplitude: float = 0.3) -> AudioBuffer:
    t = np.arange(int(seconds * TARGET_SAMPLE_RATE), dtype=np.float32) / TARGET_SAMPLE_RATE
    return validate_float_audio((amplitude * np.sin(2 * np.pi * freq * t)).astype(np.float32))


def pcm16_bytes(seconds: float = 1.0, *, freq: float = 220.0, channels: int = 1) -> bytes:
    n = int(seconds * TARGET_SAMPLE_RATE)
    t = np.arange(n) / TARGET_SAMPLE_RATE
    mono = (0.3 * 32767 * np.sin(2 * np.pi * freq * t)).astype("<i2")
    return np.repeat(mono, channels).astype("<i2").tobytes()


def write_wav(
    path: Path,
    *,
    seconds: float = 1.0,
    sample_rate: int = TARGET_SAMPLE_RATE,
    channels: int = 1,
    sample_width: int = 2,
) -> Path:
    n = int(seconds * sample_rate)
    t = np.arange(n) / sample_rate
    wave_data = np.sin(2 * np.pi * 220 * t) * 0.3
    if sample_width == 2:
        frames = np.repeat((wave_data * 32767).astype("<i2"), channels).tobytes()
    else:  # 8-bit unsigned PCM
        frames = np.repeat(((wave_data + 1) * 127.5).astype(np.uint8), channels).tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(sample_rate)
        wav.writeframes(frames)
    return path
