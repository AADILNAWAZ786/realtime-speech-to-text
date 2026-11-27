"""Layer-1 voice activity detection: classify one 30 ms PCM16 frame as speech or not.

Separate from faster-whisper's built-in Silero `vad_filter` (layer 2), which only trims
audio inside a final-pass `transcribe()` call and never decides timing.

Evaluation (2 vCPU Intel Xeon 2.1 GHz, see docs/architecture.md):
- webrtcvad-wheels 2.0.14.post1 (maintained fork of WebRTC VAD): native 10/20/30 ms frames,
  ~3 us per frame (~0.01 % of a core). Chosen.
- pysilero-vad 3.4.1: 32 ms chunks only, ~1.1 ms per chunk (~3.5 % of a core per session).
  Likely more robust in noise; kept as a future option behind FrameClassifier.
- silero-vad 6.3: requires PyTorch (+CUDA wheels); rejected as too heavy.
"""

from __future__ import annotations

import math
from typing import Protocol

import numpy as np
import webrtcvad

from app.speech.errors import InvalidPCMError

_VALID_FRAME_MS = (10, 20, 30)


def rms_dbfs(frame: bytes) -> float:
    """Frame loudness in dBFS (0 = full scale, -inf for digital silence)."""
    samples = np.frombuffer(frame, dtype="<i2").astype(np.float64)
    if samples.size == 0:
        return -math.inf
    rms = math.sqrt(float(np.mean(samples * samples))) / 32768.0
    return 20 * math.log10(rms) if rms > 0 else -math.inf


class FrameClassifier(Protocol):
    def is_speech(self, frame: bytes, sample_rate: int) -> bool: ...


def _check_frame(frame: bytes, sample_rate: int) -> None:
    samples = len(frame) // 2
    if len(frame) % 2 or samples * 1000 not in {ms * sample_rate for ms in _VALID_FRAME_MS}:
        raise InvalidPCMError(
            f"VAD frames must be 10, 20 or 30 ms of PCM16; got {len(frame)} bytes"
        )


class EnergyVad:
    """Loudness threshold only. Deterministic; useful as a fallback and in tests."""

    def __init__(self, threshold_dbfs: float = -40.0) -> None:
        self.threshold_dbfs = threshold_dbfs

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        _check_frame(frame, sample_rate)
        return rms_dbfs(frame) >= self.threshold_dbfs


class WebRtcVad:
    """WebRTC VAD plus an energy gate.

    WebRTC VAD occasionally flags faint noise as speech; frames quieter than
    `energy_threshold_dbfs` are therefore always treated as silence. One instance per
    session: the underlying detector is stateful and not thread-safe.
    """

    def __init__(
        self, aggressiveness: int = 2, energy_threshold_dbfs: float | None = -50.0
    ) -> None:
        if not 0 <= aggressiveness <= 3:
            raise ValueError("aggressiveness must be 0-3")
        self._vad = webrtcvad.Vad(aggressiveness)
        self.energy_threshold_dbfs = energy_threshold_dbfs

    def is_speech(self, frame: bytes, sample_rate: int) -> bool:
        _check_frame(frame, sample_rate)
        if self.energy_threshold_dbfs is not None and rms_dbfs(frame) < self.energy_threshold_dbfs:
            return False
        return bool(self._vad.is_speech(frame, sample_rate))
