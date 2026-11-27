# Frontend: React + TypeScript

The web client for the real-time speech-to-text app. It covers sign-up and sign-in, **live
recording** (microphone → AudioWorklet → WebSocket, with partial and final transcripts as you
speak), the transcription dashboard (search, filters, pagination), manual
create/edit/delete, and the transcription detail page with raw recognised segments.

More detail:

- [`docs/audio-processing.md`](../docs/audio-processing.md): microphone capture, the
  AudioWorklet, 16 kHz resampling, PCM16 framing.
- [`docs/websocket-frontend.md`](../docs/websocket-frontend.md): the `/ws/transcribe` client,
  sending audio safely, message handling, endings, browser lifecycle, tests.

## Stack

| Concern            | Choice                                                           |
| ------------------ | ---------------------------------------------------------------- |
| Build / dev server | Vite 8                                                           |
| UI                 | React 19, TypeScript 6 (strict)                                  |
| Styling            | Tailwind CSS 4, lucide-react icons                               |
| Routing            | React Router 7 (data router)                                     |
| Server state       | TanStack Query 5                                                 |
| Live audio         | Web Audio API, AudioWorklet (`public/pcm-worklet.js`), WebSocket |
| Tests              | Vitest 5, Testing Library, MSW 2 (mocked backend), jsdom         |
| Browser e2e        | playwright-core driving system Chrome (no browser download)      |
| Quality            | ESLint 10 (typescript-eslint `strictTypeChecked`), Prettier 3    |

## Setup

### Requirements

| Tool    | Supported versions                                                              |
| ------- | ------------------------------------------------------------------------------- |
| Node.js | **22.22.2+** (22.x), **24.15.0+** (24.x) or **26.0.0+**                         |
| npm     | 7+ (lockfile v3); use the npm bundled with your Node.js. Tested with npm 10.9.9 |

The Node.js range is the intersection of the `engines` fields of every locked dependency
(`engines.node` in `package.json`). The strictest are dev tools: `jsdom` 31 and
`w3c-xmlserializer` 6 (`^22.22.2 || ^24.15.0 || >=26.0.0`), `undici` 8 (`>=22.19.0`) and
`whatwg-url` 17 (`^22.14.0 || >=24.0.0`). Odd-numbered releases (23.x, 25.x) and 24.0–24.14
are therefore not supported. `.npmrc` sets `engine-strict=true`, so `npm ci` stops with
`EBADENGINE` on an unsupported version instead of installing a toolchain that may fail later.
The production bundle itself is static files and has no Node.js requirement at runtime.

The backend must be running (see `../backend/README.md`).

```bash
cd frontend
cp .env.example .env          # VITE_API_BASE_URL=http://localhost:8000/api/v1
npm ci                        # or: npm install
npm run dev                   # http://localhost:5173
```

The backend's `CORS_ALLOWED_ORIGINS` must include the frontend origin
(`http://localhost:5173` in development).

### Configuration

| Variable            | Required | Example                        |
| ------------------- | -------- | ------------------------------ |
| `VITE_API_BASE_URL` | yes      | `http://localhost:8000/api/v1` |

The API URL is never hardcoded. It is read from the environment at build time and validated
at start-up (`src/config/env.ts`); a missing or malformed value shows a configuration error
page instead of a broken app. `VITE_*` values are embedded in the public bundle, so never put
secrets in them.

## Scripts

| Command              | What it does                                            |
| -------------------- | ------------------------------------------------------- |
| `npm run dev`        | Dev server on port 5173                                 |
| `npm run build`      | Type-check (`tsc -b`) and production build into `dist/` |
| `npm run preview`    | Serve the production build (port 4173)                  |
| `npm run lint`       | ESLint plus a Prettier check                            |
| `npm run format`     | Format with Prettier                                    |
| `npm run typecheck`  | TypeScript only                                         |
| `npm run test`       | Run the test suite once                                 |
| `npm run test:watch` | Tests in watch mode                                     |
| `npm run e2e`        | Real-Chrome recording test against the backend (below)  |

