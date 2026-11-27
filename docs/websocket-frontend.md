# WebSocket client (browser)

How the React app uses `/ws/transcribe`. The protocol itself is defined in
`docs/websocket-protocol.md`. The client uses it as is: no new message types or fields.

| File | Role |
| --- | --- |
| `frontend/src/realtime/protocol.ts` | Message types (same field names as the backend), URL derivation, close-code texts |
| `frontend/src/realtime/transcriptionSocket.ts` | `TranscriptionSocket`: handshake, pacing, backpressure, stop, endings |
| `frontend/src/realtime/transcript.ts` | Pure reducer: partials and finals → live transcript |
| `frontend/src/hooks/useTranscriptionSocket.ts` | React binding: URL, token, query invalidation, unmount |
| `frontend/src/hooks/useLiveRecording.ts` | Orchestration of microphone + socket, recording states |
| `frontend/src/pages/RecordPage.tsx` | UI |

## URL

Derived from `VITE_API_BASE_URL` (`webSocketUrl()`): the scheme follows the API (`http` →
`ws`, `https` → `wss`), and the `/api/vN` suffix is replaced by `/ws/transcribe`, because the
WebSocket route is mounted at the server root.

| `VITE_API_BASE_URL` | WebSocket URL |
| --- | --- |
| `http://localhost:8000/api/v1` | `ws://localhost:8000/ws/transcribe` |
| `https://api.example.com/api/v1` | `wss://api.example.com/ws/transcribe` |
| `https://example.com/stt/api/v1` | `wss://example.com/stt/ws/transcribe` |

The frontend origin must be in the backend's `CORS_ALLOWED_ORIGINS`, which is also the
WebSocket `Origin` allowlist (otherwise the handshake is refused: close 1008).

## Recording flow

```
Start ─► getUserMedia + AudioContext + worklet          (status: requesting_permission)
      ─► new WebSocket(url)                              (connecting)
      ─► onopen: send {"type":"start", token, …}  ◄── first and only text before audio
      ─► wait for session_started (10 s timeout)
      ─► connect the microphone to the worklet           (recording)
      ─► binary PCM16 frames, 100 ms each
Stop  ─► disconnect the microphone, flush the worklet's tail frames   (finishing)
      ─► flush confirmed? no ─► abort the socket (code 4000), NO stop ─► interrupted
      ─► send every queued frame, then {"type":"stop"}
      ─► keep receiving: finals for the last audio, then session_saved
      ─► microphone + AudioContext released, history refetched        (completed / interrupted)
```

- The microphone permission is asked **before** connecting, so a refusal never creates an
  empty recording row on the server.
- The **JWT** is read from the existing `tokenStorage` (no second copy). It is only placed in
  the first message, never in the URL, so it stays out of proxy and access logs. The client
  never logs it.
- `start` carries `speech_mode`, `script_preference`, the optional `title` (omitted when
  empty) and the fixed `format: "pcm_s16le"`, `sample_rate: 16000`, `channels: 1`.
- **Do not close after stop.** The server still has to transcribe the last utterance. The
  socket stays open until `session_saved`. The server then closes with 1000 (the client
  closes it itself after a 3 s grace period if not). If `session_saved` does not arrive within
  120 s, the recording is reported as interrupted.

## Sending audio safely

Each frame from the worklet goes through `sendAudio()`:

1. **Frame size:** frames are split if larger than `session_started.max_frame_bytes`
   (rounded down to whole samples). Worklet frames are 3200 bytes, far below the default
   64 KB, so normally nothing is split.
2. **Bounded queue:** frames are queued in order, then sent while
   - `WebSocket.bufferedAmount` is below **32 000 bytes** (about 1 s of audio), so a stalled
     network shows up in our queue instead of in a large hidden browser buffer, and
   - a token bucket allows it: **4 s burst, 1.4× real time**. That is just under the server's
     limiter (`WS_AUDIO_BURST_SECONDS=5`, `WS_AUDIO_RATE_FACTOR=1.5`), so a backlog drained
     after a network hiccup is never rejected with 4429. A backend test
     (`tests/integration/ws/test_ws_browser_client.py`) pins these numbers to the server
     defaults.
3. **Never dropped:** if the queue exceeds **15 s** of audio, the connection cannot keep up.
   The client stops capturing and closes the socket (code 4000), and the page reports the
   recording as interrupted. The server saves everything it received, marked
   `interrupted`. Audio is never thrown away silently.

A microphone produces audio in real time, so in normal operation the queue is empty and
each frame is sent as soon as it is ready.

## Server messages

| Message | Client behaviour |
| --- | --- |
| `session_started` | Store `transcription_id`, `max_frame_bytes`, `model_name`; start the microphone |
| `partial` | Replace the provisional line of the same `utterance_id` (newer `version` only); shown in grey italics |
| `final` | Insert by `seq`, replacing that utterance's partial; later partials for it are ignored. `saved:false` shows "not saved yet" |
| `preview_paused` / `preview_resumed` | Show or hide "Live preview paused while the server catches up" |
| `utterance_discarded` | Remove that utterance's partial (it was noise) |
| `utterance_failed` | Remove its partial; count shown under the transcript |
| `error` (non-fatal) | `persistence_failed`: reflected by the `saved` flags and `session_saved.unsaved_segments` |
| `error` (fatal) | Remembered, then explained when `session_saved` or the close arrives |
| `session_saved` | End: result card from `record_saved`, `status`, `saved_segments`, `unsaved_segments`, `failed_utterances` (below) |

Text frames that are not valid JSON, or have an unknown `type`, are ignored with a visible
warning. Binary frames from the server are ignored (the protocol has none).

