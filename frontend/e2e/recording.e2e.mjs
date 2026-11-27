/**
 * Browser end-to-end test of live recording: real Chrome, real microphone pipeline
 * (getUserMedia -> AudioWorklet -> WebSocket), real FastAPI + PostgreSQL backend.
 *
 * - Chrome's fake capture device plays a generated 48 kHz STEREO WAV as the microphone
 *   (--use-file-for-fake-audio-capture), so downmix and 48 -> 16 kHz resampling run in the
 *   browser exactly as for a user.
 * - The backend runs with the FAKE speech engine (backend/tests/e2e/serve_fake_engine.py):
 *   real VAD, endpointing, protocol and persistence, but a deterministic recogniser. No
 *   Whisper weights are downloaded.
 *
 * Usage (from frontend/):   npm run e2e
 * Needs: Google Chrome (CHROME_PATH, default /usr/bin/google-chrome), PostgreSQL with
 * TEST_DATABASE_URL configured for the backend (its *_test database is wiped), and the
 * backend virtualenv (BACKEND_PYTHON, default ../backend/.venv/bin/python).
 * Optional: E2E_SCREENSHOTS=<dir> saves screenshots of each stage.
 */
import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright-core";

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const BACKEND = resolve(FRONTEND, "../backend");
const API_PORT = Number(process.env.E2E_API_PORT ?? 8765);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 5173); // must be in the backend's CORS list
const API = `http://localhost:${API_PORT}`;
const WEB = `http://localhost:${WEB_PORT}`;
const CHROME = process.env.CHROME_PATH ?? "/usr/bin/google-chrome";
const PYTHON = process.env.BACKEND_PYTHON ?? join(BACKEND, ".venv/bin/python");
const SHOTS = process.env.E2E_SCREENSHOTS ? resolve(process.env.E2E_SCREENSHOTS) : null;

const work = mkdtempSync(join(tmpdir(), "stt-e2e-"));
const wav = join(work, "speech.wav");
const children = [];
const checks = [];
let currentPage = null; // for a screenshot when something fails
const consoleErrors = [];

