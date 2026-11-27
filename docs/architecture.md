# Architecture

Status: Steps 1–5 implemented: backend foundation, speech engine, real-time processing
core, and WebSocket integration with persistence. The frontend and deployment are next.

```
Browser (React + TS, later step)
  mic → AudioWorklet → 16 kHz mono PCM16 chunks ──wss──┐          REST (CRUD) ──https──┐
                                                       ▼                               ▼
FastAPI (single process)                                                                │
┌──────────────────────────────────────────────────────────────────────────────────────┴─┐
│ app/ws: Origin check → JWT (first message) → slot → frame/rate limits → session        │
│                │ feed(pcm16)                                                            │
│  app/realtime  ▼                                                                        │
│  FrameAssembler (sample clock) → WebRtcVad → Endpointer → UtteranceBuffer               │
│        partial snapshots ─┐                       sealed utterances ─┐                  │
│                           ▼                                          ▼                  │
│                InferenceScheduler (thread pool; FINAL > PARTIAL; per-session order)     │
│                           │                                                             │
│  app/speech               ▼                                                             │
│  FasterWhisperEngine (CTranslate2, int8 CPU / float16 GPU, one shared model)            │
│                           │                                                             │
│  boundary handling (carry / de-dup / filter) → FinalTranscript events (seq)             │
│                                                                                         │
│  app/api · services · repositories ──► PostgreSQL (users, transcriptions, segments)     │
└─────────────────────────────────────────────────────────────────────────────────────────┘
```

## Packages

| Package | Responsibility | Depends on |
|---|---|---|
| `app/core`, `app/db`, `app/models`, `app/schemas`, `app/repositories`, `app/services`, `app/api` | Configuration, logging, security, ORM models, user-scoped REST CRUD | FastAPI, SQLAlchemy, PostgreSQL |
| `app/speech` | Audio validation, `SpeechEngine` interface, faster-whisper backend, language modes, results, script rendering | faster-whisper, NumPy, indic-transliteration |
| `app/realtime` | VAD, endpointing, buffering, inference scheduling, `TranscriptionSession` | `app/speech`, webrtcvad-wheels |
| `app/ws` | `/ws/transcribe`: protocol, limits, persistence, shared runtime, per-connection pipeline | `app/realtime`, `app/speech`, DB layer |

`app/realtime` and `app/speech` do not import the web or database layers, so each can be
tested on its own with fakes.

---

## Real-time processing core (`app/realtime`)

### Audio-to-transcript flow

1. **`feed(pcm16)`**: chunks of any size (≤ 64 KB) arrive. `FrameAssembler` cuts them into
   exact 30 ms frames (480 samples) and holds back any remainder for the next chunk. The
   **sample clock** advances by every sample received.
2. **VAD (layer 1)**: each frame is classified by `WebRtcVad`, which combines WebRTC VAD
   with an energy gate.
3. **Endpointer**: a pure state machine that consumes one flag per frame (see below).
4. **Buffering**: while idle, frames go to a pre-roll ring. At speech onset the utterance
   begins 300 ms before the first speech frame. Frames are appended until the endpointer
   ends the utterance, which is then **sealed** into an immutable `AudioSpan`.
5. **Partials**: about every 1 s of new audio (at least 0.5 s new, at least 0.3 s total), a
   snapshot of the open utterance is transcribed with the *partial* preset (beam 1,
   temperature 0, no word timestamps, max 120 tokens). Results are provisional, versioned
   and never stored.
6. **Finals**: sealed utterances queue per session and are transcribed **one at a time**
   with the *final* preset (beam 5, word timestamps, faster-whisper's Silero `vad_filter`
   as layer 2). Then:
   - forced-cut carry
   - hallucination filter
   - de-duplication at forced boundaries
   - timestamp clamping
   - `FinalTranscript` with the next `seq`
7. **`stop()` / `disconnect()`**: the remaining partial frame and any open utterance are
   committed, every final is awaited, and a `SessionResult` is returned.

### Endpointing state machine

```
           ≥6 of 10 frames speech               ≥90 ms silence
 SILENCE ─────────────────────────▶ IN_SPEECH ─────────────────▶ IN_PAUSE
    ▲                                  ▲  │                          │
    │                                  │  └── 15 s max length ──┐    │ ≥2 speech frames
    │                                  └─────────────────────────┼────┘ (resume, same utterance)
    │                                                            ▼
    └──────────── next frame ──────────────────────────────── FINALIZE ◀── pause long enough
```