## How a recording ends

The result card and status line use the server's own fields, most severe first
(`recordingOutcome()` in `useLiveRecording.ts`):

| `session_saved` | Card | Status line |
| --- | --- | --- |
| `record_saved: false` | Finished, but the recording could not be finalised (red) | Recording finished, but saving failed |
| `status: interrupted` | Interrupted (amber) | Recording interrupted |
| `status: completed`, `unsaved_segments` not empty | Completed, but some segments were not saved (amber) | Recording completed, but not fully saved |
| `status: completed`, `record_saved: true`, no unsaved segments | Saved (green) | Recording saved |

The card always shows the duration, the number of saved segments, the unsaved ones by
number, failed utterances, and the link to the record (which exists from `session_started`
on). "Saved" is never shown unless every condition of the last row holds.

| What happened | Page status | Notes |
| --- | --- | --- |
| `session_saved`, `status: completed` | Completed | `end_reason: recording_limit` adds a notice |
| `session_saved`, `status: interrupted` | Interrupted | Message from the preceding fatal `error` (overload, idle timeout, rate limit, shutdown, …) |
| Worklet flush not confirmed on Stop | Interrupted | No `stop` sent; socket aborted (4000) → server stores interrupted. The message says the final seconds were not sent or transcribed |
| Socket closed before `session_saved` | Interrupted | Never shown as completed. Message from the close code. The link still opens the saved text |
| Close **1009** | Interrupted | "An audio frame was too large" (only if a proxy or server limit is below 3200 bytes) |
| Client backlog over 15 s | Interrupted | "The connection is too slow…" |
| Closed before `session_started` | Error | 1008 origin, 4401 token (with a "Sign in again" button), 4409 another tab, 1013 busy, unreachable server |

When the recording ends without `session_saved`, provisional (partial) text is removed:
partials are never stored. Finals stay, because the server saves each final before sending
it.

Observed with Uvicorn: on SIGTERM, Uvicorn closes live WebSockets with **1012** before the
application's shutdown hook can send `error`/`session_saved`. The client then reports
"server is restarting" and **interrupted** from the close code, and the server still stores
the recording as interrupted (backend shutdown path).

## Browser lifecycle

- **Leaving the page in the app** (links, back/forward): React Router's `useBlocker` asks
  "Leave while recording?". *Stop and leave* flushes and sends `stop`, then navigates. The
  socket finishes in the background, so the last audio is still transcribed and saved, and
  the history refreshes when `session_saved` arrives. If the flush fails, the navigation is
  cancelled so the user sees the error. The recording is then no longer active, so the next
  navigation is not blocked.
- **Closing or reloading the tab:** a `beforeunload` prompt is shown while a recording is
  active. If the user leaves anyway, the connection drops and the server saves the
  recording as interrupted.
- **Network loss:** close 1006 (or a growing backlog) → interrupted, with what was saved.
- **Unmount** (for example a forced sign-out): an active socket is aborted (code 4000), the
  microphone and AudioContext are released. One that is already stopping is left to finish.
- **Device removed:** recording stops cleanly with a notice (see `docs/audio-processing.md`).
- **One recording at a time:** a synchronous guard ignores a second Start while one is in
  progress. The server enforces one recording per user across tabs (4409).
- Listeners are attached to a single socket instance and detached on abort. A new
  recording creates a new instance. Late callbacks from an old instance are ignored.

## History integration

Every ending (saved or not) invalidates the TanStack Query keys under `["transcriptions"]`.
The dashboard list, its counters and any cached detail page refetch, so the new recording
appears with its status (completed or interrupted) and can be opened, edited and deleted
with the existing CRUD pages.

## Tests

| Level | Where | What |
| --- | --- | --- |
| Unit | `src/realtime/transcriptionSocket.test.ts` | URL derivation, JWT-first handshake, auth failure, timeouts, audio gating, frame splitting, pacing, backpressure overflow, every message type, invalid JSON, 1006/1009/fatal endings, stop workflow, transcript reducer |
| Page (jsdom) | `src/test/recording.test.tsx` | Full flow with a fake WebSocket server, the real worklet DSP and the MSW REST fake: permission, partial/final display, stop, `session_saved`, dashboard refresh, denied/missing microphone, 4401, disconnect, server stop, 1009, double click, unplugged device, cancel, navigation blocker, unmount, `beforeunload` |
| Backend | `backend/tests/integration/ws/test_ws_browser_client.py` | Browser framing and pacing against the real server's default rate limits (fake engine) |
| Browser | `frontend/e2e/recording.e2e.mjs` (`npm run e2e`) | Real Chrome + fake capture device (generated 48 kHz stereo WAV) → real AudioWorklet → real Uvicorn/FastAPI/PostgreSQL with the **fake speech engine**: 23 protocol, persistence and UI checks, including a phone-sized run where the server shuts down mid-recording |

`npm run e2e` needs Google Chrome, PostgreSQL with the backend's `TEST_DATABASE_URL` (that
`*_test` database is wiped) and the backend virtualenv. Settings: `CHROME_PATH`,
`BACKEND_PYTHON`, `E2E_API_PORT` (8765), `E2E_WEB_PORT` (5173), `E2E_SCREENSHOTS=<dir>`.
In that run Chrome's fake device delivered audio at 44.1 kHz, so the in-browser
44.1 → 16 kHz path was the one exercised. The unit tests cover 48 kHz.

No test downloads Whisper weights. Real-model checks are the backend's opt-in
`tests/real_model` suite (`STT_REAL_MODEL_TESTS=1` with weights already present).