function check(name, ok, detail = "") {
  checks.push({ name, ok: Boolean(ok), detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `  (${detail})` : ""}`);
}

function run(command, args, options = {}) {
  // Own process group, so the whole tree (npx/vite/uvicorn) can be stopped together.
  const child = spawn(command, args, {
    stdio: ["ignore", "pipe", "pipe"],
    detached: true,
    ...options,
  });
  let output = "";
  child.stdout.on("data", (d) => (output += d));
  child.stderr.on("data", (d) => (output += d));
  child.output = () => output;
  children.push(child);
  return child;
}

function finished(child) {
  return new Promise((resolveExit, reject) => {
    child.on("exit", (code) =>
      code === 0
        ? resolveExit()
        : reject(new Error(`${child.spawnargs.join(" ")} exited ${code}\n${child.output()}`)),
    );
  });
}

async function waitForHttp(url, child, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) throw new Error(`process exited early:\n${child.output()}`);
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // not up yet
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`timed out waiting for ${url}\n${child.output()}`);
}

async function shot(page, name) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: join(SHOTS, `${name}.png`) });
}

async function main() {
  // 1. Production build pointing at the e2e API.
  console.log("building frontend…");
  const dist = join(work, "dist");
  await finished(
    run(
      join(FRONTEND, "node_modules/.bin/vite"),
      ["build", "--outDir", dist, "--emptyOutDir", "--logLevel", "warn"],
      {
        cwd: FRONTEND,
        env: { ...process.env, VITE_API_BASE_URL: `${API}/api/v1` },
      },
    ),
  );

  // 2. Backend with the fake engine (also writes the fake-microphone WAV).
  console.log("starting backend (fake speech engine)…");
  const api = run(
    PYTHON,
    [
      "-m",
      "tests.e2e.serve_fake_engine",
      "--port",
      String(API_PORT),
      "--origin",
      WEB,
      "--wav",
      wav,
    ],
    { cwd: BACKEND },
  );
  await waitForHttp(`${API}/api/v1/health`, api);

  // 3. Serve the build.
  const web = run(
    "npx",
    [
      "vite",
      "preview",
      "--outDir",
      dist,
      "--port",
      String(WEB_PORT),
      "--strictPort",
      "--host",
      "localhost",
    ],
    { cwd: FRONTEND },
  );
  await waitForHttp(WEB, web);

  // 4. Chrome with the WAV as its microphone.
  const browser = await chromium.launch({
    executablePath: CHROME,
    args: [
      "--use-fake-ui-for-media-stream",
      "--use-fake-device-for-media-stream",
      `--use-file-for-fake-audio-capture=${wav}`,
    ],
  });
  try {
    const context = await browser.newContext({ viewport: { width: 1360, height: 900 } });
    await context.grantPermissions(["microphone"], { origin: WEB });
    const page = await context.newPage();
    currentPage = page;
    page.on("console", (m) => m.type() === "error" && consoleErrors.push(m.text()));
    page.on("pageerror", (e) => consoleErrors.push(e.message));

    // Every WebSocket event, in order.
    const events = [];
    let socketUrl = null;
    page.on("websocket", (ws) => {
      socketUrl = ws.url();
      ws.on("framesent", ({ payload }) => events.push({ dir: "out", payload }));
      ws.on("framereceived", ({ payload }) => events.push({ dir: "in", payload }));
    });

    // Register through the UI (credentials generated per run).
    const email = `e2e.${randomBytes(4).toString("hex")}@example.com`;
    const password = randomBytes(12).toString("base64url");
    await page.goto(`${WEB}/register`);
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password", { exact: true }).fill(password);
    await page.getByLabel("Confirm password").fill(password);
    await page.getByRole("button", { name: "Create account" }).click();
    await page.getByText("No transcriptions yet").waitFor();

    // Record.
    await page.getByRole("link", { name: "New recording" }).first().click();
    await page.getByRole("heading", { name: "New recording" }).waitFor();
    await page.getByLabel("Language").selectOption("mixed");
    await page.getByLabel("Output script").selectOption("roman");
    await page.getByLabel("Title (optional)").fill("Browser E2E recording");
    await shot(page, "01-record-idle");
    await page.getByRole("button", { name: "Start recording" }).click();
    await page
      .getByTestId("recording-status")
      .filter({ hasText: /^Recording$/ })
      .waitFor({ timeout: 15_000 });
    await page.locator('[data-kind="partial"]').first().waitFor({ timeout: 15_000 });
    await shot(page, "02-recording-partial");
    await page.locator('[data-kind="final"]').nth(1).waitFor({ timeout: 20_000 });
    await shot(page, "03-recording-finals");
    await page.getByRole("button", { name: "Stop recording" }).click();
    await page
      .getByTestId("recording-status")
      .filter({ hasText: "Recording saved" })
      .waitFor({ timeout: 30_000 });
    await shot(page, "04-recording-saved");

    // ----- protocol checks
    const parsed = events.map((e) =>
      typeof e.payload === "string"
        ? { ...e, json: JSON.parse(e.payload) }
        : { ...e, bytes: e.payload.length },
    );
    const outText = parsed.filter((e) => e.dir === "out" && e.json);
    const outAudio = parsed.filter((e) => e.dir === "out" && e.bytes !== undefined);
    const received = parsed.filter((e) => e.dir === "in").map((e) => e.json);
    const types = received.map((m) => m.type);
    const firstOut = parsed.find((e) => e.dir === "out");
    const startedAt = parsed.findIndex((e) => e.dir === "in" && e.json?.type === "session_started");
    const firstAudioAt = parsed.findIndex((e) => e.dir === "out" && e.bytes !== undefined);
    const stopAt = parsed.findIndex((e) => e.dir === "out" && e.json?.type === "stop");
    const saved = received.find((m) => m.type === "session_saved");
    const finals = received.filter((m) => m.type === "final");

    check(
      "WebSocket URL derived from VITE_API_BASE_URL",
      socketUrl === `ws://localhost:${API_PORT}/ws/transcribe`,
      socketUrl,
    );
    check(
      "no token in the WebSocket URL",
      socketUrl && !socketUrl.includes("token") && !socketUrl.includes("ey"),
    );
    check(
      "first message is start with the JWT",
      firstOut?.json?.type === "start" &&
        typeof firstOut.json.token === "string" &&
        firstOut.json.token.length > 20,
    );
    check(
      "start declares pcm_s16le / 16000 / mono",
      firstOut?.json?.format === "pcm_s16le" &&
        firstOut.json.sample_rate === 16000 &&
        firstOut.json.channels === 1,
    );
    check(
      "start carries language, script and title",
      firstOut?.json?.speech_mode === "mixed" &&
        firstOut.json.script_preference === "roman" &&
        firstOut.json.title === "Browser E2E recording",
    );
    check("no audio before session_started", startedAt >= 0 && firstAudioAt > startedAt);
    check(
      "binary frames are 3200 bytes (100 ms), even, never larger",
      outAudio.length > 10 &&
        outAudio.slice(0, -1).every((f) => f.bytes === 3200) &&
        outAudio.every((f) => f.bytes <= 3200 && f.bytes % 2 === 0),
      `${outAudio.length} frames, last ${outAudio.at(-1)?.bytes} B`,
    );
    check(
      "stop is sent after the last audio frame",
      stopAt > parsed.findLastIndex((e) => e.dir === "out" && e.bytes !== undefined) &&
        outText.at(-1)?.json?.type === "stop",
    );
    check(
      "received session_started, partial, final, session_saved",
      ["session_started", "partial", "final", "session_saved"].every((t) => types.includes(t)),
      [...new Set(types)].join(","),
    );
    check(
      "finals arrive with seq 0, 1, 2, …",
      finals.length >= 2 && finals.every((f, i) => f.seq === i),
      `${finals.length} finals`,
    );
    check(
      "finals saved",
      finals.every((f) => f.saved === true),
    );
    check(
      "session_saved: completed / stopped",
      saved?.status === "completed" &&
        saved.end_reason === "stopped" &&
        saved.record_saved === true,
      JSON.stringify({ status: saved?.status, reason: saved?.end_reason }),
    );
    const sentMs = outAudio.reduce((n, f) => n + f.bytes, 0) / 32;
    check(
      "audio duration sent matches the server's duration",
      saved && Math.abs(sentMs - saved.duration_ms) <= 50,
      `sent ${sentMs.toFixed(0)} ms, server ${saved?.duration_ms} ms`,
    );
    check(
      "all finals saved, none failed",
      saved?.unsaved_segments.length === 0 && saved.saved_segments === finals.length,
    );
    const shownFinals = await page.locator('[data-kind="final"]').count();
    check("page shows every final", shownFinals === finals.length, `${shownFinals}`);
    check("connection closed after saving", (await page.getByText("Disconnected").count()) > 0);

    // ----- history: dashboard and detail
    const id = saved?.transcription_id;
    await page.getByRole("link", { name: "Open transcription" }).click();
    await page.getByRole("heading", { name: "Browser E2E recording" }).waitFor();
    const segments = await page
      .getByRole("list", { name: "Recognised segments" })
      .getByRole("listitem")
      .count();
    check(
      "detail page lists the saved segments",
      segments === finals.length,
      `${segments} segments`,
    );
    check("detail URL is the recording", page.url().endsWith(`/transcriptions/${id}`));
    await shot(page, "05-detail");
    await page.getByRole("link", { name: "Dashboard" }).first().click();
    const row = page
      .getByRole("list", { name: "Transcriptions" })
      .getByRole("listitem")
      .filter({ hasText: "Browser E2E recording" });
    await row.waitFor();
    check(
      "dashboard lists the recording as a completed live recording",
      (await row.getByText("Live recording").count()) === 1 &&
        (await row.getByText("Completed").count()) === 1,
    );
    await shot(page, "06-dashboard");
    check("no browser console errors", consoleErrors.length === 0, consoleErrors.join(" | "));

    // ----- scenario 2: phone-sized screen, the server shuts down mid-recording
    events.length = 0;
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${WEB}/record`);
    await page.getByRole("button", { name: "Start recording" }).click();
    await page
      .locator('[data-kind="partial"], [data-kind="final"]')
      .first()
      .waitFor({ timeout: 15_000 });
    await shot(page, "07-mobile-recording");
    process.kill(-api.pid, "SIGTERM"); // graceful shutdown: the server saves it as interrupted
    await page
      .getByTestId("recording-status")
      .filter({ hasText: "Recording interrupted" })
      .waitFor({ timeout: 30_000 });
    await page.getByLabel("Recording result").scrollIntoViewIfNeeded();
    await shot(page, "08-mobile-interrupted");
    // Uvicorn may close the socket (1012) before the app's shutdown hook sends
    // session_saved; either way the page must say "interrupted", never "saved".
    const received2 = events.filter((e) => e.dir === "in").map((e) => JSON.parse(e.payload));
    const saved2 = received2.find((m) => m.type === "session_saved");
    check(
      "server shutdown is reported as interrupted, not completed",
      (saved2 === undefined ||
        (saved2.status === "interrupted" && saved2.end_reason === "shutdown")) &&
        (await page.getByText("Recording saved").count()) === 0,
      `server sent: ${received2
        .map((m) => m.type)
        .filter((t) => t !== "partial")
        .join(",")}`,
    );
    check(
      "the page explains the restart",
      /restart/i.test((await page.getByRole("alert").textContent()) ?? ""),
    );
    check(
      "the interrupted recording links to its saved text",
      (await page.getByRole("link", { name: "Open transcription" }).count()) === 1,
    );

    console.log(
      `\ntranscript: ${saved?.text}\nmessages received: ${JSON.stringify(Object.fromEntries([...new Set(types)].map((t) => [t, types.filter((x) => x === t).length])))}`,
    );
  } catch (error) {
    if (currentPage) {
      const path = join(SHOTS ?? FRONTEND, "e2e-failure.png");
      await currentPage.screenshot({ path }).catch(() => undefined);
      console.error(`failure screenshot: ${path}`);
    }
    if (consoleErrors.length) console.error(`browser console errors:\n${consoleErrors.join("\n")}`);
    for (const child of children)
      console.error(`--- ${child.spawnargs.slice(0, 3).join(" ")}\n${child.output().slice(-3000)}`);
    throw error;
  } finally {
    await browser.close();
  }
}

try {
  await main();
} catch (error) {
  check("e2e run completed", false, error instanceof Error ? error.message : String(error));
} finally {
  for (const child of children) {
    if (child.exitCode !== null) continue;
    try {
      process.kill(-child.pid, "SIGTERM");
    } catch {
      child.kill("SIGTERM");
    }
  }
  rmSync(work, { recursive: true, force: true });
}
const failed = checks.filter((c) => !c.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
