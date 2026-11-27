"""Mocked Gemini Live SDK for tests. No network, no API key, no real Gemini calls.

Server messages are built with the REAL google.genai `types` classes, so a field name the
adapter reads that does not exist in the SDK makes these tests fail.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from google.genai import errors, types

from app.realtime.gemini_live import AUDIO_MIME_TYPE, live_connect_config

MODEL = "gemini-3.5-transcribe-live"


class EndOfInteraction:
    """Makes the fake `receive()` iterator return, like the SDK after a completed turn."""


END_OF_INTERACTION = EndOfInteraction()

Item = types.LiveServerMessage | EndOfInteraction | BaseException


def interim(text: str, **fields: Any) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            interim_input_transcription=types.Transcription(text=text, **fields)
        )
    )


def final(text: str, **fields: Any) -> types.LiveServerMessage:
    return types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text=text, **fields)
        )
    )


def setup_complete_message() -> types.LiveServerMessage:
    return types.LiveServerMessage(setup_complete=types.LiveServerSetupComplete(session_id="s1"))


def go_away(time_left: str = "5s") -> types.LiveServerMessage:
    return types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left=time_left))


def api_error(code: int, status: str, message: str) -> errors.APIError:
    """What the SDK raises (HTTP errors, or WebSocket close codes on a live connection)."""
    payload = {"error": {"code": code, "status": status, "message": message}}
    if 400 <= code < 500:
        return errors.ClientError(code, payload)
    if 500 <= code < 600:
        return errors.ServerError(code, payload)
    return errors.APIError(code, payload)


# Reacts to what the client sends: (live, audio_bytes_so_far, stream_ended) -> messages.
Reactor = Callable[["FakeLive", int, bool], list[Item]]


class FakeLive:
    """Stands in for google.genai's live AsyncSession."""

    def __init__(self, reactor: Reactor | None = None, latency: float = 0.0) -> None:
        self.reactor = reactor
        self.latency = latency  # delay before reactor items arrive (Gemini is not instant)
        self.blobs: list[types.Blob] = []
        self.audio = bytearray()
        self.stream_ended = False
        # Timeline, in order: "setup_complete" (delivered to the client), "audio",
        # "audio_stream_end".
        self.calls: list[str] = []
        # Like google-genai 3.6.1: set when connect() already consumed setup_complete.
        self.setup_complete: types.LiveServerSetupComplete | None = None
        self.inbox: asyncio.Queue[Item] = asyncio.Queue()
        self.send_error: BaseException | None = None
        self.send_gate: asyncio.Event | None = None  # set to block sending (backpressure)
        self.receive_calls = 0

    async def send_realtime_input(
        self, *, audio: Any = None, audio_stream_end: bool | None = None
    ) -> None:
        if self.send_gate is not None:
            await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        if audio is not None:
            assert isinstance(audio, types.Blob)
            assert audio.mime_type == AUDIO_MIME_TYPE
            assert audio.data is not None
            self.blobs.append(audio)
            self.audio.extend(audio.data)
            self.calls.append("audio")
        if audio_stream_end:
            self.stream_ended = True
            self.calls.append("audio_stream_end")
        if self.reactor is not None:
            for item in self.reactor(self, len(self.audio), self.stream_ended):
                if self.latency:
                    asyncio.get_running_loop().call_later(self.latency, self.push, item)
                else:
                    self.push(item)

    def push(self, item: Item) -> None:
        self.inbox.put_nowait(item)

    async def receive(self) -> AsyncIterator[types.LiveServerMessage]:
        self.receive_calls += 1
        while True:
            item = await self.inbox.get()
            if isinstance(item, EndOfInteraction):
                return
            if isinstance(item, BaseException):
                raise item
            if item.setup_complete is not None:
                self.calls.append("setup_complete")
            yield item


@dataclass
class FakeConnector:
    """GeminiConnector double: records connections, can fail on connect.

    `setup` = how the server confirms the session:
      "message" (default): a setup_complete message through receive(), after setup_delay
      "sdk":     already consumed by connect() (session.setup_complete), as google-genai does
      "never":   no confirmation at all
      "close":   the connection fails before any confirmation (setup_error)
    """

    model: str = MODEL
    setup: str = "message"
    setup_delay: float = 0.0
    setup_error: BaseException | None = None
    reactor: Reactor | None = None
    latency: float = 0.0
    connect_error: BaseException | None = None
    connect_delay: float = 0.0
    sessions: list[FakeLive] = field(default_factory=list)
    closed: int = 0
    config: Any = field(default_factory=lambda: live_connect_config(types))

    def connect(self) -> Any:
        return self._connect()

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[FakeLive]:
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        if self.connect_error is not None:
            raise self.connect_error
        live = FakeLive(self.reactor, self.latency)
        if self.setup == "sdk":
            live.setup_complete = types.LiveServerSetupComplete(session_id="s1")
        elif self.setup == "message":
            if self.setup_delay:
                asyncio.get_running_loop().call_later(
                    self.setup_delay, live.push, setup_complete_message()
                )
            else:
                live.push(setup_complete_message())
        elif self.setup == "close":
            live.push(self.setup_error or api_error(1011, "", "closed before setup"))
        self.sessions.append(live)
        try:
            yield live
        finally:
            self.closed += 1

    def audio_blob(self, chunk: bytes) -> types.Blob:
        return types.Blob(data=chunk, mime_type=AUDIO_MIME_TYPE)

    @property
    def live(self) -> FakeLive:
        assert self.sessions, "no Gemini session was opened"
        return self.sessions[-1]


def scripted(steps: list[tuple[int | str, Item]]) -> Reactor:
    """Reactor emitting each item once its trigger is reached: a byte count of received
    audio, or "end" (after audio_stream_end)."""
    pending = list(steps)

    def react(live: FakeLive, audio_bytes: int, ended: bool) -> list[Item]:
        due: list[Item] = []
        while pending:
            trigger, item = pending[0]
            ready = ended if trigger == "end" else audio_bytes >= int(trigger)
            if not ready:
                break
            due.append(item)
            pending.pop(0)
        return due

    return react


def pcm(ms: int, value: int = 1000) -> bytes:
    """`ms` of 16 kHz mono PCM16 with a constant sample value."""
    return value.to_bytes(2, "little", signed=True) * (16 * ms)


def chunks(data: bytes, size: int) -> Iterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]
