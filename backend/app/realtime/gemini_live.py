"""GeminiLiveSession: a RecordingSession backed by the Gemini Live API (gemini_live provider).

    browser PCM16 ──feed()──► 100 ms chunks ──send_realtime_input(audio=Blob)──► Gemini
    Gemini ──receive()──► interim_input_transcription ──► PartialTranscript
                     └──► input_transcription        ──► FinalTranscript (seq 0, 1, …)

Events are the same `SessionEvent`s the local Whisper session emits, so RecordingConnection
persists finals with `RecordingStore.append_final()`, applies the output script and reports
`session_saved` exactly as for Whisper. Nothing here touches the database or the client.

Mapping:
- `server_content.interim_input_transcription.text`: provisional text of the utterance in
  progress. Each one replaces the previous partial of that utterance (version + 1).
- `server_content.input_transcription.text`: finalized text of an utterance, emitted once
  per utterance (on a pause, or after `audio_stream_end`). Becomes one FinalTranscript with
  the next gap-free `seq`. Pieces explicitly marked `finished=False` are collected and
  committed together. Transcripts are never read from `model_turn.parts`, and
  `turn_complete` is not used to detect finals.
- Timing: Gemini gives no word timestamps; `words` stays empty and `avg_logprob` None.
  Utterance times use `start_offset` / `end_offset` when Gemini sends them, otherwise an
  approximation: from the end of the previous final to the audio received so far.
- Language: the BCP-47 `language_code` reduced to its primary subtag ("hi-IN" -> "hi").

Handshake (`open()`): no audio is sent until the server has confirmed the session with
`setup_complete`, as the Live API requires. google-genai's `live.connect()` already reads
that first message before yielding the session and exposes it as `session.setup_complete`;
a `setup_complete` message arriving through `receive()` is honoured too. `open()` waits for
either, within GEMINI_CONNECT_TIMEOUT_SECONDS for the whole handshake. A close, error or
timeout before it is cleaned up and raised as a SpeechProviderError.

Ending (`stop()`): the last partial chunk is sent, then `audio_stream_end`, and the session
keeps receiving until no utterance is pending (or the finalize timeout expires) before it
emits SessionEnded. A pending utterance that never got its final is reported as
UtteranceFailed, never silently dropped. Gemini sessions last at most 10 minutes: the
session closes itself at `max_session_ms` (and on a GoAway notice), like the Whisper
session at its recording limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from app.models.enums import SpeechMode
from app.realtime.contract import ProviderFailure, SpeechProviderError
from app.realtime.endpointer import CommitReason
from app.realtime.events import (
    FinalTranscript,
    PartialTranscript,
    SessionEnded,
    SessionEvent,
    UtteranceDiscarded,
    UtteranceFailed,
)
from app.realtime.session import SessionClosedError, SessionOverloadedError, SessionState
from app.speech.errors import AudioValidationError

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000  # 32
CHUNK_BYTES = 100 * BYTES_PER_MS  # 3200 bytes = 100 ms, as the Live API recommends
AUDIO_MIME_TYPE = f"audio/pcm;rate={SAMPLE_RATE}"
DECODE_STRATEGY = "gemini_live"
MODEL_LABEL_PREFIX = "gemini:"

# Loggers that could print request details (headers carry the API key) at DEBUG level.
QUIET_LOGGERS = ("google_genai", "google.genai", "websockets", "httpx", "httpcore")


# ----------------------------------------------------------------- SDK boundary


class LiveSessionLike(Protocol):
    """The part of google.genai's AsyncSession this module uses (mocked in tests).

    It also reads the optional `setup_complete` attribute (google-genai sets it when
    connect() has already received the server's setup confirmation)."""

    async def send_realtime_input(
        self, *, audio: Any = None, audio_stream_end: bool | None = None
    ) -> None: ...

    def receive(self) -> AsyncIterator[Any]: ...


class GeminiConnector(Protocol):
    """Opens one Live API session per recording and wraps audio for sending."""

    model: str

    def connect(self) -> AbstractAsyncContextManager[LiveSessionLike]: ...

    def audio_blob(self, chunk: bytes) -> Any: ...


@dataclass
class GenAiConnector:
    """GeminiConnector built on the official Google Gen AI SDK (google-genai).

    The SDK is imported here, lazily: with SPEECH_PROVIDER=whisper it is never needed."""

    client: Any
    types: Any
    model: str
    config: Any

    @classmethod
    def create(cls, *, api_key: str, model: str) -> GenAiConnector:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - depends on the installation
            raise RuntimeError(
                "SPEECH_PROVIDER=gemini_live needs the google-genai package "
                "(pip install -r requirements-cloud.txt)"
            ) from exc
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        return cls(client=client, types=types, model=model, config=live_connect_config(types))

    def connect(self) -> AbstractAsyncContextManager[LiveSessionLike]:
        connection: AbstractAsyncContextManager[LiveSessionLike] = self.client.aio.live.connect(
            model=self.model, config=self.config
        )
        return connection

    def audio_blob(self, chunk: bytes) -> Any:
        return self.types.Blob(data=chunk, mime_type=AUDIO_MIME_TYPE)


def live_connect_config(types: Any) -> Any:
    """Transcription-only Live session: text responses, input transcription on, automatic
    language detection (empty language_codes), VERBATIM transcripts (no SMART rewriting)."""
    return types.LiveConnectConfig(
        response_modalities=["TEXT"],
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=[],
            mode=types.AudioTranscriptionConfigMode.VERBATIM,
        ),
    )


