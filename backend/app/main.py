"""FastAPI application factory and ASGI entry point.

Run with:  uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000

Factory mode means importing this module has no side effects (no settings are read and
no engine is created until the server, or a test, calls `create_app()`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.rate_limit import LoginRateLimiter
from app.db.session import create_engine, create_session_factory
from app.speech.engine import SpeechEngine
from app.ws.persistence import RecordingStore
from app.ws.routes import router as ws_router
from app.ws.runtime import SpeechRuntime

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None, *, speech_engine: SpeechEngine | None = None
) -> FastAPI:
    """Build the application. `speech_engine` replaces the faster-whisper engine (tests)."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("Starting %s (%s)", settings.app_name, settings.app_env.value)
        # Shared speech runtime: validates the CPU budget (raises -> startup fails).
        runtime = SpeechRuntime.create(settings, engine=speech_engine)
        app.state.speech = runtime
        try:
            await app.state.recording_store.mark_stale_recordings_interrupted()
        except Exception as exc:  # DB may be down at boot; readiness reports it
            logger.warning("Could not check for stale recordings: %s", type(exc).__name__)
        if settings.speech_preload_model:
            await runtime.preload()
        try:
            yield
        finally:
            # Order: stop live sessions (they save their finals), then inference, then DB.
            await runtime.aclose(settings.ws_shutdown_timeout_seconds)
            await app.state.engine.dispose()
            logger.info("Shutdown complete")

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # Shared resources live on app.state so each app instance (incl. tests) is isolated.
    engine = create_engine(settings)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.recording_store = RecordingStore(app.state.session_factory)
    app.state.login_rate_limiter = LoginRateLimiter(
        max_attempts=settings.login_rate_limit_attempts,
        window_seconds=settings.login_rate_limit_window_seconds,
        enabled=settings.login_rate_limit_enabled,
    )

    # Middleware: the last one added runs first (outermost).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,  # bearer tokens, not cookies
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)
    app.include_router(ws_router)
    return app
