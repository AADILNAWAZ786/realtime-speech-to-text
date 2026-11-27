"""faster-whisper (CTranslate2) backend.

- Multilingual models only: English-only names are rejected by SpeechEngineConfig, and any
  model (including local paths / repo ids) is checked again after loading.
- CPU runs int8 by default; CUDA runs float16 by default. Asking for CUDA without a GPU
  fails loudly instead of silently falling back to a much slower CPU run.
- Loading is lazy (first use) and thread-safe (double-checked lock), so concurrent first
  requests load the model exactly once.
- faster-whisper / ctranslate2 are imported only when a model is loaded, keeping app and
  test imports fast.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any, Protocol

from app.speech.audio import AudioBuffer
from app.speech.config import SpeechEngineConfig
from app.speech.engine import (
    DecodeOptions,
    EngineInfo,
    RawSegment,
    RawTranscription,
    RawWord,
    SpeechEngine,
)
from app.speech.errors import ModelLoadError
from app.speech.language import DecodingPlan, LanguageDetection

logger = logging.getLogger(__name__)

CPU_COMPUTE_TYPES = frozenset({"int8", "int8_float32", "int16", "float32"})
CUDA_COMPUTE_TYPES = frozenset(
    {
        "float16",
        "int8_float16",
        "int8",
        "int8_float32",
        "int16",
        "float32",
        "bfloat16",
        "int8_bfloat16",
    }
)
DEFAULT_COMPUTE_TYPE = {"cpu": "int8", "cuda": "float16"}


class WhisperModelLike(Protocol):
    """The subset of faster_whisper.WhisperModel this engine uses."""

    def transcribe(self, audio: Any, **kwargs: Any) -> tuple[Iterable[Any], Any]: ...

    def detect_language(self, audio: Any = ..., **kwargs: Any) -> tuple[str, float, Any]: ...


ModelFactory = Callable[..., WhisperModelLike]


def _default_model_factory(*args: Any, **kwargs: Any) -> WhisperModelLike:
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ModelLoadError(
            "faster-whisper is not installed (pip install -r requirements.txt)",
            code="dependency_missing",
        ) from exc
    model: WhisperModelLike = WhisperModel(*args, **kwargs)
    return model


def _default_cuda_device_count() -> int:
    try:
        import ctranslate2
    except ImportError:  # pragma: no cover - pinned dependency
        return 0
    count: int = ctranslate2.get_cuda_device_count()
    return count


def resolve_device_and_compute(
    config: SpeechEngineConfig, cuda_device_count: Callable[[], int]
) -> tuple[str, str]:
    """Pick the concrete device and compute type, or raise ModelLoadError."""
    if config.device == "auto":
        device = "cuda" if cuda_device_count() > 0 else "cpu"
    elif config.device == "cuda":
        if cuda_device_count() <= 0:
            raise ModelLoadError(
                "device=cuda was requested but no CUDA device is available "
                "(check the NVIDIA driver / CUDA libraries, or use device=cpu)",
                code="cuda_unavailable",
            )
        device = "cuda"
    else:
        device = "cpu"

    compute_type = (
        DEFAULT_COMPUTE_TYPE[device] if config.compute_type == "auto" else config.compute_type
    )
    allowed = CPU_COMPUTE_TYPES if device == "cpu" else CUDA_COMPUTE_TYPES
    if compute_type not in allowed:
        raise ModelLoadError(
            f"compute_type={compute_type!r} is not supported on {device}; "
            f"choose one of: {', '.join(sorted(allowed))}",
            code="unsupported_compute_type",
        )
    return device, compute_type


class FasterWhisperEngine(SpeechEngine):
    def __init__(
        self,
        config: SpeechEngineConfig | None = None,
        *,
        model_factory: ModelFactory | None = None,
        cuda_device_count: Callable[[], int] | None = None,
    ) -> None:
        self.config = config or SpeechEngineConfig()
        super().__init__(
            mixed_strategy=self.config.mixed_strategy,
            mixed_prompt=self.config.mixed_prompt,
            max_audio_seconds=self.config.max_audio_seconds,
        )
        self._model_factory = model_factory or _default_model_factory
        self._cuda_device_count = cuda_device_count or _default_cuda_device_count
        self._lock = threading.Lock()
        self._model: WhisperModelLike | None = None
        self._device: str | None = None
        self._compute_type: str | None = None
        self._load_ms: float | None = None

    # ----- loading -----

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        if self._model is not None:  # fast path, no lock
            return
        with self._lock:
            if self._model is not None:  # another thread finished loading meanwhile
                return
            device, compute_type = resolve_device_and_compute(self.config, self._cuda_device_count)
            started = time.perf_counter()
            try:
                model = self._model_factory(
                    self.config.model,
                    device=device,
                    device_index=self.config.device_index,
                    compute_type=compute_type,
                    cpu_threads=self.config.cpu_threads,
                    num_workers=self.config.num_workers,
                    download_root=(
                        str(self.config.download_root) if self.config.download_root else None
                    ),
                    local_files_only=self.config.local_files_only,
                )
            except ModelLoadError:
                raise
            except Exception as exc:
                hint = (
                    " The model is not in the local cache and local_files_only=True "
                    "forbids downloading it."
                    if self.config.local_files_only
                    else ""
                )
                raise ModelLoadError(
                    f"Failed to load Whisper model {self.config.model!r} on "
                    f"{device}/{compute_type}: {type(exc).__name__}: "
                    f"{str(exc).rstrip('.')}.{hint}"
                ) from exc

            if not self._is_multilingual(model):
                raise ModelLoadError(
                    f"Model {self.config.model!r} is English-only; a multilingual model is "
                    "required for Hindi and Hinglish",
                    code="model_not_multilingual",
                )
            self._load_ms = (time.perf_counter() - started) * 1000
            self._device, self._compute_type = device, compute_type
            self._model = model
            logger.info(
                "Loaded Whisper model %s on %s/%s in %.0f ms",
                self.config.model,
                device,
                compute_type,
                self._load_ms,
            )

    @staticmethod
    def _is_multilingual(model: WhisperModelLike) -> bool:
        inner = getattr(model, "model", None)
        flag = getattr(inner, "is_multilingual", None)
        return True if flag is None else bool(flag)  # unknown -> trust the config check

    def _require_model(self) -> WhisperModelLike:
        self.load()
        assert self._model is not None  # noqa: S101 - load() either sets it or raises
        return self._model

    @property
    def info(self) -> EngineInfo:
        return EngineInfo(
            model_name=self.config.model,
            device=self._device or self.config.device,
            compute_type=self._compute_type or self.config.compute_type,
            cpu_threads=self.config.cpu_threads,
            num_workers=self.config.num_workers,
            loaded=self.is_loaded,
            load_ms=self._load_ms,
        )

    # ----- inference -----

    def _transcribe_raw(
        self, audio: AudioBuffer, plan: DecodingPlan, options: DecodeOptions
    ) -> RawTranscription:
        model = self._require_model()
        kwargs: dict[str, Any] = {
            "language": plan.language,
            "task": "transcribe",  # never translate
            "multilingual": plan.multilingual,
            "initial_prompt": plan.build_prompt(options.context_prompt),
            "beam_size": options.beam_size,
            "temperature": list(options.temperature),
            "condition_on_previous_text": options.condition_on_previous_text,
            "vad_filter": options.vad_filter,
            "word_timestamps": options.word_timestamps,
            "without_timestamps": False,
            "no_speech_threshold": options.no_speech_threshold,
            "log_prob_threshold": options.log_prob_threshold,
            "compression_ratio_threshold": options.compression_ratio_threshold,
            "max_new_tokens": options.max_new_tokens,
            "hotwords": options.hotwords,
        }
        if options.vad_filter:
            kwargs["vad_parameters"] = {"min_silence_duration_ms": options.vad_min_silence_ms}

        segments, info = model.transcribe(audio.samples, **kwargs)
        all_probs = getattr(info, "all_language_probs", None) or ()
        return RawTranscription(
            segments=self._convert_segments(segments),  # lazy; consumed by the base class
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            all_language_probs=tuple((str(c), float(p)) for c, p in all_probs),
        )

    @staticmethod
    def _convert_segments(segments: Iterable[Any]) -> Iterator[RawSegment]:
        for seg in segments:
            words = tuple(
                RawWord(float(w.start), float(w.end), str(w.word), float(w.probability))
                for w in (getattr(seg, "words", None) or ())
            )
            yield RawSegment(
                start=float(seg.start),
                end=float(seg.end),
                text=str(seg.text),
                avg_logprob=float(seg.avg_logprob),
                no_speech_prob=float(seg.no_speech_prob),
                compression_ratio=float(seg.compression_ratio),
                words=words,
            )

    def _detect_language_raw(self, audio: AudioBuffer) -> LanguageDetection:
        model = self._require_model()
        language, probability, all_probs = model.detect_language(
            audio=audio.samples, language_detection_segments=1
        )
        ranked = sorted(((str(c), float(p)) for c, p in (all_probs or ())), key=lambda x: -x[1])
        return LanguageDetection(
            language=str(language), probability=float(probability), top=tuple(ranked[:10])
        )
