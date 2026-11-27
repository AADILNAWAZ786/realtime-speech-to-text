"""SPEECH_PROVIDER selection, Gemini settings validation and the runtime per provider."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from pydantic import ValidationError

from app.core.config import Settings, SpeechProvider
from app.models.enums import SpeechMode
from app.realtime.gemini_live import (
    AUDIO_MIME_TYPE,
    GeminiLiveSession,
    GenAiConnector,
    quiet_sdk_loggers,
)
from app.realtime.session import TranscriptionSession
from app.speech.faster_whisper_engine import FasterWhisperEngine
from app.ws.runtime import SpeechRuntime
from tests.unit.realtime.fakes import TimelineEngine
from tests.unit.realtime.gemini_fakes import FakeConnector

SECRET = "fake-gemini-key-for-tests-only-123456"
BASE: dict[str, Any] = {
    "database_url": "postgresql+asyncpg://user@localhost/db",
    "jwt_secret_key": "kQ7v!pL2#xR9@mZ4$wT6&nB8*cF1^hJ3",
}


def settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **{**BASE, **values})  # type: ignore[call-arg]


# ----------------------------------------------------------------- settings


def test_whisper_is_the_default_and_needs_no_gemini_key() -> None:
    s = settings()
    assert s.speech_provider is SpeechProvider.WHISPER
    assert s.gemini_api_key is None
    assert s.max_recording_seconds == s.ws_max_recording_seconds == 1800  # unchanged


def test_gemini_requires_a_key() -> None:
    with pytest.raises(ValidationError, match="GEMINI_API_KEY is required"):
        settings(speech_provider="gemini_live")
    with pytest.raises(ValidationError, match="GEMINI_API_KEY is required"):
        settings(speech_provider="gemini_live", gemini_api_key="   ")
    with pytest.raises(ValidationError, match="placeholder"):
        settings(speech_provider="gemini_live", gemini_api_key="your-gemini-api-key")


def test_gemini_defaults_and_limits() -> None:
    s = settings(speech_provider="gemini_live", gemini_api_key=SECRET)
    assert s.gemini_live_model == "gemini-3.5-transcribe-live"
    assert s.gemini_max_session_seconds == 600
    assert s.max_recording_seconds == 600  # Gemini's limit, not WS_MAX_RECORDING_SECONDS
    assert (
        settings(
            speech_provider="gemini_live", gemini_api_key=SECRET, gemini_max_session_seconds=300
        ).max_recording_seconds
        == 300
    )
    with pytest.raises(ValidationError):
        settings(
            speech_provider="gemini_live", gemini_api_key=SECRET, gemini_max_session_seconds=601
        )


def test_provider_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPEECH_PROVIDER", "gemini_live")
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    s = Settings(_env_file=None, **BASE)  # type: ignore[call-arg]
    assert s.speech_provider is SpeechProvider.GEMINI_LIVE
    monkeypatch.setenv("SPEECH_PROVIDER", "openai")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **BASE)  # type: ignore[call-arg]


def test_the_key_is_never_revealed_by_settings() -> None:
    s = settings(speech_provider="gemini_live", gemini_api_key=SECRET)
    assert SECRET not in repr(s)
    assert SECRET not in str(s)
    assert SECRET not in s.model_dump_json()
    with pytest.raises(ValidationError) as exc_info:
        settings(speech_provider="gemini_live", gemini_api_key=SECRET, gemini_live_model="")
    assert SECRET not in str(exc_info.value)  # hide_input_in_errors


# ----------------------------------------------------------------- runtime


def test_whisper_runtime_is_unchanged() -> None:
    runtime = SpeechRuntime.create(settings(), engine=TimelineEngine([]), cpu_count=4)
    assert runtime.provider is SpeechProvider.WHISPER
    assert runtime.engine is not None and runtime.scheduler is not None
    assert runtime.gemini is None
    assert runtime.max_recording_seconds == 1800
    session = runtime.new_session(mode=SpeechMode.MIXED, on_event=lambda e: None, session_id="x")
    assert isinstance(session, TranscriptionSession)


def test_gemini_runtime_creates_no_whisper_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Whisper must not be constructed with SPEECH_PROVIDER=gemini_live")

    monkeypatch.setattr("app.ws.runtime.FasterWhisperEngine", forbidden)
    monkeypatch.setattr("app.ws.runtime.SpeechSettings", forbidden)
    monkeypatch.setattr("app.ws.runtime.validate_cpu_budget", forbidden)
    connector = FakeConnector()
    runtime = SpeechRuntime.create(
        settings(speech_provider="gemini_live", gemini_api_key=SECRET, inference_concurrency=64),
        gemini_connector=connector,
        cpu_count=1,  # would fail Whisper's CPU budget: not checked for Gemini
    )
    assert runtime.provider is SpeechProvider.GEMINI_LIVE
    assert runtime.engine is None and runtime.scheduler is None
    assert runtime.model_name == "gemini:gemini-3.5-transcribe-live"
    assert runtime.max_recording_seconds == 600
    session = runtime.new_session(mode=SpeechMode.HINDI, on_event=lambda e: None, session_id="x")
    assert isinstance(session, GeminiLiveSession)
    assert connector.sessions == []  # nothing connects until a recording starts


async def test_gemini_runtime_preload_and_close_need_no_model() -> None:
    runtime = SpeechRuntime.create(
        settings(speech_provider="gemini_live", gemini_api_key=SECRET, speech_preload_model=True),
        gemini_connector=FakeConnector(),
    )
    await runtime.preload()  # no-op: nothing to download
    await runtime.aclose(1.0)


def test_whisper_runtime_still_builds_faster_whisper_lazily() -> None:
    from app.speech.config import SpeechSettings

    runtime = SpeechRuntime.create(
        settings(),
        speech_settings=SpeechSettings(_env_file=None, cpu_threads=1),
        cpu_count=4,  # type: ignore[call-arg]
    )
    assert isinstance(runtime.engine, FasterWhisperEngine)
    assert not runtime.engine.is_loaded  # still no download at startup


# ----------------------------------------------------------------- the real SDK client


def test_genai_connector_uses_the_documented_live_configuration() -> None:
    """Builds the real google-genai client (no network call happens until connect)."""
    from google.genai import types

    connector = GenAiConnector.create(api_key=SECRET, model="gemini-3.5-transcribe-live")
    assert connector.client._api_client._http_options.api_version == "v1beta"
    config = connector.config
    assert isinstance(config, types.LiveConnectConfig)
    assert config.response_modalities == [types.Modality.TEXT]
    transcription = config.input_audio_transcription
    assert transcription is not None
    assert transcription.language_codes == []  # automatic language detection
    assert transcription.mode is types.AudioTranscriptionConfigMode.VERBATIM  # not SMART
    blob = connector.audio_blob(b"\x01\x00" * 1600)
    assert isinstance(blob, types.Blob)
    assert blob.mime_type == AUDIO_MIME_TYPE == "audio/pcm;rate=16000"
    assert SECRET not in repr(connector.config)


def test_genai_connector_connects_with_model_and_config() -> None:
    calls: list[dict[str, Any]] = []

    class Live:
        def connect(self, **kwargs: Any) -> str:
            calls.append(kwargs)
            return "context-manager"

    class Aio:
        live = Live()

    class Client:
        aio = Aio()

    connector = GenAiConnector(client=Client(), types=None, model="m", config="cfg")
    assert connector.connect() == "context-manager"  # type: ignore[comparison-overlap]
    assert calls == [{"model": "m", "config": "cfg"}]


def test_sdk_loggers_are_capped_at_warning() -> None:
    logging.getLogger("google_genai").setLevel(logging.DEBUG)
    logging.getLogger("websockets").setLevel(logging.DEBUG)
    quiet_sdk_loggers()
    assert logging.getLogger("google_genai").level == logging.WARNING
    assert logging.getLogger("websockets.client").getEffectiveLevel() >= logging.WARNING


def test_whisper_without_faster_whisper_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """requirements-cloud.txt has no faster-whisper: SPEECH_PROVIDER=whisper must fail
    clearly at startup instead of on the first recording."""
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(
        "app.ws.runtime.importlib.util.find_spec",
        lambda name, *a: None if name == "faster_whisper" else real(name, *a),
    )
    with pytest.raises(RuntimeError, match=r"requirements-cloud\.txt only supports"):
        SpeechRuntime.create(settings(), cpu_count=4)
    # An injected engine (tests) never needs the package.
    SpeechRuntime.create(settings(), engine=TimelineEngine([]), cpu_count=4)
