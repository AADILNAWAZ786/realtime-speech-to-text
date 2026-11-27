"""Shared speech runtime: the speech provider and one recording registry per process.

SPEECH_PROVIDER selects how recordings are transcribed:

- `whisper` (default): one faster-whisper engine and one inference scheduler shared by all
  recordings; each recording gets a TranscriptionSession (VAD, endpointing, WHISPER_*).
- `gemini_live`: each recording gets a GeminiLiveSession on the Gemini Live API. No
  Whisper engine is created, no model is downloaded or preloaded, CUDA is never probed,
  and the recording limit is GEMINI_MAX_SESSION_SECONDS (<= 600 s).

Built in the FastAPI lifespan. Deployment constraint: run ONE Uvicorn worker per process
(and one process per machine/container). Each worker would load its own copy of the model
and keep its own in-memory registry, so per-user and global session limits, and the CPU
budget, would no longer hold.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from app.core.config import Settings, SpeechProvider
from app.models.enums import SpeechMode
from app.realtime.config import RealtimeConfig
from app.realtime.contract import RecordingSession
from app.realtime.events import SessionEvent
from app.realtime.gemini_live import (
    MODEL_LABEL_PREFIX,
    GeminiConnector,
    GeminiLiveSession,
    GenAiConnector,
    quiet_sdk_loggers,
)
from app.realtime.scheduler import InferenceScheduler, validate_cpu_budget
from app.realtime.session import TranscriptionSession
from app.speech.config import SpeechSettings
from app.speech.engine import SpeechEngine
from app.speech.faster_whisper_engine import FasterWhisperEngine
from app.ws.limits import RecordingRegistry

logger = logging.getLogger(__name__)

REALTIME_ENV_PREFIX = "REALTIME_"


def realtime_config_from_env(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> RealtimeConfig:
    """RealtimeConfig defaults, overridden by REALTIME_<FIELD> variables, with the WebSocket
    limits (frame size, recording length) taking precedence so they cannot disagree."""
    env = os.environ if environ is None else environ
    fields = set(RealtimeConfig.model_fields)
    values: dict[str, object] = {
        key[len(REALTIME_ENV_PREFIX) :].lower(): value
        for key, value in env.items()
        if key.startswith(REALTIME_ENV_PREFIX) and key[len(REALTIME_ENV_PREFIX) :].lower() in fields
    }
    values["max_chunk_bytes"] = settings.ws_max_frame_bytes
    values["max_session_ms"] = settings.ws_max_recording_seconds * 1000
    return RealtimeConfig.model_validate(values)


def threads_per_job(cpu_threads: int, concurrency: int, cpu_count: int) -> int:
    """Explicit CTranslate2 threads per job: the configured value, or an even split of the
    cores left after reserving one for the event loop."""
    if cpu_threads > 0:
        return cpu_threads
    return max(1, (cpu_count - 1) // concurrency)


@dataclass
class SpeechRuntime:
    registry: RecordingRegistry
    realtime: RealtimeConfig
    concurrency: int
    threads_per_job: int
    provider: SpeechProvider = SpeechProvider.WHISPER
    # whisper only:
    engine: SpeechEngine | None = None
    scheduler: InferenceScheduler | None = None
    # gemini_live only:
    gemini: GeminiConnector | None = None
    settings: Settings | None = None

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        engine: SpeechEngine | None = None,
        speech_settings: SpeechSettings | None = None,
        cpu_count: int | None = None,
        gemini_connector: GeminiConnector | None = None,
    ) -> SpeechRuntime:
        if settings.speech_provider is SpeechProvider.GEMINI_LIVE:
            return cls._create_gemini(settings, connector=gemini_connector)
        return cls._create_whisper(
            settings, engine=engine, speech_settings=speech_settings, cpu_count=cpu_count
        )

    @classmethod
    def _create_whisper(
        cls,
        settings: Settings,
        *,
        engine: SpeechEngine | None,
        speech_settings: SpeechSettings | None,
        cpu_count: int | None,
    ) -> SpeechRuntime:
        cpus = cpu_count or os.cpu_count() or 1
        concurrency = settings.inference_concurrency
        if engine is None:
            if importlib.util.find_spec("faster_whisper") is None:
                # e.g. installed from requirements-cloud.txt: fail at startup, not on the
                # first recording.
                raise RuntimeError(
                    "SPEECH_PROVIDER=whisper needs faster-whisper: install requirements.txt "
                    "(requirements-cloud.txt only supports SPEECH_PROVIDER=gemini_live)"
                )
            speech = speech_settings or SpeechSettings()
            threads = threads_per_job(speech.cpu_threads, concurrency, cpus)
            config = speech.to_engine_config(cpu_threads=threads, num_workers=concurrency)
            engine = FasterWhisperEngine(config)
        else:
            threads = 1  # injected engine (tests): one thread per job
        if not settings.inference_allow_cpu_oversubscription:
            validate_cpu_budget(concurrency, threads, cpu_count=cpus)
        else:
            logger.warning("CPU oversubscription allowed: %d x %d threads", concurrency, threads)

        _warn_multiple_workers()
        runtime = cls(
            registry=RecordingRegistry(settings.ws_max_active_sessions),
            realtime=realtime_config_from_env(settings),
            concurrency=concurrency,
            threads_per_job=threads,
            provider=SpeechProvider.WHISPER,
            engine=engine,
            scheduler=InferenceScheduler(concurrency),
            settings=settings,
        )
        info = engine.info
        logger.info(
            "Speech runtime: model=%s device=%s compute=%s jobs=%d threads/job=%d max_sessions=%d",
            info.model_name,
            info.device,
            info.compute_type,
            concurrency,
            threads,
            settings.ws_max_active_sessions,
        )
        return runtime

    @classmethod
    def _create_gemini(
        cls, settings: Settings, *, connector: GeminiConnector | None
    ) -> SpeechRuntime:
        quiet_sdk_loggers()
        if connector is None:
            key = settings.gemini_api_key  # presence validated by Settings
            assert key is not None  # noqa: S101
            connector = GenAiConnector.create(
                api_key=key.get_secret_value(), model=settings.gemini_live_model
            )
        _warn_multiple_workers()
        runtime = cls(
            registry=RecordingRegistry(settings.ws_max_active_sessions),
            realtime=realtime_config_from_env(settings),
            concurrency=0,
            threads_per_job=0,
            provider=SpeechProvider.GEMINI_LIVE,
            gemini=connector,
            settings=settings,
        )
        # Never log the key; the model name and limits are not secret.
        logger.info(
            "Speech runtime: provider=gemini_live model=%s max_session=%ds max_sessions=%d",
            settings.gemini_live_model,
            settings.max_recording_seconds,
            settings.ws_max_active_sessions,
        )
        return runtime

    @property
    def model_name(self) -> str:
        """Stored with each recording and sent in session_started."""
        if self.provider is SpeechProvider.GEMINI_LIVE:
            assert self.gemini is not None  # noqa: S101
            return f"{MODEL_LABEL_PREFIX}{self.gemini.model}"
        assert self.engine is not None  # noqa: S101
        return self.engine.info.model_name

    @property
    def max_recording_seconds(self) -> int:
        if self.settings is not None:
            return self.settings.max_recording_seconds
        return self.realtime.max_session_ms // 1000

    def new_session(
        self,
        *,
        mode: SpeechMode,
        on_event: Callable[[SessionEvent], None],
        session_id: str,
    ) -> RecordingSession:
        """The session for one recording, for the configured provider."""
        if self.provider is SpeechProvider.GEMINI_LIVE:
            assert self.gemini is not None and self.settings is not None  # noqa: S101
            settings = self.settings
            return GeminiLiveSession(
                connector=self.gemini,
                on_event=on_event,
                mode=mode,
                session_id=session_id,
                max_session_ms=settings.max_recording_seconds * 1000,
                max_chunk_bytes=settings.ws_max_frame_bytes,
                connect_timeout=settings.gemini_connect_timeout_seconds,
                finalize_timeout=settings.gemini_finalize_timeout_seconds,
            )
        assert self.engine is not None and self.scheduler is not None  # noqa: S101
        return TranscriptionSession(
            engine=self.engine,
            scheduler=self.scheduler,
            mode=mode,
            config=self.realtime,
            on_event=on_event,
            session_id=session_id,
        )

    async def preload(self) -> None:
        """Load the Whisper model now (SPEECH_PRELOAD_MODEL). Nothing to load for Gemini."""
        if self.engine is not None:
            await asyncio.to_thread(self.engine.load)

    async def aclose(self, shutdown_timeout: float) -> None:
        await self.registry.shutdown(shutdown_timeout)
        if self.scheduler is not None:
            await self.scheduler.aclose()


def _warn_multiple_workers() -> None:
    web_concurrency = os.environ.get("WEB_CONCURRENCY", "1")
    if web_concurrency.isdigit() and int(web_concurrency) > 1:
        logger.warning(
            "WEB_CONCURRENCY=%s: live transcription requires a single worker per process",
            web_concurrency,
        )
