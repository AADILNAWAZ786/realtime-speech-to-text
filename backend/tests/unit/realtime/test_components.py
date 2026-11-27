"""Clock, frame assembly, VAD, buffers and boundary helpers."""

from __future__ import annotations

import numpy as np
import pytest

from app.realtime.boundary import (
    choose_word_cut,
    dedupe_prefix,
    find_silence_cut,
    is_probable_hallucination,
    normalise_token,
)
from app.realtime.buffer import AudioContinuityError, AudioSpan, Frame, PreRoll, UtteranceBuffer
from app.realtime.clock import FrameAssembler, SampleClock, ms_to_samples, samples_to_ms
from app.realtime.vad import EnergyVad, WebRtcVad, rms_dbfs
from app.speech.errors import InvalidPCMError
from app.speech.results import TranscriptSegment, WordTiming
from tests.unit.realtime.fakes import voiced

FRAME = 480  # 30 ms at 16 kHz


def pcm(samples: np.ndarray) -> bytes:
    return bytes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())


def frame_at(start: int, *, speech: bool = True, n: int = FRAME) -> Frame:
    samples = np.full(n, 0.1 if speech else 0.0, dtype=np.float32)
    samples.setflags(write=False)
    return Frame(start, samples, speech)


# ---------------------------------------------------------------- clock / assembly


def test_ms_sample_conversion() -> None:
    assert ms_to_samples(1000) == 16_000
    assert samples_to_ms(16_000) == 1000
    assert samples_to_ms(479) == 29  # rounds down: never past received audio
    assert samples_to_ms(ms_to_samples(12_345)) == 12_345


def test_sample_clock() -> None:
    clock = SampleClock()
    clock.advance(16_000)
    clock.advance(8)
    assert (clock.samples, clock.ms) == (16_008, 1000)
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_frame_assembler_handles_arbitrary_chunks() -> None:
    assembler = FrameAssembler(FRAME)
    data = bytes(range(256)) * 20  # 5120 bytes = 2560 samples
    frames = (
        assembler.push(data[:1000])
        + assembler.push(data[1000 : 1001 + 999])
        + assembler.push(data[2000:])
    )
    assert all(len(f) == FRAME * 2 for f in frames)
    assert len(frames) == 2560 // FRAME
    assert b"".join(frames) + assembler.flush() == data  # no byte lost or reordered
    assert assembler.clock.samples == 2560


def test_frame_assembler_rejects_odd_chunks() -> None:
    assembler = FrameAssembler(FRAME)
    with pytest.raises(InvalidPCMError):
        assembler.push(b"\x00\x01\x02")
    assert assembler.clock.samples == 0


def test_frame_assembler_remainder() -> None:
    assembler = FrameAssembler(FRAME)
    assert assembler.push(b"\x00" * 200) == []
    assert assembler.pending_samples == 100
    assert len(assembler.flush()) == 200
    assert assembler.flush() == b""


# ---------------------------------------------------------------- VAD


def test_rms_dbfs() -> None:
    assert rms_dbfs(pcm(np.zeros(FRAME))) == float("-inf")
    full = rms_dbfs(pcm(np.ones(FRAME)))
    assert -0.1 < full <= 0.0
    assert -27 < rms_dbfs(pcm(np.full(FRAME, 0.05))) < -25


def test_webrtc_vad_on_voiced_and_silent_frames() -> None:
    vad = WebRtcVad(aggressiveness=2)
    speech = pcm(voiced(300))
    flags = [
        vad.is_speech(speech[i : i + FRAME * 2], 16_000) for i in range(0, len(speech), FRAME * 2)
    ]
    assert sum(flags) >= 9
    assert not vad.is_speech(pcm(np.zeros(FRAME)), 16_000)


def test_energy_gate_suppresses_faint_noise() -> None:
    faint = pcm(np.random.default_rng(1).standard_normal(FRAME) * 0.0005)  # about -66 dBFS
    assert not WebRtcVad(energy_threshold_dbfs=-50).is_speech(faint, 16_000)


def test_vad_rejects_wrong_frame_size() -> None:
    with pytest.raises(InvalidPCMError):
        WebRtcVad().is_speech(b"\x00" * 1000, 16_000)
    with pytest.raises(InvalidPCMError):
        EnergyVad().is_speech(b"\x00" * 961, 16_000)
    with pytest.raises(ValueError):
        WebRtcVad(aggressiveness=4)


def test_energy_vad() -> None:
    vad = EnergyVad(threshold_dbfs=-40)
    assert vad.is_speech(pcm(np.full(FRAME, 0.1)), 16_000)
    assert not vad.is_speech(pcm(np.full(FRAME, 0.001)), 16_000)


# ---------------------------------------------------------------- spans / buffers


def test_span_split_and_concat_are_lossless() -> None:
    span = AudioSpan.from_frames([frame_at(0), frame_at(FRAME), frame_at(2 * FRAME)])
    left, right = span.split(700)  # inside the second frame
    assert (left.start_sample, left.end_sample, right.start_sample, right.end_sample) == (
        0,
        700,
        700,
        1440,
    )
    assert left.num_samples + right.num_samples == span.num_samples
    joined = left.concat(right)
    np.testing.assert_array_equal(joined.audio().samples, span.audio().samples)
    with pytest.raises(AudioContinuityError):
        right.concat(left)


