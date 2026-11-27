"""Audio frames, spans, the pre-roll ring and the utterance buffer.

Every frame carries its absolute `start_sample`, so audio can be split, carried and
re-joined without ever losing or duplicating a sample: spans can only be concatenated
when they are exactly contiguous.
"""

from __future__ import annotations

import itertools
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

from app.realtime.clock import samples_to_ms
from app.speech.audio import AudioBuffer, FloatAudio


class AudioContinuityError(Exception):
    """Programming error: an operation would lose or duplicate audio."""


def _frozen(samples: FloatAudio) -> FloatAudio:
    if samples.flags.writeable:
        samples = samples.copy()
        samples.setflags(write=False)
    return samples


@dataclass(frozen=True, slots=True)
class Frame:
    start_sample: int
    samples: FloatAudio
    is_speech: bool

    @property
    def num_samples(self) -> int:
        return int(self.samples.shape[0])

    @property
    def end_sample(self) -> int:
        return self.start_sample + self.num_samples

    def split(self, at_sample: int) -> tuple[Frame, Frame]:
        if not self.start_sample < at_sample < self.end_sample:
            raise AudioContinuityError("split point must be strictly inside the frame")
        k = at_sample - self.start_sample
        return (
            Frame(self.start_sample, self.samples[:k], self.is_speech),
            Frame(at_sample, self.samples[k:], self.is_speech),
        )


@dataclass(frozen=True, slots=True)
class AudioSpan:
    """Contiguous audio [start_sample, end_sample) with its per-frame VAD flags."""

    start_sample: int
    frames: tuple[Frame, ...]

    @classmethod
    def from_frames(cls, frames: Sequence[Frame]) -> AudioSpan:
        if not frames:
            raise AudioContinuityError("a span needs at least one frame")
        for left, right in itertools.pairwise(frames):
            if left.end_sample != right.start_sample:
                raise AudioContinuityError(
                    f"frames are not contiguous ({left.end_sample} != {right.start_sample})"
                )
        return cls(frames[0].start_sample, tuple(frames))

    @property
    def end_sample(self) -> int:
        return self.frames[-1].end_sample

    @property
    def num_samples(self) -> int:
        return self.end_sample - self.start_sample

    @property
    def start_ms(self) -> int:
        return samples_to_ms(self.start_sample)

    @property
    def end_ms(self) -> int:
        return samples_to_ms(self.end_sample)

    @property
    def duration_ms(self) -> int:
        return samples_to_ms(self.num_samples)

    @property
    def speech_frames(self) -> int:
        return sum(1 for frame in self.frames if frame.is_speech)

    def audio(self) -> AudioBuffer:
        samples = np.concatenate([frame.samples for frame in self.frames])
        samples.setflags(write=False)
        return AudioBuffer(samples=samples)

    def split(self, at_sample: int) -> tuple[AudioSpan, AudioSpan]:
        """[start, at) and [at, end). The cut may fall inside a frame."""
        if not self.start_sample < at_sample < self.end_sample:
            raise AudioContinuityError("split point must be strictly inside the span")
        left: list[Frame] = []
        right: list[Frame] = []
        for frame in self.frames:
            if frame.end_sample <= at_sample:
                left.append(frame)
            elif frame.start_sample >= at_sample:
                right.append(frame)
            else:
                head, tail = frame.split(at_sample)
                left.append(head)
                right.append(tail)
        return AudioSpan.from_frames(left), AudioSpan.from_frames(right)

    def concat(self, other: AudioSpan) -> AudioSpan:
        if self.end_sample != other.start_sample:
            raise AudioContinuityError("spans are not contiguous")
        return AudioSpan(self.start_sample, self.frames + other.frames)


class PreRoll:
    """Recent frames kept while no utterance is active, to include the speech onset."""

    def __init__(self, max_frames: int) -> None:
        self._frames: deque[Frame] = deque(maxlen=max(max_frames, 1))

    def push(self, frame: Frame) -> None:
        self._frames.append(frame)

    def extend(self, frames: Iterable[Frame]) -> None:
        for frame in frames:
            self.push(frame)

    def take_last(self, count: int) -> list[Frame]:
        frames = list(self._frames)[-count:] if count > 0 else []
        self._frames.clear()
        return frames

    def __len__(self) -> int:
        return len(self._frames)


class UtteranceBuffer:
    """Frames of the utterance currently being spoken."""

    def __init__(self) -> None:
        self._frames: list[Frame] = []

    @property
    def is_active(self) -> bool:
        return bool(self._frames)

    @property
    def start_sample(self) -> int:
        self._require_active()
        return self._frames[0].start_sample

    @property
    def end_sample(self) -> int:
        self._require_active()
        return self._frames[-1].end_sample

    @property
    def num_samples(self) -> int:
        return self.end_sample - self.start_sample if self._frames else 0

    @property
    def duration_ms(self) -> int:
        return samples_to_ms(self.num_samples)

    @property
    def frames(self) -> tuple[Frame, ...]:
        return tuple(self._frames)

    def _require_active(self) -> None:
        if not self._frames:
            raise AudioContinuityError("no active utterance")

    def begin(self, frames: Sequence[Frame]) -> None:
        if self._frames:
            raise AudioContinuityError("an utterance is already active")
        self._frames = list(AudioSpan.from_frames(frames).frames)

    def append(self, frame: Frame) -> None:
        if self._frames and frame.start_sample != self._frames[-1].end_sample:
            raise AudioContinuityError("frame is not contiguous with the buffer")
        self._frames.append(frame)

    def prepend(self, span: AudioSpan) -> None:
        """Put carried audio back in front of the current utterance."""
        if self._frames and span.end_sample != self._frames[0].start_sample:
            raise AudioContinuityError("carried audio is not contiguous with the buffer")
        self._frames = list(span.frames) + self._frames

    def snapshot(self) -> AudioSpan:
        """Immutable view of the utterance so far (for partial transcription)."""
        self._require_active()
        return AudioSpan.from_frames(self._frames)

    def seal(self, end_sample: int | None = None) -> tuple[AudioSpan, list[Frame]]:
        """Close the utterance at `end_sample` (default: everything).

        Returns the sealed span and the frames after `end_sample` (e.g. trimmed trailing
        silence, which the caller hands back to the pre-roll). The buffer becomes empty.
        """
        span = self.snapshot()
        self._frames = []
        if end_sample is None or end_sample >= span.end_sample:
            return span, []
        if end_sample <= span.start_sample:
            raise AudioContinuityError("cannot seal an empty utterance")
        sealed, rest = span.split(end_sample)
        return sealed, list(rest.frames)

    def split_off(self, at_sample: int) -> AudioSpan:
        """Seal [start, at) and keep [at, end) as the ongoing utterance."""
        span = self.snapshot()
        sealed, rest = span.split(at_sample)
        self._frames = list(rest.frames)
        return sealed
