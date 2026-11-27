# WebSocket protocol: `/ws/transcribe`

Live transcription. One WebSocket connection is one recording. Text frames carry JSON
control messages, and binary frames carry audio. Implementation: `backend/app/ws/`.
Messages are validated with Pydantic (`app/ws/protocol.py`).
Browser client: `docs/websocket-frontend.md` (and `docs/audio-processing.md` for the
microphone pipeline).

## Connection sequence

```
client                                              server
  │  WebSocket handshake (Origin header)               │  Origin not in CORS_ALLOWED_ORIGINS → refused (HTTP 403 / 1008)
  │──────────────────────────────────────────────────►│  accept
  │  {"type":"start","token":…}   within 5 s           │  validate JWT → one recording per user → global limit
  │──────────────────────────────────────────────────►│  create transcriptions row (status=recording)
  │◄──────────────────────────────── session_started  │
  │  binary PCM16 frames (≤ 64 KB each)  …             │  VAD → endpointing → partial / final inference
  │──────────────────────────────────────────────────►│
  │◄────────────────────────────────────────── partial │  provisional, never stored
  │◄──────────────────────────────────────────── final │  sent only after its segment is saved
  │  {"type":"stop"}                                   │  commit remaining audio, wait for every final
  │──────────────────────────────────────────────────►│
  │◄────────────────────────────────── session_saved  │  status=completed, duration_ms stored
  │◄──────────────────────────────────────── close 1000│
```

The JWT goes **in the first message, never in the URL**, so it stays out of proxy and
access logs. The server never logs it.

## Client → server

### `start` (first message; required within `WS_AUTH_TIMEOUT_SECONDS`, default 5 s)

```json
{
  "type": "start",
  "token": "<access token from POST /api/v1/auth/login>",
  "speech_mode": "mixed",
  "script_preference": "roman",
  "format": "pcm_s16le",
  "sample_rate": 16000,
  "channels": 1,
  "title": "My recording"
}
```

| Field | Required | Values | Default |
|---|---|---|---|
| `type` | yes | `"start"` | |
| `token` | yes | access token (1–4096 characters) | |
| `speech_mode` | no | `auto`, `en`, `hi`, `mixed` | `auto` |
| `script_preference` | no | `as_recognised`, `roman`, `devanagari` | `as_recognised` |
| `format` | no | `pcm_s16le` only | `pcm_s16le` |
| `sample_rate` | no | `16000` only | `16000` |
| `channels` | no | `1` only | `1` |
| `title` | no | 1–200 characters | `Recording - <date> UTC` |

Unknown fields are rejected.

### Audio (binary frames)

Raw **PCM16 little-endian, 16 kHz, mono**, at most `WS_MAX_FRAME_BYTES` (64 KB, about 2 s)
per frame. The length must be a non-zero even number of bytes. Frames can be any size: the
server re-frames them into 30 ms frames internally. 100–250 ms per frame is a good choice.

### `stop`

```json
{"type": "stop"}
```

Ends the recording normally. Any other text message after `start` is a protocol error.

## Server → client

| `type` | When | Fields |
|---|---|---|
| `session_started` | after successful `start` | `transcription_id`, `speech_mode`, `script_preference`, `model_name`, `audio{format,sample_rate,channels}`, `max_frame_bytes`, `max_duration_ms` |
| `partial` | about every 1 s of speech | `utterance_id`, `version`, `text` (in the chosen script), `raw_text`, `start_ms`, `end_ms` |
| `final` | an utterance is committed | `seq`, `utterance_id`, `text` (chosen script), `raw_text` (model output), `start_ms`, `end_ms`, `language`, `avg_logprob`, `commit_reason`, `saved` |
| `preview_paused` / `preview_resumed` | inference backlog high, then low again | `backlog_ms` |
| `utterance_discarded` | an utterance had no usable speech (noise) | `utterance_id`, `start_ms`, `end_ms`, `reason` |
| `utterance_failed` | final inference failed after retry | `utterance_id`, `start_ms`, `end_ms`, `code` |
| `session_saved` | recording finished and stored | `transcription_id`, `status`, `end_reason`, `duration_ms`, `final_count`, `saved_segments`, `unsaved_segments`, `failed_utterances`, `record_saved`, `text` |
| `error` | any failure | `code`, `message` (generic), `fatal`; sometimes `seq` or `unsaved_segments` |

**Client rules:**

- **Partials:** a `partial` replaces the previous partial with the same `utterance_id`.
  Partials are provisional and never stored.
- **Finals:** a `final` with `seq` *n* replaces the partial line of its `utterance_id`.
  `seq` runs 0, 1, 2, … with no gaps, in order. Timestamps are milliseconds from the start
  of the recording.
- **`saved: false`** means the database was unavailable. The text is still delivered and the
  server retries before finishing; `session_saved.unsaved_segments` lists any that were
  finally lost.
- **Fatal errors:** an `error` with `fatal: true` is followed, for an active recording, by
  `session_saved` and then the close frame.

`session_saved.status`: `completed` when the client stopped, or the maximum recording length
was reached (`end_reason: recording_limit`). Otherwise `interrupted`.

`end_reason`: `stopped`, `recording_limit`, `disconnected`, `idle_timeout`, `rate_limited`,
`protocol_error`, `overloaded`, `shutdown`, `internal_error`, and `provider_error` (only with
`SPEECH_PROVIDER=gemini_live`: the cloud speech service failed mid-recording).

