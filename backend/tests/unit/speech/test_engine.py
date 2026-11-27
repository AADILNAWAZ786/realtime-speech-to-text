"""FasterWhisperEngine + SpeechEngine base behaviour, using FakeWhisperModel (no weights)."""

from __future__ import annotations

import threading
from typing import Any

import numpy as np
import pytest

from app.models.enums import SpeechMode
from app.speech.audio import AudioBuffer, decode_pcm16
from app.speech.config import MixedStrategy, SpeechEngineConfig
from app.speech.engine import DecodeOptions, RawSegment, RawTranscription, RawWord
from app.speech.errors import (
    AudioTooLongError,
    EmptyAudioError,
    InferenceError,
    InvalidAudioValuesError,
    ModelLoadError,
)
from app.speech.faster_whisper_engine import FasterWhisperEngine, resolve_device_and_compute
from app.speech.language import DEFAULT_MIXED_PROMPT
from tests.unit.speech.fakes import (
    ENGLISH_SEGMENTS,
    HINDI_SEGMENTS,
    HINGLISH_SEGMENTS,
    FakeSpeechEngine,
    FakeWhisperModel,
    ModelFactorySpy,
    make_segment,
    pcm16_bytes,
    raw_segments,
    tone,
)


def _engine(
    spy: ModelFactorySpy | None = None, *, cuda: int = 0, **config: Any
) -> tuple[FasterWhisperEngine, ModelFactorySpy]:
    spy = spy or ModelFactorySpy()
    engine = FasterWhisperEngine(
        SpeechEngineConfig(**config), model_factory=spy, cuda_device_count=lambda: cuda
    )
    return engine, spy


def _model(spy: ModelFactorySpy) -> FakeWhisperModel:
    assert len(spy.instances) == 1
    return spy.instances[0]


# ---------- Initialisation ----------


def test_model_is_loaded_lazily_on_first_use() -> None:
    engine, spy = _engine()
    assert not engine.is_loaded
    assert spy.calls == []
    engine.transcribe(tone(1.0), mode=SpeechMode.ENGLISH)
    assert engine.is_loaded
    assert len(spy.calls) == 1
    engine.transcribe(tone(1.0), mode=SpeechMode.ENGLISH)
    assert len(spy.calls) == 1  # loaded once


def test_cpu_int8_model_configuration_passed_to_faster_whisper() -> None:
    engine, spy = _engine(
        model="base", cpu_threads=2, num_workers=2, download_root="/srv/m", local_files_only=True
    )
    engine.load()
    args, kwargs = spy.calls[0]
    assert args == ("base",)
    assert kwargs == {
        "device": "cpu",
        "device_index": 0,
        "compute_type": "int8",
        "cpu_threads": 2,
        "num_workers": 2,
        "download_root": "/srv/m",
        "local_files_only": True,
    }
    info = engine.info
    assert (info.device, info.compute_type, info.loaded) == ("cpu", "int8", True)
    assert info.load_ms is not None


def test_gpu_float16_when_cuda_requested_and_available() -> None:
    engine, spy = _engine(device="cuda", cuda=1)
    engine.load()
    assert spy.calls[0][1]["device"] == "cuda"
    assert spy.calls[0][1]["compute_type"] == "float16"


@pytest.mark.parametrize(
    ("device", "compute", "cuda", "expected"),
    [
        ("cpu", "auto", 1, ("cpu", "int8")),
        ("auto", "auto", 0, ("cpu", "int8")),
        ("auto", "auto", 2, ("cuda", "float16")),
        ("cuda", "int8_float16", 1, ("cuda", "int8_float16")),
        ("cpu", "float32", 0, ("cpu", "float32")),
    ],
)
def test_device_and_compute_resolution(
    device: str, compute: str, cuda: int, expected: tuple[str, str]
) -> None:
    config = SpeechEngineConfig(device=device, compute_type=compute)
    assert resolve_device_and_compute(config, lambda: cuda) == expected


def test_cuda_requested_without_gpu_fails_clearly() -> None:
    engine, spy = _engine(device="cuda", cuda=0)
    with pytest.raises(ModelLoadError, match="no CUDA device") as exc_info:
        engine.load()
    assert exc_info.value.code == "cuda_unavailable"
    assert spy.calls == []  # no silent CPU fallback


def test_float16_on_cpu_rejected() -> None:
    engine, _ = _engine(compute_type="float16")
    with pytest.raises(ModelLoadError, match="not supported on cpu") as exc_info:
        engine.load()
    assert exc_info.value.code == "unsupported_compute_type"


def test_model_load_failure_is_wrapped() -> None:
    engine, _ = _engine(ModelFactorySpy(error=OSError("disk read error")))
    with pytest.raises(ModelLoadError, match=r"'small' on cpu/int8: OSError: disk read error"):
        engine.load()
    assert not engine.is_loaded