## Structure

```
public/
  pcm-worklet.js           AudioWorklet: downmix, resample to 16 kHz, PCM16, 100 ms frames
e2e/
  recording.e2e.mjs        real Chrome + fake microphone WAV + backend (fake speech engine)
src/
  main.tsx                 entry: loads config, renders <App>
  app/                     App, providers, routes, QueryClient policy
  config/env.ts            VITE_API_BASE_URL parsing/validation
  api/
    types.ts               backend contracts (schemas, enums, limits)
    client.ts              fetch wrapper: base URL, Bearer token, ApiError, 401 hook
    endpoints.ts           typed auth + transcription endpoints
    ApiProvider.tsx        injects the API (real in the app, MSW-backed in tests)
  auth/
    tokenStorage.ts        JWT + expiry in localStorage (MVP)
    AuthProvider.tsx       session state, login/register/logout, restore on refresh
    RouteGuards.tsx        ProtectedRoute, PublicOnlyRoute
  audio/microphone.ts      MicrophoneCapture: permission, audio graph, flush, cleanup, errors
  realtime/
    protocol.ts            /ws/transcribe message types, URL derivation, close codes
    transcriptionSocket.ts handshake (JWT in first message), pacing, backpressure, stop
    transcript.ts          partial/final reducer for the live transcript
  hooks/                   TanStack Query hooks, debounce, useMicrophone,
                           useTranscriptionSocket, useLiveRecording
  lib/                     formatting, validation, URL params, helpers
  components/
    ui/                    Button, Field, Card/Badge/Skeleton, Modal/ConfirmDialog,
                           Toast, Pagination, Empty/Error states, Spinner
    layout/                AppLayout (sidebar + mobile drawer), AuthLayout
    transcriptions/        list item, filters, badges, segment list
    recording/             live transcript, level meter
  pages/                   Login, Register, Dashboard, NewTranscription,
                           TranscriptionDetail, Record, NotFound
  test/                    MSW fake backend, fake audio + WebSocket, render helpers, page tests
```

## Routes

| Path                  | Access          | Page                                          |
| --------------------- | --------------- | --------------------------------------------- |
| `/login`, `/register` | signed-out only | Sign in / create account                      |
| `/`                   | signed-in       | Dashboard: stats, search, filters, pagination |
| `/transcriptions/new` | signed-in       | Manual transcription form                     |
| `/transcriptions/:id` | signed-in       | Detail; `?edit=1` opens edit mode             |
| `/record`             | signed-in       | Live recording                                |

Unauthenticated visitors are sent to `/login` and returned to the page they asked for after
signing in. Only same-app paths are accepted as a return target.

## Design notes

- **Auth.** The JWT and its expiry are stored in `localStorage` (MVP trade-off: readable by
  page scripts, so XSS would expose it; an httpOnly cookie is the later upgrade). On load the
  session is restored with `GET /auth/me`. A 401 clears the token; a network error or 5xx
  keeps it and shows a retry page, so a server restart does not sign everyone out. Any
  authenticated request that gets a 401 ends the session with a notice. The token is only
  sent in the `Authorization` header, never in a URL, and is never logged.
- **Errors.** `ApiClient` turns the backend's `{detail, code, errors}` body into an `ApiError`
  with field errors (from 422 `loc`), `Retry-After` for 429, and the `X-Request-ID`. Forms
  show field errors inline and a summary alert. Queries retry only network errors and 5xx.
- **Dashboard state lives in the URL** (`?page=2&q=…&status=…`), so back/forward, reload and
  shared links work. Search is debounced (300 ms) and resets to page 1. The stats come from
  the list endpoint's `total` with `page_size=1`, because the backend has no stats endpoint.
- **Editing** sends only the changed fields in the `PATCH`. The raw recognised segments are
  shown separately and are read-only: editing the text never changes them.
