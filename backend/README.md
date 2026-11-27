# Backend - Real-Time Speech to Text

FastAPI + PostgreSQL backend.
- **Step 2 (foundation):** configuration, logging, database models and migrations, JWT
  authentication and user-scoped transcription CRUD.
- **Step 3 (speech engine):** `app/speech/`, a faster-whisper engine (multilingual models,
  CPU int8 / GPU float16), audio validation, language modes, structured results, and
  output-script rendering. There is a manual evaluation CLI; see
  [docs/speech-evaluation.md](../docs/speech-evaluation.md).
- **Step 4 (real-time core):** `app/realtime/`, covering VAD (WebRTC), endpointing, utterance
  buffering with lossless forced cuts, a prioritised inference scheduler, and
  `TranscriptionSession` (partial and final transcripts). See
  [docs/architecture.md](../docs/architecture.md).

- **Step 5 (WebSocket integration):** `app/ws/` and `/ws/transcribe`, covering JWT and Origin
  checks, an asyncio event pipeline, exactly-once segment persistence, limits, and a
  lifespan-managed shared engine and scheduler. See
  [docs/websocket-protocol.md](../docs/websocket-protocol.md).

## Stack

Python 3.12 · FastAPI · SQLAlchemy 2 (async, asyncpg) · Alembic · Pydantic v2 ·
PyJWT · Argon2id (argon2-cffi) · Pytest

## Layout

```
backend/
├── app/
│   ├── main.py              # app factory (create_app), middleware, routers, lifespan
│   ├── core/                # config, logging, security (JWT/Argon2), rate limit, errors, middleware
│   ├── db/                  # declarative base (naming convention), async engine/session
│   ├── models/              # SQLAlchemy models + enums/limits
│   ├── schemas/             # Pydantic request/response models
│   ├── repositories/        # data access (owner-scoped queries)
│   ├── services/            # business logic, transactions
│   ├── ws/                  # /ws/transcribe
│   │   ├── protocol.py      #   Pydantic messages, error and close codes
│   │   ├── limits.py        #   audio rate limiter, recording registry
│   │   ├── persistence.py   #   RecordingStore (exactly-once finals, retries)
│   │   ├── runtime.py       #   shared engine/scheduler, CPU budget, REALTIME_* config
│   │   ├── connection.py    #   handshake + per-recording pipeline
│   │   └── routes.py
│   ├── realtime/            # real-time core (no web/DB dependencies)
│   │   ├── config.py        #   RealtimeConfig (VAD, endpointing, partials, limits)
│   │   ├── clock.py         #   sample clock, 30 ms frame assembly
│   │   ├── vad.py           #   WebRtcVad (+ energy gate), EnergyVad
│   │   ├── endpointer.py    #   SILENCE → IN_SPEECH ↔ IN_PAUSE → FINALIZE
│   │   ├── buffer.py        #   frames, spans, pre-roll, utterance buffer
│   │   ├── boundary.py      #   safe cuts, word-boundary carry, de-dup, hallucination filter
│   │   ├── scheduler.py     #   InferenceScheduler (thread pool, FINAL > PARTIAL)
│   │   ├── session.py       #   TranscriptionSession orchestrator (Whisper)
│   │   ├── gemini_live.py   #   GeminiLiveSession (SPEECH_PROVIDER=gemini_live)
│   │   ├── contract.py      #   RecordingSession interface both sessions implement
│   │   └── events.py        #   transport-neutral session events
│   ├── speech/              # speech engine (no web/DB dependencies)
│   │   ├── audio.py         #   PCM16 / float validation & conversion, file loading
│   │   ├── config.py        #   SpeechEngineConfig, WHISPER_* settings
│   │   ├── language.py      #   modes -> decoding plans, auto-language decision
│   │   ├── engine.py        #   SpeechEngine interface (consumes the segment generator)
│   │   ├── faster_whisper_engine.py
│   │   ├── results.py       #   immutable results, timestamp normalisation
│   │   ├── script.py        #   output-script rendering (deterministic transliteration)
│   │   ├── cli.py           #   manual evaluation CLI
│   │   └── errors.py
│   └── api/                 # deps (auth, DB, services) + v1 routers
├── scripts/transcribe_file.py   # CLI: transcribe one file
├── scripts/ws_client.py         # sample WebSocket client (streams a file)
├── alembic/                 # migration environment + versions/
└── tests/
    ├── unit/                # no database; unit/speech and unit/realtime use fake engines
    ├── integration/         # real PostgreSQL
    └── real_model/          # opt-in real Whisper tests (STT_REAL_MODEL_TESTS=1)
```