def quiet_sdk_loggers() -> None:
    """Never let a DEBUG log level print SDK request details (headers hold the API key)."""
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


# ----------------------------------------------------------------- error handling

_AUTH_MARKERS = ("api key", "api_key", "permission_denied", "unauthenticated", "unauthorized")
_QUOTA_MARKERS = ("resource_exhausted", "quota", "rate limit", "rate-limit", "too many requests")


def classify_provider_error(exc: BaseException) -> ProviderFailure:
    """Map an SDK / network exception to a client-safe failure class.

    The exception text is inspected only to classify it. It is never logged or sent: SDK
    errors can echo request details."""
    if isinstance(exc, TimeoutError):
        return ProviderFailure.UNAVAILABLE
    codes: set[int] = set()
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(value, int):
            codes.add(value)
    text = f"{getattr(exc, 'status', '')} {exc}".lower()
    if codes & {401, 403} or any(marker in text for marker in _AUTH_MARKERS):
        return ProviderFailure.AUTH
    if 429 in codes or any(marker in text for marker in _QUOTA_MARKERS):
        return ProviderFailure.QUOTA
    return ProviderFailure.UNAVAILABLE


def is_normal_close(exc: BaseException) -> bool:
    """The Live connection ended with WebSocket close code 1000 (SDK: APIError(1000))."""
    return getattr(exc, "code", None) == 1000


_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)s\s*$")


def offset_ms(value: object) -> int | None:
    """Protobuf Duration JSON ("1.250s") to milliseconds; None if absent or unparsable."""
    if not isinstance(value, str):
        return None
    match = _DURATION.match(value)
    return round(float(match.group(1)) * 1000) if match else None


def primary_language(code: object) -> str | None:
    """'hi-IN' -> 'hi'; None for missing values."""
    if not isinstance(code, str) or not code.strip():
        return None
    return code.strip().split("-")[0].lower()[:8]  # LANGUAGE_CODE_MAX_LENGTH


# ----------------------------------------------------------------- the session


@dataclass(frozen=True)
class GeminiSessionResult:
    session_id: str
    finals: tuple[FinalTranscript, ...]
    failures: tuple[UtteranceFailed, ...]
    duration_ms: int
    end_reason: CommitReason


@dataclass
class _Utterance:
    utterance_id: int
    start_ms: int
    version: int = 0
    text: str = ""
    end_ms: int = 0


