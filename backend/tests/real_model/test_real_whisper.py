"""Smoke tests with a real model (opt-in; see conftest.py). Assertions are deliberately
loose: they check the engine works end to end, not recognition quality (that is what the
manual evaluation in docs/speech-evaluation.md measures)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from app.models.enums import SpeechMode
from app.speech.audio import load_audio_file, validate_float_audio
from app.speech.engine import DecodeOptions
from app.speech.faster_whisper_engine import FasterWhisperEngine

_PREFIX_TO_MODE = {"en": SpeechMode.ENGLISH, "hi": SpeechMode.HINDI, "mixed": SpeechMode.MIXED}


def test_model_is_multilingual_and_on_cpu_int8(real_engine: FasterWhisperEngine) -> None:
    info = real_engine.info
    assert info.loaded
    assert (info.device, info.compute_type) == ("cpu", "int8")


def test_silence_produces_no_hallucinated_text_with_vad(real_engine: FasterWhisperEngine) -> None:
    silence = validate_float_audio(np.zeros(16_000 * 3, dtype=np.float32))
    result = real_engine.transcribe(silence, mode=SpeechMode.AUTO, options=DecodeOptions.final())
    assert result.text == ""
    assert result.processing_ms > 0


@pytest.mark.parametrize("mode", list(SpeechMode))
def test_every_mode_runs_end_to_end(real_engine: FasterWhisperEngine, mode: SpeechMode) -> None:
    rng = np.random.default_rng(0)
    noise = validate_float_audio((rng.standard_normal(16_000 * 2) * 0.01).astype(np.float32))
    result = real_engine.transcribe(noise, mode=mode, options=DecodeOptions.partial())
    assert result.audio_duration_ms == 2000
    assert all(0 <= s.start_ms <= s.end_ms <= 2000 for s in result.segments)


def test_recordings(real_engine: FasterWhisperEngine, real_audio_dir: Path | None) -> None:
    if real_audio_dir is None:
        pytest.skip("set STT_REAL_AUDIO_DIR to a folder of en_*/hi_*/mixed_* recordings")
    files = sorted(p for p in real_audio_dir.iterdir() if p.name.split("_")[0] in _PREFIX_TO_MODE)
    if not files:
        pytest.skip(f"no en_*/hi_*/mixed_* files in {real_audio_dir}")
    for path in files:
        mode = _PREFIX_TO_MODE[path.name.split("_")[0]]
        result = real_engine.transcribe(load_audio_file(path), mode=mode)
        assert result.text, f"{path.name}: no text recognised"
        assert result.segments[-1].end_ms <= result.audio_duration_ms