## Setup

Prerequisites: Python 3.12 and PostgreSQL 13+ (`gen_random_uuid()` is built in from 13).

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt    # runtime only: requirements.txt
cp .env.example .env                   # then fill in the values (see below)
```

### Speech provider

`SPEECH_PROVIDER` selects the recogniser. **`whisper`** (default) is local faster-whisper,
with nothing else to configure. **`gemini_live`** is Google's Gemini Live API for cloud
deployment: it needs `GEMINI_API_KEY` (a server-side secret) and can be installed from
`requirements-cloud.txt`, which leaves out the Whisper stack. The browser protocol, storage
and history are the same for both. See [`docs/speech-providers.md`](../docs/speech-providers.md).

### Database

```bash
# As a PostgreSQL superuser (choose your own password):
psql -U postgres -c "CREATE ROLE stt_user LOGIN PASSWORD '<password>';"
psql -U postgres -c "CREATE DATABASE stt_dev OWNER stt_user;"
psql -U postgres -c "CREATE DATABASE stt_test OWNER stt_user;"
```

Then in `.env`:

```
DATABASE_URL=postgresql+asyncpg://stt_user:<password>@localhost:5432/stt_dev
TEST_DATABASE_URL=postgresql+asyncpg://stt_user:<password>@localhost:5432/stt_test
JWT_SECRET_KEY=<output of: python -c "import secrets; print(secrets.token_urlsafe(48))">
```

The app refuses to start if `DATABASE_URL` or `JWT_SECRET_KEY` is missing, if the secret is
under 32 characters or looks like a placeholder. Rejected values are never echoed in errors.

### Migrations

```bash
alembic upgrade head          # apply all migrations
alembic current               # show the applied revision
alembic history               # list revisions
alembic downgrade -1          # roll back the latest migration
alembic downgrade base        # roll back everything (drops all app tables)
alembic upgrade head --sql    # print the SQL without executing it
alembic check                 # fail if models and migrations have drifted
alembic revision --autogenerate -m "describe change"   # new migration (always review it)
```

The URL comes from `DATABASE_URL`; `alembic.ini` contains no credentials.

### Run the server

```bash
# Development
uvicorn app.main:create_app --factory --reload --port 8000 \
    --ws websockets --ws-max-size 65536

# Production: exactly ONE worker per process (the model, recording registry and limits
# are per process); behind a proxy add --proxy-headers --forwarded-allow-ips=<proxy ip>
SPEECH_PRELOAD_MODEL=true uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 \
    --workers 1 --ws websockets --ws-max-size 65536 --ws-ping-interval 20 --ws-ping-timeout 20
