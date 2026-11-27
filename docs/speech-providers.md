# Speech providers: local Whisper and Gemini Live

The backend can transcribe with one of two providers, chosen by one environment variable:

| `SPEECH_PROVIDER` | Where it runs | Used for |
| --- | --- | --- |
| `whisper` (default) | Locally: faster-whisper `small`, CPU int8 | Local development and self-hosting |
| `gemini_live` | Google's Gemini Live API (`gemini-3.5-transcribe-live`) | Cloud deployment (Render) |

The browser, the WebSocket protocol, authentication, persistence and the history pages
are identical for both. The frontend never talks to Gemini and never sees the API key.

## Switching

**Local Whisper** (no change from before; nothing new is required):

```bash
cd backend
pip install -r requirements-dev.txt          # or requirements.txt for runtime only
# .env: SPEECH_PROVIDER=whisper (or leave it unset)
uvicorn app.main:create_app --factory --ws websockets --ws-max-size 65536
```

**Gemini Live in the cloud** (for example Render):

```bash
pip install -r requirements-cloud.txt        # no faster-whisper / ctranslate2 / av
```

Environment variables (set the key as a secret in the host's dashboard, not in any file):

| Variable | Value |
| --- | --- |
| `SPEECH_PROVIDER` | `gemini_live` |
| `GEMINI_API_KEY` | your key (required; startup fails without it) |
| `GEMINI_LIVE_MODEL` | `gemini-3.5-transcribe-live` (default) |
| `GEMINI_MAX_SESSION_SECONDS` | `600` (default; maximum 600) |
| `GEMINI_CONNECT_TIMEOUT_SECONDS` | `10` (default) |
| `GEMINI_FINALIZE_TIMEOUT_SECONDS` | `10` (default) |

Start command: `uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT --ws websockets --ws-max-size 65536`
(one worker, as for Whisper: the per-user recording limit is kept in memory).

`requirements-cloud.txt` supports `gemini_live` only. Starting it with
`SPEECH_PROVIDER=whisper` fails at startup with a clear message.

## What changes with `gemini_live`

- No Whisper engine is built. No model is downloaded or preloaded (`SPEECH_PRELOAD_MODEL`
  is ignored), CUDA is never probed, and the CPU-budget check and inference scheduler are
  not used.
- Each recording opens one Live API session when it starts. If Gemini cannot be reached,
  the client gets an error immediately and no empty recording is left in the history.
- The recording limit is `GEMINI_MAX_SESSION_SECONDS` (≤ 600 s: Live sessions last at most
  10 minutes), sent to the browser as `session_started.max_duration_ms`. It is enforced on
  audio received and on time since connecting, and also on a Gemini `GoAway` notice. The
  recording then ends as `completed` with `end_reason: recording_limit`, like Whisper's
  limit. Whisper keeps `WS_MAX_RECORDING_SECONDS`.
- `model_name` on the recording is `gemini:gemini-3.5-transcribe-live`.

## How Gemini Live is called

Code: `backend/app/realtime/gemini_live.py`, with the official Google Gen AI SDK
(`google-genai`, async Live API):

```python
client = genai.Client(api_key=..., http_options=types.HttpOptions(api_version="v1beta"))
config = types.LiveConnectConfig(
    response_modalities=["TEXT"],
    input_audio_transcription=types.AudioTranscriptionConfig(
        language_codes=[],                                  # automatic language detection
        mode=types.AudioTranscriptionConfigMode.VERBATIM,   # literal, not SMART
    ),
)
async with client.aio.live.connect(model=model, config=config) as session:
    await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000"))
    ...
    await session.send_realtime_input(audio_stream_end=True)
    async for message in session.receive(): ...   # re-entered after every completed turn
```

**Handshake:** no audio is sent until the server confirms the session with
`setup_complete`. google-genai's `live.connect()` already reads that first message and
keeps it as `session.setup_complete`. A `setup_complete` arriving through `receive()` is
accepted too. One `GEMINI_CONNECT_TIMEOUT_SECONDS` deadline covers connecting plus setup. A
timeout, error or close before confirmation closes the receiver, the sender and the
connection, and the client gets the generic `transcription_unavailable` (or
`transcription_quota_exceeded`) error.

Audio is the browser's 16 kHz mono PCM16, unchanged. Incoming frames of any size are
buffered and forwarded in 100 ms chunks (3200 bytes). The remainder is sent on stop, then
`audio_stream_end`.

The SDK's `receive()` iterator ends after each completed interaction, so the session calls
it again in a loop. Otherwise every utterance after the first would be lost.

## Event mapping and persistence

`GeminiLiveSession` implements the same contract as the Whisper `TranscriptionSession`
(`backend/app/realtime/contract.py`) and emits the same events. `RecordingConnection` and
`RecordingStore` are reused unchanged.

| Gemini | Event | Then (existing code) |
| --- | --- | --- |
| `server_content.interim_input_transcription.text` | `PartialTranscript` (same `utterance_id`, `version` + 1; replaces the previous partial) | `partial` message; never persisted |
| `server_content.input_transcription.text` | `FinalTranscript` (next gap-free `seq`) | `RecordingStore.append_final()` (exactly once per `seq`), then the `final` message |
| `input_transcription` with `finished: false` | collected, committed as one final when the finished piece arrives (or on stop) | as above |
| empty `input_transcription` after a partial | `UtteranceDiscarded` (`no_speech`) | `utterance_discarded` message |
| partial whose final never arrives (finalize timeout or provider failure) | `UtteranceFailed` (`final_timeout` / `provider_error`) | `utterance_failed`, counted in `session_saved.failed_utterances` |
| end of the session | `SessionEnded`, only after every pending final | record finished (`completed` / `interrupted`), `session_saved` |

Transcripts are never read from `model_turn.parts`, and `turn_complete` is not used to
detect finals. A completed interaction after `audio_stream_end` only shortens the wait
for pending finals.

Fields Gemini does not provide are left empty, never invented:
- `avg_logprob` is `null`, and `words` (word timings) is empty.
- Utterance times use Gemini's `start_offset` / `end_offset` when present. Otherwise they
  are approximate: from the end of the previous final to the audio received so far.
- `language` is the BCP-47 `language_code` reduced to its primary subtag (`hi-IN` → `hi`).
- `decode_strategy` is `gemini_live`.

The output script (`as_recognised` / `roman` / `devanagari`) is applied by the existing
code, exactly as for Whisper: `raw_text` keeps Gemini's text and `text` is rendered.

## Ending and failures

| Situation | Result |
| --- | --- |
| Stop | Remainder + `audio_stream_end`; wait until nothing is pending and Gemini is done or quiet for 1.5 s (≤ `GEMINI_FINALIZE_TIMEOUT_SECONDS`); then `completed` |
| Browser disconnect | Same drain of pending finals, then `interrupted` |
| Server shutdown (SIGTERM on Render) | Same drain within `WS_SHUTDOWN_TIMEOUT_SECONDS`, then `interrupted`, `server_shutdown` error |
| Gemini unreachable or key rejected at start | `error` `transcription_unavailable`, close 1013 (1011 for a rejected key); empty recording deleted |
| Quota or rate limit at start | `error` `transcription_quota_exceeded`, close 1013; empty recording deleted |
| Gemini fails mid-recording | same error codes; `session_saved` `interrupted`, `end_reason: provider_error`; finals so far are kept |
| Gemini cannot keep up with the audio | `server_overloaded` (existing), `interrupted` |
| 600 s limit or GoAway | `completed`, `end_reason: recording_limit` |

**Secrets:**
- `GEMINI_API_KEY` is a `SecretStr`: masked in `repr`, logs and settings errors.
- The SDK sends it in a request header, not the URL.
- Provider exceptions are logged by **type only**, and clients get only the two generic
  error codes above.
- The SDK and `websockets` loggers are capped at WARNING so a DEBUG log level cannot print
  request headers.

## Tests

All Gemini tests use a mocked SDK (`backend/tests/unit/realtime/gemini_fakes.py`, whose
server messages are built from the real `google.genai.types`). No test calls Gemini.

- `tests/unit/test_speech_provider.py`: provider selection, default Whisper, key validation
  and masking, no Whisper engine for Gemini, the real SDK client's Live configuration
  (`v1beta`, `TEXT`, `language_codes=[]`, `VERBATIM`).
- `tests/unit/realtime/test_gemini_live.py`: 100 ms buffering and the remainder flush,
  partial and final mapping, offsets, pieces, re-entering `receive()`, pending finals on stop
  and disconnect, failure classification (key, quota, timeout), the 600 s and wall-clock
  limits, GoAway, backpressure, and no provider text in logs.
- `tests/integration/ws/test_ws_gemini.py`: the real app with PostgreSQL. Covers `/ws/transcribe`
  messages, persistence through `RecordingStore`, the history API and ownership,
  disconnect, shutdown, connect failures (no row left), quota mid-recording, the session
  limit, and Whisper still being the default.

Not covered: no test has used the real Gemini service (see the limitations in the
project report).