| Rule | Default | Purpose |
|---|---|---|
| Onset: speech frames within the window | 6 of 10 frames (~180 ms) | Ignore clicks and coughs |
| Short burst | ≥ 5 consecutive speech frames that end below onset, or are still pending at stop | Keep very short words ("no", "haan") |
| Pre-roll | 300 ms | Don't clip the first syllable |
| Enter IN_PAUSE | 90 ms of silence | Ignore dips between words |
| Resume from pause | 2 consecutive speech frames | One click doesn't cancel a pause |
| End of utterance | 800 ms of pause, if ≥ 1000 ms of speech | Normal sentence end |
| Short utterances | need 1500 ms of pause | "haan… toh" stays one utterance |
| Soft limit | after 10 s, commit at a pause ≥ 300 ms | Split long speech at breaths |
| Hard limit | 15 s, forced safe cut | Bounds buffer size and compute |
| Trailing silence kept | 200 ms | Less audio to decode, fewer hallucinations |

All values live in `RealtimeConfig`, which validates them (for example, it rejects
`pause_enter < end_silence ≤ short_utterance_silence` violations).

#### Very short words

The 6-of-10 onset rule alone loses words of 150 ms or less: WebRTC flags about one frame
per 30 ms of voice. The **short-burst rule** handles them without making live detection
more sensitive:

- A run of at least 5 consecutive speech frames that ends below the onset threshold
  opens the utterance retroactively, directly in `IN_PAUSE`. The normal short-utterance
  rules then apply: it ends after 1.5 s of silence, or merges with speech that follows.
- When recording stops, `flush_pending()` does the same for a burst still in the onset
  window, so a word followed by an immediate stop is committed rather than discarded.
- Why 5 frames: with 30 ms frames, a sound of 90 ms or less overlaps at most 4 frames, so
  clicks and knocks are ignored however they are aligned. A word of 150 ms or more always
  covers at least 5 frames. Words of 120–149 ms are kept only when they overlap 5 frames.
