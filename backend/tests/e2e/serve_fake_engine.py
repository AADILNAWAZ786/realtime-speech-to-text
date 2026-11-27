"""Serve the real app with the fake speech engine, for browser end-to-end tests.

    python -m tests.e2e.serve_fake_engine --port 8765 --origin http://localhost:5173 \
        --wav /tmp/speech.wav

- Database: TEST_DATABASE_URL (must end in "_test"). Its schema is wiped and rebuilt with
  the real Alembic migrations, exactly like the pytest suite does.
- Speech: TimelineEngine from tests/unit/realtime/fakes.py - a deterministic "recogniser"
  that returns Hinglish words laid along the recording timeline. No model is downloaded.
- --wav writes a 48 kHz STEREO speech-like test signal (speech / silence pattern) that
  Chrome can play as a fake microphone (--use-file-for-fake-audio-capture). Stereo 48 kHz
  exercises the browser's downmix and 48 kHz -> 16 kHz resampling.
- Uvicorn runs as documented: --ws websockets --ws-max-size 65536. WebSocket limits are the
  production defaults (real-time pacing is enforced).

Test-only tool: never use it as a server.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import wave
from pathlib import Path

import numpy as np
import uvicorn
from sqlalchemy.engine import make_url

from alembic import command
from app.core.config import Settings
from app.main import create_app
from tests.conftest import _reset_schema, _TestEnv, alembic_config
from tests.unit.realtime.fakes import SpokenWord, TimelineEngine

SCRIPT = (
    "aaj ki meeting mein hum project ka status discuss karenge "
    "frontend ready hai aur backend tests pass ho gaye "
    "next week deployment plan karte hain thank you everyone"
)
VOCABULARY = SCRIPT.split()

# (kind, ms): the spoken pattern of the generated WAV.
PATTERN = [
    ("silence", 600),
    ("speech", 2400),
    ("silence", 1200),
    ("speech", 2700),
    ("silence", 1500),
]


def hinglish_words(total_ms: int = 600_000, word_ms: int = 300) -> list[SpokenWord]:
    """Words every `word_ms` along the whole timeline: any decoded speech window has text."""
    return [
        SpokenWord(VOCABULARY[i % len(VOCABULARY)], start, start + word_ms)
        for i, start in enumerate(range(0, total_ms - word_ms, word_ms))
    ]


def voiced(seconds: float, rate: int, start: int) -> np.ndarray:
    """Speech-like signal (harmonics + syllable-rate modulation), detected as speech by VAD."""
    t = (np.arange(int(seconds * rate)) + start) / rate
    harmonics = sum(0.3 / k * np.sin(2 * np.pi * 140 * k * t) for k in range(1, 12))
    return np.asarray(harmonics * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)), dtype=np.float64)


def write_wav(path: Path, rate: int = 48_000) -> float:
    """Write the PATTERN as 48 kHz stereo PCM16 (right channel slightly quieter)."""
    parts, cursor = [], 0
    for kind, ms in PATTERN:
        n = rate * ms // 1000
        parts.append(voiced(ms / 1000, rate, cursor) if kind == "speech" else np.zeros(n))
        cursor += n
    mono = np.clip(np.concatenate(parts) * 0.8, -1, 1)
    stereo = np.stack([mono, mono * 0.7], axis=1)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes((stereo * 32767).astype("<i2").tobytes())
    return len(mono) / rate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--origin", default="http://localhost:5173")
    parser.add_argument("--wav", type=Path, help="write the fake-microphone WAV here and continue")
    args = parser.parse_args()

    url = _TestEnv().test_database_url
    if url is None:
        raise SystemExit("TEST_DATABASE_URL is not set (see .env.example)")
    database_url = url.get_secret_value()
    if not (make_url(database_url).database or "").endswith("_test"):
        raise SystemExit("TEST_DATABASE_URL must name a database ending in _test")

    if args.wav:
        seconds = write_wav(args.wav)
        print(f"wrote {args.wav} ({seconds:.1f} s, 48 kHz stereo)", flush=True)

    asyncio.run(_reset_schema(database_url))
    command.upgrade(alembic_config(database_url), "head")

    settings = Settings(
        app_env="development",
        database_url=database_url,
        jwt_secret_key=secrets.token_urlsafe(48),  # per run; never stored
        cors_allowed_origins=[args.origin],
        login_rate_limit_enabled=False,
        log_level="WARNING",
    )
    app = create_app(settings, speech_engine=TimelineEngine(hinglish_words()))
    print(f"fake-engine server on http://{args.host}:{args.port}", flush=True)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ws="websockets",
        ws_max_size=65536,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