def test_load_failure_with_local_files_only_explains_why() -> None:
    engine, _ = _engine(
        ModelFactorySpy(error=FileNotFoundError("snapshot missing")), local_files_only=True
    )
    with pytest.raises(ModelLoadError, match="forbids downloading"):
        engine.load()


def test_failed_load_can_be_retried() -> None:
    spy = ModelFactorySpy(error=RuntimeError("transient"))
    engine, _ = _engine(spy)
    with pytest.raises(ModelLoadError):
        engine.load()
    spy.error = None
    engine.load()
    assert engine.is_loaded


def test_english_only_model_detected_after_load() -> None:
    """A custom path/repo id passes config validation; the loaded model is checked too."""
    engine, _ = _engine(ModelFactorySpy(model_kwargs={"multilingual": False}), model="/m/custom")
    with pytest.raises(ModelLoadError, match="English-only") as exc_info:
        engine.load()
    assert exc_info.value.code == "model_not_multilingual"
    assert not engine.is_loaded


def test_concurrent_first_use_loads_exactly_once() -> None:
    engine, spy = _engine(ModelFactorySpy(delay=0.05))
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            engine.transcribe(tone(0.5), mode=SpeechMode.ENGLISH)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(spy.calls) == 1


# ---------- Language modes: English / Hindi / Hinglish ----------


def test_english_audio() -> None:
    engine, spy = _engine()
    result = engine.transcribe(tone(3.0), mode=SpeechMode.ENGLISH)
    call = _model(spy).transcribe_calls[0]
    assert call["language"] == "en"
    assert call["task"] == "transcribe"
    assert call["initial_prompt"] is None
    assert result.text == "Let's start the meeting. The report is ready."
    assert (result.language, result.language_forced) == ("en", True)
    assert result.decode_strategy == "en"


def test_hindi_audio_preserves_devanagari_exactly() -> None:
    engine, spy = _engine(ModelFactorySpy(model_kwargs={"by_language": {"hi": HINDI_SEGMENTS}}))
    result = engine.transcribe(tone(3.0), mode=SpeechMode.HINDI)
    assert _model(spy).transcribe_calls[0]["language"] == "hi"
    assert result.text == "आज मौसम बहुत अच्छा है। हम शाम को मिलेंगे।"
    assert [seg.raw_text for seg in result.segments] == [text for _, _, text in HINDI_SEGMENTS]
    assert result.language == "hi"


def test_hinglish_audio_uses_mixed_plan_and_keeps_mixed_script() -> None:
    engine, spy = _engine()
    result = engine.transcribe(
        tone(3.0), mode=SpeechMode.MIXED, options=DecodeOptions(context_prompt="पिछली बात")
    )
    call = _model(spy).transcribe_calls[0]
    assert call["language"] == "hi"
    assert call["multilingual"] is False
    assert call["initial_prompt"] == f"{DEFAULT_MIXED_PROMPT} पिछली बात"
    assert result.text == "आज की meeting cancel हो गई। कल फिर call करते हैं, okay?"
    assert result.decode_strategy == "mixed:hi_prompted"
    assert result.speech_mode is SpeechMode.MIXED


def test_hinglish_multilingual_strategy() -> None:
    engine, spy = _engine(mixed_strategy=MixedStrategy.MULTILINGUAL)
    result = engine.transcribe(tone(1.0), mode=SpeechMode.MIXED)
    call = _model(spy).transcribe_calls[0]
    assert (call["language"], call["multilingual"]) == (None, True)
    assert result.language_forced is False
    assert result.decode_strategy == "mixed:multilingual"


def test_auto_mode_reports_detected_language() -> None:
    spy = ModelFactorySpy(
        model_kwargs={
            "detected_language": "hi",
            "detected_probability": 0.81,
            "all_language_probs": [("hi", 0.81), ("ur", 0.12), ("en", 0.05)],
            "by_language": {None: HINGLISH_SEGMENTS},
        }
    )
    engine, spy = _engine(spy)
    result = engine.transcribe(tone(3.0), mode=SpeechMode.AUTO)
    assert _model(spy).transcribe_calls[0]["language"] is None
    assert (result.language, result.language_probability) == ("hi", 0.81)
    assert result.language_forced is False
    assert result.all_language_probs[0] == ("hi", 0.81)


def test_detect_language() -> None:
    spy = ModelFactorySpy(
        model_kwargs={
            "detected_language": "hi",
            "detected_probability": 0.6,
            "all_language_probs": [("en", 0.3), ("hi", 0.6), ("ur", 0.1)],
        }
    )
    engine, _ = _engine(spy)
    detection = engine.detect_language(tone(2.0))
    assert detection.language == "hi"
    assert detection.top[0] == ("hi", 0.6)  # sorted, most likely first
    assert detection.probability_of("hi", "ur") == pytest.approx(0.7)


