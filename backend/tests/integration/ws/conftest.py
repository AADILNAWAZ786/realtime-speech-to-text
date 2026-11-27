"""Harness for /ws/transcribe tests: real app + PostgreSQL + a fake speech engine.

Starlette's TestClient runs the app (and its lifespan) on its own event-loop thread; the
tests talk to it synchronously. Database assertions use a separate short-lived engine.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient, WebSocketTestSession

from app.core.config import Settings
from app.main import create_app
from app.speech.engine import SpeechEngine
from app.ws.persistence import RecordingStore
from tests.conftest import DEFAULT_PASSWORD, truncate_all
from tests.unit.realtime.fakes import Script, SpokenWord, TimelineEngine, words_in

ORIGIN = "http://localhost:5173"
WS_PATH = "/ws/transcribe"


def db_rows(url: str, sql: str, **params: Any) -> list[dict[str, Any]]:
    async def run() -> list[dict[str, Any]]:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params)
                return [dict(row._mapping) for row in result]
        finally:
            await engine.dispose()

    return asyncio.run(run())


@dataclass
class Harness:
    client: TestClient
    app: FastAPI
    settings: Settings
    db_url: str

    # ----- users

    def token(self, email: str = "alice@example.com") -> str:
        register = self.client.post(
            "/api/v1/auth/register", json={"email": email, "password": DEFAULT_PASSWORD}
        )
        assert register.status_code in (201, 409), register.text
        login = self.client.post(
            "/api/v1/auth/login", json={"email": email, "password": DEFAULT_PASSWORD}
        )
        assert login.status_code == 200, login.text
        token: str = login.json()["access_token"]
        return token

    def auth(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # ----- websocket

    def connect(self, origin: str | None = ORIGIN) -> Any:
        headers = {"origin": origin} if origin is not None else {}
        return self.client.websocket_connect(WS_PATH, headers=headers)

    @staticmethod
    def start(ws: WebSocketTestSession, token: str, **fields: Any) -> dict[str, Any]:
        ws.send_json({"type": "start", "token": token, **fields})
        message: dict[str, Any] = ws.receive_json()
        assert message["type"] == "session_started", message
        return message

    @staticmethod
    def send_audio(
        ws: WebSocketTestSession, pcm: bytes, *, chunk_ms: int = 250, pause: float = 0.003
    ) -> None:
        step = 16_000 * chunk_ms // 1000 * 2
        for offset in range(0, len(pcm), step):
            ws.send_bytes(pcm[offset : offset + step])
            if pause:
                time.sleep(pause)  # lets the server interleave inference with receiving

    @staticmethod
    def receive_until(
        ws: WebSocketTestSession, kind: str = "session_saved"
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        while True:
            message: dict[str, Any] = ws.receive_json()
            messages.append(message)
            if message["type"] == kind:
                return messages

    def record(
        self,
        token: str,
        pcm: bytes,
        *,
        stop: bool = True,
        **start_fields: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Full happy path: connect, start, stream, stop, collect until session_saved."""
        with self.connect() as ws:
            started = self.start(ws, token, **start_fields)
            self.send_audio(ws, pcm)
            ws.send_json({"type": "stop"})
            return started, self.receive_until(ws)

    # ----- database

    def transcription(self, transcription_id: str | uuid.UUID) -> dict[str, Any]:
        (row,) = db_rows(
            self.db_url, "SELECT * FROM transcriptions WHERE id = :id", id=str(transcription_id)
        )
        return row

    def segments(self, transcription_id: str | uuid.UUID) -> list[dict[str, Any]]:
        return db_rows(
            self.db_url,
            "SELECT * FROM transcription_segments WHERE transcription_id = :id ORDER BY seq",
            id=str(transcription_id),
        )

    def wait_status(
        self,
        transcription_id: str | uuid.UUID,
        *,
        not_status: str = "recording",
        limit_s: float = 10,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + limit_s
        while True:
            row = self.transcription(transcription_id)
            if row["status"] != not_status:
                return row
            if time.monotonic() > deadline:
                raise AssertionError(f"recording still {not_status!r}")
            time.sleep(0.05)


@pytest.fixture
def harness_factory(
    make_settings: Callable[..., Settings], migrated_database: str
) -> Iterator[Callable[..., Any]]:
    @contextmanager
    def build(engine: SpeechEngine | None = None, **overrides: Any) -> Iterator[Harness]:
        values: dict[str, Any] = {
            "ws_audio_burst_seconds": 600.0,  # tests stream faster than real time
            "ws_auth_timeout_seconds": 2.0,
            "ws_idle_timeout_seconds": 10.0,
        }
        values.update(overrides)
        settings = make_settings(**values)
        app = create_app(settings, speech_engine=engine or TimelineEngine([]))
        app.state.recording_store = RecordingStore(
            app.state.session_factory, retry_delays=(0.01, 0.02)
        )
        try:
            with TestClient(app) as client:
                yield Harness(client, app, settings, migrated_database)
        finally:
            asyncio.run(_truncate(migrated_database))

    yield build


async def _truncate(url: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        await truncate_all(engine)
    finally:
        await engine.dispose()


def speech(*parts: tuple[str, int], word_ms: int = 300) -> tuple[bytes, list[SpokenWord]]:
    """PCM16 for ("speech" | "silence", ms) parts and the words spoken in it."""
    script = Script(list(parts))
    pcm = script.pcm()
    return pcm, words_in(script.speech_regions, word_ms=word_ms)


def two_sentences() -> tuple[bytes, list[SpokenWord]]:
    return speech(("speech", 2100), ("silence", 1200), ("speech", 2100), ("silence", 1500))


@contextmanager
def live_uvicorn(app: FastAPI, **uvicorn_options: Any) -> Iterator[str]:
    """Serve `app` with a real Uvicorn server on a free port; yields the base URL.

    Defaults mirror the documented command: --ws websockets --ws-max-size 65536.
    """
    import socket
    import threading

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    options: dict[str, Any] = {"ws": "websockets", "ws_max_size": 65536, "log_level": "warning"}
    options.update(uvicorn_options)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, **options))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture
def app_logs(caplog: pytest.LogCaptureFixture) -> Callable[[], pytest.LogCaptureFixture]:
    """create_app() reconfigures logging (removing pytest's capture handler). Call the
    returned function after the app has started to capture its log records again."""
    import logging

    def attach() -> pytest.LogCaptureFixture:
        root = logging.getLogger()
        if caplog.handler not in root.handlers:
            root.addHandler(caplog.handler)
        caplog.handler.setLevel(logging.DEBUG)
        root.setLevel(logging.DEBUG)
        return caplog

    return attach
