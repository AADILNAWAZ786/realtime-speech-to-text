"""WebSocket protocol for /ws/transcribe (documented in docs/websocket-protocol.md).

Client -> server
- text  {"type": "start", ...}   first message, within ws_auth_timeout_seconds
- binary PCM16 little-endian, 16 kHz, mono, <= ws_max_frame_bytes per message
- text  {"type": "stop"}

Server -> client (all JSON text): session_started, partial, final, preview_paused,
preview_resumed, utterance_discarded, utterance_failed, session_saved, error.
"""

from __future__ import annotations

import json
import uuid
from enum import IntEnum, StrEnum
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.models.enums import TITLE_MAX_LENGTH, ScriptPreference, SpeechMode
from app.schemas.common import strip_optional


class CloseCode(IntEnum):
    NORMAL = 1000
    POLICY_VIOLATION = 1008  # Origin not allowed
    MESSAGE_TOO_BIG = 1009
    INTERNAL_ERROR = 1011
    SERVICE_RESTART = 1012  # server shutting down
    TRY_AGAIN_LATER = 1013  # server busy / overloaded
    BAD_REQUEST = 4400  # malformed message, invalid audio
    UNAUTHORIZED = 4401  # missing / invalid / expired token, audio before auth
    TIMEOUT = 4408  # authentication deadline or idle timeout
    CONFLICT = 4409  # this user already has an active recording
    RATE_LIMITED = 4429  # audio faster than real time allows


class ErrorCode(StrEnum):
    AUTH_TIMEOUT = "auth_timeout"
    AUTH_REQUIRED = "auth_required"
    INVALID_TOKEN = "invalid_token"  # noqa: S105 - error code, not a secret
    TOKEN_EXPIRED = "token_expired"  # noqa: S105
    INVALID_MESSAGE = "invalid_message"
    INVALID_AUDIO = "invalid_audio"
    FRAME_TOO_LARGE = "frame_too_large"
    RATE_LIMITED = "rate_limited"
    RECORDING_IN_PROGRESS = "recording_in_progress"
    SERVER_BUSY = "server_busy"
    SERVER_OVERLOADED = "server_overloaded"
    IDLE_TIMEOUT = "idle_timeout"
    SERVER_SHUTDOWN = "server_shutdown"
    PERSISTENCE_FAILED = "persistence_failed"
    INTERNAL_ERROR = "internal_error"


# Generic, client-safe messages: never include exception text, tokens or internals.
ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.AUTH_TIMEOUT: "Authentication not received in time",
    ErrorCode.AUTH_REQUIRED: "Send a start message with a token before audio",
    ErrorCode.INVALID_TOKEN: "Invalid token",
    ErrorCode.TOKEN_EXPIRED: "Token has expired",
    ErrorCode.INVALID_MESSAGE: "Invalid message",
    ErrorCode.INVALID_AUDIO: "Audio must be PCM16 little-endian, 16 kHz, mono",
    ErrorCode.FRAME_TOO_LARGE: "Audio frame is too large",
    ErrorCode.RATE_LIMITED: "Audio is arriving faster than real time",
    ErrorCode.RECORDING_IN_PROGRESS: "You already have an active recording",
    ErrorCode.SERVER_BUSY: "Too many active recordings; try again later",
    ErrorCode.SERVER_OVERLOADED: "Transcription is falling behind; recording stopped",
    ErrorCode.IDLE_TIMEOUT: "No data received; recording stopped",
    ErrorCode.SERVER_SHUTDOWN: "Server is restarting; recording stopped",
    ErrorCode.PERSISTENCE_FAILED: "Some transcript segments could not be saved",
    ErrorCode.INTERNAL_ERROR: "Internal server error",
}


class ProtocolError(Exception):
    """A client-facing failure that ends the connection with a close code."""

    def __init__(self, code: ErrorCode, close_code: CloseCode) -> None:
        super().__init__(code.value)
        self.code = code
        self.close_code = close_code


# ---------------------------------------------------------------- client messages


# Optional: missing, null, "" and whitespace all mean "default title". The length limit sits
# on the str member so None is never length-checked (see schemas/common.py).
OptionalTitleStr = Annotated[
    Annotated[str, Field(max_length=TITLE_MAX_LENGTH)] | None,
    AfterValidator(strip_optional),
]


class _ClientMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StartMessage(_ClientMessage):
    type: Literal["start"]
    token: str = Field(min_length=1, max_length=4096, repr=False)  # never in repr/logs
    speech_mode: SpeechMode = SpeechMode.AUTO
    script_preference: ScriptPreference = ScriptPreference.AS_RECOGNISED
    format: Literal["pcm_s16le"] = "pcm_s16le"
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    title: OptionalTitleStr = None


class StopMessage(_ClientMessage):
    type: Literal["stop"]


ClientMessage = Annotated[StartMessage | StopMessage, Field(discriminator="type")]
_client_message = TypeAdapter[StartMessage | StopMessage](ClientMessage)


def parse_client_message(text: str) -> StartMessage | StopMessage:
    """Parse a JSON text frame; raises ProtocolError(invalid_message) on any problem."""
    try:
        data = json.loads(text)
        return _client_message.validate_python(data)
    except (ValueError, TypeError, ValidationError) as exc:
        # TypeError: defence in depth - a schema/validator bug must still end as a
        # protocol error (4400), never as an unhandled exception that drops the socket.
        raise ProtocolError(ErrorCode.INVALID_MESSAGE, CloseCode.BAD_REQUEST) from exc


# ---------------------------------------------------------------- server messages


def error_message(code: ErrorCode, *, fatal: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "type": "error",
        "code": code.value,
        "message": ERROR_MESSAGES[code],
        "fatal": fatal,
        **extra,
    }


def session_started_message(
    *,
    transcription_id: uuid.UUID,
    speech_mode: SpeechMode,
    script_preference: ScriptPreference,
    model_name: str,
    max_frame_bytes: int,
    max_duration_ms: int,
) -> dict[str, Any]:
    return {
        "type": "session_started",
        "transcription_id": str(transcription_id),
        "speech_mode": speech_mode.value,
        "script_preference": script_preference.value,
        "model_name": model_name,
        "audio": {"format": "pcm_s16le", "sample_rate": 16000, "channels": 1},
        "max_frame_bytes": max_frame_bytes,
        "max_duration_ms": max_duration_ms,
    }
