from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.enums import SpeechMode
from app.speech.config import (
    MixedStrategy,
    SpeechEngineConfig,
    SpeechSettings,
    is_english_only_model,
)
from app.speech.language import (
    DEFAULT_MIXED_PROMPT,
    LanguageDetection,
    decide_session_language,
    plan_for_mode,
)

# ---------- Config ----------


def test_defaults_are_cpu_small_auto_compute() -> None:
    config = SpeechEngineConfig()
    assert (config.model, config.device, config.compute_type) == ("small", "cpu", "auto")
    assert config.num_workers == 1
    assert config.local_files_only is False


@pytest.mark.parametrize(
    "name",
    [
        "tiny.en",
        "base.en",
        "small.en",
        "medium.en",
        "distil-large-v3",
        "distil-small.en",
        "Systran/faster-whisper-small.en",
        "/models/whisper-base.en",
        "SMALL.EN",
    ],
)
def test_english_only_models_rejected(name: str) -> None:
    assert is_english_only_model(name)
    with pytest.raises(ValidationError, match="English-only"):
        SpeechEngineConfig(model=name)


@pytest.mark.parametrize(
    "name",
    [
        "tiny",
        "base",
        "small",
        "medium",
        "large-v3",
        "large-v3-turbo",
        "Systran/faster-whisper-small",
        "/opt/models/my-hinglish-ct2",
    ],
)
def test_multilingual_and_custom_models_accepted(name: str) -> None:
    assert SpeechEngineConfig(model=name).model == name


@pytest.mark.parametrize(
    "overrides",
    [
        {"cpu_threads": -1},
        {"num_workers": 0},
        {"device": "tpu"},
        {"compute_type": "int4"},
        {"model": ""},
        {"max_audio_seconds": 0},
        {"unknown": 1},
    ],
)
def test_invalid_config_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        SpeechEngineConfig(**overrides)


def test_config_is_immutable() -> None:
    config = SpeechEngineConfig()
    with pytest.raises(ValidationError):
        config.model = "tiny"  # type: ignore[misc]


def test_speech_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_MODEL", "base")
    monkeypatch.setenv("WHISPER_CPU_THREADS", "2")
    monkeypatch.setenv("WHISPER_MIXED_STRATEGY", "multilingual")
    monkeypatch.setenv("WHISPER_DOWNLOAD_ROOT", "/srv/whisper-models")
    config = SpeechSettings(_env_file=None).to_engine_config()
    assert config.model == "base"
    assert config.cpu_threads == 2
    assert config.mixed_strategy is MixedStrategy.MULTILINGUAL
    assert config.download_root == Path("/srv/whisper-models")


def test_cli_overrides_win_and_none_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_MODEL", "base")
    settings = SpeechSettings(_env_file=None)
    assert settings.to_engine_config(model="tiny", device=None).model == "tiny"
    assert settings.to_engine_config(model=None).model == "base"


def test_english_only_model_from_environment_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WHISPER_MODEL", "small.en")
    with pytest.raises(ValidationError, match="English-only"):
        SpeechSettings(_env_file=None).to_engine_config()


# ---------- Decoding plans ----------


def test_english_plan() -> None:
    plan = plan_for_mode(SpeechMode.ENGLISH)
    assert (plan.language, plan.multilingual, plan.style_prompt, plan.strategy) == (
        "en",
        False,
        None,
        "en",
    )


def test_hindi_plan() -> None:
    plan = plan_for_mode(SpeechMode.HINDI)
    assert (plan.language, plan.multilingual, plan.strategy) == ("hi", False, "hi")


def test_auto_plan_lets_whisper_detect() -> None:
    plan = plan_for_mode(SpeechMode.AUTO)
    assert plan.language is None
    assert plan.strategy == "auto"


@pytest.mark.parametrize(
    ("strategy", "language", "multilingual", "has_prompt"),
    [
        (MixedStrategy.HINDI_PROMPTED, "hi", False, True),
        (MixedStrategy.HINDI, "hi", False, False),
        (MixedStrategy.AUTO_DETECT, None, False, False),
        (MixedStrategy.MULTILINGUAL, None, True, False),
    ],
)
def test_mixed_strategies(
    strategy: MixedStrategy, language: str | None, multilingual: bool, has_prompt: bool
) -> None:
    plan = plan_for_mode(SpeechMode.MIXED, mixed_strategy=strategy)
    assert plan.language == language
    assert plan.multilingual is multilingual
    assert (plan.style_prompt is not None) is has_prompt
    assert plan.strategy == f"mixed:{strategy.value}"
    assert len(plan.strategy) <= 32  # fits transcriptions.decode_strategy


def test_mixed_prompt_is_mixed_script_and_overridable() -> None:
    plan = plan_for_mode(SpeechMode.MIXED)
    assert plan.style_prompt == DEFAULT_MIXED_PROMPT
    assert any("ऀ" <= ch <= "ॿ" for ch in DEFAULT_MIXED_PROMPT)
    assert any("a" <= ch <= "z" for ch in DEFAULT_MIXED_PROMPT)
    custom = plan_for_mode(SpeechMode.MIXED, mixed_prompt="haan, meeting kal hai")
    assert custom.style_prompt == "haan, meeting kal hai"


def test_build_prompt_combines_style_and_context() -> None:
    mixed = plan_for_mode(SpeechMode.MIXED)
    assert mixed.build_prompt("पिछला text") == f"{DEFAULT_MIXED_PROMPT} पिछला text"
    english = plan_for_mode(SpeechMode.ENGLISH)
    assert english.build_prompt(None) is None
    assert english.build_prompt("  previous  ") == "previous"


# ---------- Session language decision (auto mode) ----------


def _detection(*probs: tuple[str, float]) -> LanguageDetection:
    return LanguageDetection(probs[0][0], probs[0][1], tuple(probs))


@pytest.mark.parametrize(
    ("probs", "expected"),
    [
        ((("en", 0.95), ("hi", 0.03)), SpeechMode.ENGLISH),
        ((("hi", 0.90), ("en", 0.05)), SpeechMode.HINDI),
        ((("hi", 0.55), ("en", 0.40)), SpeechMode.MIXED),
        ((("en", 0.50), ("hi", 0.45)), SpeechMode.MIXED),
        ((("ur", 0.60), ("hi", 0.30), ("en", 0.05)), SpeechMode.HINDI),  # Urdu folded in
        ((("fr", 0.80), ("en", 0.10)), SpeechMode.AUTO),
        ((("en", 0.40), ("de", 0.30), ("hi", 0.05)), SpeechMode.AUTO),
    ],
    ids=["english", "hindi", "mixed-hi-lead", "mixed-en-lead", "urdu-as-hindi", "other", "unsure"],
)
def test_decide_session_language(
    probs: tuple[tuple[str, float], ...], expected: SpeechMode
) -> None:
    decision = decide_session_language(_detection(*probs))
    assert decision.mode is expected
    assert decision.reason