- **Live recording.** Start asks for the microphone, then opens `ws(s)://…/ws/transcribe`
  (derived from `VITE_API_BASE_URL`) and sends `{"type":"start", token, …}` as the first
  message, never the token in the URL. Audio flows only after `session_started`: 16 kHz mono
  PCM16 in 100 ms frames from the AudioWorklet, paced under the server's rate limit with a
  bounded queue (never dropped). Stop flushes the last samples, sends `stop` and waits for
  `session_saved`. Partials are shown in grey italics and replaced in place; finals are
  ordered by `seq`. A connection that ends without `session_saved` is shown as
  **interrupted**, never as completed. The page warns before leaving during a recording.
  Every ending refreshes the history.
- **Stop is only "normal" if the audio was flushed.** If the AudioWorklet does not confirm
  that it delivered its last frames (2 s timeout, or a sample-count mismatch), the client
  does not send `stop`. It aborts the socket, so the server stores the recording as
  **interrupted**, and the page says the final seconds were not sent.
- **The result card follows `session_saved`:** fully saved, completed with unsaved segments
  (`unsaved_segments`), finished but not finalised (`record_saved: false`), or interrupted.
  Success is shown only when `record_saved` is true, `status` is `completed` and no segment
  is unsaved. Saved and unsaved counts, failed utterances and the link to the record are
  always shown.
- **Registration** omits `display_name` when it is empty (the API also accepts `null`, `""`
  or whitespace, stored as no name).
- **Accessibility.** Labelled controls with `aria-invalid`/`aria-describedby`, dialogs with
  focus trap, Escape and focus restore, toasts in a live region, visible focus rings.

## Tests

`npm run test` runs Vitest with jsdom. Page tests render the real app (routes, providers and
API client); MSW intercepts `fetch` and answers from an in-memory fake backend
(`src/test/fakeBackend.ts`) that follows the API contracts. That fake exists only in the
test suite. Unhandled requests fail the test.

Covered: login, registration, protected routes, session restore and expiry, 401 handling,
logout, API client error handling, dashboard loading/empty/error states, pagination, page
size, debounced search, filters, create, edit (PATCH of changed fields only), discard
confirmation, delete confirmation, detail with segments, not-found.

Live recording: the worklet DSP (`public/pcm-worklet.js` imported directly: mono, 48/44.1
→ 16 kHz, anti-aliasing, PCM16, frames and leftovers), `MicrophoneCapture` with mocked
`getUserMedia` / `AudioContext` / `AudioWorkletNode` (the fake node runs the real DSP), the
WebSocket client against a fake socket (handshake, JWT first, auth failure, partial
replacement, final ordering, preview pause, `session_saved`, invalid JSON, disconnect, 1009,
server errors, backpressure, stop workflow), and the whole recording page with the history
refresh. See `docs/websocket-frontend.md`.

### Browser end-to-end (`npm run e2e`)

Runs the production build in real Google Chrome. A generated 48 kHz stereo WAV is played
through Chrome's fake microphone into the real AudioWorklet, and the audio is streamed to a
real Uvicorn + FastAPI + PostgreSQL backend that uses the **fake speech engine**
(`backend/tests/e2e/serve_fake_engine.py`). No Whisper weights are needed. It checks the
WebSocket protocol, the frames, persistence, the dashboard, the detail page, and an
interrupted recording on a phone-sized screen.

```bash
# PostgreSQL running; backend/.env has TEST_DATABASE_URL (a *_test database: it is wiped)
cd frontend
npm run e2e                                   # CHROME_PATH=/usr/bin/google-chrome by default
E2E_SCREENSHOTS=./e2e-shots npm run e2e       # also save screenshots
```

Ports: API 8765, web 5173 (`E2E_API_PORT`, `E2E_WEB_PORT`). The backend virtualenv is
`../backend/.venv` (`BACKEND_PYTHON`).

## Known limitations

- The access token is not refreshed; when it expires the user signs in again (a recording
  started with a valid token continues until it ends).
- Dashboard stats need three small list requests.
- Firefox and Safari support AudioWorklet but are not covered by the automated browser
  test (Chrome only).