With `SPEECH_PROVIDER=gemini_live` the messages and fields are the same. Differences:
- `model_name` is `gemini:<model>`, and `max_duration_ms` is at most 600 000 (Gemini Live
  sessions last at most 10 minutes).
- `final.avg_logprob` is `null`, and utterance times may be approximate (see
  `docs/speech-providers.md`).

## Errors and close codes

| Close code | `error.code` | Cause |
|---|---|---|
| 1000 | — | Normal end (after `session_saved`) |
| 1008 | — | Origin not allowed (handshake refused before accept) |
| 1009 | `frame_too_large` | Binary frame larger than `WS_MAX_FRAME_BYTES` |
| 1011 | `internal_error` | The recording row could not be created |
| 1012 | `server_shutdown` | Server is restarting (recording saved as interrupted) |
| 1013 | `server_busy` | Global limit `WS_MAX_ACTIVE_SESSIONS` reached |
| 1013 | `server_overloaded` | Transcription backlog too large (all finals still saved) |
| 1013 | `transcription_unavailable` | `gemini_live` only: the speech service is unreachable or failed (generic; no provider details) |
| 1011 | `transcription_unavailable` | `gemini_live` only: the server's Gemini API key was rejected (a server configuration problem) |
| 1013 | `transcription_quota_exceeded` | `gemini_live` only: the speech service's rate limit or quota is exhausted |
| 4400 | `invalid_message` / `invalid_audio` | Malformed JSON, wrong audio format, unexpected message, odd or empty frame |
| 4401 | `auth_required` / `invalid_token` / `token_expired` | Audio or `stop` before `start`, bad or expired JWT |
| 4408 | `auth_timeout` / `idle_timeout` | No `start` in time, or nothing received for `WS_IDLE_TIMEOUT_SECONDS` |
| 4409 | `recording_in_progress` | This user already has an active recording |
| 4429 | `rate_limited` | Audio faster than real time beyond the allowed burst |

Error messages are fixed and generic. They never contain exception text, SQL, tokens or
stack traces.

## Limits (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `CORS_ALLOWED_ORIGINS` | — | Comma-separated allowlist, also used for the WebSocket `Origin` |
| `WS_ALLOW_MISSING_ORIGIN` | `false` | Accept clients that send no `Origin` (non-browser) |
| `WS_AUTH_TIMEOUT_SECONDS` | `5` | Deadline for the `start` message |
| `WS_IDLE_TIMEOUT_SECONDS` | `30` | Close a recording that sends nothing |
| `WS_MAX_FRAME_BYTES` | `65536` | Largest binary frame |
| `WS_MAX_TEXT_BYTES` | `4096` | Largest text frame |
| `WS_MAX_RECORDING_SECONDS` | `1800` | Recording length limit (audio time) |
| `WS_MAX_ACTIVE_SESSIONS` | `3` | Concurrent recordings per server process |
| `WS_AUDIO_RATE_FACTOR` | `1.5` | Maximum audio speed relative to real time |
| `WS_AUDIO_BURST_SECONDS` | `5` | Audio a client may send ahead of real time |
| `WS_SHUTDOWN_TIMEOUT_SECONDS` | `30` | Time given to live recordings to save on shutdown |

Text limits are measured in **UTF-8 bytes**, not characters: one Devanagari character is
3 bytes.

**Protocol-level limit.** Run Uvicorn with `--ws websockets --ws-max-size 65536`, and keep
`--ws-max-size` at least `WS_MAX_FRAME_BYTES`:

- With equal limits, an oversized frame is refused by Uvicorn before the application
  buffers it. The client sees close **1009** with no `error` message, and the recording is
  saved as `interrupted`. Frames already received by Uvicorn but not yet read by the
  application may be lost in that abort.
- With a larger `--ws-max-size`, the application rejects such frames itself:
  `error: frame_too_large`, then `session_saved`, then close 1009.

Clients should never send frames larger than `session_started.max_frame_bytes`. The sample
client caps its frames to that value, and reports a 1009 close clearly if one happens.

## Example (Python, `websockets`)

`backend/scripts/ws_client.py` is a complete client:

```bash
cd backend
python scripts/ws_client.py recording.wav --api http://localhost:8000 \
    --email you@example.com --password 'your-password' --mode mixed --script roman
```

Sample output (from the automated end-to-end run with the test engine):

```
streaming /tmp/demo.wav (6.90 s)
session_started  id=04155c93-… model=timeline-fake mode=mixed script=roman
  … aaj ki
  … aaj ki meeting cancel ho gai
final #0 [00:00.000 -> 00:01.800] aaj ki meeting cancel ho gai
  … kal phir call karate hain okay thik hai
final #1 [00:03.000 -> 00:05.400] kal phir call karate hain okay thik hai
session_saved    status=completed reason=stopped duration=6.90s segments=2 unsaved=[] failed=0
text: aaj ki meeting cancel ho gai kal phir call karate hain okay thik hai
```

Minimal client logic:

```python
async with websockets.connect("ws://localhost:8000/ws/transcribe",
                              origin="http://localhost:5173") as ws:
    await ws.send(json.dumps({"type": "start", "token": token, "speech_mode": "mixed"}))
    assert json.loads(await ws.recv())["type"] == "session_started"
    for chunk in pcm_chunks:          # 16 kHz mono PCM16, e.g. 100 ms = 3200 bytes
        await ws.send(chunk)
    await ws.send(json.dumps({"type": "stop"}))
    async for message in ws:          # partial / final / … / session_saved
        print(json.loads(message))
```
