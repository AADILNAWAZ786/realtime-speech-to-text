from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from app.speech.audio import (
    TARGET_SAMPLE_RATE,
    AudioBuffer,
    decode_pcm16,
    duration_ms,
    duration_seconds,
    load_audio_file,
    pcm16_to_float32,
    read_wav_pcm16,
    validate_float_audio,
    validate_pcm16,
)
from app.speech.errors import (
    AudioTooLongError,
    AudioValidationError,
    EmptyAudioError,
    InvalidAudioValuesError,
    InvalidPCMError,
    UnsupportedChannelCountError,
    UnsupportedSampleRateError,
)
from tests.unit.speech.fakes import pcm16_bytes, write_wav

# ---------- PCM16 validation ----------


def test_valid_pcm16_returns_sample_count() -> None:
    data = pcm16_bytes(0.5)
    assert validate_pcm16(data, sample_rate=16_000) == 8_000


@pytest.mark.parametrize("data", [b"", bytearray(), memoryview(b"")])
def test_empty_pcm_rejected(data: bytes) -> None:
    with pytest.raises(EmptyAudioError):
        validate_pcm16(data, sample_rate=16_000)


def test_odd_byte_count_rejected() -> None:
    with pytest.raises(InvalidPCMError, match="even number of bytes"):
        validate_pcm16(b"\x00\x01\x02", sample_rate=16_000)


def test_non_bytes_rejected() -> None:
    with pytest.raises(InvalidPCMError, match="bytes-like"):
        validate_pcm16("not bytes", sample_rate=16_000)  # type: ignore[arg-type]


@pytest.mark.parametrize("rate", [8_000, 22_050, 44_100, 48_000])
def test_wrong_sample_rate_rejected(rate: int) -> None:
    with pytest.raises(UnsupportedSampleRateError, match=f"got {rate} Hz"):
        validate_pcm16(pcm16_bytes(0.1), sample_rate=rate)


@pytest.mark.parametrize("channels", [0, 2, 6])
def test_non_mono_rejected(channels: int) -> None:
    with pytest.raises(UnsupportedChannelCountError):
        validate_pcm16(pcm16_bytes(0.1), sample_rate=16_000, channels=channels)


def test_max_duration_enforced() -> None:
    validate_pcm16(pcm16_bytes(1.0), sample_rate=16_000, max_duration_seconds=1.0)
    with pytest.raises(AudioTooLongError):
        validate_pcm16(pcm16_bytes(1.01), sample_rate=16_000, max_duration_seconds=1.0)


def test_all_validation_errors_share_a_base_class_and_code() -> None:
    with pytest.raises(AudioValidationError) as exc_info:
        validate_pcm16(b"", sample_rate=16_000)
    assert exc_info.value.code == "empty_audio"


# ---------- Conversion ----------


def test_pcm16_to_float32_scaling_and_endianness() -> None:
    data = struct.pack("<5h", 0, 16384, -16384, 32767, -32768)
    samples = pcm16_to_float32(data)
    assert samples.dtype == np.float32
    np.testing.assert_allclose(samples, [0.0, 0.5, -0.5, 32767 / 32768, -1.0])


def test_decode_pcm16_returns_frozen_buffer() -> None:
    buffer = decode_pcm16(pcm16_bytes(0.25), sample_rate=16_000)
    assert isinstance(buffer, AudioBuffer)
    assert buffer.num_samples == 4_000
    assert buffer.duration_ms == 250
    assert not buffer.samples.flags.writeable
    with pytest.raises(ValueError, match="read-only"):
        buffer.samples[0] = 1.0


def test_decode_pcm16_from_memoryview() -> None:
    data = memoryview(pcm16_bytes(0.1))
    assert decode_pcm16(data, sample_rate=16_000).num_samples == 1_600


# ---------- Duration ----------


@pytest.mark.parametrize(
    ("samples", "expected_s", "expected_ms"),
    [(0, 0.0, 0), (1, 1 / 16_000, 0), (16, 0.001, 1), (16_000, 1.0, 1000), (24_008, 1.5005, 1500)],
)
def test_duration(samples: int, expected_s: float, expected_ms: int) -> None:
    assert duration_seconds(samples) == pytest.approx(expected_s)
    assert duration_ms(samples) == expected_ms


def test_duration_rejects_bad_input() -> None:
    with pytest.raises(UnsupportedSampleRateError):
        duration_seconds(10, 0)
    with pytest.raises(ValueError):
        duration_seconds(-1)


# ---------- Float validation ----------