class GeminiLiveSession:
    """RecordingSession for one recording on the Gemini Live API."""

    def __init__(
        self,
        *,
        connector: GeminiConnector,
        on_event: Callable[[SessionEvent], None] | None = None,
        mode: SpeechMode = SpeechMode.AUTO,
        session_id: str = "",
        max_session_ms: int = 600_000,
        max_chunk_bytes: int = 64 * 1024,
        connect_timeout: float = 10.0,
        finalize_timeout: float = 10.0,
        settle_seconds: float = 1.5,
        max_backlog_ms: int = 10_000,
        clock: Callable[[], float] = time.monotonic,  # session-limit clock (tests inject)
    ) -> None:
        self._connector = connector
        self._on_event = on_event
        self.requested_mode = mode  # informational: Gemini detects the language itself
        self.session_id = session_id
        self._max_session_ms = max_session_ms
        self._max_chunk_bytes = max_chunk_bytes
        self._connect_timeout = connect_timeout
        self._finalize_timeout = finalize_timeout
        self._settle = settle_seconds
        self._max_backlog_chunks = max(1, max_backlog_ms // 100)
        self._clock = clock
        self.events: list[SessionEvent] = []

        self._state = SessionState.OPEN
        self._end_reason: CommitReason | None = None
        self._failure: ProviderFailure | None = None
        self._received_bytes = 0
        self._pending = bytearray()  # < CHUNK_BYTES waiting for more audio
        self._outbox: asyncio.Queue[bytes | None] = asyncio.Queue()  # None = stream end
        self._opened_at: float | None = None

        self._live_cm: AbstractAsyncContextManager[LiveSessionLike] | None = None
        self._live: LiveSessionLike | None = None
        self._sender: asyncio.Task[None] | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._activity = asyncio.Event()  # set on every server message
        # The server confirmed the session (setup_complete). Audio is never sent before.
        self._setup_complete = asyncio.Event()
        self._stream_end_sent = False
        # Gemini completed an interaction after audio_stream_end: nothing more is coming.
        # Only used to stop waiting early - never to decide what is final.
        self._done_after_stream_end = False
        self._last_message_at: float | None = None
        self._close_task: asyncio.Task[GeminiSessionResult] | None = None

        self._utterance_ids = itertools.count()
        self._open: _Utterance | None = None
        self._fragments: list[str] = []  # input_transcription pieces with finished=False
        self._seq = 0
        self._last_end_ms = 0
        self._finals: list[FinalTranscript] = []
        self._failures: list[UtteranceFailed] = []

    # ------------------------------------------------------------------ contract

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def received_ms(self) -> int:
        return self._received_bytes // BYTES_PER_MS

    @property
    def finals(self) -> tuple[FinalTranscript, ...]:
        return tuple(self._finals)

    @property
    def failure(self) -> ProviderFailure | None:
        return self._failure

    async def open(self) -> None:
        """Connect to Gemini Live and wait for the server's `setup_complete`.

        Returns only once the session is confirmed: no audio can be sent earlier. One
        deadline (connect_timeout) covers connecting AND the setup confirmation. Raises
        SpeechProviderError (nothing else leaks out); on failure everything opened so far
        (receiver, sender, connection) is closed again."""
        if self._live is not None or self._close_task is not None:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._connect_timeout
        try:
            cm = self._connector.connect()
            live = await asyncio.wait_for(cm.__aenter__(), self._connect_timeout)
        except Exception as exc:
            self._fail(classify_provider_error(exc), exc, during="connect")
            raise SpeechProviderError(self._failure or ProviderFailure.UNAVAILABLE) from None
        self._live_cm, self._live = cm, live
        # google-genai's connect() reads setup_complete itself and keeps it on the session.
        if getattr(live, "setup_complete", None) is not None:
            self._setup_complete.set()
        self._receiver = asyncio.create_task(
            self._receive_loop(live), name=f"gemini-recv-{self.session_id}"
        )
        self._sender = asyncio.create_task(
            self._send_loop(live), name=f"gemini-send-{self.session_id}"
        )
        if not self._setup_complete.is_set():
            try:
                await asyncio.wait_for(self._await_setup(), max(deadline - loop.time(), 0.0))
            except TimeoutError as exc:
                self._fail(ProviderFailure.UNAVAILABLE, exc, during="setup")
        if not self._setup_complete.is_set() and self._failure is None:
            # The receiver ended (connection closed) without confirming the session.
            self._fail(ProviderFailure.UNAVAILABLE, ConnectionError(), during="setup")
        if self._failure is not None:
            failure = self._failure
            await self._shutdown_connection()
            self._live = None
            raise SpeechProviderError(failure)
        self._opened_at = self._clock()

    async def _await_setup(self) -> None:
        """Until setup_complete arrives, or the receiver stops (error or close)."""
        assert self._receiver is not None  # noqa: S101
        setup = asyncio.ensure_future(self._setup_complete.wait())
        try:
            await asyncio.wait({setup, self._receiver}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            setup.cancel()

    async def feed(self, pcm16: bytes | bytearray | memoryview) -> None:
        if self._failure is not None:
            raise SpeechProviderError(self._failure)
        if self._state is not SessionState.OPEN:
            raise SessionClosedError(self._end_reason)
        if self._live is None:
            raise SessionClosedError(None, "session is not open")
        data = bytes(pcm16)
        if len(data) > self._max_chunk_bytes:
            raise AudioValidationError(
                f"audio chunk of {len(data)} bytes exceeds {self._max_chunk_bytes}",
                code="chunk_too_large",
            )
        if len(data) % 2:
            raise AudioValidationError("PCM16 audio must have an even byte length")
        self._received_bytes += len(data)
        self._pending.extend(data)
        while len(self._pending) >= CHUNK_BYTES:
            self._outbox.put_nowait(bytes(self._pending[:CHUNK_BYTES]))
            del self._pending[:CHUNK_BYTES]
        if self._outbox.qsize() > self._max_backlog_chunks:  # Gemini is not keeping up
            self._begin_close(CommitReason.OVERLOADED)
            raise SessionOverloadedError(CommitReason.OVERLOADED, "Gemini send backlog too large")
        if self.received_ms >= self._max_session_ms or self._session_time_exceeded():
            self._begin_close(CommitReason.SESSION_LIMIT)
            return
        await asyncio.sleep(0)  # let the sender and receiver run
        if self._failure is not None:
            # It failed while we yielded: report it as a failure, never as a normal
            # "closed" state (the caller would read that as the recording limit).
            raise SpeechProviderError(self._failure)

    async def stop(self, reason: CommitReason = CommitReason.STOP) -> GeminiSessionResult:
        """Send the remaining audio, wait for pending finals, emit SessionEnded. Idempotent."""
        self._begin_close(reason)
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(), name=f"gemini-close-{self.session_id}"
            )
        return await asyncio.shield(self._close_task)

    async def disconnect(self) -> GeminiSessionResult:
        return await self.stop(CommitReason.DISCONNECT)

    # ------------------------------------------------------------------ internals

    def _emit(self, event: SessionEvent) -> None:
        self.events.append(event)
        if self._on_event is not None:
            self._on_event(event)

    def _session_time_exceeded(self) -> bool:
        if self._opened_at is None:
            return False
        return (self._clock() - self._opened_at) * 1000 >= self._max_session_ms

    def _begin_close(self, reason: CommitReason) -> None:
        if self._state is SessionState.OPEN:
            self._state = SessionState.CLOSING
            self._end_reason = reason

    def _fail(self, failure: ProviderFailure, exc: BaseException, *, during: str) -> None:
        if self._failure is None:
            self._failure = failure
            # Exception type only: the message may contain request details.
            logger.warning(
                "Gemini Live %s failed (%s): %s",
                during,
                failure.value,
                type(exc).__name__,
                extra={"transcription_id": self.session_id},
            )
        self._begin_close(CommitReason.DISCONNECT)
        self._activity.set()

    async def _send_loop(self, live: LiveSessionLike) -> None:
        try:
            await self._setup_complete.wait()  # never send audio before setup_complete
            while True:
                chunk = await self._outbox.get()
                if chunk is None:
                    await live.send_realtime_input(audio_stream_end=True)
                    self._stream_end_sent = True
                    return
                await live.send_realtime_input(audio=self._connector.audio_blob(chunk))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(classify_provider_error(exc), exc, during="send")

    async def _receive_loop(self, live: LiveSessionLike) -> None:
        try:
            while True:
                received = False
                # receive() ends after each completed interaction: listen again.
                async for message in live.receive():
                    received = True
                    self._on_message(message)
                if self._stream_end_sent:
                    self._done_after_stream_end = True
                    self._activity.set()
                if not received:
                    await asyncio.sleep(0.05)  # defensive: never spin on an empty stream
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._state is not SessionState.OPEN and is_normal_close(exc):
                return  # Gemini closed normally after audio_stream_end
            self._fail(classify_provider_error(exc), exc, during="receive")

    @staticmethod
    def _now() -> float:
        """Clock for waiting (finalize / settle): the event loop's, never the injectable
        session-limit clock, so a frozen or skewed clock cannot stall the shutdown."""
        return asyncio.get_running_loop().time()

    def _on_message(self, message: Any) -> None:
        self._last_message_at = self._now()
        if getattr(message, "setup_complete", None) is not None:
            self._setup_complete.set()  # the receiver keeps running for transcripts
        if getattr(message, "go_away", None) is not None:
            logger.info(
                "Gemini Live is ending the session soon (GoAway)",
                extra={"transcription_id": self.session_id},
            )
            self._begin_close(CommitReason.SESSION_LIMIT)
        content = getattr(message, "server_content", None)
        if content is not None:
            interim = getattr(content, "interim_input_transcription", None)
            if interim is not None:
                self._on_interim(interim)
            final = getattr(content, "input_transcription", None)
            if final is not None:
                self._on_final(final)
        self._activity.set()

    def _times(self, transcription: Any, utterance: _Utterance | None) -> tuple[int, int]:
        start = offset_ms(getattr(transcription, "start_offset", None))
        end = offset_ms(getattr(transcription, "end_offset", None))
        if start is None:
            start = utterance.start_ms if utterance else self._last_end_ms
        if end is None:
            end = self.received_ms
        return start, max(start, end)

    def _on_interim(self, transcription: Any) -> None:
        text = (getattr(transcription, "text", None) or "").strip()
        if not text:
            return
        if self._open is None:
            self._open = _Utterance(next(self._utterance_ids), start_ms=self._last_end_ms)
        utterance = self._open
        utterance.start_ms, utterance.end_ms = self._times(transcription, utterance)
        utterance.version += 1
        utterance.text = text
        self._emit(
            PartialTranscript(
                utterance_id=utterance.utterance_id,
                version=utterance.version,
                text=text,
                start_ms=utterance.start_ms,
                end_ms=utterance.end_ms,
            )
        )

    def _on_final(self, transcription: Any) -> None:
        piece = getattr(transcription, "text", None) or ""
        if getattr(transcription, "finished", None) is False:
            self._fragments.append(piece)  # more of this utterance follows
            return
        text = " ".join("".join([*self._fragments, piece]).split())
        self._fragments.clear()
        utterance = self._open
        if not text:
            if utterance is not None:
                start, end = self._times(transcription, utterance)
                self._emit(
                    UtteranceDiscarded(utterance.utterance_id, start, end, reason="no_speech")
                )
                self._open = None
            return
        self._commit(text, transcription, utterance)

    def _commit(self, text: str, transcription: Any, utterance: _Utterance | None) -> None:
        utterance_id = utterance.utterance_id if utterance else next(self._utterance_ids)
        start, end = self._times(transcription, utterance)
        final = FinalTranscript(
            seq=self._seq,
            utterance_id=utterance_id,
            text=text,
            start_ms=start,
            end_ms=end,
            language=primary_language(getattr(transcription, "language_code", None)),
            avg_logprob=None,  # not provided by Gemini
            commit_reason=self._commit_reason(),
            decode_strategy=DECODE_STRATEGY,
            words=(),  # no word-level timestamps from Gemini Live
        )
        self._seq += 1
        self._last_end_ms = max(self._last_end_ms, end)
        self._open = None
        self._finals.append(final)
        self._emit(final)

    def _commit_reason(self) -> CommitReason:
        """PAUSE while recording (Gemini ended the utterance), else why the session ended."""
        if self._state is SessionState.OPEN:
            return CommitReason.PAUSE
        return self._end_reason or CommitReason.STOP

    def _pending_utterance(self) -> bool:
        return self._open is not None or bool(self._fragments)

    async def _await_pending_finals(self) -> None:
        """After audio_stream_end, keep receiving until no utterance is pending AND either
        Gemini completed its interaction or it has been quiet for `settle` seconds; at most
        the finalize timeout. A final can arrive without any earlier partial, so quiet time
        alone must be generous (default 1.5 s)."""
        deadline = self._now() + self._finalize_timeout
        stream_end_at = self._now()
        while self._failure is None and self._receiver is not None and not self._receiver.done():
            now = self._now()
            if now >= deadline:
                return
            last = max(self._last_message_at or stream_end_at, stream_end_at)
            if not self._pending_utterance() and (
                self._done_after_stream_end or now - last >= self._settle
            ):
                return
            self._activity.clear()
            wait = min(deadline - now, self._settle)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._activity.wait(), timeout=max(wait, 0.01))

    async def _close(self) -> GeminiSessionResult:
        reason = self._end_reason or CommitReason.STOP
        if self._live is not None and self._failure is None:
            if self._pending:  # the last, partial chunk
                self._outbox.put_nowait(bytes(self._pending))
                self._pending.clear()
            self._outbox.put_nowait(None)  # -> audio_stream_end
            if self._sender is not None:
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(self._sender), self._finalize_timeout)
            await self._await_pending_finals()

        # Text Gemini already finalized in pieces (finished=False) is real transcript: keep it.
        if self._fragments:
            text = " ".join("".join(self._fragments).split())
            self._fragments.clear()
            if text:
                self._commit(text, None, self._open)
        if self._open is not None:  # a partial whose final never arrived
            utterance = self._open
            self._open = None
            failure = UtteranceFailed(
                utterance_id=utterance.utterance_id,
                start_ms=utterance.start_ms,
                end_ms=max(utterance.end_ms, self.received_ms),
                error_code="provider_error" if self._failure else "final_timeout",
                message="No final transcript was received for this speech",
            )
            self._failures.append(failure)
            self._emit(failure)

        await self._shutdown_connection()
        self._state = SessionState.CLOSED
        result = GeminiSessionResult(
            session_id=self.session_id,
            finals=tuple(self._finals),
            failures=tuple(self._failures),
            duration_ms=self.received_ms,
            end_reason=reason,
        )
        self._emit(
            SessionEnded(
                reason=reason,
                duration_ms=result.duration_ms,
                final_count=len(self._finals),
                failed_count=len(self._failures),
            )
        )
        return result

    async def _shutdown_connection(self) -> None:
        for task in (self._sender, self._receiver):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._sender, self._receiver):
            if task is not None:
                with contextlib.suppress(BaseException):
                    await task
        if self._live_cm is not None:
            cm, self._live_cm = self._live_cm, None
            try:
                await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=5)
            except BaseException as exc:  # closing errors do not matter any more
                logger.debug("Gemini Live close: %s", type(exc).__name__)
