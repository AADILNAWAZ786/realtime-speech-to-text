"""Endpointing state machine: SILENCE -> IN_SPEECH <-> IN_PAUSE -> FINALIZE -> SILENCE.

Pure logic: it sees one speech/non-speech flag per frame (plus the current utterance length
from the buffer) and emits events. It never touches audio, so every rule is unit-testable.

Rules (frame counts derived from RealtimeConfig):
- Onset: >= onset_min_speech_frames speech frames within the last onset_window_frames.
- Short burst: a run of >= short_burst_min_frames consecutive speech frames that ends
  below the onset threshold (a quick "no" / "haan") opens the utterance retroactively,
  directly in IN_PAUSE, so the short-utterance silence rule then applies. Shorter runs
  (clicks, knocks) are ignored. At stop, `flush_pending()` does the same for a burst still
  in the window. The final pass (Silero vad_filter + hallucination filter) is the second
  opinion that discards non-speech.
- IN_SPEECH -> IN_PAUSE after pause_enter_ms of consecutive silence.
- IN_PAUSE -> IN_SPEECH after resume_min_speech_frames consecutive speech frames
  (a single click does not cancel a pause).
- Utterance end (FINALIZE):
  * pause >= end_silence_ms, if the utterance has >= min_speech_ms of speech;
  * pause >= short_utterance_silence_ms otherwise (short utterances wait longer, so
    "haan... toh" is not split into two finals);
  * after soft_max_utterance_ms, any pause >= soft_pause_ms;
  * at max_utterance_ms, a forced end (the session chooses a safe cut point).
- FINALIZE is transient: the next frame is processed in SILENCE.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from app.realtime.config import RealtimeConfig


class EndpointState(StrEnum):
    SILENCE = "silence"
    IN_SPEECH = "in_speech"
    IN_PAUSE = "in_pause"
    FINALIZE = "finalize"


class CommitReason(StrEnum):
    PAUSE = "pause"
    SOFT_LIMIT = "soft_limit"
    MAX_LENGTH = "max_length"
    STOP = "stop"
    DISCONNECT = "disconnect"
    SESSION_LIMIT = "session_limit"
    OVERLOADED = "overloaded"
    CARRY = "carry"  # audio carried over from a forced cut, committed on its own


class EndpointEventKind(StrEnum):
    SPEECH_START = "speech_start"
    PAUSE_START = "pause_start"
    SPEECH_RESUME = "speech_resume"
    UTTERANCE_END = "utterance_end"


@dataclass(frozen=True, slots=True)
class EndpointEvent:
    kind: EndpointEventKind
    frame_index: int
    lookback_frames: int = 0  # SPEECH_START: frames back to the first speech frame (incl.)
    reason: CommitReason | None = None  # UTTERANCE_END
    trailing_silence_frames: int = 0  # UTTERANCE_END: silence at the end of the utterance
    short_burst: bool = False  # SPEECH_START opened by the short-burst rule


class Endpointer:
    def __init__(self, config: RealtimeConfig | None = None) -> None:
        cfg = config or RealtimeConfig()
        self._cfg = cfg
        self._onset_min = cfg.onset_min_speech_frames
        self._pause_enter = cfg.frames(cfg.pause_enter_ms)
        self._end_silence = cfg.frames(cfg.end_silence_ms)
        self._short_silence = cfg.frames(cfg.short_utterance_silence_ms)
        self._soft_pause = cfg.frames(cfg.soft_pause_ms)
        self._resume_min = cfg.resume_min_speech_frames
        self._burst_min = cfg.short_burst_min_frames

        self.state = EndpointState.SILENCE
        self._window: deque[bool] = deque(maxlen=cfg.onset_window_frames)
        self._run = 0  # consecutive speech frames while in SILENCE
        self._frame_index = -1
        self._reset_utterance()

    def _reset_utterance(self) -> None:
        self._speech_frames = 0
        self._silence_run = 0
        self._speech_run = 0
        self._pause_frames = 0  # frames since the current pause began (incl. its onset run)

    @property
    def is_active(self) -> bool:
        return self.state in (EndpointState.IN_SPEECH, EndpointState.IN_PAUSE)

    @property
    def speech_ms(self) -> int:
        return self._speech_frames * self._cfg.frame_ms

    @property
    def frame_index(self) -> int:
        return self._frame_index

    def process(self, is_speech: bool, utterance_ms: int = 0) -> list[EndpointEvent]:
        """Advance by one frame. `utterance_ms` is the buffered utterance length."""
        self._frame_index += 1
        if self.state is EndpointState.FINALIZE:
            self.state = EndpointState.SILENCE

        if self.state is EndpointState.SILENCE:
            return self._process_silence(is_speech)

        events: list[EndpointEvent] = []
        if is_speech:
            self._speech_frames += 1

        if self.state is EndpointState.IN_SPEECH:
            if is_speech:
                self._silence_run = 0
            else:
                self._silence_run += 1
                if self._silence_run >= self._pause_enter:
                    self.state = EndpointState.IN_PAUSE
                    self._pause_frames = self._silence_run
                    self._speech_run = 0
                    events.append(self._event(EndpointEventKind.PAUSE_START))
        else:  # IN_PAUSE
            self._pause_frames += 1
            if is_speech:
                self._speech_run += 1
                if self._speech_run >= self._resume_min:
                    self.state = EndpointState.IN_SPEECH
                    self._silence_run = 0
                    self._speech_run = 0
                    self._pause_frames = 0
                    events.append(self._event(EndpointEventKind.SPEECH_RESUME))
            else:
                self._silence_run += 1
                self._speech_run = 0

        reason = self._end_reason(utterance_ms)
        if reason is not None:
            events.append(self._finalize(reason))
        return events

    def _process_silence(self, is_speech: bool) -> list[EndpointEvent]:
        self._window.append(is_speech)
        run_before = self._run
        self._run = self._run + 1 if is_speech else 0
        if sum(self._window) >= self._onset_min:
            return [self._open(self._lookback(), EndpointState.IN_SPEECH, sum(self._window))]
        if not is_speech and self._burst_min and run_before >= self._burst_min:
            # A short burst just ended without reaching onset: open it retroactively,
            # already pausing (this frame is its first silent frame).
            event = self._open(run_before + 1, EndpointState.IN_PAUSE, run_before, short_burst=True)
            self._silence_run = self._pause_frames = 1
            return [event]
        return []

    def _lookback(self) -> int:
        first_speech = next(i for i, flag in enumerate(self._window) if flag)
        return len(self._window) - first_speech

    def _open(
        self, lookback: int, state: EndpointState, speech_frames: int, *, short_burst: bool = False
    ) -> EndpointEvent:
        self._window.clear()
        self._run = 0
        self._reset_utterance()
        self._speech_frames = speech_frames
        self.state = state
        return self._event(
            EndpointEventKind.SPEECH_START, lookback_frames=lookback, short_burst=short_burst
        )

    def flush_pending(self) -> EndpointEvent | None:
        """Recording is stopping while idle: open an utterance for recent speech that is
        long enough to be a short word (>= short_burst_min_frames in the onset window) but
        never reached the onset threshold. None if there is nothing worth committing."""
        if self.is_active or not self._burst_min or sum(self._window) < self._burst_min:
            return None
        return self._open(
            self._lookback(), EndpointState.IN_SPEECH, sum(self._window), short_burst=True
        )

    def _end_reason(self, utterance_ms: int) -> CommitReason | None:
        if self.state is EndpointState.IN_PAUSE:
            required = (
                self._end_silence
                if self.speech_ms >= self._cfg.min_speech_ms
                else self._short_silence
            )
            if self._silence_run >= required:
                return CommitReason.PAUSE
            if (
                utterance_ms >= self._cfg.soft_max_utterance_ms
                and self._silence_run >= self._soft_pause
            ):
                return CommitReason.SOFT_LIMIT
        if utterance_ms >= self._cfg.max_utterance_ms:
            return CommitReason.MAX_LENGTH
        return None

    def _finalize(self, reason: CommitReason) -> EndpointEvent:
        trailing = self._pause_frames if self.state is EndpointState.IN_PAUSE else 0
        if reason is CommitReason.MAX_LENGTH:
            trailing = 0  # the session picks the cut point itself
        event = self._event(
            EndpointEventKind.UTTERANCE_END, reason=reason, trailing_silence_frames=trailing
        )
        self.state = EndpointState.FINALIZE
        self._reset_utterance()
        return event

    def force_end(self, reason: CommitReason) -> EndpointEvent | None:
        """End the current utterance now (stop / disconnect). None if nothing is active."""
        if not self.is_active:
            return None
        return self._finalize(reason)

    def continue_speech(self, carried_speech_frames: int = 0) -> None:
        """After a forced cut, keep going as one ongoing utterance (no new onset needed)."""
        self.state = EndpointState.IN_SPEECH
        self._reset_utterance()
        self._speech_frames = carried_speech_frames

    def _event(self, kind: EndpointEventKind, **kwargs: object) -> EndpointEvent:
        return EndpointEvent(kind=kind, frame_index=self._frame_index, **kwargs)  # type: ignore[arg-type]
