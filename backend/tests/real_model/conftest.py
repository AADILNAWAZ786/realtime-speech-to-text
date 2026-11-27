"""Optional tests against a REAL faster-whisper model.

Skipped unless STT_REAL_MODEL_TESTS=1. They never download weights: the model must already
be in the local cache (download it once with scripts/transcribe_file.py, see
docs/speech-evaluation.md). Configure with:

    STT_REAL_MODEL_TESTS=1         enable these tests
    STT_REAL_MODEL=tiny            model size / path (default: tiny)
    STT_REAL_MODEL_ROOT=/path      optional cache directory (WHISPER_DOWNLOAD_ROOT)
    STT_REAL_AUDIO_DIR=/path       optional folder of recordings named en_*.wav, hi_*.wav,
                                   mixed_*.wav (any format ffmpeg/PyAV can decode)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.speech.config import SpeechEngineConfig
from app.speech.errors import ModelLoadError
from app.speech.faster_whisper_engine import FasterWhisperEngine

ENABLED = os.environ.get("STT_REAL_MODEL_TESTS") == "1"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).resolve().parent
    skip = pytest.mark.skip(reason="real-model tests disabled (set STT_REAL_MODEL_TESTS=1)")
    for item in items:
        if here in Path(item.path).resolve().parents:
            item.add_marker(pytest.mark.real_model)
            if not ENABLED:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def real_engine() -> FasterWhisperEngine:
    os.environ["HF_HUB_OFFLINE"] = "1"  # belt and braces: no network for weights
    root = os.environ.get("STT_REAL_MODEL_ROOT")
    config = SpeechEngineConfig(
        model=os.environ.get("STT_REAL_MODEL", "tiny"),
        device="cpu",
        compute_type="int8",
        local_files_only=True,  # never download during pytest
        download_root=Path(root) if root else None,
    )
    engine = FasterWhisperEngine(config)
    try:
        engine.load()
    except ModelLoadError as exc:
        pytest.skip(f"model {config.model!r} not available locally: {exc}")
    return engine


@pytest.fixture(scope="session")
def real_audio_dir() -> Path | None:
    value = os.environ.get("STT_REAL_AUDIO_DIR")
    return Path(value) if value else None
