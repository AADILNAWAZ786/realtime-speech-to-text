"""Shared speech runtime: one engine, one scheduler, one recording registry per process.

Built in the FastAPI lifespan. Deployment constraint: run ONE Uvicorn worker per process
(and one process per machine/container). Each worker would load its own copy of the model
and keep its own in-memory registry, so per-user and global session limits, and the CPU
budget, would no longer hold.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.config import Settings
from app.realtime.config import RealtimeConfig
from app.realtime.scheduler import InferenceScheduler, validate_cpu_budget
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
    engine: SpeechEngine
    scheduler: InferenceScheduler
    registry: RecordingRegistry
    realtime: RealtimeConfig
    concurrency: int
    threads_per_job: int

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        engine: SpeechEngine | None = None,
        speech_settings: SpeechSettings | None = None,
        cpu_count: int | None = None,
    ) -> SpeechRuntime:
        cpus = cpu_count or os.cpu_count() or 1
        concurrency = settings.inference_concurrency
        if engine is None:
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

        web_concurrency = os.environ.get("WEB_CONCURRENCY", "1")
        if web_concurrency.isdigit() and int(web_concurrency) > 1:
            logger.warning(
                "WEB_CONCURRENCY=%s: live transcription requires a single worker per process",
                web_concurrency,
            )

        runtime = cls(
            engine=engine,
            scheduler=InferenceScheduler(concurrency),
            registry=RecordingRegistry(settings.ws_max_active_sessions),
            realtime=realtime_config_from_env(settings),
            concurrency=concurrency,
            threads_per_job=threads,
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

    async def preload(self) -> None:
        await asyncio.to_thread(self.engine.load)

    async def aclose(self, shutdown_timeout: float) -> None:
        await self.registry.shutdown(shutdown_timeout)
        await self.scheduler.aclose()
