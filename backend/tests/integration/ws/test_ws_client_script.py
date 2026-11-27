"""End to end over a real socket: uvicorn + the sample client script + PostgreSQL."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from app.core.config import Settings
from app.main import create_app
from tests.conftest import DEFAULT_PASSWORD
from tests.integration.ws.conftest import _truncate, db_rows, live_uvicorn, two_sentences
from tests.unit.realtime.fakes import TimelineEngine

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "ws_client.py"


def _load_client() -> Any:
    spec = importlib.util.spec_from_file_location("ws_client", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def live_server(
    make_settings: Callable[..., Settings], migrated_database: str
) -> Iterator[tuple[str, list[Any]]]:
    pcm, words = two_sentences()
    settings = make_settings(ws_audio_burst_seconds=600.0)
    app = create_app(settings, speech_engine=TimelineEngine(words))
    try:
        with live_uvicorn(app) as api:  # --ws websockets --ws-max-size 65536
            yield api, [pcm, words]
    finally:
        asyncio.run(_truncate(migrated_database))


def _write_wav(path: Path, pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(pcm)
    return path


def test_sample_client_streams_a_file_end_to_end(
    live_server: tuple[str, list[Any]], tmp_path: Path, migrated_database: str
) -> None:
    api, (pcm, words) = live_server
    register = httpx.post(
        f"{api}/api/v1/auth/register",
        json={"email": "client@example.com", "password": DEFAULT_PASSWORD},
    )
    assert register.status_code == 201
    wav = _write_wav(tmp_path / "clip.wav", pcm)

    out = io.StringIO()
    code = _load_client().main(
        [
            str(wav),
            "--api",
            api,
            "--email",
            "client@example.com",
            "--password",
            DEFAULT_PASSWORD,
            "--mode",
            "hi",
            "--speed",
            "0",
            "--title",
            "From the client",
        ],
        stdout=out,
    )
    output = out.getvalue()
    assert code == 0, output
    assert "session_started" in output and "final #1" in output
    assert "session_saved    status=completed" in output
    assert DEFAULT_PASSWORD not in output
    (row,) = db_rows(migrated_database, "SELECT * FROM transcriptions")
    assert (row["title"], row["status"], row["source"]) == ("From the client", "completed", "live")
    assert row["text"] == " ".join(w.text for w in words)


def test_sample_client_requires_credentials(tmp_path: Path) -> None:
    wav = _write_wav(tmp_path / "x.wav", (np.zeros(1600, dtype="<i2")).tobytes())
    out = io.StringIO()
    assert _load_client().main([str(wav), "--api", "http://127.0.0.1:9"], stdout=out) == 2
    assert "--token" in out.getvalue()
