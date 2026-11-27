#!/usr/bin/env python3
"""Sample client for /ws/transcribe: streams an audio file and prints live results.

Usage (from backend/, with the server running):
    python scripts/ws_client.py recording.wav --email you@example.com --password '...' \\
        --mode mixed --script roman

    STT_TOKEN=<access token> python scripts/ws_client.py clip.m4a --speed 2

The file is converted to 16 kHz mono PCM16 (any format PyAV can read) and sent at real-time
pace (--speed 2 = twice as fast; the server allows bursts up to WS_AUDIO_BURST_SECONDS and
then at most WS_AUDIO_RATE_FACTOR x real time). The token is never printed.

Exit code: 0 when the recording was saved as completed, 1 otherwise, 2 for usage errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.speech.audio import load_audio_file

FRAME_TOO_BIG = 1009  # WebSocket close code: message too big


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stream an audio file to /ws/transcribe.")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--api", default="http://localhost:8000", help="Backend base URL")
    parser.add_argument("--ws-url", help="WebSocket URL (default: derived from --api)")
    parser.add_argument("--origin", default="http://localhost:5173", help="Origin header")
    parser.add_argument("--email")
    parser.add_argument("--password")
    parser.add_argument("--token", help="Access token (or set STT_TOKEN)")
    parser.add_argument("--mode", default="auto", choices=["auto", "en", "hi", "mixed"])
    parser.add_argument(
        "--script", default="as_recognised", choices=["as_recognised", "roman", "devanagari"]
    )
    parser.add_argument("--title")
    parser.add_argument("--chunk-ms", type=int, default=100)
    parser.add_argument("--speed", type=float, default=1.0, help="0 = no pacing")
    parser.add_argument("--json", action="store_true", help="Print raw server messages")
    return parser


def ws_url_from_api(api: str) -> str:
    parts = urlparse(api)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunparse((scheme, parts.netloc, "/ws/transcribe", "", "", ""))


def login(api: str, email: str, password: str) -> str:
    request = urllib.request.Request(  # noqa: S310 - URL comes from the user's own CLI args
        f"{api.rstrip('/')}/api/v1/auth/login",
        data=json.dumps({"email": email, "password": password}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        token: str = json.load(response)["access_token"]
        return token


def _ms(ms: int) -> str:
    return f"{ms // 60_000:02d}:{ms % 60_000 / 1000:06.3f}"


def _print_event(message: dict[str, Any], out: TextIO, raw: bool) -> None:
    if raw:
        out.write(json.dumps(message, ensure_ascii=False) + "\n")
        return
    kind = message["type"]
    if kind == "session_started":
        out.write(
            f"session_started  id={message['transcription_id']} model={message['model_name']} "
            f"mode={message['speech_mode']} script={message['script_preference']}\n"
        )
    elif kind == "partial":
        out.write(f"  … {message['text']}\n")
    elif kind == "final":
        saved = "" if message["saved"] else "  [NOT SAVED]"
        out.write(
            f"final #{message['seq']} [{_ms(message['start_ms'])} -> {_ms(message['end_ms'])}] "
            f"{message['text']}{saved}\n"
        )
    elif kind == "session_saved":
        out.write(
            f"session_saved    status={message['status']} reason={message['end_reason']} "
            f"duration={message['duration_ms'] / 1000:.2f}s segments={message['saved_segments']}"
            f" unsaved={message['unsaved_segments']} failed={message['failed_utterances']}\n"
            f"text: {message['text']}\n"
        )
    else:
        out.write(f"{kind}: {json.dumps({k: v for k, v in message.items() if k != 'type'})}\n")
    out.flush()


async def stream(args: argparse.Namespace, token: str, pcm: bytes, out: TextIO) -> int:
    url = args.ws_url or ws_url_from_api(args.api)
    start: dict[str, Any] = {
        "type": "start",
        "token": token,
        "speech_mode": args.mode,
        "script_preference": args.script,
        "format": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
    }
    if args.title:
        start["title"] = args.title
    saved: dict[str, Any] = {}

    async with connect(url, origin=args.origin, max_size=2**20) as ws:
        await ws.send(json.dumps(start))
        first = json.loads(await ws.recv())
        _print_event(first, out, args.json)
        if first["type"] != "session_started":
            return 1

        async def reader() -> None:
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    _print_event(message, out, args.json)
                    if message["type"] == "session_saved":
                        saved.update(message)
            except ConnectionClosed:
                pass

        reading = asyncio.create_task(reader())
        step = 16_000 * args.chunk_ms // 1000 * 2
        # Never send more than the server accepts per frame (announced in session_started).
        max_frame = int(first.get("max_frame_bytes", 65_536)) // 2 * 2
        if step > max_frame:
            out.write(
                f"note: --chunk-ms {args.chunk_ms} exceeds the server's frame limit; "
                f"sending {max_frame}-byte frames\n"
            )
            step = max_frame
        delay = args.chunk_ms / 1000 / args.speed if args.speed > 0 else 0
        try:
            for offset in range(0, len(pcm), step):
                await ws.send(pcm[offset : offset + step])
                if delay:
                    await asyncio.sleep(delay)
            await ws.send(json.dumps({"type": "stop"}))
        except ConnectionClosed:
            pass  # the server ended the recording (limit/error); the reader has the reason
        await reading
        close_code = ws.close_code
    if close_code == FRAME_TOO_BIG:
        out.write(
            "error: the server rejected an audio frame as too large (close 1009); "
            "use a smaller --chunk-ms or check the server's --ws-max-size\n"
        )
    return 0 if saved.get("status") == "completed" else 1


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    out = stdout or sys.stdout
    args = build_parser().parse_args(argv)
    token = args.token or os.environ.get("STT_TOKEN")
    if not token:
        if not (args.email and args.password):
            out.write("error: give --token / STT_TOKEN, or --email and --password\n")
            return 2
        try:
            token = login(args.api, args.email, args.password)
        except urllib.error.URLError as exc:
            out.write(f"error: login failed ({exc})\n")
            return 1
    audio = load_audio_file(args.audio)
    samples = np.clip(audio.samples, -1.0, 1.0)
    pcm = (samples * 32767).astype("<i2").tobytes()
    out.write(f"streaming {args.audio} ({audio.duration_seconds:.2f} s)\n")
    return asyncio.run(stream(args, token, pcm, out))


if __name__ == "__main__":
    raise SystemExit(main())
