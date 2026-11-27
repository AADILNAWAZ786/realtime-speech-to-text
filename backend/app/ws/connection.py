"""The /ws/transcribe connection: handshake, then a per-recording event pipeline.

    receive loop ──feed()──► TranscriptionSession ──on_event──► events queue
                                                                   │
                                       ┌───────────────────────────┘
                                       ▼
                            event consumer (one task)
                     finals: persist FIRST, then queue for sending
                     partials & notices: queue for sending (never persisted)
                                       │
                                       ▼
                     outbound queue ──► sender (one task, the only WebSocket writer)

- The sender is the only code that writes to the socket, so writes never interleave.
- A broken socket only marks the sender as broken; the consumer keeps persisting.
- `session.stop()` / `disconnect()` drain every final before SessionEnded is emitted; the
  consumer finishes the database record when it sees SessionEnded, so no final is lost.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from app.core.config import Settings
from app.core.exceptions import AuthenticationError
from app.models.enums import ScriptPreference, TranscriptionStatus
from app.realtime.contract import ProviderFailure, RecordingSession, SpeechProviderError
from app.realtime.endpointer import CommitReason
from app.realtime.events import (
    FinalTranscript,
    PartialTranscript,
    PreviewPaused,
    PreviewResumed,
    SessionEnded,
    SessionEvent,
    UtteranceDiscarded,
    UtteranceFailed,
)
from app.realtime.session import (
    SessionClosedError,
    SessionOverloadedError,
    SessionState,
)
from app.services.auth_service import AuthService
from app.speech.errors import AudioValidationError
from app.speech.script import render
from app.ws.limits import AudioRateLimiter
from app.ws.persistence import DisplayedFinal, RecordingStore, displayed_text
from app.ws.protocol import (
    CloseCode,
    ErrorCode,
    ProtocolError,
    StartMessage,
    StopMessage,
    error_message,
    parse_client_message,
    session_started_message,
)
from app.ws.runtime import SpeechRuntime

logger = logging.getLogger(__name__)


class EndReason(StrEnum):
    STOPPED = "stopped"
    RECORDING_LIMIT = "recording_limit"
    DISCONNECTED = "disconnected"
    IDLE_TIMEOUT = "idle_timeout"
    RATE_LIMITED = "rate_limited"
    PROTOCOL_ERROR = "protocol_error"
    OVERLOADED = "overloaded"
    SHUTDOWN = "shutdown"
    INTERNAL_ERROR = "internal_error"
    PROVIDER_ERROR = "provider_error"  # the cloud speech provider failed mid-recording


@dataclass(frozen=True, slots=True)
class Ending:
    reason: EndReason
    status: TranscriptionStatus
    commit_reason: CommitReason
    close_code: CloseCode | None  # None: the client is already gone
    error: ErrorCode | None = None


def _ending(
    reason: EndReason, close_code: CloseCode | None, error: ErrorCode | None = None
) -> Ending:
    completed = reason in (EndReason.STOPPED, EndReason.RECORDING_LIMIT)
    return Ending(
        reason=reason,
        status=TranscriptionStatus.COMPLETED if completed else TranscriptionStatus.INTERRUPTED,
        commit_reason=CommitReason.STOP if completed else CommitReason.DISCONNECT,
        close_code=close_code,
        error=error,
    )


def provider_error(failure: ProviderFailure) -> tuple[ErrorCode, CloseCode]:
    """Client-safe error for a speech provider failure. A rejected API key is a server
    configuration problem: the client only learns that the service is unavailable."""
    if failure is ProviderFailure.QUOTA:
        return ErrorCode.TRANSCRIPTION_QUOTA_EXCEEDED, CloseCode.TRY_AGAIN_LATER
    if failure is ProviderFailure.AUTH:
        return ErrorCode.TRANSCRIPTION_UNAVAILABLE, CloseCode.INTERNAL_ERROR
    return ErrorCode.TRANSCRIPTION_UNAVAILABLE, CloseCode.TRY_AGAIN_LATER


@dataclass(frozen=True, slots=True)
class _Close:
    code: CloseCode | None


# ---------------------------------------------------------------- handshake


def text_exceeds_limit(text: str, limit_bytes: int) -> bool:
    """WS_MAX_TEXT_BYTES is a byte limit: measure the UTF-8 size, not characters (one
    Devanagari character is 3 bytes). Logs the sizes only, never the content."""
    size = len(text.encode("utf-8"))
    if size > limit_bytes:
        logger.warning(
            "WebSocket text frame of %d bytes exceeds the %d-byte limit", size, limit_bytes
        )
        return True
    return False


def origin_allowed(origin: str | None, settings: Settings) -> bool:
    if origin is None:
        return settings.ws_allow_missing_origin
    return origin.rstrip("/") in settings.cors_allowed_origins


async def _reject(websocket: WebSocket, error: ProtocolError) -> None:
    """Send a generic error and close. The client may already be gone: ignore failures."""
    with contextlib.suppress(Exception):
        await websocket.send_json(error_message(error.code))
    with contextlib.suppress(Exception):
        await websocket.close(code=error.close_code)


async def _receive_start(websocket: WebSocket, settings: Settings) -> StartMessage | None:
    try:
        message = await asyncio.wait_for(websocket.receive(), settings.ws_auth_timeout_seconds)
    except TimeoutError as exc:
        raise ProtocolError(ErrorCode.AUTH_TIMEOUT, CloseCode.TIMEOUT) from exc
    if message["type"] == "websocket.disconnect":
        return None
    if message.get("bytes") is not None:  # audio before authentication
        raise ProtocolError(ErrorCode.AUTH_REQUIRED, CloseCode.UNAUTHORIZED)
    text = message.get("text") or ""
    if text_exceeds_limit(text, settings.ws_max_text_bytes):
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, CloseCode.BAD_REQUEST)
    parsed = parse_client_message(text)
    if not isinstance(parsed, StartMessage):
        raise ProtocolError(ErrorCode.AUTH_REQUIRED, CloseCode.UNAUTHORIZED)
    return parsed


async def _authenticate(websocket: WebSocket, token: str) -> uuid.UUID:
    app = websocket.app
    try:
        async with app.state.session_factory() as db:
            user = await AuthService(db, app.state.settings).get_user_from_token(token)
    except AuthenticationError as exc:
        code = ErrorCode.TOKEN_EXPIRED if exc.code == "token_expired" else ErrorCode.INVALID_TOKEN
        raise ProtocolError(code, CloseCode.UNAUTHORIZED) from exc
    except Exception as exc:
        # e.g. the database is unreachable during the user lookup. Log only the error type:
        # driver messages can contain SQL, parameters or connection details, and the
        # token must never be logged. The client gets a generic internal error.
        logger.error("WebSocket authentication failed: %s", type(exc).__name__)
        raise ProtocolError(ErrorCode.INTERNAL_ERROR, CloseCode.INTERNAL_ERROR) from exc
    user_id: uuid.UUID = user.id
    return user_id


def _default_title() -> str:
    return f"Recording - {datetime.now(UTC):%d %b %Y, %H:%M} UTC"


async def handle_transcribe(websocket: WebSocket) -> None:
    """Entry point for /ws/transcribe."""
    app = websocket.app
    settings: Settings = app.state.settings
    runtime: SpeechRuntime = app.state.speech
    store: RecordingStore = app.state.recording_store

    if not origin_allowed(websocket.headers.get("origin"), settings):
        logger.warning("WebSocket rejected: origin not allowed")
        await websocket.close(code=CloseCode.POLICY_VIOLATION)  # before accept -> HTTP 403
        return
    await websocket.accept()

    try:
        start = await _receive_start(websocket, settings)
        if start is None:
            return
        user_id = await _authenticate(websocket, start.token)
        lease = runtime.registry.acquire(user_id)
    except ProtocolError as error:
        await _reject(websocket, error)
        return

    pipeline: asyncio.Task[None] | None = None
    try:
        try:
            transcription_id = await store.create_recording(
                user_id=user_id,
                title=start.title or _default_title(),
                speech_mode=start.speech_mode,
                script_preference=start.script_preference,
                model_name=runtime.model_name,
            )
        except Exception:
            logger.exception("Could not create the recording")
            await _reject(
                websocket, ProtocolError(ErrorCode.INTERNAL_ERROR, CloseCode.INTERNAL_ERROR)
            )
            return
        connection = RecordingConnection(
            websocket,
            settings=settings,
            runtime=runtime,
            store=store,
            transcription_id=transcription_id,
            start=start,
        )
        try:
            await connection.session.open()  # Gemini: connect now; Whisper: no-op
        except SpeechProviderError as exc:
            # Nothing was streamed: remove the empty recording so failed attempts do not
            # fill the history, then tell the client (generic message only).
            with contextlib.suppress(Exception):
                await store.discard_recording(transcription_id)
            await _reject(websocket, ProtocolError(*provider_error(exc.failure)))
            return
        lease.attach(connection)
        # The pipeline runs in its own task that owns the slot: if this handler is
        # cancelled (server-side cancellation, abrupt shutdown), the recording still
        # commits its audio, saves every final and is marked interrupted.
        pipeline = connection.start(on_done=lease.release)
        try:
            await asyncio.shield(pipeline)
        except asyncio.CancelledError:
            connection.request_disconnect()
            raise
    finally:
        if pipeline is None:
            lease.release()


# ---------------------------------------------------------------- per-recording pipeline


class RecordingConnection:
    def __init__(
        self,
        websocket: WebSocket,
        *,
        settings: Settings,
        runtime: SpeechRuntime,
        store: RecordingStore,
        transcription_id: uuid.UUID,
        start: StartMessage,
    ) -> None:
        self._ws = websocket
        self._settings = settings
        self._runtime = runtime
        self._store = store
        self.transcription_id = transcription_id
        self._speech_mode = start.speech_mode
        self._script: ScriptPreference = start.script_preference

        self._events: asyncio.Queue[SessionEvent] = asyncio.Queue()
        self._outbound: asyncio.Queue[dict[str, Any] | _Close] = asyncio.Queue()
        # Whisper (TranscriptionSession) or Gemini Live (GeminiLiveSession), per
        # SPEECH_PROVIDER. Both emit the same events into this connection's pipeline.
        self.session: RecordingSession = runtime.new_session(
            mode=start.speech_mode,
            on_event=self._events.put_nowait,  # sync callback -> async pipeline
            session_id=str(transcription_id),
        )
        self._limiter = AudioRateLimiter(
            factor=settings.ws_audio_rate_factor, burst_seconds=settings.ws_audio_burst_seconds
        )

        self._ending: Ending | None = None
        self._ending_known = asyncio.Event()  # the consumer finishes only after this
        self._receive_task: asyncio.Task[Ending] | None = None
        self._task: asyncio.Task[None] | None = None
        self._interrupt_ending: Ending | None = None
        self._socket_broken = False
        self._closed = asyncio.Event()
        self._db_finished = False

        self.saved: list[DisplayedFinal] = []
        self.unsaved: list[DisplayedFinal] = []
        self.failed_utterances: list[UtteranceFailed] = []

    # ----- control

    def start(self, *, on_done: Callable[[], None]) -> asyncio.Task[None]:
        """Run the pipeline in its own task (kept referenced until it finishes)."""

        async def runner() -> None:
            try:
                await self.run()
            finally:
                on_done()

        self._task = asyncio.create_task(runner(), name=f"recording-{self.transcription_id}")
        return self._task

    def request_shutdown(self) -> None:
        """Application shutdown: stop receiving, then commit and save like a disconnect."""
        self._interrupt(
            _ending(EndReason.SHUTDOWN, CloseCode.SERVICE_RESTART, ErrorCode.SERVER_SHUTDOWN)
        )

    def request_disconnect(self) -> None:
        """The request handler was cancelled: the client can no longer be reached."""
        self._socket_broken = True
        self._interrupt(_ending(EndReason.DISCONNECTED, None))

    def _interrupt(self, ending: Ending) -> None:
        if self._interrupt_ending is None:
            self._interrupt_ending = ending
        if self._receive_task is not None and not self._receive_task.done():
            self._receive_task.cancel()

    async def wait_closed(self) -> None:
        await self._closed.wait()

    def _send(self, message: dict[str, Any]) -> None:
        self._outbound.put_nowait(message)

    # ----- main flow

    async def run(self) -> None:
        sender = asyncio.create_task(self._send_loop(), name=f"ws-send-{self.transcription_id}")
        consumer = asyncio.create_task(self._consume(), name=f"ws-events-{self.transcription_id}")
        close_code: CloseCode | None = CloseCode.INTERNAL_ERROR
        try:
            self._send(
                session_started_message(
                    transcription_id=self.transcription_id,
                    speech_mode=self._speech_mode,
                    script_preference=self._script,
                    model_name=self._runtime.model_name,
                    max_frame_bytes=self._settings.ws_max_frame_bytes,
                    max_duration_ms=self._runtime.max_recording_seconds * 1000,
                )
            )
            self._receive_task = asyncio.create_task(self._receive_loop())
            if self._interrupt_ending is not None:  # interrupted before receiving started
                self._receive_task.cancel()
            try:
                ending = await self._receive_task
            except asyncio.CancelledError:
                if self._interrupt_ending is None:
                    raise
                ending = self._interrupt_ending
            except Exception:
                logger.exception("Receive loop failed")
                ending = _ending(
                    EndReason.INTERNAL_ERROR, CloseCode.INTERNAL_ERROR, ErrorCode.INTERNAL_ERROR
                )

            self._ending = ending
            self._ending_known.set()
            close_code = ending.close_code
            if ending.error is not None:
                self._send(error_message(ending.error))
            # Commit remaining audio and wait for every final; SessionEnded follows them.
            await self.session.stop(ending.commit_reason)
            await consumer  # persists everything, finishes the record, queues session_saved
        except BaseException:
            self._ending_known.set()
            consumer.cancel()
            raise
        finally:
            await self._ensure_db_finished()
            self._outbound.put_nowait(_Close(close_code))
            try:
                await asyncio.wait_for(sender, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                sender.cancel()
            self._closed.set()

    async def _receive_loop(self) -> Ending:
        settings = self._settings
        while True:
            try:
                message = await asyncio.wait_for(
                    self._ws.receive(), settings.ws_idle_timeout_seconds
                )
            except TimeoutError:
                return _ending(EndReason.IDLE_TIMEOUT, CloseCode.TIMEOUT, ErrorCode.IDLE_TIMEOUT)
            if message["type"] == "websocket.disconnect":
                self._socket_broken = True  # nothing more can be sent
                return _ending(EndReason.DISCONNECTED, None)
            data: bytes | None = message.get("bytes")
            if data is not None:
                ending = await self._on_audio(data)
                if ending is not None:
                    return ending
                continue
            text = message.get("text") or ""
            if text_exceeds_limit(text, settings.ws_max_text_bytes):
                return self._protocol_error(ErrorCode.INVALID_MESSAGE, CloseCode.BAD_REQUEST)
            try:
                parsed = parse_client_message(text)
            except ProtocolError as error:
                return self._protocol_error(error.code, error.close_code)
            if isinstance(parsed, StopMessage):
                return _ending(EndReason.STOPPED, CloseCode.NORMAL)
            return self._protocol_error(ErrorCode.INVALID_MESSAGE, CloseCode.BAD_REQUEST)

    @staticmethod
    def _protocol_error(code: ErrorCode, close: CloseCode) -> Ending:
        return _ending(EndReason.PROTOCOL_ERROR, close, code)

    async def _on_audio(self, data: bytes) -> Ending | None:
        if len(data) > self._settings.ws_max_frame_bytes:
            return self._protocol_error(ErrorCode.FRAME_TOO_LARGE, CloseCode.MESSAGE_TOO_BIG)
        if len(data) == 0 or len(data) % 2:
            return self._protocol_error(ErrorCode.INVALID_AUDIO, CloseCode.BAD_REQUEST)
        if not self._limiter.allow(len(data) // 2):
            return _ending(EndReason.RATE_LIMITED, CloseCode.RATE_LIMITED, ErrorCode.RATE_LIMITED)
        try:
            await self.session.feed(data)
        except SpeechProviderError as exc:  # before SessionClosedError: it is a subclass
            code, close = provider_error(exc.failure)
            return _ending(EndReason.PROVIDER_ERROR, close, code)
        except SessionOverloadedError:
            return _ending(
                EndReason.OVERLOADED, CloseCode.TRY_AGAIN_LATER, ErrorCode.SERVER_OVERLOADED
            )
        except SessionClosedError:
            return _ending(EndReason.RECORDING_LIMIT, CloseCode.NORMAL)
        except AudioValidationError:
            return self._protocol_error(ErrorCode.INVALID_AUDIO, CloseCode.BAD_REQUEST)
        if self.session.state is not SessionState.OPEN:  # max recording length reached
            return _ending(EndReason.RECORDING_LIMIT, CloseCode.NORMAL)
        return None

    # ----- event consumer (the only place finals are persisted)

    async def _consume(self) -> None:
        while True:
            event = await self._events.get()
            if isinstance(event, SessionEnded):
                await self._finish(event)
                return
            try:
                await self._handle_event(event)
            except Exception:  # never let one event stop the pipeline
                logger.exception("Failed to handle session event %s", event.type)

    async def _handle_event(self, event: SessionEvent) -> None:
        if isinstance(event, PartialTranscript):
            self._send(
                {
                    "type": "partial",
                    "utterance_id": event.utterance_id,
                    "version": event.version,
                    "text": render(event.text, self._script).text,
                    "raw_text": event.text,
                    "start_ms": event.start_ms,
                    "end_ms": event.end_ms,
                }
            )
        elif isinstance(event, FinalTranscript):
            await self._handle_final(event)
        elif isinstance(event, PreviewPaused | PreviewResumed):
            self._send({"type": event.type, "backlog_ms": event.backlog_ms})
        elif isinstance(event, UtteranceDiscarded):
            self._send(
                {
                    "type": "utterance_discarded",
                    "utterance_id": event.utterance_id,
                    "start_ms": event.start_ms,
                    "end_ms": event.end_ms,
                    "reason": event.reason,
                }
            )
        elif isinstance(event, UtteranceFailed):
            self.failed_utterances.append(event)
            logger.error(
                "Final transcription failed",
                extra={"transcription_id": str(self.transcription_id), "code": event.error_code},
            )
            self._send(
                {
                    "type": "utterance_failed",
                    "utterance_id": event.utterance_id,
                    "start_ms": event.start_ms,
                    "end_ms": event.end_ms,
                    "code": event.error_code,
                }
            )
        # SpeechStarted / LanguageLocked are internal: not part of the client protocol.

    async def _handle_final(self, final: FinalTranscript) -> None:
        item = DisplayedFinal(final, displayed_text(final, self._script))
        saved = False
        if not self.unsaved:  # once a save fails, later finals wait to keep the DB order
            try:
                await self._store.append_final(self.transcription_id, item)
                saved = True
            except Exception:
                logger.error(
                    "Final segment could not be saved; keeping it for a retry",
                    extra={"transcription_id": str(self.transcription_id), "seq": final.seq},
                )
        if saved:
            self.saved.append(item)
        else:
            if not self.unsaved:
                self._send(error_message(ErrorCode.PERSISTENCE_FAILED, fatal=False, seq=final.seq))
            self.unsaved.append(item)
        self._send(self._final_message(item, saved=saved))  # after the save attempt

    @staticmethod
    def _final_message(item: DisplayedFinal, *, saved: bool) -> dict[str, Any]:
        final = item.final
        return {
            "type": "final",
            "seq": final.seq,
            "utterance_id": final.utterance_id,
            "text": item.displayed_text,
            "raw_text": final.text,
            "start_ms": final.start_ms,
            "end_ms": final.end_ms,
            "language": final.language,
            "avg_logprob": final.avg_logprob,
            "commit_reason": final.commit_reason.value,
            "saved": saved,
        }

    async def _flush_unsaved(self) -> None:
        while self.unsaved:
            try:
                await self._store.append_final(self.transcription_id, self.unsaved[0])
            except Exception:
                return  # still failing: reported in session_saved
            self.saved.append(self.unsaved.pop(0))

    async def _finish(self, ended: SessionEnded) -> None:
        # The session can end itself (recording limit, overload) before the receive loop
        # has decided the outcome: wait for it so the right status is stored.
        await self._ending_known.wait()
        await self._flush_unsaved()
        ending = self._ending or _ending(EndReason.INTERNAL_ERROR, CloseCode.INTERNAL_ERROR)
        language = next((i.final.language for i in self.saved if i.final.language), None)
        try:
            await self._store.finish_recording(
                self.transcription_id,
                status=ending.status,
                duration_ms=ended.duration_ms,
                language=language,
            )
            self._db_finished = True
        except Exception:
            logger.error(
                "Could not finish the recording",
                extra={"transcription_id": str(self.transcription_id)},
            )
        if self.unsaved:
            self._send(
                error_message(
                    ErrorCode.PERSISTENCE_FAILED,
                    fatal=False,
                    unsaved_segments=[i.final.seq for i in self.unsaved],
                )
            )
        self._send(
            {
                "type": "session_saved",
                "transcription_id": str(self.transcription_id),
                "status": ending.status.value,
                "end_reason": ending.reason.value,
                "duration_ms": ended.duration_ms,
                "final_count": len(self.saved) + len(self.unsaved),
                "saved_segments": len(self.saved),
                "unsaved_segments": [i.final.seq for i in self.unsaved],
                "failed_utterances": len(self.failed_utterances),
                "record_saved": self._db_finished,
                "text": " ".join(i.displayed_text for i in self.saved),
            }
        )

    async def _ensure_db_finished(self) -> None:
        """Safety net: a recording must never stay 'recording' after its connection ends."""
        if self._db_finished:
            return
        with contextlib.suppress(Exception):
            await self._store.finish_recording(
                self.transcription_id,
                status=TranscriptionStatus.INTERRUPTED,
                duration_ms=self.session.received_ms,
                language=None,
            )
            self._db_finished = True

    # ----- sender (the only WebSocket writer)

    async def _send_loop(self) -> None:
        while True:
            item = await self._outbound.get()
            if isinstance(item, _Close):
                if (
                    not self._socket_broken
                    and item.code is not None
                    and self._ws.application_state is not WebSocketState.DISCONNECTED
                ):
                    with contextlib.suppress(Exception):
                        await self._ws.close(code=item.code)
                return
            if self._socket_broken:
                continue  # keep draining; persistence never waits for the client
            try:
                await self._ws.send_json(item)
            except Exception:
                self._socket_broken = True
                logger.info(
                    "Client unreachable; continuing without sending",
                    extra={"transcription_id": str(self.transcription_id)},
                )
