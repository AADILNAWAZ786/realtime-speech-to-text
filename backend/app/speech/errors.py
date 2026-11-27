"""Error hierarchy for the speech package.

Every error carries a stable machine-readable `code` so later layers (WebSocket protocol,
CLI exit codes) can map errors without parsing messages.
"""

from __future__ import annotations


class SpeechError(Exception):
    code: str = "speech_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


# ---------- Audio input ----------


class AudioValidationError(SpeechError):
    code = "invalid_audio"


class EmptyAudioError(AudioValidationError):
    code = "empty_audio"


class InvalidPCMError(AudioValidationError):
    code = "invalid_pcm"


class UnsupportedSampleRateError(AudioValidationError):
    code = "unsupported_sample_rate"


class UnsupportedChannelCountError(AudioValidationError):
    code = "unsupported_channel_count"


class AudioTooLongError(AudioValidationError):
    code = "audio_too_long"


class InvalidAudioValuesError(AudioValidationError):
    code = "invalid_audio_values"


# ---------- Engine ----------


class ModelLoadError(SpeechError):
    code = "model_load_failed"


class InferenceError(SpeechError):
    code = "inference_failed"