def test_decode_options_mapped_to_faster_whisper() -> None:
    engine, spy = _engine()
    engine.transcribe(tone(1.0), mode=SpeechMode.ENGLISH, options=DecodeOptions.final())
    final = _model(spy).transcribe_calls[0]
    assert final["beam_size"] == 5
    assert final["vad_filter"] is True
    assert final["vad_parameters"] == {"min_silence_duration_ms": 500}
    assert final["word_timestamps"] is True
    assert final["condition_on_previous_text"] is False

    engine.transcribe(tone(1.0), mode=SpeechMode.ENGLISH, options=DecodeOptions.partial())
    partial = _model(spy).transcribe_calls[1]
    assert partial["beam_size"] == 1
    assert partial["temperature"] == [0.0]
    assert partial["vad_filter"] is False
    assert "vad_parameters" not in partial
    assert partial["max_new_tokens"] == 120


def test_invalid_decode_options() -> None:
    with pytest.raises(ValueError):
        DecodeOptions(beam_size=0)
    with pytest.raises(ValueError):
        DecodeOptions(temperature=())


# ---------- Generator consumption ----------


def test_generator_fully_consumed_before_returning() -> None:
    spy = ModelFactorySpy(model_kwargs={"segment_delay": 0.05})
    engine, spy = _engine(spy)
    result = engine.transcribe(tone(3.0), mode=SpeechMode.ENGLISH)
    model = _model(spy)
    assert model.generator_finished
    assert model.consumed == len(ENGLISH_SEGMENTS)
    assert len(result.segments) == len(ENGLISH_SEGMENTS)
    # Decoding time inside the generator is part of the measured processing time.
    assert result.processing_ms >= 0.05 * len(ENGLISH_SEGMENTS) * 1000 * 0.9


def test_error_during_generator_consumption_raises_inference_error() -> None:
    engine, _ = _engine(ModelFactorySpy(model_kwargs={"fail_at_segment": 1}))
    with pytest.raises(InferenceError, match="RuntimeError: simulated decoder failure"):
        engine.transcribe(tone(3.0), mode=SpeechMode.ENGLISH)


def test_base_engine_consumes_any_lazy_iterable() -> None:
    consumed: list[int] = []

    def lazy() -> Any:
        for i, seg in enumerate(raw_segments((0, 1, " one"), (1, 2, " two"))):
            consumed.append(i)
            yield seg

    engine = FakeSpeechEngine(RawTranscription(lazy(), "en", 1.0))
    result = engine.transcribe(tone(2.0), mode=SpeechMode.ENGLISH)
    assert consumed == [0, 1]
    assert result.text == "one two"
    assert engine.loaded


# ---------- Timestamps ----------


def test_timestamps_in_ms_relative_to_whole_recording() -> None:
    segments = [make_segment(0.0, 1.2345, " a"), make_segment(1.2345, 2.5, " b")]
    engine, _ = _engine(ModelFactorySpy(model_kwargs={"segments": segments}))
    result = engine.transcribe(tone(3.0), mode=SpeechMode.ENGLISH, offset_ms=12_500)
    assert [(s.start_ms, s.end_ms) for s in result.segments] == [
        (12_500, 13_734),
        (13_734, 15_000),
    ]
    assert result.offset_ms == 12_500


def test_timestamps_clamped_to_audio_and_never_negative() -> None:
    segments = [
        make_segment(-0.2, 0.5, " starts early"),
        make_segment(0.9, 0.7, " ends before start"),
        make_segment(1.5, 9.9, " overshoots"),
    ]
    engine, _ = _engine(ModelFactorySpy(model_kwargs={"segments": segments}))
    result = engine.transcribe(tone(2.0), mode=SpeechMode.ENGLISH, offset_ms=1000)
    assert [(s.start_ms, s.end_ms) for s in result.segments] == [
        (1000, 1500),
        (1900, 1900),
        (2500, 3000),
    ]
    assert all(seg.duration_ms >= 0 for seg in result.segments)


def test_word_timestamps_offset_and_clamped() -> None:
    words = [
        type("W", (), {"start": 0.1, "end": 0.4, "word": " Hello", "probability": 0.9})(),
        type("W", (), {"start": 0.5, "end": 5.0, "word": " world", "probability": 0.8})(),
    ]
    engine, _ = _engine(
        ModelFactorySpy(
            model_kwargs={"segments": [make_segment(0, 1, " Hello world", words=words)]}
        )
    )
    result = engine.transcribe(tone(1.0), mode=SpeechMode.ENGLISH, offset_ms=2000)
    timings = [(w.start_ms, w.end_ms, w.word) for w in result.segments[0].words]
    assert timings == [(2100, 2400, " Hello"), (2500, 3000, " world")]