- Bursts that the VAD accepts but that are not speech are dropped by the final pass
  (faster-whisper's Silero `vad_filter` plus the hallucination filter) as
  `UtteranceDiscarded`. They never become text or use a sequence number.

Measured with WebRTC VAD on synthetic voiced bursts:

| Sound | Stop right after (aligned / +15 ms) | Mid-recording (aligned / +15 ms) |
|---|---|---|
| 60–90 ms | ignored / ignored | ignored / ignored |
| 120 ms | ignored / ignored | ignored / kept |
| 140 ms | ignored / kept | kept / kept |
| 150 ms and longer | kept / kept | kept / kept |

`short_burst_min_frames` is configurable; `0` turns the rule off.

### Forced cuts without lost or duplicated words

When an utterance reaches 15 s:

1. **Silence cut (preferred).** Find the longest non-speech run of at least 150 ms in the last 3 s
   and cut at its midpoint. No word straddles a silence.
2. **Word-boundary cut (fallback).** Seal the whole 15 s. The final pass returns word
   timestamps. Commit the words that end before *end − 1 s*, cut midway into the next gap,
   and **carry** the rest of the audio forward. The carried audio is joined to whatever
   follows it, which is either:
   - an already-queued utterance,
   - the live buffer,
   - a continuation that hasn't started yet, or
   - if the recording stopped, a separate `carry` final.
3. **De-duplication.** At forced boundaries only, leading words that repeat the previous
   final's last words are dropped (at least 2 words; case and punctuation insensitive).
   Whisper can echo its prompt. Elsewhere, text is never rewritten.

Every sample belongs to exactly one sealed utterance, the pre-roll, or trimmed silence.
`AudioSpan.concat` refuses non-contiguous audio. The tests use a "perfect recogniser"
over a known word timeline. They check that 40 s of continuous speech is committed with
every word exactly once, in order, across every carry path.

### Timestamps and sequence numbers

- Time = **samples received** since the session started, never wall-clock time. At 16 kHz,
  1 ms is exactly 16 samples.
- Each utterance knows its absolute `start_sample`. The engine receives
  `offset_ms = start_ms`, so segment and word times are relative to the whole recording.
- Final start/end come from the first and last committed word. They are clamped inside the
  utterance and never earlier than the previous final's end, so they only increase.
- `seq` is assigned only when a non-empty final is committed, giving 0, 1, 2, … with no gaps.
  Discarded or failed utterances don't use a number.
- `utterance_id` links partials to their final, and is separate from `seq`.

### Inference scheduler

| Rule | Implementation |
|---|---|
| Blocking inference off the event loop | `ThreadPoolExecutor(max_concurrency)`; all bookkeeping on the loop thread |
| Finals before partials | Finals are always picked first |
| Finals never dropped | `aclose()` drains every final; final failures go back to the caller |
| One final at a time per session | A final waits while the same session has one running |
| Stale partials skipped | `is_stale()` is checked when a job would start; the result is also dropped if it arrives late |
| One queued partial per session | A newer request replaces it (the old future resolves to `None`); round-robin across sessions |
| CPU limit | `max_concurrency × engine cpu_threads ≤ vCPUs − 1`. `recommended_concurrency()` returns the largest value that fits and **raises `ValueError`** when even one job cannot fit (1 vCPU, or more threads than spare cores), unless `allow_oversubscription=True` is passed, which logs a warning. `validate_cpu_budget()` checks a configured value. |
| Bounded bookkeeping | Submission timestamps are kept only for queued jobs. Every path that removes a job (dispatched, replaced, cancelled by the caller or the scheduler, stale, dropped at shutdown) releases its entry; keys are unique job sequence numbers, not `id()`. Queue-wait statistics keep the last 1000 values. |

### Failure handling and backpressure

- **Final failures** are retried once (configurable), then reported as `UtteranceFailed`
  with the audio range. They are never silent, and the session keeps going.
- **Partial failures** are logged and ignored, because the final pass is authoritative.
- **Backlog:** if ≥ 10 s of audio is waiting for finals, partials pause (`PreviewPaused`) and
  resume below 5 s. Above 30 s the session stops accepting audio
  (`SessionOverloadedError`), but **finishes every queued final**.
- A **session limit** of 30 min triggers a clean stop (`session_limit`).

### Language handling in auto mode

The first utterance of at least 3 s runs `detect_language`, then `decide_session_language`
(from Step 3) inside the same final job. The session locks to `en`, `hi` or `mixed`
(`LanguageLocked`), and later partials and finals use that mode. If no confident decision
is made, it retries on later utterances, up to 3 attempts. Explicit modes never run
detection.

### Events (transport-neutral)

`SpeechStarted`, `PartialTranscript` (provisional), `FinalTranscript` (committed),
`UtteranceDiscarded`, `UtteranceFailed`, `LanguageLocked`, `PreviewPaused`,
`PreviewResumed`, `SessionEnded`. The WebSocket layer will map these to protocol messages
and save `FinalTranscript`s as `transcription_segments` rows.

### VAD evaluation

Measured on the development machine (2 vCPU Intel Xeon 2.1 GHz):

| Option | Frames | Cost | Dependencies | Decision |
|---|---|---|---|---|
| `webrtcvad-wheels` 2.0.14.post1 (maintained WebRTC VAD fork) | 10/20/30 ms | ~3 µs/frame (~0.01 % of a core) | small C extension | **Used** |
| `pysilero-vad` 3.4.1 | 32 ms only | ~1.1 ms/chunk (~3.5 % of a core per session) | 1 package, 10 MB | Possible future option behind `FrameClassifier` if noise tests need it |
| `silero-vad` 6.3 | 32 ms | — | PyTorch + CUDA (31 packages) | Rejected |
| `webrtcvad` 2.0.10 (original) | 10/20/30 ms | — | needs compiling, unmaintained | Rejected |

Robustness to background noise has not been measured; it needs the real recordings
described in [speech-evaluation.md](speech-evaluation.md). With synthetic signals, WebRTC
VAD (aggressiveness 2) flagged 98–100 % of voiced frames, 0 % of silent frames, and 5 % of
faint −60 dB noise frames. The energy gate suppresses that last case.

---

## WebSocket integration (`app/ws`)

The protocol is documented in [websocket-protocol.md](websocket-protocol.md).

### Handshake

1. **Origin** must be in `CORS_ALLOWED_ORIGINS`; otherwise the server refuses before
   accepting (close 1008). This blocks cross-site WebSocket hijacking.
2. The **first message** must be `start` with the JWT, within 5 s. The token is validated
   by the existing `AuthService.get_user_from_token`, using a short-lived DB session.
   Audio before `start` is rejected with 4401.
3. **One recording per user** and a **global limit** (`RecordingRegistry`) apply.
4. A `transcriptions` row is created with `source=live` and `status=recording`, plus the
   model name, mode and script.

### Per-recording pipeline

```
 handler task ── shield ──► pipeline task (owns the recording slot)
                             ├─ receive loop: frame checks → rate limiter → session.feed()
                             ├─ TranscriptionSession ──on_event (sync)──► asyncio.Queue
                             ├─ event consumer (1 task): finals → DB first → outbound queue
                             └─ sender (1 task): the only WebSocket writer
```

- **Thin route.** The WebSocket route holds no inference logic. Audio goes to
  `TranscriptionSession.feed()`, and its events go through one `asyncio.Queue`.
- **Finals.** The consumer saves each final, then queues the `final` message. Partials and
  notices are queued directly and never touch the database.
- **One writer.** Only the sender task writes to the socket. If a write fails, the socket is
  marked broken and the sender keeps draining its queue; persistence is unaffected.
- **Stop and disconnect.** `session.stop()`/`disconnect()` commit the remaining audio and wait
  for every final. `SessionEnded` follows the last final, so the consumer finishes the
  record only after everything is saved.
- **Handler cancellation.** The pipeline runs in its own task. If the request handler is
  cancelled (a server-side cancellation or an abrupt shutdown), the pipeline still commits,
  saves and marks the recording `interrupted` before releasing the slot.

### Persistence guarantees (`RecordingStore`)

- Every operation uses a **short-lived `AsyncSession`**; nothing is held open for a whole
  recording.
- **One transaction per final:** insert the `transcription_segments` row (raw model text,
  `seq`, ms timestamps, language, `avg_logprob`), then append the script-rendered text to
  `transcriptions.text`, with `is_edited=false`.
- **Exactly once:** `UNIQUE (transcription_id, seq)` rejects a duplicate. That rolls back
  the whole transaction, so the text is never appended twice, and a retry after an
  ambiguous failure is reported as already saved.
- **Outages:** transient database errors are retried with backoff. If a final still can't be
  saved, it and all later finals are held in memory, in order, and retried when the
  recording ends. The client receives the text with `saved: false` and a non-fatal
  `persistence_failed` error. `session_saved.unsaved_segments` lists anything finally lost.
  Nothing is dropped silently.
- **End status:** `completed` (stop, recording limit) or `interrupted` (disconnect, idle,
  rate limit, protocol error, overload, shutdown), together with `duration_ms`.
- **Never stuck in `recording`:** a safety net marks the recording `interrupted` if the
  normal finish failed, and at startup any `recording` rows left by a crash are marked
  `interrupted`.

### Application lifecycle

The FastAPI lifespan builds a `SpeechRuntime` containing:

- one `FasterWhisperEngine`, configured from `WHISPER_*` variables;
- one `InferenceScheduler` with `INFERENCE_CONCURRENCY` jobs;
- one `RecordingRegistry`;
- a `RealtimeConfig` built from `REALTIME_*` overrides.

`validate_cpu_budget()` runs at startup, so an impossible configuration fails to start. If
`WHISPER_CPU_THREADS=0`, the threads per job are set explicitly to (vCPUs − 1) ÷ jobs.
The model loads on first use, or at startup with `SPEECH_PRELOAD_MODEL=true`.

On shutdown, live recordings get `server_shutdown`, are committed and saved, and close with
1012. Then the scheduler drains and the database engine is disposed.

**Deployment constraint: run one Uvicorn worker per process.** Each worker would load its
own copy of the model and have its own in-memory registry, so the per-user and global
limits, and the CPU budget, would no longer hold. A warning is logged if
`WEB_CONCURRENCY > 1`. To scale out, run more containers behind a load balancer.

---

## Known limitations

- **No real-model measurements yet.** Model weights could not be downloaded in the build
  environment, so latency, accuracy and the default thresholds still need checking on
  target hardware.
- **Partial scheduling runs on audio time.** With a real-time stream this matches wall-clock
  time. If the backend falls behind, backpressure takes over.
- **Live sessions are in memory.** A server crash interrupts open recordings. Finals saved
  so far are kept, and the next startup marks those recordings `interrupted`.
- **Limits are per process.** The recording registry and the login and audio rate limits
  live in one process. Several instances would need a shared store, for example Redis.
- **JWT revocation.** Tokens are checked only at `start`; a long recording continues even if
  the token expires partway through.
