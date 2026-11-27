"""WebSocket building blocks without a database: protocol, limits, runtime config."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest

from app.core.config import Settings
from app.models.enums import ScriptPreference, SpeechMode
from app.speech.config import SpeechSettings
from app.speech.faster_whisper_engine import FasterWhisperEngine
from app.ws.limits import AudioRateLimiter, RecordingRegistry
from app.ws.protocol import (
    CloseCode,
    ErrorCode,
    ProtocolError,
    StartMessage,
    StopMessage,
    error_message,
    parse_client_message,
)
from app.ws.runtime import SpeechRuntime, realtime_config_from_env, threads_per_job
from tests.unit.realtime.fakes import TimelineEngine


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://u:p@localhost/db",
        "jwt_secret_key": secrets.token_urlsafe(48),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


# ---------------------------------------------------------------- protocol


def test_parse_start_with_defaults() -> None:
    message = parse_client_message('{"type": "start", "token": "abc"}')
    assert isinstance(message, StartMessage)
    assert message.speech_mode is SpeechMode.AUTO
    assert message.script_preference is ScriptPreference.AS_RECOGNISED
    assert (message.format, message.sample_rate, message.channels) == ("pcm_s16le", 16000, 1)


def test_parse_full_start_and_stop() -> None:
    message = parse_client_message(
        '{"type":"start","token":"t","speech_mode":"mixed","script_preference":"roman",'
        '"format":"pcm_s16le","sample_rate":16000,"channels":1,"title":"  My recording "}'
    )
    assert isinstance(message, StartMessage)
    assert message.title == "My recording"
    assert isinstance(parse_client_message('{"type": "stop"}'), StopMessage)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"type": "pause"}',
        '{"type": "start"}',
        '{"type": "start", "token": "t", "sample_rate": 48000}',
        '{"type": "stop", "extra": 1}',
    ],
)
def test_invalid_client_messages(raw: str) -> None:
    with pytest.raises(ProtocolError) as exc_info:
        parse_client_message(raw)
    assert exc_info.value.code is ErrorCode.INVALID_MESSAGE
    assert exc_info.value.close_code is CloseCode.BAD_REQUEST


def test_token_is_hidden_from_repr() -> None:
    message = parse_client_message('{"type": "start", "token": "super-secret-token"}')
    assert "super-secret-token" not in repr(message)


def test_error_messages_are_generic() -> None:
    for code in ErrorCode:
        message = error_message(code)
        assert message["message"] and message["code"] == code.value
        assert "Traceback" not in message["message"]


# ---------------------------------------------------------------- rate limiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_rate_limiter_allows_burst_then_real_time_factor() -> None:
    clock = Clock()
    limiter = AudioRateLimiter(factor=1.5, burst_seconds=2.0, clock=clock)
    assert limiter.allow(32_000)  # 2 s burst
    assert not limiter.allow(1)
    clock.now = 1.0  # 1 s later: 1.5 s of audio allowed
    assert limiter.allow(24_000)
    assert not limiter.allow(16)


def test_rate_limiter_sustained_real_time_is_fine() -> None:
    clock = Clock()
    limiter = AudioRateLimiter(factor=1.5, burst_seconds=1.0, clock=clock)
    for step in range(1, 600):  # 10 minutes at real time, 250 ms chunks
        clock.now = step * 0.25
        assert limiter.allow(4000)


# ---------------------------------------------------------------- registry


async def test_registry_limits_and_release() -> None:
    registry = RecordingRegistry(max_sessions=2)
    alice, bob, carol = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    lease = registry.acquire(alice)
    with pytest.raises(ProtocolError) as same_user:
        registry.acquire(alice)
    assert same_user.value.code is ErrorCode.RECORDING_IN_PROGRESS
    registry.acquire(bob)
    with pytest.raises(ProtocolError) as full:
        registry.acquire(carol)
    assert (full.value.code, full.value.close_code) == (
        ErrorCode.SERVER_BUSY,
        CloseCode.TRY_AGAIN_LATER,
    )
    lease.release()
    lease.release()  # idempotent
    assert not registry.is_active(alice)
    registry.acquire(carol)
    assert registry.active_count == 2


async def test_registry_shutdown_refuses_new_recordings() -> None:
    registry = RecordingRegistry(max_sessions=2)
    await registry.shutdown(1.0)
    with pytest.raises(ProtocolError) as exc_info:
        registry.acquire(uuid.uuid4())
    assert exc_info.value.code is ErrorCode.SERVER_SHUTDOWN


# ---------------------------------------------------------------- runtime


def test_realtime_config_from_env_and_ws_limits_win() -> None:
    settings = _settings(ws_max_frame_bytes=32_768, ws_max_recording_seconds=600)
    config = realtime_config_from_env(
        settings,
        {"REALTIME_END_SILENCE_MS": "700", "REALTIME_MAX_CHUNK_BYTES": "999", "OTHER": "x"},
    )
    assert config.end_silence_ms == 700
    assert config.max_chunk_bytes == 32_768  # the WebSocket limit is authoritative
    assert config.max_session_ms == 600_000


def test_invalid_realtime_env_fails_fast() -> None:
    with pytest.raises(ValueError):
        realtime_config_from_env(_settings(), {"REALTIME_END_SILENCE_MS": "50"})


@pytest.mark.parametrize(
    ("cpu_threads", "concurrency", "cpus", "expected"),
    [(0, 1, 2, 1), (0, 1, 8, 7), (0, 2, 8, 3), (3, 1, 8, 3), (0, 1, 1, 1)],
)
def test_threads_per_job(cpu_threads: int, concurrency: int, cpus: int, expected: int) -> None:
    assert threads_per_job(cpu_threads, concurrency, cpus) == expected


def test_runtime_builds_faster_whisper_with_explicit_threads_without_loading() -> None:
    speech = SpeechSettings(_env_file=None, model="base")
    runtime = SpeechRuntime.create(_settings(), speech_settings=speech, cpu_count=8)
    assert isinstance(runtime.engine, FasterWhisperEngine)
    assert runtime.engine.config.cpu_threads == 7
    assert runtime.engine.config.num_workers == 1
    assert not runtime.engine.is_loaded  # lazy: no model download at startup
    assert runtime.scheduler.max_concurrency == 1


def test_runtime_rejects_cpu_budget_violation() -> None:
    speech = SpeechSettings(_env_file=None, cpu_threads=4)
    with pytest.raises(ValueError, match="exceeds the budget"):
        SpeechRuntime.create(_settings(), speech_settings=speech, cpu_count=4)
    with pytest.raises(ValueError, match="exceeds the budget"):
        SpeechRuntime.create(_settings(), engine=TimelineEngine([]), cpu_count=1)


def test_runtime_oversubscription_is_explicit(caplog: pytest.LogCaptureFixture) -> None:
    runtime = SpeechRuntime.create(
        _settings(inference_allow_cpu_oversubscription=True),
        engine=TimelineEngine([]),
        cpu_count=1,
    )
    assert runtime.concurrency == 1
    assert "oversubscription" in caplog.text.lower()
