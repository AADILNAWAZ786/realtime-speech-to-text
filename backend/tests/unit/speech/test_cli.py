"""CLI tests: the real FasterWhisperEngine with an injected fake model (no weights)."""

from __future__ import annotations

import io
import json
import os
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

from app.speech.cli import EXIT_AUDIO, EXIT_INFERENCE, EXIT_MODEL, EXIT_OK, EXIT_USAGE, main
from app.speech.config import SpeechEngineConfig
from app.speech.faster_whisper_engine import FasterWhisperEngine
from tests.unit.speech.fakes import ModelFactorySpy, write_wav

BACKEND_DIR = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # SpeechSettings reads WHISPER_* and ./.env: run from an empty directory.
    for key in list(os.environ):
        if key.startswith("WHISPER_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)


def _run(argv: list[str], spy: ModelFactorySpy | None = None) -> tuple[int, str, str, Any]:
    spy = spy or ModelFactorySpy()
    created: list[FasterWhisperEngine] = []

    def factory(config: SpeechEngineConfig) -> FasterWhisperEngine:
        engine = FasterWhisperEngine(config, model_factory=spy, cuda_device_count=lambda: 0)
        created.append(engine)
        return engine

    out, err = io.StringIO(), io.StringIO()
    code = main(argv, engine_factory=factory, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue(), spy


def test_text_report(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav", seconds=3.0)
    code, out, err, spy = _run([str(wav), "--model", "base", "--mode", "mixed", "--threads", "2"])
    assert code == EXIT_OK, err
    assert "Model:         base [cpu, int8, threads=2]" in out
    assert "Mode:          mixed (strategy: mixed:hi_prompted)" in out
    assert "Language:      hi (forced by mode)" in out
    assert "आज की meeting cancel हो गई। कल फिर call करते हैं, okay?" in out
    assert "[00:00.000 -> 00:01.600]" in out
    assert "Processing:" in out
    assert spy.calls[0][1]["cpu_threads"] == 2


def test_text_report_with_roman_script(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav", seconds=3.0)
    code, out, _, _ = _run([str(wav), "--mode", "mixed", "--script", "roman"])
    assert code == EXIT_OK
    assert "Text (as recognised):" in out
    assert "Text (roman, method=deterministic_transliteration):" in out
    assert "aaj ki meeting cancel ho gai." in out


def test_json_output(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav", seconds=3.0)
    code, out, _, _ = _run([str(wav), "--mode", "en", "--format", "json", "--script", "roman"])
    assert code == EXIT_OK
    payload = json.loads(out)
    assert payload["text"] == "Let's start the meeting. The report is ready."
    assert payload["language"] == "en" and payload["language_forced"] is True
    assert payload["audio_duration_ms"] == 3000
    assert len(payload["segments"]) == 2
    assert payload["segments"][1]["start_ms"] == 1400
    assert payload["script"]["method"] == "identity"  # no Devanagari to convert
    assert payload["model_load_ms"] is not None


def test_partial_preset_and_no_vad(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav", seconds=1.0)
    code, _, _, spy = _run([str(wav), "--preset", "partial", "--no-vad"])
    assert code == EXIT_OK
    call = spy.instances[0].transcribe_calls[0]
    assert call["beam_size"] == 1 and call["vad_filter"] is False


def test_final_preset_enables_vad_by_default(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav", seconds=1.0)
    _, _, _, spy = _run([str(wav)])
    assert spy.instances[0].transcribe_calls[0]["vad_filter"] is True


def test_missing_file(tmp_path: Path) -> None:
    code, _, err, spy = _run([str(tmp_path / "nope.wav")])
    assert code == EXIT_AUDIO
    assert "file_not_found" in err
    assert spy.calls == []  # no model loaded for bad input


def test_undecodable_file(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"garbage" * 20)
    code, _, err, _ = _run([str(bad)])
    assert code == EXIT_AUDIO
    assert "could not decode" in err


def test_english_only_model_rejected_as_usage_error(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav")
    code, _, err, _ = _run([str(wav), "--model", "small.en"])
    assert code == EXIT_USAGE
    assert "English-only" in err


def test_model_load_failure(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav")
    code, _, err, _ = _run([str(wav)], ModelFactorySpy(error=OSError("no space left")))
    assert code == EXIT_MODEL
    assert "no space left" in err and "model_load_failed" in err


def test_cuda_unavailable(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav")
    code, _, err, _ = _run([str(wav), "--device", "cuda"])
    assert code == EXIT_MODEL
    assert "cuda_unavailable" in err


def test_inference_failure(tmp_path: Path) -> None:
    wav = write_wav(tmp_path / "clip.wav", seconds=3.0)
    code, _, err, _ = _run([str(wav)], ModelFactorySpy(model_kwargs={"fail_at_segment": 0}))
    assert code == EXIT_INFERENCE
    assert "inference_failed" in err


def test_invalid_mode_is_argparse_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _run([str(tmp_path / "x.wav"), "--mode", "french"])
    assert exc_info.value.code == EXIT_USAGE


def test_script_wrapper_shows_help(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(sys, "argv", ["transcribe_file.py", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(str(BACKEND_DIR / "scripts" / "transcribe_file.py"), run_name="__main__")
    assert exc_info.value.code == 0
    assert "--mode {auto,en,hi,mixed}" in capsys.readouterr().out