def test_validate_float_audio_accepts_float64_and_copies() -> None:
    source = np.zeros(1600, dtype=np.float64)
    buffer = validate_float_audio(source)
    assert buffer.samples.dtype == np.float32
    assert source.flags.writeable  # caller's array untouched


def test_validate_float_audio_does_not_alias_mutable_input() -> None:
    source = np.zeros(1600, dtype=np.float32)
    buffer = validate_float_audio(source)
    source[0] = 0.5
    assert buffer.samples[0] == 0.0


@pytest.mark.parametrize(
    ("samples", "error"),
    [
        (np.array([], dtype=np.float32), EmptyAudioError),
        (np.zeros((2, 100), dtype=np.float32), UnsupportedChannelCountError),
        (np.array([0.1, np.nan], dtype=np.float32), InvalidAudioValuesError),
        (np.array([0.1, np.inf], dtype=np.float32), InvalidAudioValuesError),
        (np.array([1200.0, -800.0], dtype=np.float32), InvalidAudioValuesError),  # unscaled
        (np.array([1, 2, 3], dtype=np.int16), InvalidAudioValuesError),
    ],
    ids=["empty", "2d", "nan", "inf", "unscaled", "int-dtype"],
)
def test_validate_float_audio_rejects(samples: np.ndarray, error: type[Exception]) -> None:
    with pytest.raises(error):
        validate_float_audio(samples)


def test_validate_float_audio_rejects_wrong_rate() -> None:
    with pytest.raises(UnsupportedSampleRateError):
        validate_float_audio(np.zeros(10, dtype=np.float32), sample_rate=48_000)


# ---------- Files ----------


def test_read_wav_pcm16(tmp_path: Path) -> None:
    buffer = read_wav_pcm16(write_wav(tmp_path / "ok.wav", seconds=0.5))
    assert buffer.num_samples == 8_000
    assert buffer.sample_rate == TARGET_SAMPLE_RATE


@pytest.mark.parametrize(
    ("kwargs", "error", "match"),
    [
        ({"sample_rate": 44_100}, UnsupportedSampleRateError, "44100 Hz"),
        ({"channels": 2}, UnsupportedChannelCountError, "2"),
        ({"sample_width": 1}, InvalidPCMError, "8-bit"),
    ],
    ids=["44k", "stereo", "8bit"],
)
def test_read_wav_rejects_wrong_format_with_ffmpeg_hint(
    tmp_path: Path, kwargs: dict[str, int], error: type[Exception], match: str
) -> None:
    path = write_wav(tmp_path / "bad.wav", seconds=0.1, **kwargs)
    with pytest.raises(error, match=match) as exc_info:
        read_wav_pcm16(path)
    assert "ffmpeg" in str(exc_info.value)


def test_read_wav_rejects_non_wav(tmp_path: Path) -> None:
    path = tmp_path / "fake.wav"
    path.write_bytes(b"this is not a wav file")
    with pytest.raises(InvalidPCMError, match="not a valid PCM WAV"):
        read_wav_pcm16(path)


def test_read_wav_rejects_empty_wav(tmp_path: Path) -> None:
    with pytest.raises(EmptyAudioError):
        read_wav_pcm16(write_wav(tmp_path / "empty.wav", seconds=0))


def test_load_audio_file_missing(tmp_path: Path) -> None:
    with pytest.raises(AudioValidationError) as exc_info:
        load_audio_file(tmp_path / "missing.wav")
    assert exc_info.value.code == "file_not_found"


def test_load_audio_file_resamples_other_formats(tmp_path: Path) -> None:
    """44.1 kHz stereo WAV is decoded and resampled by PyAV (no model involved)."""
    path = write_wav(tmp_path / "cd.wav", seconds=1.0, sample_rate=44_100, channels=2)
    buffer = load_audio_file(path)
    assert buffer.sample_rate == TARGET_SAMPLE_RATE
    assert abs(buffer.num_samples - 16_000) <= 160  # resampler edge effects


def test_load_audio_file_strict_mode_refuses_resampling(tmp_path: Path) -> None:
    path = write_wav(tmp_path / "cd.wav", seconds=0.1, sample_rate=44_100)
    with pytest.raises(UnsupportedSampleRateError):
        load_audio_file(path, allow_resample=False)


def test_load_audio_file_undecodable(tmp_path: Path) -> None:
    path = tmp_path / "noise.mp3"
    path.write_bytes(b"\x00garbage" * 50)
    with pytest.raises(InvalidPCMError, match="could not decode"):
        load_audio_file(path)
