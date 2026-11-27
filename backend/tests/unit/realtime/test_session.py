"""End-to-end session behaviour: real WebRTC VAD + endpointer + buffers + scheduler,
with the TimelineEngine standing in for Whisper."""

from __future__ import annotations

import asyncio
import itertools

import numpy as np
import pytest

from app.models.enums import SpeechMode
from app.realtime.config import RealtimeConfig
from app.realtime.endpointer import CommitReason
from app.realtime.events import (
    FinalTranscript,
    LanguageLocked,
    PartialTranscript,
    PreviewPaused,
    SessionEnded,
    SpeechStarted,
    UtteranceDiscarded,
    UtteranceFailed,
)
from app.realtime.scheduler import InferenceScheduler
from app.realtime.session import (
    SessionClosedError,
    SessionOverloadedError,
    SessionState,
    TranscriptionSession,
)
from app.speech.errors import AudioValidationError, InvalidPCMError
from app.speech.language import LanguageDetection
from tests.unit.realtime.fakes import (
    Script,
    SpokenWord,
    TimelineEngine,
    final_words,
    run_session,
    voiced,
    wait_for,
    words_in,
)


def _setup(
    *parts: tuple[str, int], **engine_kwargs: object
) -> tuple[bytes, TimelineEngine, list[str]]:
    script = Script(list(parts))
    pcm = script.pcm()
    words = words_in(script.speech_regions)
    return pcm, TimelineEngine(words, **engine_kwargs), [w.text for w in words]  # type: ignore[arg-type]


# ---------------------------------------------------------------- speech start / pauses


