"""Endpointing state machine, frame by frame (pure: no audio)."""

from __future__ import annotations

import pytest

from app.realtime.config import RealtimeConfig
from app.realtime.endpointer import (
    CommitReason,
    Endpointer,
    EndpointEvent,
    EndpointEventKind,
    EndpointState,
)

CFG = RealtimeConfig()
F = CFG.frame_ms  # 30 ms


def feed(ep: Endpointer, pattern: str, *, start_ms: int = 0) -> list[EndpointEvent]:
    """'S' = speech frame, '.' = silence frame. Utterance length grows with each frame."""
    events: list[EndpointEvent] = []
    elapsed = start_ms
    for ch in pattern:
        if ep.is_active:
            elapsed += F
        events += ep.process(ch == "S", elapsed if ep.is_active else 0)
        if ep.state is EndpointState.FINALIZE:
            elapsed = 0
    return events


def state(ep: Endpointer) -> EndpointState:
    """Read the state through a call so type checkers don't narrow it between asserts."""
    return ep.state


def kinds(events: list[EndpointEvent]) -> list[EndpointEventKind]:
    return [e.kind for e in events]


def frames(ms: int) -> int:
    return CFG.frames(ms)


def test_onset_needs_six_of_ten_speech_frames() -> None:
    ep = Endpointer(CFG)
    assert feed(ep, "S.S.S.S.S") == []  # 5 speech frames in the window: not yet
    assert state(ep) is EndpointState.SILENCE
    events = feed(ep, "S")  # window "S.S.S.S.SS": 6 of 10
    assert kinds(events) == [EndpointEventKind.SPEECH_START]
    assert state(ep) is EndpointState.IN_SPEECH


def test_onset_lookback_points_at_first_speech_frame() -> None:
    ep = Endpointer(CFG)
    feed(ep, "..........")
    (event,) = feed(ep, "SSSSSS")
    assert event.lookback_frames == 6  # first speech frame was 6 frames ago (incl. current)
    ep2 = Endpointer(CFG)
    (event2,) = feed(ep2, "S..SSSSS")
    assert event2.lookback_frames == 8


def test_isolated_clicks_never_start_speech() -> None:
    ep = Endpointer(CFG)
    assert feed(ep, ("S" + "." * 9) * 20) == []


def test_short_dip_inside_speech_is_not_a_pause() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * 20)
    assert feed(ep, "..S") == []  # 60 ms dip < pause_enter_ms (90 ms)
    assert state(ep) is EndpointState.IN_SPEECH


def test_pause_then_resume_keeps_the_utterance() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * 40)
    events = feed(ep, "." * frames(450) + "SS")
    assert kinds(events) == [EndpointEventKind.PAUSE_START, EndpointEventKind.SPEECH_RESUME]
    assert state(ep) is EndpointState.IN_SPEECH


def test_single_click_does_not_cancel_a_pause() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * 40)  # 1.2 s of speech (> min_speech_ms)
    events = feed(ep, "." * 10 + "S" + "." * (frames(800) - 10))
    assert EndpointEventKind.SPEECH_RESUME not in kinds(events)
    assert events[-1].kind is EndpointEventKind.UTTERANCE_END
    assert events[-1].reason is CommitReason.PAUSE


def test_800ms_silence_ends_a_normal_utterance() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * 40)
    assert EndpointEventKind.UTTERANCE_END not in kinds(feed(ep, "." * (frames(800) - 1)))
    (end,) = [e for e in feed(ep, ".") if e.kind is EndpointEventKind.UTTERANCE_END]
    assert end.reason is CommitReason.PAUSE
    assert end.trailing_silence_frames == frames(800)
    assert state(ep) is EndpointState.FINALIZE


def test_finalize_is_transient() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * 40 + "." * frames(800))
    assert state(ep) is EndpointState.FINALIZE
    feed(ep, ".")
    assert state(ep) is EndpointState.SILENCE


def test_short_utterance_needs_longer_silence() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * 20)  # 600 ms < min_speech_ms
    assert EndpointEventKind.UTTERANCE_END not in kinds(feed(ep, "." * frames(1000)))
    events = feed(ep, "." * (frames(1500) - frames(1000)))
    assert events[-1].kind is EndpointEventKind.UTTERANCE_END


def test_short_utterance_merges_when_speech_resumes() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * 20 + "." * frames(1000))
    events = feed(ep, "SS" + "S" * 30)
    assert EndpointEventKind.SPEECH_RESUME in kinds(events)
    assert EndpointEventKind.UTTERANCE_END not in kinds(events)


def test_soft_limit_commits_at_shorter_pause() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * frames(10_200))
    events = feed(ep, "." * frames(300), start_ms=10_200)
    assert events[-1].reason is CommitReason.SOFT_LIMIT


def test_before_soft_limit_short_pause_does_not_commit() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * frames(5_000))
    events = feed(ep, "." * frames(300) + "SS", start_ms=5_000)
    assert EndpointEventKind.UTTERANCE_END not in kinds(events)