def test_empty_segments_dropped_and_reindexed() -> None:
    segments = [
        make_segment(0, 1, " first"),
        make_segment(1, 2, "   "),
        make_segment(2, 3, " third"),
    ]
    engine, _ = _engine(ModelFactorySpy(model_kwargs={"segments": segments}))
    result = engine.transcribe(tone(3.0), mode=SpeechMode.ENGLISH)
    assert [(s.index, s.text) for s in result.segments] == [(0, "first"), (1, "third")]


def test_silence_yields_empty_result() -> None:
    engine, _ = _engine(ModelFactorySpy(model_kwargs={"segments": []}))
    result = engine.transcribe(tone(1.0, amplitude=0.0), mode=SpeechMode.AUTO)
    assert result.is_empty
    assert result.text == ""
    assert result.segments == ()


def test_negative_offset_rejected() -> None:
    engine, _ = _engine()
    with pytest.raises(ValueError):
        engine.transcribe(tone(1.0), offset_ms=-1)


# ---------- Input validation happens before any model work ----------


def test_empty_audio_rejected_without_loading_model() -> None:
    engine, spy = _engine()
    empty = AudioBuffer(samples=np.zeros(0, dtype=np.float32))
    with pytest.raises(EmptyAudioError):
        engine.transcribe(empty)
    assert spy.calls == []


def test_hand_built_invalid_buffer_rejected() -> None:
    engine, _ = _engine()
    with pytest.raises(InvalidAudioValuesError):
        engine.transcribe(AudioBuffer(samples=np.full(100, 3000.0, dtype=np.float32)))


def test_non_audiobuffer_rejected() -> None:
    engine, _ = _engine()
    with pytest.raises(TypeError, match="AudioBuffer"):
        engine.transcribe(np.zeros(100, dtype=np.float32))  # type: ignore[arg-type]


def test_audio_longer_than_configured_limit_rejected() -> None:
    engine, spy = _engine(max_audio_seconds=1.0)
    with pytest.raises(AudioTooLongError):
        engine.transcribe(tone(1.5))
    assert spy.calls == []


def test_pcm16_bytes_end_to_end() -> None:
    engine, spy = _engine()
    audio = decode_pcm16(pcm16_bytes(2.0), sample_rate=16_000)
    result = engine.transcribe(audio, mode=SpeechMode.ENGLISH)
    sent = _model(spy).transcribe_calls[0]["audio"]
    assert sent.dtype == np.float32 and sent.ndim == 1 and sent.shape == (32_000,)
    assert result.audio_duration_ms == 2000


def test_result_metadata_and_serialisation() -> None:
    engine, _ = _engine(model="base")
    result = engine.transcribe(tone(3.0), mode=SpeechMode.ENGLISH)
    data = result.to_dict()
    assert data["model_name"] == "base"
    assert data["device"] == "cpu" and data["compute_type"] == "int8"
    assert data["text"] == result.text
    assert data["segments"][0]["raw_text"] == " Let's start the meeting."
    assert data["segments"][0]["text"] == "Let's start the meeting."
    assert data["segments"][0]["avg_logprob"] == -0.25
    assert data["real_time_factor"] == pytest.approx(result.processing_ms / 3000, abs=1e-4)


def test_raw_types_are_immutable() -> None:
    segment = RawSegment(0, 1, " x", -0.1, 0.0, 1.0, (RawWord(0, 1, " x", 0.9),))
    with pytest.raises(AttributeError):
        segment.text = "changed"  # type: ignore[misc]


# ---------- Real faster-whisper library, no weights, no network ----------


def test_real_library_reports_missing_model_without_downloading(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uses the real default factory (faster_whisper.WhisperModel) and the real CUDA probe
    against an empty cache with downloads forbidden: a clear ModelLoadError, nothing fetched."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    engine = FasterWhisperEngine(
        SpeechEngineConfig(model="tiny", download_root=tmp_path, local_files_only=True)
    )
    with pytest.raises(ModelLoadError, match="forbids downloading") as exc_info:
        engine.load()
    assert exc_info.value.code == "model_load_failed"
    assert not engine.is_loaded
    assert list(tmp_path.iterdir()) == [] or all(p.is_dir() for p in tmp_path.iterdir())
    assert not any(p.suffix == ".bin" for p in tmp_path.rglob("*"))


def test_detect_language_errors_are_wrapped() -> None:
    engine, spy = _engine()
    engine.load()

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("feature extraction failed")

    _model(spy).detect_language = broken  # type: ignore[method-assign]
    with pytest.raises(InferenceError, match="Language detection failed: RuntimeError"):
        engine.detect_language(tone(1.0))
