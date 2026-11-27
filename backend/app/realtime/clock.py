"""Sample-count clock and frame assembly.

Time in the pipeline is measured by *samples received*, never by wall-clock time, so
network jitter and processing delays cannot shift timestamps. At 16 kHz, 1 ms = 16 samples
exactly, so millisecond conversions are lossless at frame granularity.
"""

from __future__ import annotations

from app.realtime.config import SAMPLE_RATE
from app.speech.audio import PCM16_SAMPLE_WIDTH
from app.speech.errors import InvalidPCMError


def samples_to_ms(samples: int, sample_rate: int = SAMPLE_RATE) -> int:
    """Whole milliseconds (rounded down: never past the audio actually received)."""
    return samples * 1000 // sample_rate


def ms_to_samples(ms: int, sample_rate: int = SAMPLE_RATE) -> int:
    return ms * sample_rate // 1000


class SampleClock:
    """Absolute position in the recording, in samples since the session started."""

    def __init__(self) -> None:
        self._samples = 0

    def advance(self, samples: int) -> int:
        if samples < 0:
            raise ValueError("cannot move the clock backwards")
        self._samples += samples
        return self._samples

    @property
    def samples(self) -> int:
        return self._samples

    @property
    def ms(self) -> int:
        return samples_to_ms(self._samples)


class FrameAssembler:
    """Turns arbitrarily sized PCM16 chunks into exact fixed-size frames.

    Leftover samples (less than one frame) are kept for the next chunk, so no audio is
    dropped at chunk boundaries; `flush()` returns them when the recording stops.
    """

    def __init__(self, frame_samples: int) -> None:
        if frame_samples <= 0:
            raise ValueError("frame_samples must be positive")
        self._frame_bytes = frame_samples * PCM16_SAMPLE_WIDTH
        self._pending = bytearray()
        self.clock = SampleClock()

    @property
    def pending_samples(self) -> int:
        return len(self._pending) // PCM16_SAMPLE_WIDTH

    def push(self, data: bytes | bytearray | memoryview) -> list[bytes]:
        size = memoryview(data).nbytes
        if size % PCM16_SAMPLE_WIDTH:
            raise InvalidPCMError(f"PCM16 chunk must have an even number of bytes, got {size}")
        self._pending += data
        self.clock.advance(size // PCM16_SAMPLE_WIDTH)
        frames: list[bytes] = []
        complete = len(self._pending) - len(self._pending) % self._frame_bytes
        for offset in range(0, complete, self._frame_bytes):
            frames.append(bytes(self._pending[offset : offset + self._frame_bytes]))
        del self._pending[:complete]
        return frames

    def flush(self) -> bytes:
        """Return (and clear) the incomplete trailing frame."""
        remainder = bytes(self._pending)
        self._pending.clear()
        return remainder