def test_max_length_forces_end_even_in_continuous_speech() -> None:
    ep = Endpointer(CFG)
    # Utterance time counts from the onset (6th frame), so feed a little past 15 s.
    events = feed(ep, "S" * (frames(15_000) + 20))
    ends = [e for e in events if e.kind is EndpointEventKind.UTTERANCE_END]
    assert ends and ends[0].reason is CommitReason.MAX_LENGTH
    assert ends[0].trailing_silence_frames == 0


def test_continue_speech_after_forced_cut() -> None:
    ep = Endpointer(CFG)
    feed(ep, "S" * (frames(15_000) + 20))
    ep.continue_speech(carried_speech_frames=10)
    assert state(ep) is EndpointState.IN_SPEECH
    assert ep.speech_ms == 300
    assert feed(ep, "S") == []  # no new onset needed


def test_force_end_only_when_active() -> None:
    ep = Endpointer(CFG)
    assert ep.force_end(CommitReason.STOP) is None
    feed(ep, "S" * 10 + "." * 5)
    event = ep.force_end(CommitReason.STOP)
    assert event is not None and event.reason is CommitReason.STOP
    assert event.trailing_silence_frames == 5


@pytest.mark.parametrize(
    "overrides",
    [
        {"onset_min_speech_frames": 11},
        {"pause_enter_ms": 900},
        {"end_silence_ms": 2000},
        {"soft_pause_ms": 900},
        {"soft_max_utterance_ms": 20_000},
        {"boundary_search_ms": 16_000},
        {"forced_carry_ms": 8_000},
        {"frame_ms": 25},
    ],
)
def test_inconsistent_thresholds_rejected(overrides: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        RealtimeConfig(**overrides)


def test_thresholds_are_configurable() -> None:
    cfg = RealtimeConfig(end_silence_ms=500, pause_enter_ms=60)
    ep = Endpointer(cfg)
    feed(ep, "S" * 40)
    events = feed(ep, "." * cfg.frames(500))
    assert events[-1].reason is CommitReason.PAUSE


# ---------------------------------------------------------------- short bursts


def test_short_burst_opens_utterance_when_it_ends() -> None:
    ep = Endpointer(CFG)
    assert feed(ep, "..........SSSSS") == []  # 5 frames: below the 6-of-10 onset
    (event,) = feed(ep, ".")
    assert event.kind is EndpointEventKind.SPEECH_START
    assert event.short_burst and event.lookback_frames == 6  # burst + this silent frame
    assert state(ep) is EndpointState.IN_PAUSE
    assert ep.speech_ms == 150


def test_short_burst_uses_short_utterance_silence_rule() -> None:
    ep = Endpointer(CFG)
    feed(ep, "SSSSS.")
    assert EndpointEventKind.UTTERANCE_END not in kinds(feed(ep, "." * (frames(800))))
    events = feed(ep, "." * (frames(1500) - frames(800)))
    assert events[-1].reason is CommitReason.PAUSE


def test_short_burst_merges_with_following_speech() -> None:
    ep = Endpointer(CFG)
    feed(ep, "SSSSS.")
    events = feed(ep, "." * 5 + "SS" + "S" * 20)
    assert kinds(events) == [EndpointEventKind.SPEECH_RESUME]


@pytest.mark.parametrize("burst", ["S", "SS", "SSS", "SSSS"])
def test_bursts_below_threshold_are_ignored(burst: str) -> None:
    ep = Endpointer(CFG)
    assert feed(ep, (burst + "." * 12) * 10) == []  # <= 4 frames: a click or knock
    assert state(ep) is EndpointState.SILENCE


def test_short_burst_rule_can_be_disabled() -> None:
    ep = Endpointer(RealtimeConfig(short_burst_min_frames=0))
    assert feed(ep, "SSSSS" + "." * 20) == []
    assert ep.flush_pending() is None


def test_short_burst_threshold_is_configurable() -> None:
    ep = Endpointer(RealtimeConfig(short_burst_min_frames=3))
    (event,) = feed(ep, "SSS.")
    assert event.short_burst


def test_flush_pending_at_stop() -> None:
    ep = Endpointer(CFG)
    feed(ep, ".....SSSSS")  # stopped mid-burst, before any silence frame
    event = ep.flush_pending()
    assert event is not None and event.short_burst and event.lookback_frames == 5
    assert state(ep) is EndpointState.IN_SPEECH
    end = ep.force_end(CommitReason.STOP)
    assert end is not None and end.reason is CommitReason.STOP


def test_flush_pending_ignores_clicks_and_active_utterances() -> None:
    ep = Endpointer(CFG)
    feed(ep, "......SSSS")
    assert ep.flush_pending() is None  # <= 4 frames: a click, not a word
    ep2 = Endpointer(CFG)
    feed(ep2, "S" * 10)
    assert ep2.flush_pending() is None  # already active: handled by force_end


def test_short_burst_threshold_validated() -> None:
    with pytest.raises(ValueError, match="short_burst_min_frames"):
        RealtimeConfig(short_burst_min_frames=7)
