"""TranscriptionSession: continuous PCM16 audio in, partial and final transcripts out.

    feed(pcm16) -> frame assembly -> VAD -> endpointer -> utterance buffer
                                                     |-> partial job (replaceable)
                                                     '-> sealed utterance -> final queue
    final queue -> one final job at a time -> boundary handling -> FinalTranscript (seq)

Guarantees:
- Timestamps come from an absolute sample clock (ms since the session started).
- Every received sample is in exactly one sealed utterance, the pre-roll, or trailing
  silence; forced cuts never duplicate audio, so words are not transcribed twice.
- Final jobs are never dropped: failures are retried, then reported as UtteranceFailed.
- Partials are provisional: replaced, skipped when stale, never part of the result.
- `stop()` (or a disconnect) commits the remaining audio and waits for every final.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import partial as bind

import numpy as np

from app.models.enums import SpeechMode
from app.realtime.boundary import (
    choose_word_cut,
    dedupe_prefix,
    find_silence_cut,
    is_probable_hallucination,
)
from app.realtime.buffer import AudioSpan, Frame, PreRoll, UtteranceBuffer
from app.realtime.clock import FrameAssembler, ms_to_samples, samples_to_ms
from app.realtime.config import RealtimeConfig
from app.realtime.endpointer import CommitReason, Endpointer, EndpointEvent, EndpointEventKind
from app.realtime.events import (
    FinalTranscript,
    LanguageLocked,
    PartialTranscript,
    PreviewPaused,
    PreviewResumed,
    SessionEnded,
    SessionEvent,
    SpeechStarted,
    UtteranceDiscarded,
    UtteranceFailed,
)
from app.realtime.scheduler import InferenceScheduler
from app.realtime.vad import FrameClassifier, WebRtcVad
from app.speech.audio import pcm16_to_float32
from app.speech.engine import DecodeOptions, SpeechEngine
from app.speech.errors import AudioValidationError
from app.speech.language import LanguageDetection, SessionLanguageDecision, decide_session_language
from app.speech.results import TranscriptionResult, TranscriptSegment, WordTiming

logger = logging.getLogger(__name__)


class SessionState(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


class SessionClosedError(RuntimeError):
    def __init__(
        self, reason: CommitReason | None, message: str = "session is not accepting audio"
    ):
        super().__init__(f"{message} (reason={reason})")
        self.reason = reason


class SessionOverloadedError(SessionClosedError):
    pass


@dataclass(frozen=True, slots=True)
class SealedUtterance:
    utterance_id: int
    span: AudioSpan
    reason: CommitReason
    # Forced cut without silence: words ending after this sample are carried forward.
    word_cut_keep_until_sample: int | None = None
    # Starts right after a forced cut: check its first words against the previous final.
    after_forced_boundary: bool = False


@dataclass(frozen=True, slots=True)
class _FinalOutcome:
    result: TranscriptionResult
    detection: LanguageDetection | None = None
    decision: SessionLanguageDecision | None = None


@dataclass(frozen=True)
class SessionResult:
    session_id: str
    finals: tuple[FinalTranscript, ...]
    failures: tuple[UtteranceFailed, ...]
    duration_ms: int
    end_reason: CommitReason
    speech_mode: SpeechMode
    language: str | None = None

    @property
    def text(self) -> str:
        return " ".join(final.text for final in self.finals)


@dataclass
class _PartialState:
    version: int = 0
    emitted_version: int = 0
    last_request_end: int | None = None
    last_audio_end: int = 0
    futures: set[asyncio.Future[object]] = field(default_factory=set)


class TranscriptionSession:
    def __init__(
        self,
        *,
        engine: SpeechEngine,
        scheduler: InferenceScheduler,
        mode: SpeechMode = SpeechMode.AUTO,
        config: RealtimeConfig | None = None,
        classifier: FrameClassifier | None = None,
        on_event: Callable[[SessionEvent], None] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.cfg = config or RealtimeConfig()
        self.session_id = session_id or uuid.uuid4().hex
        self.requested_mode = mode
        self._engine = engine
        self._scheduler = scheduler
        self._classifier = classifier or WebRtcVad(
            self.cfg.vad_aggressiveness, self.cfg.energy_threshold_dbfs
        )
        self._on_event = on_event
        self.events: list[SessionEvent] = []

        self._assembler = FrameAssembler(self.cfg.frame_samples)
        self._endpointer = Endpointer(self.cfg)
        self._buffer = UtteranceBuffer()
        self._preroll_frames = self.cfg.frames(self.cfg.preroll_ms)
        self._preroll = PreRoll(self._preroll_frames + self.cfg.onset_window_frames)
        self._cursor = 0  # absolute sample index of the next frame
        self._last_flag = False

        self._utterance_ids = itertools.count()
        self._current_utterance: int | None = None
        self._current_after_forced = False

        self._partial = _PartialState()
        self._preview_paused = False

        self._final_queue: deque[SealedUtterance] = deque()
        self._final_inflight: SealedUtterance | None = None
        self._final_task: asyncio.Task[None] | None = None
        self._finals: list[FinalTranscript] = []
        self._failures: list[UtteranceFailed] = []
        self._seq = 0

        self._locked_mode: SpeechMode | None = None if mode is SpeechMode.AUTO else mode
        self._locked_language: str | None = None
        self._detect_attempts = 0

        self._state = SessionState.OPEN
        self._end_reason: CommitReason | None = None
        self._close_task: asyncio.Task[SessionResult] | None = None

    # ------------------------------------------------------------------ public API

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def endpoint_state(self) -> str:
        return self._endpointer.state.value

    @property
    def received_ms(self) -> int:
        return self._assembler.clock.ms

    @property
    def finals(self) -> tuple[FinalTranscript, ...]:
        return tuple(self._finals)

    @property
    def effective_mode(self) -> SpeechMode:
        return self._locked_mode or self.requested_mode

    @property
    def backlog_ms(self) -> int:
        queued = sum(u.span.duration_ms for u in self._final_queue)
        inflight = self._final_inflight.span.duration_ms if self._final_inflight else 0
        return queued + inflight

    async def feed(self, pcm16: bytes | bytearray | memoryview) -> None:
        """Process one chunk of 16 kHz mono PCM16 audio (any size)."""
        if self._state is not SessionState.OPEN:
            raise SessionClosedError(self._end_reason)
        size = memoryview(pcm16).nbytes
        if size > self.cfg.max_chunk_bytes:
            raise AudioValidationError(
                f"audio chunk of {size} bytes exceeds {self.cfg.max_chunk_bytes}",
                code="chunk_too_large",
            )
        for frame in self._assembler.push(pcm16):
            self._process_frame(frame)

        self._update_preview_state()
        if self.backlog_ms > self.cfg.max_backlog_ms:
            self._begin_close(CommitReason.OVERLOADED)
            raise SessionOverloadedError(
                CommitReason.OVERLOADED, f"final backlog {self.backlog_ms} ms is too large"
            )
        if self._assembler.clock.ms >= self.cfg.max_session_ms:
            self._begin_close(CommitReason.SESSION_LIMIT)
            return
        self._maybe_schedule_partial()
        await asyncio.sleep(0)  # let completed jobs deliver their results

    async def stop(self, reason: CommitReason = CommitReason.STOP) -> SessionResult:
        """Commit remaining audio, wait for every final, and return the session result.

        Idempotent: later calls (with any reason) return the same result.
        """
        self._begin_close(reason)
        assert self._close_task is not None  # noqa: S101
        return await asyncio.shield(self._close_task)

    async def disconnect(self) -> SessionResult:
        """The client went away: same as stop, recorded as a disconnect."""
        return await self.stop(CommitReason.DISCONNECT)

    # ------------------------------------------------------------------ frames

    def _process_frame(self, pcm: bytes) -> None:
        is_speech = self._classifier.is_speech(pcm, self.cfg.sample_rate)
        samples = pcm16_to_float32(pcm)
        samples.setflags(write=False)
        frame = Frame(self._cursor, samples, is_speech)
        self._cursor = frame.end_sample
        self._last_flag = is_speech
        self._accept_frame(frame)
        utterance_ms = self._buffer.duration_ms if self._buffer.is_active else 0
        for event in self._endpointer.process(is_speech, utterance_ms):
            self._handle_endpoint_event(event)

    def _accept_frame(self, frame: Frame) -> None:
        if self._endpointer.is_active:
            if not self._buffer.is_active:  # continuation right after a forced cut
                self._buffer.begin([frame])
            else:
                self._buffer.append(frame)
        else:
            self._preroll.push(frame)

    def _handle_endpoint_event(self, event: EndpointEvent) -> None:
        if event.kind is EndpointEventKind.SPEECH_START:
            frames = self._preroll.take_last(event.lookback_frames + self._preroll_frames)
            self._buffer.begin(frames)
            self._start_utterance(after_forced=False)
        elif event.kind is EndpointEventKind.UTTERANCE_END:
            assert event.reason is not None  # noqa: S101
            if event.reason is CommitReason.MAX_LENGTH:
                self._forced_cut()
            else:
                self._seal_current(event.reason, event.trailing_silence_frames)

    def _start_utterance(self, *, after_forced: bool, start_sample: int | None = None) -> None:
        self._current_utterance = next(self._utterance_ids)
        self._current_after_forced = after_forced
        start = start_sample if start_sample is not None else self._buffer.start_sample
        self._partial.last_request_end = None
        self._partial.last_audio_end = start
        self._emit(SpeechStarted(self._current_utterance, samples_to_ms(start)))

    def _seal_current(self, reason: CommitReason, trailing_silence_frames: int) -> None:
        if not self._buffer.is_active or self._current_utterance is None:
            return
        trailing = trailing_silence_frames * self.cfg.frame_samples
        keep = self.cfg.samples(self.cfg.trailing_silence_keep_ms)
        end = self._buffer.end_sample - max(0, trailing - keep)
        span, rest = self._buffer.seal(max(end, self._buffer.start_sample + 1))
        self._preroll.extend(rest)  # trimmed trailing silence becomes pre-roll
        self._enqueue_final(
            SealedUtterance(
                self._current_utterance,
                span,
                reason,
                after_forced_boundary=self._current_after_forced,
            )
        )
        self._current_utterance = None
        self._current_after_forced = False

    def _forced_cut(self) -> None:
        """Max utterance length reached: cut at silence if possible, else at a word boundary."""
        assert self._current_utterance is not None  # noqa: S101
        start, end = self._buffer.start_sample, self._buffer.end_sample
        search_from = max(end - self.cfg.samples(self.cfg.boundary_search_ms), start + 1)
        cut = find_silence_cut(
            self._buffer.frames,
            search_from_sample=search_from,
            min_silence_samples=self.cfg.samples(self.cfg.min_boundary_silence_ms),
        )
        utterance_id, after_forced = self._current_utterance, self._current_after_forced
        if cut is not None and start < cut < end:
            sealed = self._buffer.split_off(cut)
            self._enqueue_final(
                SealedUtterance(utterance_id, sealed, CommitReason.MAX_LENGTH, None, after_forced)
            )
            remaining_speech = sum(1 for f in self._buffer.frames if f.is_speech)
            self._endpointer.continue_speech(remaining_speech)
            self._start_utterance(after_forced=True)
        else:
            sealed, _ = self._buffer.seal()
            keep_until = sealed.end_sample - self.cfg.samples(self.cfg.forced_carry_ms)
            self._enqueue_final(
                SealedUtterance(
                    utterance_id, sealed, CommitReason.MAX_LENGTH, keep_until, after_forced
                )
            )
            self._endpointer.continue_speech(0)
            self._start_utterance(after_forced=True, start_sample=sealed.end_sample)

    # ------------------------------------------------------------------ partials

    def _maybe_schedule_partial(self) -> None:
        if (
            not self.cfg.partials_enabled
            or self._preview_paused
            or self._state is not SessionState.OPEN
            or not self._buffer.is_active
            or self._current_utterance is None
        ):
            return
        end = self._buffer.end_sample
        if self._buffer.duration_ms < self.cfg.partial_min_audio_ms:
            return
        last = self._partial.last_request_end
        if last is not None and end - last < self.cfg.samples(self.cfg.partial_interval_ms):
            return
        if end - self._partial.last_audio_end < self.cfg.samples(self.cfg.partial_min_new_audio_ms):
            return

        snapshot = self._buffer.snapshot()
        utterance_id = self._current_utterance
        self._partial.version += 1
        version = self._partial.version
        self._partial.last_request_end = end
        self._partial.last_audio_end = end
        mode, context = self.effective_mode, self._context()

        def job() -> TranscriptionResult:
            return self._engine.transcribe(
                snapshot.audio(),
                mode=mode,
                options=DecodeOptions.partial(context),
                offset_ms=snapshot.start_ms,
            )

        future = self._scheduler.submit_partial(
            self.session_id,
            job,
            is_stale=lambda: (
                self._current_utterance != utterance_id or self._state is not SessionState.OPEN
            ),
        )
        self._partial.futures.add(future)  # type: ignore[arg-type]
        future.add_done_callback(bind(self._on_partial_done, utterance_id, version, snapshot))

    def _on_partial_done(
        self,
        utterance_id: int,
        version: int,
        snapshot: AudioSpan,
        future: asyncio.Future[TranscriptionResult | None],
    ) -> None:
        self._partial.futures.discard(future)
        if future.cancelled():
            return
        if future.exception() is not None:
            logger.debug("Partial transcription failed: %s", future.exception())
            return  # previews are best effort; the final pass is authoritative
        result = future.result()
        if (
            result is None
            or utterance_id != self._current_utterance
            or self._state is not SessionState.OPEN
            or version <= self._partial.emitted_version
        ):
            return  # replaced, stale or out of order
        self._partial.emitted_version = version
        self._emit(
            PartialTranscript(
                utterance_id, version, result.text, snapshot.start_ms, snapshot.end_ms
            )
        )

    def _update_preview_state(self) -> None:
        backlog = self.backlog_ms
        threshold = self.cfg.preview_pause_backlog_ms
        if not self._preview_paused and threshold and backlog >= threshold:
            self._preview_paused = True
            self._scheduler.cancel_partials(self.session_id)
            self._emit(PreviewPaused(backlog))
        elif self._preview_paused and backlog < threshold // 2:
            self._preview_paused = False
            self._emit(PreviewResumed(backlog))

    # ------------------------------------------------------------------ finals

    def _enqueue_final(self, utterance: SealedUtterance) -> None:
        self._final_queue.append(utterance)
        self._ensure_final_worker()

    def _ensure_final_worker(self) -> None:
        if self._final_task is None or self._final_task.done():
            self._final_task = asyncio.get_running_loop().create_task(
                self._final_worker(), name=f"stt-finals-{self.session_id}"
            )

    async def _final_worker(self) -> None:
        while self._final_queue:
            utterance = self._final_queue.popleft()
            self._final_inflight = utterance
            try:
                await self._finalize(utterance)
            finally:
                self._final_inflight = None
                if self._state is SessionState.OPEN:
                    self._update_preview_state()

    def _plan_language(self, utterance: SealedUtterance) -> tuple[SpeechMode, bool]:
        """(mode to decode with, whether to detect the language first)."""
        if self._locked_mode is not None:
            return self._locked_mode, False
        detect = (
            utterance.span.duration_ms >= self.cfg.auto_detect_min_ms
            and self._detect_attempts < self.cfg.auto_detect_max_attempts
        )
        return SpeechMode.AUTO, detect

    async def _finalize(self, utterance: SealedUtterance) -> None:
        mode, detect = self._plan_language(utterance)
        context = self._context()
        engine = self._engine

        def job() -> _FinalOutcome:
            audio = utterance.span.audio()
            detection = decision = None
            use_mode = mode
            if detect:
                detection = engine.detect_language(audio)
                decision = decide_session_language(detection)
                use_mode = decision.mode
            result = engine.transcribe(
                audio,
                mode=use_mode,
                options=DecodeOptions.final(context),
                offset_ms=utterance.span.start_ms,
            )
            return _FinalOutcome(result, detection, decision)

        outcome: _FinalOutcome | None = None
        error: BaseException | None = None
        for attempt in range(self.cfg.final_retries + 1):
            try:
                outcome = await self._scheduler.submit_final(self.session_id, job)
                break
            except Exception as exc:  # never lose a final silently: retry, then report
                error = exc
                logger.warning(
                    "Final transcription attempt %d failed: %s", attempt + 1, type(exc).__name__
                )
        if outcome is None:
            code = getattr(error, "code", "inference_failed")
            failure = UtteranceFailed(
                utterance.utterance_id,
                utterance.span.start_ms,
                utterance.span.end_ms,
                str(code),
                f"{type(error).__name__}: {error}",
            )
            self._failures.append(failure)
            self._emit(failure)
            return

        if outcome.decision is not None and outcome.detection is not None:
            self._apply_language_decision(outcome.detection, outcome.decision)
        self._commit(utterance, outcome.result)

    def _apply_language_decision(
        self, detection: LanguageDetection, decision: SessionLanguageDecision
    ) -> None:
        self._detect_attempts += 1
        if (
            decision.mode is SpeechMode.AUTO
            and self._detect_attempts < self.cfg.auto_detect_max_attempts
        ):
            return  # not confident yet: try again on the next long utterance
        self._locked_mode = decision.mode
        self._locked_language = detection.language
        self._emit(
            LanguageLocked(
                decision.mode, detection.language, detection.probability, decision.reason
            )
        )

    def _commit(self, utterance: SealedUtterance, result: TranscriptionResult) -> None:
        segments = [s for s in result.segments if not self._is_hallucination(s)]
        words: list[WordTiming] = [w for s in segments for w in s.words]
        committed_end_ms = utterance.span.end_ms
        text: str

        if utterance.word_cut_keep_until_sample is not None and words:
            choice = choose_word_cut(
                words, keep_until_ms=samples_to_ms(utterance.word_cut_keep_until_sample)
            )
            if choice is not None:
                keep, cut_ms = choice
                cut_sample = min(
                    max(ms_to_samples(cut_ms), utterance.span.start_sample + 1),
                    utterance.span.end_sample - 1,
                )
                _, carried = utterance.span.split(cut_sample)
                self._carry_forward(carried)
                words = words[:keep]
                committed_end_ms = samples_to_ms(cut_sample)
        text = (
            "".join(w.word for w in words).strip()
            if utterance.word_cut_keep_until_sample is not None and words
            else " ".join(s.text for s in segments if s.text)
        )

        if utterance.after_forced_boundary and self._finals and self.cfg.dedupe_max_words:
            tokens = [w.word for w in words] if words else text.split()
            drop = dedupe_prefix(
                self._finals[-1].text.split(),
                tokens,
                min_words=self.cfg.dedupe_min_words,
                max_words=self.cfg.dedupe_max_words,
            )
            if drop:
                logger.info("Removed %d duplicated word(s) at a forced boundary", drop)
                if words:
                    words = words[drop:]
                    text = "".join(w.word for w in words).strip()
                else:
                    text = " ".join(tokens[drop:])

        if not text.strip():
            reason = "filtered" if result.segments else "no_speech"
            self._emit(
                UtteranceDiscarded(
                    utterance.utterance_id, utterance.span.start_ms, committed_end_ms, reason
                )
            )
            return

        start_ms = words[0].start_ms if words else segments[0].start_ms
        end_ms = words[-1].end_ms if words else segments[-1].end_ms
        floor = max(utterance.span.start_ms, self._finals[-1].end_ms if self._finals else 0)
        start_ms = min(max(start_ms, floor), committed_end_ms)
        end_ms = min(max(end_ms, start_ms), committed_end_ms)

        final = FinalTranscript(
            seq=self._seq,
            utterance_id=utterance.utterance_id,
            text=text,
            start_ms=start_ms,
            end_ms=end_ms,
            language=result.language,
            avg_logprob=_weighted_logprob(segments),
            commit_reason=utterance.reason,
            decode_strategy=result.decode_strategy,
            words=tuple(words),
        )
        self._seq += 1
        self._finals.append(final)
        self._emit(final)

    def _carry_forward(self, span: AudioSpan) -> None:
        """Hand audio after a word-boundary cut to whatever directly follows it."""
        if self._final_queue and self._final_queue[0].span.start_sample == span.end_sample:
            head = self._final_queue[0]
            self._final_queue[0] = replace(
                head, span=span.concat(head.span), after_forced_boundary=True
            )
        elif self._buffer.is_active and self._buffer.start_sample == span.end_sample:
            self._buffer.prepend(span)
            self._current_after_forced = True
        elif (
            self._state is SessionState.OPEN
            and self._endpointer.is_active
            and not self._buffer.is_active
            and self._cursor == span.end_sample
        ):
            # Still mid-utterance but the continuation has no frames yet: start it with
            # the carried audio so the sentence is not split into a tiny extra final.
            self._buffer.begin(span.frames)
            self._current_after_forced = True
        else:
            self._final_queue.appendleft(
                SealedUtterance(
                    next(self._utterance_ids), span, CommitReason.CARRY, after_forced_boundary=True
                )
            )
            self._ensure_final_worker()

    def _is_hallucination(self, segment: TranscriptSegment) -> bool:
        return is_probable_hallucination(
            segment,
            no_speech_prob=self.cfg.hallucination_no_speech_prob,
            logprob=self.cfg.hallucination_logprob,
            compression_ratio=self.cfg.hallucination_compression_ratio,
        )

    def _context(self) -> str | None:
        if not self._finals or not self.cfg.context_chars:
            return None
        return " ".join(f.text for f in self._finals)[-self.cfg.context_chars :] or None

    # ------------------------------------------------------------------ closing

    def _begin_close(self, reason: CommitReason) -> None:
        if self._close_task is not None:
            return
        self._state = SessionState.CLOSING
        self._end_reason = reason
        self._commit_remaining(reason)
        self._scheduler.cancel_partials(self.session_id)
        self._close_task = asyncio.get_running_loop().create_task(
            self._drain_and_close(reason), name=f"stt-close-{self.session_id}"
        )

    def _commit_remaining(self, reason: CommitReason) -> None:
        # A very short word right before stop may not have reached onset yet: keep it.
        pending = self._endpointer.flush_pending()
        if pending is not None:
            self._handle_endpoint_event(pending)
        remainder = self._assembler.flush()
        if remainder and self._endpointer.is_active:
            samples = pcm16_to_float32(remainder)
            samples.setflags(write=False)
            frame = Frame(self._cursor, samples, self._last_flag)
            self._cursor = frame.end_sample
            self._accept_frame(frame)
        elif remainder:
            self._cursor += len(remainder) // 2  # < 1 frame outside any utterance
        event = self._endpointer.force_end(reason)
        if event is not None:
            self._seal_current(reason, event.trailing_silence_frames)

    async def _drain_and_close(self, reason: CommitReason) -> SessionResult:
        while self._final_task is not None and not self._final_task.done():
            await self._final_task
        for future in list(self._partial.futures):
            future.cancel()
        self._state = SessionState.CLOSED
        duration = self._assembler.clock.ms
        self._emit(SessionEnded(reason, duration, len(self._finals), len(self._failures)))
        return SessionResult(
            session_id=self.session_id,
            finals=tuple(self._finals),
            failures=tuple(self._failures),
            duration_ms=duration,
            end_reason=reason,
            speech_mode=self.effective_mode,
            language=self._locked_language,
        )

    # ------------------------------------------------------------------ helpers

    def _emit(self, event: SessionEvent) -> None:
        if self._on_event is None:
            self.events.append(event)  # recorded only when no consumer is attached
            return
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:  # a broken consumer must not break transcription
                logger.exception("Session event handler failed")


def _weighted_logprob(segments: list[TranscriptSegment]) -> float | None:
    if not segments:
        return None
    weights = np.array([max(s.duration_ms, 1) for s in segments], dtype=np.float64)
    values = np.array([s.avg_logprob for s in segments], dtype=np.float64)
    return float(np.average(values, weights=weights))