```

**WebSocket message size: two limits.**

- `--ws-max-size` (Uvicorn): the largest WebSocket message, binary or text, that the
  protocol layer accepts. Anything larger is refused **before** it reaches the application,
  so an oversized frame is never fully buffered in memory. Uvicorn's own default is 16 MiB.
- `WS_MAX_FRAME_BYTES` (application, default 65536): the largest audio frame a recording
  accepts. It is announced to clients in `session_started.max_frame_bytes`.

`--ws-max-size` **must be at least `WS_MAX_FRAME_BYTES`**. If it is smaller, valid audio
frames are rejected at the protocol level. With both set to 65536:

- A larger frame is cut off by Uvicorn with close code **1009**, before any application
  code runs. The recording is then saved as `interrupted`, like a disconnect, with the
  finals produced so far. The client gets the close code but no `error`/`session_saved`
  message. Audio frames Uvicorn had already received but the app had not yet read may be
  discarded in this abort (verified in tests). Clients must therefore never exceed
  `session_started.max_frame_bytes`.
- If `--ws-max-size` is larger, frames between the two limits reach the app, which
  replies with `error: frame_too_large`, then `session_saved`, then close 1009.

Text messages (`WS_MAX_TEXT_BYTES`, default 4096, measured in UTF-8 bytes) are far below
both limits.

Note: with Uvicorn 0.54, `--ws websockets` selects Uvicorn's legacy `websockets`
implementation, which it reports as deprecated; a future Uvicorn release will point
`--ws websockets` at `websockets-sansio`. The tests run the frame-limit checks against both
`websockets` and `websockets-sansio`, which behave the same here. To switch early, use
`--ws websockets-sansio`.

### Live transcription from the command line

```bash
python scripts/ws_client.py recording.wav --api http://localhost:8000 \
    --email you@example.com --password '...' --mode mixed --script roman
```

The first recording loads the Whisper model (or set `SPEECH_PRELOAD_MODEL=true`).

- OpenAPI docs: http://localhost:8000/docs (disable with `DOCS_ENABLED=false`)
- Behind a reverse proxy, add `--proxy-headers --forwarded-allow-ips=<proxy ip>` so login
  rate limiting sees real client IPs. Run a single worker (later steps keep the speech model
  in process memory).

### Speech engine (manual transcription)

```bash
python scripts/transcribe_file.py recording.wav --model small --mode mixed --script roman
```

The first run downloads the model into the Hugging Face cache; nothing is downloaded into
the repo. See [docs/speech-evaluation.md](../docs/speech-evaluation.md).

### Tests

```bash
pytest tests/unit               # no PostgreSQL needed (includes fake-model speech tests)
pytest                          # full suite (needs TEST_DATABASE_URL)
STT_REAL_MODEL_TESTS=1 pytest tests/real_model   # optional: real cached model, never downloads
pytest --cov --cov-report=term  # with coverage
ruff check . && ruff format --check . && mypy app
```

The suite drops and recreates the schema of the test database via the real migrations, so
the database name **must end in `_test`** (the suite refuses to run otherwise).

## API (prefix `/api/v1`)

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/health` | - | Liveness |
| GET | `/health/ready` | - | Readiness (DB check), 503 if unavailable |
| POST | `/auth/register` | - | Create account → 201 |
| POST | `/auth/login` | - | Email + password → bearer token |
| GET | `/auth/me` | Bearer | Current user |
| POST | `/transcriptions` | Bearer | Create manually → 201 |
| GET | `/transcriptions` | Bearer | List: `page`, `page_size` (≤100), `q`, `speech_mode`, `status`, `source` |
| GET | `/transcriptions/{id}` | Bearer | Detail; `include_segments=true` for raw segments |
| PATCH | `/transcriptions/{id}` | Bearer | Edit `title` and/or `text` |
| DELETE | `/transcriptions/{id}` | Bearer | Delete → 204 |
| WS | `/ws/transcribe` | JWT in first message | Live recording ([protocol](../docs/websocket-protocol.md)) |

Errors always look like `{"detail": "...", "code": "..."}` (validation errors add `errors`).
Another user's transcription returns **404**, identical to a non-existent one.

## Security notes

- Argon2id password hashing (RFC 9106 profile), run off the event loop; hashes are upgraded
  on login when parameters change. Unknown emails still cost one hash (no timing oracle).
- JWT: algorithm pinned, `exp/iat/nbf/sub/iss/aud/jti` required, `type=access` enforced.
- Failed logins limited per client IP + email (in-memory, single process).
- Request bodies reject unknown fields; text ≤ 100,000 chars; passwords ≤ 128 chars.
- Logs carry request IDs and never contain tokens, passwords or transcript text.