async def test_speech_start_includes_preroll() -> None:
    pcm, engine, _ = _setup(("silence", 1000), ("speech", 2000), ("silence", 1500))
    run = await run_session(pcm, engine)
    started = run.of(SpeechStarted)
    assert len(started) == 1
    # Onset is confirmed a few frames in, but the utterance starts 300 ms (pre-roll) before
    # the first speech frame. Speech begins at 1000 ms, inside the 990-1020 ms frame.
    first_speech_frame_ms = (1000 // 30) * 30
    assert started[0].start_ms == first_speech_frame_ms - 300


async def test_single_sentence_is_one_final_with_accurate_timestamps() -> None:
    pcm, engine, spoken = _setup(("silence", 500), ("speech", 3000), ("silence", 1500))
    run = await run_session(pcm, engine)
    (final,) = run.result.finals
    assert final.text.split() == spoken
    assert final.seq == 0
    assert final.commit_reason is CommitReason.PAUSE
    assert (final.start_ms, final.end_ms) == (500, 3500)  # first word start, last word end
    assert final.words[0].start_ms == 500 and final.words[-1].end_ms == 3500


async def test_short_pause_does_not_split_sentence() -> None:
    pcm, engine, spoken = _setup(
        ("speech", 2100), ("silence", 450), ("speech", 2100), ("silence", 1500)
    )
    run = await run_session(pcm, engine)
    assert len(run.result.finals) == 1
    assert final_words(run.result) == spoken


async def test_long_pause_splits_into_two_finals() -> None:
    pcm, engine, spoken = _setup(
        ("speech", 2100), ("silence", 1200), ("speech", 2100), ("silence", 1500)
    )
    run = await run_session(pcm, engine)
    first, second = run.result.finals
    assert [f.seq for f in run.result.finals] == [0, 1]
    assert final_words(run.result) == spoken
    assert first.end_ms == 2100 and second.start_ms == 3300  # absolute across utterances
    assert first.commit_reason is second.commit_reason is CommitReason.PAUSE


async def test_short_utterance_waits_longer_and_merges_with_continuation() -> None:
    # 600 ms "haan" + 1000 ms pause: below min_speech_ms, so 1500 ms of silence is needed;
    # speech resumes first, so it stays one utterance.
    pcm, engine, spoken = _setup(
        ("speech", 600), ("silence", 1000), ("speech", 1500), ("silence", 2000)
    )
    run = await run_session(pcm, engine)
    assert len(run.result.finals) == 1
    assert final_words(run.result) == spoken


async def test_short_utterance_committed_after_long_silence() -> None:
    pcm, engine, spoken = _setup(
        ("speech", 600), ("silence", 1800), ("speech", 1500), ("silence", 1500)
    )
    run = await run_session(pcm, engine)
    assert len(run.result.finals) == 2
    assert final_words(run.result) == spoken


async def test_min_speech_is_configurable() -> None:
    pcm, engine, _ = _setup(("speech", 600), ("silence", 1000), ("speech", 1500), ("silence", 2000))
    config = RealtimeConfig(min_speech_ms=300)  # 600 ms now counts as a full utterance
    run = await run_session(pcm, engine, config=config)
    assert len(run.result.finals) == 2


async def test_soft_limit_commits_at_next_short_pause() -> None:
    pcm, engine, spoken = _setup(
        ("speech", 11_100), ("silence", 360), ("speech", 3000), ("silence", 1500)
    )
    run = await run_session(pcm, engine)
    first, _second = run.result.finals
    assert first.commit_reason is CommitReason.SOFT_LIMIT
    assert first.end_ms == 11_100
    assert final_words(run.result) == spoken


async def test_silence_only_produces_nothing() -> None:
    pcm, engine, _ = _setup(("silence", 5000))
    run = await run_session(pcm, engine)
    assert run.result.finals == ()
    assert run.of(SpeechStarted) == []
    assert engine.calls == []


# ---------------------------------------------------------------- long speech / forced cuts


async def test_continuous_speech_forced_word_boundary_cuts_lose_and_duplicate_nothing() -> None:
    pcm, engine, spoken = _setup(("speech", 40_000), ("silence", 1500))
    run = await run_session(pcm, engine)
    finals = run.result.finals
    assert len(finals) >= 3
    assert final_words(run.result) == spoken  # every word exactly once, in order
    assert finals[0].commit_reason is CommitReason.MAX_LENGTH
    final_calls = [c for c in engine.calls if c.kind == "final"]
    assert (
        max(c.duration_ms for c in final_calls) <= 15_000 + RealtimeConfig().forced_carry_ms + 300
    )
    assert [f.seq for f in finals] == list(range(len(finals)))
    for left, right in itertools.pairwise(finals):
        assert left.end_ms <= right.start_ms  # monotonic, no overlap


async def test_forced_cut_prefers_silence_inside_long_utterance() -> None:
    parts: list[tuple[str, int]] = []
    for _ in range(9):  # ~19 s of speech with 210 ms breaths (too short to end it)
        parts += [("speech", 1950), ("silence", 210)]
    parts += [("silence", 1500)]
    pcm, engine, spoken = _setup(*parts)
    run = await run_session(pcm, engine)
    first = run.result.finals[0]
    assert first.commit_reason is CommitReason.MAX_LENGTH
    assert 12_000 <= first.end_ms <= 15_000
    assert final_words(run.result) == spoken
    # The cut was at a silence: the next utterance starts after the gap, no carry needed.
    final_calls = [c for c in engine.calls if c.kind == "final"]
    assert all(c.duration_ms <= 15_000 for c in final_calls)


async def test_duplicate_words_at_forced_boundary_are_removed() -> None:
    # The engine echoes the last two words of its prompt (a real Whisper failure mode).
    pcm, engine, spoken = _setup(("speech", 32_000), ("silence", 1500), echo_prompt_words=2)
    run = await run_session(pcm, engine)
    assert final_words(run.result) == spoken


async def test_echo_after_a_normal_pause_is_not_altered() -> None:
    # De-duplication is limited to forced boundaries; elsewhere text is never rewritten.
    pcm, engine, spoken = _setup(
        ("speech", 2100),
        ("silence", 1200),
        ("speech", 2100),
        ("silence", 1500),
        echo_prompt_words=2,
    )
    run = await run_session(pcm, engine)
    assert final_words(run.result) == spoken[:7] + spoken[5:7] + spoken[7:]


# ---------------------------------------------------------------- partials


async def test_partials_are_provisional_versioned_and_not_in_result() -> None:
    pcm, engine, spoken = _setup(("speech", 4000), ("silence", 1500))
    run = await run_session(pcm, engine)
    partials = run.of(PartialTranscript)
    assert len(partials) >= 3
    assert [p.version for p in partials] == sorted(p.version for p in partials)
    assert all(p.utterance_id == partials[0].utterance_id for p in partials)
    assert len(partials[-1].text.split()) > len(partials[0].text.split())  # growing preview
    partial_calls = [c for c in engine.calls if c.kind == "partial"]
    assert partial_calls and not any(c.word_timestamps for c in partial_calls)
    assert all(isinstance(f, FinalTranscript) for f in run.result.finals)
    assert final_words(run.result) == spoken


async def test_partials_can_be_disabled() -> None:
    pcm, engine, _ = _setup(("speech", 3000), ("silence", 1500))
    run = await run_session(pcm, engine, config=RealtimeConfig(partials_enabled=False))
    assert run.of(PartialTranscript) == []
    assert all(c.kind == "final" for c in engine.calls)


async def test_late_partial_result_is_discarded_after_utterance_ends() -> None:
    pcm, engine, spoken = _setup(("speech", 2100), ("silence", 1500), delay=0.15)
    # No pacing: the whole recording arrives (and the utterance ends) while the first
    # partial is still running, so its result is stale when it comes back.
    run = await run_session(pcm, engine, pace=False)
    assert sum(1 for c in engine.calls if c.kind == "partial") >= 1
    assert run.scheduler.stats.partials_completed >= 1
    assert run.of(PartialTranscript) == []  # stale result never shown
    assert final_words(run.result) == spoken


async def test_continuation_absorbs_carry_instead_of_a_tiny_extra_final() -> None:
    pcm, engine, spoken = _setup(("speech", 40_000), ("silence", 1500))
    run = await run_session(pcm, engine)
    reasons = [f.commit_reason for f in run.result.finals]
    assert CommitReason.CARRY not in reasons
    assert all(len(f.text.split()) > 10 for f in run.result.finals)
    assert final_words(run.result) == spoken


# ---------------------------------------------------------------- stop / disconnect


async def test_stop_mid_speech_commits_remaining_audio_including_partial_frame() -> None:
    pcm, engine, spoken = _setup(("speech", 3000))
    tail = voiced(10, start_sample=48_000)[:100]
    extra = (tail * 32767).astype("<i2").tobytes()  # 100 samples: less than one frame
    run = await run_session(pcm, engine, extra_bytes=extra)
    (final,) = run.result.finals
    assert final.commit_reason is CommitReason.STOP
    assert final_words(run.result) == spoken
    final_call = next(c for c in engine.calls if c.kind == "final")
    assert final_call.offset_ms + final_call.duration_ms == 3006  # 3000 ms + 100 samples
    assert run.result.duration_ms == 3006
    assert run.of(SessionEnded)[0].reason is CommitReason.STOP


async def test_disconnect_commits_and_closes() -> None:
    pcm, engine, spoken = _setup(("speech", 2500))
    scheduler = InferenceScheduler(1)
    session = TranscriptionSession(engine=engine, scheduler=scheduler, mode=SpeechMode.HINDI)
    for offset in range(0, len(pcm), 8000):
        await session.feed(pcm[offset : offset + 8000])
    result = await session.disconnect()
    assert result.end_reason is CommitReason.DISCONNECT
    assert result.text.split() == spoken
    assert session.state is SessionState.CLOSED
    with pytest.raises(SessionClosedError):
        await session.feed(pcm[:960])
    assert await session.stop() is result  # idempotent
    await scheduler.aclose()


async def test_stop_waits_for_running_final() -> None:
    pcm, engine, spoken = _setup(("speech", 2100), ("silence", 1200), final_delay=0.3)
    scheduler = InferenceScheduler(1)
    session = TranscriptionSession(engine=engine, scheduler=scheduler, mode=SpeechMode.HINDI)
    for offset in range(0, len(pcm), 8000):
        await session.feed(pcm[offset : offset + 8000])
    await wait_for(lambda: scheduler.running == 1)  # the final is in progress
    result = await session.stop()
    assert result.text.split() == spoken
    await scheduler.aclose()


async def test_stop_with_no_audio() -> None:
    _, engine, _ = _setup(("silence", 100))
    scheduler = InferenceScheduler(1)
    session = TranscriptionSession(engine=engine, scheduler=scheduler)
    result = await session.stop()
    assert result.finals == () and result.duration_ms == 0
    await scheduler.aclose()


async def test_invalid_chunks_rejected() -> None:
    _, engine, _ = _setup(("silence", 100))
    scheduler = InferenceScheduler(1)
    session = TranscriptionSession(engine=engine, scheduler=scheduler)
    with pytest.raises(InvalidPCMError):
        await session.feed(b"\x00\x01\x02")
    with pytest.raises(AudioValidationError, match="exceeds"):
        await session.feed(b"\x00" * (64 * 1024 + 2))
    await session.stop()
    await scheduler.aclose()


# ---------------------------------------------------------------- failures / backlog


async def test_failed_final_is_retried() -> None:
    pcm, engine, spoken = _setup(("speech", 2100), ("silence", 1500), fail_finals=1)
    run = await run_session(pcm, engine)
    assert final_words(run.result) == spoken
    assert run.result.failures == ()
    assert sum(1 for c in engine.calls if c.kind == "final") == 2


async def test_final_failure_is_reported_never_silent_and_session_continues() -> None:
    pcm, engine, _ = _setup(
        ("speech", 2100),
        ("silence", 1200),
        ("speech", 2100),
        ("silence", 1500),
        always_fail_finals=True,
    )
    run = await run_session(pcm, engine)
    failures = run.of(UtteranceFailed)
    assert len(failures) == 2  # both utterances reported, with their audio ranges
    assert failures[0].start_ms < failures[0].end_ms <= failures[1].start_ms
    assert failures[0].error_code == "inference_failed"
    assert run.result.failures == tuple(failures)
    assert run.of(SessionEnded)[0].failed_count == 2


async def test_backlog_pauses_preview_and_overload_keeps_all_final_audio() -> None:
    parts: list[tuple[str, int]] = []
    for _ in range(8):
        parts += [("speech", 2100), ("silence", 1000)]
    pcm, engine, spoken = _setup(*parts, final_delay=0.2)
    config = RealtimeConfig(preview_pause_backlog_ms=3000, max_backlog_ms=6000)
    scheduler = InferenceScheduler(1)
    events: list[object] = []
    session = TranscriptionSession(
        engine=engine,
        scheduler=scheduler,
        mode=SpeechMode.HINDI,
        config=config,
        on_event=events.append,
    )
    with pytest.raises(SessionOverloadedError):
        for offset in range(0, len(pcm), 8000):  # much faster than real time
            await session.feed(pcm[offset : offset + 8000])
    result = await session.stop()
    await scheduler.aclose()
    assert any(isinstance(e, PreviewPaused) for e in events)
    assert result.end_reason is CommitReason.OVERLOADED
    committed = result.text.split()
    assert committed == spoken[: len(committed)]  # nothing lost or reordered
    assert len(committed) >= 7 * 3  # all utterances received before the overload


async def test_discarded_utterance_does_not_consume_a_sequence_number() -> None:
    script = Script([("speech", 2100), ("silence", 1200), ("speech", 2100), ("silence", 1500)])
    pcm = script.pcm()
    words = [w for w in words_in(script.speech_regions) if w.start_ms >= 3000]  # first is noise
    run = await run_session(pcm, TimelineEngine(words))
    assert len(run.of(UtteranceDiscarded)) == 1
    assert [f.seq for f in run.result.finals] == [0]


# ---------------------------------------------------------------- language (auto mode)


async def test_auto_mode_detects_once_and_locks_language() -> None:
    detection = LanguageDetection("hi", 0.55, (("hi", 0.55), ("en", 0.40)))
    pcm, engine, _ = _setup(
        ("speech", 3300),
        ("silence", 1200),
        ("speech", 3300),
        ("silence", 1500),
        detection=detection,
    )
    run = await run_session(pcm, engine, mode=SpeechMode.AUTO)
    (locked,) = run.of(LanguageLocked)
    assert locked.mode is SpeechMode.MIXED
    assert engine.detect_calls == 1
    finals = [c for c in engine.calls if c.kind == "final"]
    assert [c.mode for c in finals] == [SpeechMode.MIXED, SpeechMode.MIXED]
    assert run.result.speech_mode is SpeechMode.MIXED


async def test_auto_mode_short_utterance_does_not_lock() -> None:
    pcm, engine, _ = _setup(("speech", 1500), ("silence", 1800))
    run = await run_session(pcm, engine, mode=SpeechMode.AUTO)
    assert engine.detect_calls == 0
    assert run.of(LanguageLocked) == []
    assert run.result.speech_mode is SpeechMode.AUTO


async def test_explicit_mode_never_detects() -> None:
    pcm, engine, _ = _setup(("speech", 3300), ("silence", 1500))
    await run_session(pcm, engine, mode=SpeechMode.ENGLISH)
    assert engine.detect_calls == 0
    assert {c.mode for c in engine.calls} == {SpeechMode.ENGLISH}


# ---------------------------------------------------------------- concurrency across sessions


async def test_two_sessions_share_scheduler_with_bounded_concurrency() -> None:
    script = Script([("speech", 2100), ("silence", 1200), ("speech", 2100), ("silence", 1500)])
    pcm = script.pcm()
    engine = TimelineEngine(words_in(script.speech_regions), delay=0.02)
    scheduler = InferenceScheduler(2)
    sessions = [
        TranscriptionSession(engine=engine, scheduler=scheduler, mode=SpeechMode.HINDI)
        for _ in range(3)
    ]

    async def drive(session: TranscriptionSession) -> list[str]:
        for offset in range(0, len(pcm), 8000):
            await session.feed(pcm[offset : offset + 8000])
            await asyncio.sleep(0.001)
        return (await session.stop()).text.split()

    results = await asyncio.gather(*(drive(s) for s in sessions))
    await scheduler.aclose()
    expected = [w.text for w in engine.words]
    assert results == [expected] * 3
    assert engine.max_active <= 2
    assert scheduler.stats.max_observed_concurrency <= 2


async def test_carry_is_prepended_to_an_already_queued_utterance() -> None:
    # Slow finals + no pacing: the continuation is sealed (by its pause) before the forced
    # cut's final returns, so the carried audio must join the queued utterance.
    pcm, engine, spoken = _setup(("speech", 17_000), ("silence", 1500), final_delay=0.1)
    run = await run_session(pcm, engine, pace=False)
    assert final_words(run.result) == spoken
    reasons = [f.commit_reason for f in run.result.finals]
    assert reasons == [CommitReason.MAX_LENGTH, CommitReason.PAUSE]
    second_call = [c for c in engine.calls if c.kind == "final"][1]
    assert second_call.offset_ms < 15_000  # the queued utterance now starts at the cut


async def test_stop_exactly_at_a_forced_cut_commits_the_carry() -> None:
    # Recording ends right at the 15 s cut: nothing follows, so the carried words become
    # their own final instead of being lost.
    pcm, engine, spoken = _setup(("speech", 15_000))
    run = await run_session(pcm, engine, chunk_ms=500)
    assert final_words(run.result) == spoken
    assert run.result.finals[-1].commit_reason in (CommitReason.CARRY, CommitReason.STOP)
    assert [f.seq for f in run.result.finals] == list(range(len(run.result.finals)))


async def test_stop_during_forced_cut_final_commits_carry_as_its_own_final() -> None:
    # Stop arrives while the cut's final is still running and nothing follows the cut:
    # the carried audio is committed as a separate CARRY final, never dropped.
    pcm, engine, spoken = _setup(("speech", 15_000), final_delay=0.1)
    run = await run_session(pcm, engine, pace=False)
    assert final_words(run.result) == spoken
    assert [f.commit_reason for f in run.result.finals] == [
        CommitReason.MAX_LENGTH,
        CommitReason.CARRY,
    ]
    assert run.result.finals[1].start_ms == run.result.finals[0].end_ms


# ---------------------------------------------------------------- very short words


def _short_word(ms: int, *, lead: int = 600, after: int = 0) -> tuple[bytes, TimelineEngine]:
    parts: list[tuple[str, int]] = [("silence", lead), ("speech", ms)]
    if after:
        parts.append(("silence", after))
    return Script(parts).pcm(), TimelineEngine([SpokenWord("haan", lead, lead + ms)])


@pytest.mark.parametrize("word_ms", [150, 180, 250, 400])
async def test_short_word_followed_by_immediate_stop_is_kept(word_ms: int) -> None:
    pcm, engine = _short_word(word_ms)
    run = await run_session(pcm, engine)
    (final,) = run.result.finals
    assert final.text == "haan"
    assert final.commit_reason is CommitReason.STOP
    assert (final.start_ms, final.end_ms) == (600, 600 + word_ms)


async def test_short_word_with_sub_frame_tail_then_stop() -> None:
    pcm, engine = _short_word(160)  # ends mid-frame; the stop flush keeps the tail too
    run = await run_session(pcm, engine, extra_bytes=b"\x00\x00" * 100)
    assert run.result.text == "haan"
    final_call = next(c for c in engine.calls if c.kind == "final")
    assert final_call.offset_ms + final_call.duration_ms == run.result.duration_ms


async def test_short_word_then_brief_silence_then_stop() -> None:
    pcm, engine = _short_word(150, after=400)
    run = await run_session(pcm, engine)
    assert run.result.text == "haan"


async def test_short_word_in_the_middle_of_a_recording_is_kept() -> None:
    script = Script(
        [("silence", 600), ("speech", 150), ("silence", 2000), ("speech", 2100), ("silence", 1500)]
    )
    pcm = script.pcm()
    words = [SpokenWord("na", 600, 750), *words_in(script.speech_regions[1:])]
    run = await run_session(pcm, TimelineEngine(words))
    assert run.result.finals[0].text == "na"
    assert run.result.finals[0].commit_reason is CommitReason.PAUSE
    assert final_words(run.result) == [w.text for w in words]


@pytest.mark.parametrize("lead_ms", [600, 610, 620])  # aligned and misaligned to frames
@pytest.mark.parametrize("click_ms", [30, 60, 90])
async def test_clicks_before_stop_are_not_committed(click_ms: int, lead_ms: int) -> None:
    pcm, engine = _short_word(click_ms, lead=lead_ms)
    run = await run_session(pcm, engine)
    assert run.result.finals == ()
    assert engine.calls == []  # no inference spent on clicks


async def test_knocks_during_recording_are_ignored() -> None:
    parts: list[tuple[str, int]] = []
    for _ in range(10):
        parts += [("silence", 400), ("speech", 90)]  # not aligned to the 30 ms grid
    run = await run_session(Script([*parts, ("silence", 500)]).pcm(), TimelineEngine([]))
    assert run.of(SpeechStarted) == []
    assert run.session._scheduler.stats.finals_completed == 0


@pytest.mark.parametrize("lead_ms", [600, 615])
async def test_word_of_150ms_is_kept_regardless_of_frame_alignment(lead_ms: int) -> None:
    pcm, engine = _short_word(150, lead=lead_ms)
    run = await run_session(pcm, engine)
    assert run.result.text == "haan"


async def test_faint_background_noise_never_opens_an_utterance() -> None:
    rng = np.random.default_rng(3)
    noise = (rng.standard_normal(16_000 * 3) * 0.0005 * 32767).astype("<i2").tobytes()
    run = await run_session(noise, TimelineEngine([]))
    assert run.of(SpeechStarted) == [] and run.result.finals == ()


async def test_noise_burst_that_passes_vad_is_discarded_by_the_final_pass() -> None:
    # A 150 ms burst the VAD accepts, but the recogniser hears no words in (noise):
    # it is discarded, never committed as text and never given a sequence number.
    pcm, _ = _short_word(150)
    run = await run_session(pcm, TimelineEngine([]))
    assert run.result.finals == ()
    assert len(run.of(UtteranceDiscarded)) == 1