def test_span_requires_contiguous_frames() -> None:
    with pytest.raises(AudioContinuityError):
        AudioSpan.from_frames([frame_at(0), frame_at(FRAME + 1)])
    with pytest.raises(AudioContinuityError):
        AudioSpan.from_frames([])


def test_span_audio_is_read_only_and_timestamps() -> None:
    span = AudioSpan.from_frames([frame_at(16_000), frame_at(16_480)])
    assert (span.start_ms, span.end_ms, span.duration_ms) == (1000, 1060, 60)
    assert not span.audio().samples.flags.writeable


def test_preroll_keeps_only_recent_frames() -> None:
    ring = PreRoll(3)
    for i in range(5):
        ring.push(frame_at(i * FRAME))
    taken = ring.take_last(2)
    assert [f.start_sample for f in taken] == [3 * FRAME, 4 * FRAME]
    assert len(ring) == 0


def test_buffer_seal_trims_and_returns_trailing_frames() -> None:
    buffer = UtteranceBuffer()
    buffer.begin([frame_at(0), frame_at(FRAME)])
    buffer.append(frame_at(2 * FRAME, speech=False))
    sealed, rest = buffer.seal(2 * FRAME + 100)
    assert (sealed.start_sample, sealed.end_sample) == (0, 2 * FRAME + 100)
    assert [(f.start_sample, f.end_sample) for f in rest] == [(2 * FRAME + 100, 3 * FRAME)]
    assert not buffer.is_active


def test_buffer_split_off_keeps_remainder_active() -> None:
    buffer = UtteranceBuffer()
    buffer.begin([frame_at(i * FRAME) for i in range(4)])
    sealed = buffer.split_off(2 * FRAME)
    assert sealed.end_sample == buffer.start_sample == 2 * FRAME
    assert buffer.end_sample == 4 * FRAME


def test_buffer_prepend_requires_contiguity() -> None:
    buffer = UtteranceBuffer()
    buffer.begin([frame_at(2 * FRAME)])
    buffer.prepend(AudioSpan.from_frames([frame_at(FRAME)]))
    assert buffer.start_sample == FRAME
    with pytest.raises(AudioContinuityError):
        buffer.prepend(AudioSpan.from_frames([frame_at(5 * FRAME)]))
    with pytest.raises(AudioContinuityError):
        buffer.append(frame_at(10 * FRAME))
    with pytest.raises(AudioContinuityError):
        buffer.begin([frame_at(0)])


def test_inactive_buffer_guards() -> None:
    buffer = UtteranceBuffer()
    assert buffer.num_samples == 0 and buffer.duration_ms == 0
    with pytest.raises(AudioContinuityError):
        buffer.snapshot()


# ---------------------------------------------------------------- boundaries


def test_find_silence_cut_picks_longest_run_after_search_start() -> None:
    flags = "SSS..SSS....SS.S"  # runs: 2 frames at 3, 4 frames at 8, 1 frame at 14
    frames = [frame_at(i * FRAME, speech=c == "S") for i, c in enumerate(flags)]
    cut = find_silence_cut(frames, search_from_sample=0, min_silence_samples=2 * FRAME)
    assert cut == 8 * FRAME + 2 * FRAME  # midpoint of the 4-frame run
    late = find_silence_cut(frames, search_from_sample=12 * FRAME, min_silence_samples=FRAME)
    assert late == 14 * FRAME + FRAME // 2
    assert find_silence_cut(frames, search_from_sample=0, min_silence_samples=5 * FRAME) is None


def _words(*spans: tuple[int, int]) -> list[WordTiming]:
    return [WordTiming(s, e, f" w{i}", 0.9) for i, (s, e) in enumerate(spans)]


def test_choose_word_cut() -> None:
    words = _words((0, 300), (300, 600), (650, 900), (900, 1200))
    assert choose_word_cut(words, keep_until_ms=620) == (2, 625)  # middle of the 600-650 gap
    assert choose_word_cut(words, keep_until_ms=100) is None  # nothing to keep
    assert choose_word_cut(words, keep_until_ms=5000) is None  # nothing to carry
    assert choose_word_cut([], keep_until_ms=500) is None


def test_dedupe_prefix() -> None:
    previous = ["आज", "की", "meeting", "cancel", "हो", "गई।"]
    assert dedupe_prefix(previous, ["हो", "गई", "कल", "फिर"]) == 2
    assert dedupe_prefix(previous, ["Cancel,", "हो", "गई।", "कल"]) == 3  # case & punctuation
    assert dedupe_prefix(["haan"], ["haan", "haan"]) == 0  # single word: kept
    assert dedupe_prefix(previous, ["कल", "फिर"]) == 0
    assert dedupe_prefix([], ["a", "b"]) == 0
    assert normalise_token(" Meeting,") == "meeting"


def test_hallucination_signals() -> None:
    def seg(nsp: float, lp: float, cr: float) -> TranscriptSegment:
        return TranscriptSegment(0, 0, 100, " text", lp, nsp, cr)

    kwargs = {"no_speech_prob": 0.6, "logprob": -1.0, "compression_ratio": 2.4}
    assert is_probable_hallucination(seg(0.9, -1.5, 1.2), **kwargs)
    assert is_probable_hallucination(seg(0.1, -0.2, 3.1), **kwargs)  # repetitive loop
    assert not is_probable_hallucination(seg(0.9, -0.3, 1.2), **kwargs)  # confident
    assert not is_probable_hallucination(seg(0.1, -1.5, 1.2), **kwargs)
