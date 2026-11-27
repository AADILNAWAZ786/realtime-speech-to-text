"""Audio validation and conversion.

The engine's only accepted input format is 16 kHz, mono, float32 samples in [-1.0, 1.0],
wrapped in an `AudioBuffer`. Raw bytes from the browser (PCM16 little-endian) are
validated and converted with `decode_pcm16`.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import numpy.typing as npt

from app.speech.errors import (
    AudioTooLongError,
    AudioValidationError,
    EmptyAudioError,
    InvalidAudioValuesError,
    InvalidPCMError,
    UnsupportedChannelCountError,
    UnsupportedSampleRateError,
)

TARGET_SAMPLE_RATE = 16_000
PCM16_SAMPLE_WIDTH = 2  # bytes per sample
_PCM16_SCALE = 32_768.0
# Float input outside this range almost always means unscaled int16 values were passed.
_FLOAT_PEAK_LIMIT = 1.0 + 1e-3

FloatAudio = npt.NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class AudioBuffer:
    """Validated audio: 1-D float32 samples at `sample_rate` (always 16 kHz)."""

    samples: FloatAudio
    sample_rate: int = TARGET_SAMPLE_RATE

    @property
    def num_samples(self) -> int:
        return int(self.samples.shape[0])

    @property
    def duration_seconds(self) -> float:
        return duration_seconds(self.num_samples, self.sample_rate)

    @property
    def duration_ms(self) -> int:
        return duration_ms(self.num_samples, self.sample_rate)


def duration_seconds(num_samples: int, sample_rate: int = TARGET_SAMPLE_RATE) -> float:
    if sample_rate <= 0:
        raise UnsupportedSampleRateError(f"Sample rate must be positive, got {sample_rate}")
    if num_samples < 0:
        raise ValueError("num_samples must be >= 0")
    return num_samples / sample_rate


def duration_ms(num_samples: int, sample_rate: int = TARGET_SAMPLE_RATE) -> int:
    """Duration in whole milliseconds (rounded down, so it never exceeds the real audio)."""
    if sample_rate <= 0:
        raise UnsupportedSampleRateError(f"Sample rate must be positive, got {sample_rate}")
    return (num_samples * 1000) // sample_rate


def _check_format(sample_rate: int, channels: int) -> None:
    if sample_rate != TARGET_SAMPLE_RATE:
        raise UnsupportedSampleRateError(
            f"Expected {TARGET_SAMPLE_RATE} Hz audio, got {sample_rate} Hz"
        )
    if channels != 1:
        raise UnsupportedChannelCountError(f"Expected mono audio (1 channel), got {channels}")


def _check_duration(num_samples: int, max_duration_seconds: float | None) -> None:
    if max_duration_seconds is not None:
        seconds = duration_seconds(num_samples)
        if seconds > max_duration_seconds:
            raise AudioTooLongError(
                f"Audio is {seconds:.2f} s; the limit is {max_duration_seconds:.2f} s"
            )


def validate_pcm16(
    data: bytes | bytearray | memoryview,
    *,
    sample_rate: int,
    channels: int = 1,
    max_duration_seconds: float | None = None,
) -> int:
    """Validate raw PCM16 little-endian mono audio. Returns the number of samples."""
    if not isinstance(data, bytes | bytearray | memoryview):
        raise InvalidPCMError(f"PCM data must be bytes-like, got {type(data).__name__}")
    _check_format(sample_rate, channels)
    size = memoryview(data).nbytes
    if size == 0:
        raise EmptyAudioError("Audio contains no samples")
    if size % PCM16_SAMPLE_WIDTH != 0:
        raise InvalidPCMError(
            f"PCM16 data must have an even number of bytes, got {size} "
            "(truncated frame or wrong sample format)"
        )
    num_samples = size // PCM16_SAMPLE_WIDTH
    _check_duration(num_samples, max_duration_seconds)
    return num_samples


def pcm16_to_float32(data: bytes | bytearray | memoryview) -> FloatAudio:
    """Convert PCM16 little-endian bytes to float32 in [-1.0, 1.0). No format checks."""
    ints = np.frombuffer(data, dtype="<i2")
    return (ints.astype(np.float32) / np.float32(_PCM16_SCALE)).astype(np.float32, copy=False)


def _freeze(samples: FloatAudio) -> FloatAudio:
    samples.setflags(write=False)  # buffers are shared between pipeline stages: no mutation
    return samples


def decode_pcm16(
    data: bytes | bytearray | memoryview,
    *,
    sample_rate: int,
    channels: int = 1,
    max_duration_seconds: float | None = None,
) -> AudioBuffer:
    """Validate PCM16 bytes and convert them to an `AudioBuffer`."""
    validate_pcm16(
        data,
        sample_rate=sample_rate,
        channels=channels,
        max_duration_seconds=max_duration_seconds,
    )
    return AudioBuffer(samples=_freeze(pcm16_to_float32(data)), sample_rate=sample_rate)


def validate_float_audio(
    samples: npt.NDArray[np.floating],
    *,
    sample_rate: int = TARGET_SAMPLE_RATE,
    max_duration_seconds: float | None = None,
) -> AudioBuffer:
    """Validate float samples (e.g. from a decoder) and wrap them in an `AudioBuffer`."""
    if not isinstance(samples, np.ndarray):
        raise InvalidAudioValuesError(f"Expected a NumPy array, got {type(samples).__name__}")
    if not np.issubdtype(samples.dtype, np.floating):
        raise InvalidAudioValuesError(
            f"Expected floating-point samples, got dtype {samples.dtype}; "
            "convert PCM16 with decode_pcm16()"
        )
    if samples.ndim != 1:
        raise UnsupportedChannelCountError(
            f"Expected 1-D mono samples, got an array with shape {samples.shape}"
        )
    _check_format(sample_rate, 1)
    if samples.size == 0:
        raise EmptyAudioError("Audio contains no samples")
    if not np.all(np.isfinite(samples)):
        raise InvalidAudioValuesError("Audio contains NaN or infinite values")
    peak = float(np.max(np.abs(samples)))
    if peak > _FLOAT_PEAK_LIMIT:
        raise InvalidAudioValuesError(
            f"Float samples must be within [-1.0, 1.0], peak is {peak:.3f} "
            "(were unscaled int16 values passed?)"
        )
    _check_duration(int(samples.size), max_duration_seconds)
    as_float32 = np.ascontiguousarray(samples, dtype=np.float32)
    if as_float32 is samples and samples.flags.writeable:
        as_float32 = as_float32.copy()  # never freeze (or alias) the caller's mutable array
    return AudioBuffer(samples=_freeze(as_float32), sample_rate=sample_rate)


_FFMPEG_HINT = "convert it with: ffmpeg -i input.m4a -ar 16000 -ac 1 -c:a pcm_s16le output.wav"


def read_wav_pcm16(path: str | Path, *, max_duration_seconds: float | None = None) -> AudioBuffer:
    """Strictly read a 16 kHz mono PCM16 WAV file (no resampling)."""
    try:
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frames = wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise InvalidPCMError(f"{path}: not a valid PCM WAV file ({exc})") from exc
    if sample_width != PCM16_SAMPLE_WIDTH:
        raise InvalidPCMError(
            f"{path}: expected 16-bit samples, got {sample_width * 8}-bit; {_FFMPEG_HINT}"
        )
    try:
        return decode_pcm16(
            frames,
            sample_rate=sample_rate,
            channels=channels,
            max_duration_seconds=max_duration_seconds,
        )
    except (UnsupportedSampleRateError, UnsupportedChannelCountError) as exc:
        raise type(exc)(f"{path}: {exc}; {_FFMPEG_HINT}") from exc


def load_audio_file(
    path: str | Path,
    *,
    allow_resample: bool = True,
    max_duration_seconds: float | None = None,
) -> AudioBuffer:
    """Load an audio file for manual evaluation.

    16 kHz mono PCM16 WAV is read strictly. Anything else (MP3, M4A, 44.1 kHz WAV, stereo)
    is decoded and resampled with PyAV via faster-whisper when `allow_resample` is True.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise AudioValidationError(f"{file_path}: file not found", code="file_not_found")
    if file_path.suffix.lower() == ".wav":
        try:
            return read_wav_pcm16(file_path, max_duration_seconds=max_duration_seconds)
        except AudioTooLongError:
            raise
        except AudioValidationError:
            if not allow_resample:
                raise
    elif not allow_resample:
        raise AudioValidationError(
            f"{file_path}: only 16 kHz mono PCM16 WAV is accepted without resampling",
            code="unsupported_format",
        )

    try:
        from faster_whisper import decode_audio  # lazy: heavy optional import
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise AudioValidationError(
            "Decoding non-WAV audio requires faster-whisper (PyAV)", code="decoder_missing"
        ) from exc
    try:
        samples = decode_audio(str(file_path), sampling_rate=TARGET_SAMPLE_RATE)
    except Exception as exc:  # PyAV raises many specific error types
        raise InvalidPCMError(f"{file_path}: could not decode audio ({exc})") from exc
    return validate_float_audio(np.asarray(samples), max_duration_seconds=max_duration_seconds)
