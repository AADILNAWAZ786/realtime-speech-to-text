import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FakeWebSocket, sessionSaved, sessionStarted } from "../test/fakeWebSocket";
import {
  parseServerMessage,
  webSocketUrl,
  type FinalMessage,
  type PartialMessage,
} from "./protocol";
import {
  emptyTranscript,
  endTranscript,
  pendingFinals,
  transcriptReducer,
  type TranscriptState,
} from "./transcript";
import {
  SendPacer,
  SocketError,
  TranscriptionSocket,
  splitFrame,
  type SocketEnd,
  type TranscriptionSocketOptions,
} from "./transcriptionSocket";

const URL_ = "ws://localhost:8000/ws/transcribe";

function setup(overrides: Partial<TranscriptionSocketOptions> = {}) {
  const ends: SocketEnd[] = [];
  const messages: unknown[] = [];
  const warnings: string[] = [];
  const phases: string[] = [];
  let clock = 0;
  const socket = new TranscriptionSocket({
    url: URL_,
    getToken: () => "jwt-abc",
    start: { speech_mode: "mixed", script_preference: "roman", title: "Standup" },
    createSocket: (url) => new FakeWebSocket(url) as unknown as WebSocket,
    onEnd: (e) => ends.push(e),
    onMessage: (m) => messages.push(m),
    onWarning: (w) => warnings.push(w),
    onPhase: (p) => phases.push(p),
    now: () => clock,
    ...overrides,
  });
  return {
    socket,
    ends,
    messages,
    warnings,
    phases,
    advance: (ms: number) => (clock += ms),
    ws: () => FakeWebSocket.latest(),
  };
}

async function started(overrides: Partial<TranscriptionSocketOptions> = {}, session = {}) {
  const t = setup(overrides);
  const connecting = t.socket.connect();
  t.ws().open();
  t.ws().receive(sessionStarted(session));
  await connecting;
  return t;
}

const frame = (bytes = 3200) => new ArrayBuffer(bytes);

beforeEach(() => {
  FakeWebSocket.instances = [];
});

afterEach(() => {
  vi.useRealTimers();
});

describe("WebSocket URL", () => {
  it("derives ws:// and wss:// from the API base URL", () => {
    expect(webSocketUrl("http://localhost:8000/api/v1")).toBe("ws://localhost:8000/ws/transcribe");
    expect(webSocketUrl("https://api.example.com/api/v1/")).toBe(
      "wss://api.example.com/ws/transcribe",
    );
    expect(webSocketUrl("https://example.com/stt/api/v1")).toBe(
      "wss://example.com/stt/ws/transcribe",
    );
    expect(webSocketUrl("http://127.0.0.1:9000")).toBe("ws://127.0.0.1:9000/ws/transcribe");
    expect(() => webSocketUrl("ftp://x/api/v1")).toThrow();
  });
});

describe("connection and authentication", () => {
  it("sends the JWT in the first message only, never in the URL", async () => {
    const t = setup();
    const connecting = t.socket.connect();
    const ws = t.ws();
    expect(ws.url).toBe(URL_);
    expect(ws.url).not.toContain("jwt");
    expect(ws.binaryType).toBe("arraybuffer");
    expect(ws.sent).toHaveLength(0); // nothing before the connection opens
    ws.open();
    expect(ws.textMessages).toEqual([
      {
        type: "start",
        token: "jwt-abc",
        speech_mode: "mixed",
        script_preference: "roman",
        format: "pcm_s16le",
        sample_rate: 16000,
        channels: 1,
        title: "Standup",
      },
    ]);
    expect(t.phases).toEqual(["connecting", "authenticating"]);
    ws.receive(sessionStarted());
    await expect(connecting).resolves.toMatchObject({ transcription_id: expect.any(String) });
    expect(t.socket.currentPhase).toBe("streaming");
  });

  it("does not send audio before session_started", () => {
    const t = setup();
    void t.socket.connect();
    t.ws().open();
    expect(t.socket.sendAudio(frame())).toBe(false);
    expect(t.ws().audioFrames).toHaveLength(0);
  });

  it("fails without opening a socket when signed out", async () => {
    const t = setup({ getToken: () => null });
    await expect(t.socket.connect()).rejects.toMatchObject({ code: "no_token" });
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("reports an authentication failure (error + close 4401)", async () => {
    const t = setup();
    const connecting = t.socket.connect();
    t.ws().open();
    t.ws().receive({
      type: "error",
      code: "token_expired",
      message: "Token has expired",
      fatal: true,
    });
    t.ws().serverClose(4401);
    const error = await connecting.catch((e: unknown) => e);
    expect(error).toBeInstanceOf(SocketError);
    expect(error).toMatchObject({ code: "rejected", closeCode: 4401, serverCode: "token_expired" });
    expect((error as Error).message).toMatch(/expired/i);
    expect(t.ends).toHaveLength(0); // onEnd is only for sessions that started
  });

  it("reports a server that cannot be reached", async () => {
    const t = setup();
    const connecting = t.socket.connect();
    t.ws().serverClose(1006);
    await expect(connecting).rejects.toMatchObject({
      code: "connection_failed",
      message: expect.stringMatching(/could not connect/i),
    });
  });

  it("explains a cloud speech provider that is unavailable or out of quota", async () => {
    const a = setup();
    const first = a.socket.connect();
    a.ws().open();
    a.ws().receive({ type: "error", code: "transcription_unavailable", message: "x", fatal: true });
    a.ws().serverClose(1011);
    await expect(first).rejects.toMatchObject({
      code: "rejected",
      serverCode: "transcription_unavailable",
      message: expect.stringMatching(/speech recognition service is unavailable/i),
    });

    const b = await started();
    b.ws().receive({
      type: "error",
      code: "transcription_quota_exceeded",
      message: "x",
      fatal: true,
    });
    b.ws().receive(sessionSaved({ status: "interrupted", end_reason: "provider_error" }));
    expect(b.ends[0]).toMatchObject({
      kind: "saved",
      saved: { status: "interrupted", end_reason: "provider_error" },
      error: { code: "transcription_quota_exceeded" },
    });
  });

  it("explains a refused origin and an existing recording", async () => {
    const a = setup();
    const first = a.socket.connect();
    a.ws().serverClose(1008);
    await expect(first).rejects.toMatchObject({
      code: "rejected",
      message: expect.stringMatching(/origin/i),
    });

    const b = setup();
    const second = b.socket.connect();
    b.ws().open();
    b.ws().receive({ type: "error", code: "recording_in_progress", message: "x", fatal: true });
    b.ws().serverClose(4409);
    await expect(second).rejects.toMatchObject({ message: expect.stringMatching(/another tab/i) });
  });

  it("times out if session_started never arrives", async () => {
    vi.useFakeTimers();
    const t = setup({ handshakeTimeoutMs: 1000 });
    const connecting = t.socket.connect();
    t.ws().open();
    vi.advanceTimersByTime(1000);
    await expect(connecting).rejects.toMatchObject({ code: "handshake_timeout" });
    expect(t.ws().closedByClient).not.toBeNull();
  });

  it("can be aborted while connecting", async () => {
    const t = setup();
    const connecting = t.socket.connect();
    t.socket.abort();
    await expect(connecting).rejects.toMatchObject({ code: "aborted" });
  });
});

describe("sending audio", () => {
  it("sends binary frames while streaming", async () => {
    const t = await started();
    expect(t.socket.sendAudio(frame())).toBe(true);
    expect(t.socket.sendAudio(frame())).toBe(true);
    expect(t.ws().audioFrames.map((f) => f.byteLength)).toEqual([3200, 3200]);
  });

  it("never exceeds session_started.max_frame_bytes", async () => {
    const t = await started({}, { max_frame_bytes: 1001 });
    t.socket.sendAudio(frame(3200));
    const sizes = t.ws().audioFrames.map((f) => f.byteLength);
    expect(sizes).toEqual([1000, 1000, 1000, 200]);
    expect(sizes.every((n) => n % 2 === 0)).toBe(true);
  });

  it("holds frames while the socket buffer is full and sends them in order later", async () => {
    vi.useFakeTimers();
    const t = await started();
    const ws = t.ws();
    ws.bufferedAmount = 64_000; // network stalled
    const tagged = [1, 2, 3].map((n) => new Uint8Array(3200).fill(n).buffer);
    for (const f of tagged) t.socket.sendAudio(f);
    expect(ws.audioFrames).toHaveLength(0);
    expect(t.socket.backlogMs).toBe(300);
    ws.bufferedAmount = 0;
    t.advance(100);
    vi.advanceTimersByTime(60);
    expect(ws.audioFrames.map((f) => new Uint8Array(f)[0])).toEqual([1, 2, 3]);
    expect(t.socket.backlogMs).toBe(0);
  });

  it("paces sending to stay under the server's real-time limit", async () => {
    vi.useFakeTimers();
    const t = await started({ limits: { burstMs: 1000, rateFactor: 1 } });
    for (let i = 0; i < 15; i++) t.socket.sendAudio(frame()); // 1.5 s at once
    expect(t.ws().audioBytes).toBe(32_000); // burst: 1 s
    t.advance(500);
    vi.advanceTimersByTime(60);
    expect(t.ws().audioBytes).toBe(48_000); // + 0.5 s
  });

  it("stops with an error instead of dropping audio when it cannot keep up", async () => {
    const t = await started({ limits: { maxBacklogMs: 1000 } });
    t.ws().bufferedAmount = 1_000_000;
    for (let i = 0; i < 10; i++) expect(t.socket.sendAudio(frame())).toBe(true);
    expect(t.socket.sendAudio(frame())).toBe(false); // 1.1 s queued > 1 s
    expect(t.ends).toHaveLength(1);
    const end = t.ends[0];
    expect(end?.kind).toBe("interrupted");
    if (end?.kind === "interrupted") {
      expect(end.error.code).toBe("backpressure");
      expect(end.transcriptionId).toBe(sessionStarted().transcription_id);
    }
    expect(t.ws().closedByClient).not.toBeNull(); // the server saves it as interrupted
  });
});

describe("server messages", () => {
  it("passes every protocol message on, in order", async () => {
    const t = await started();
    const sequence = [
      {
        type: "partial",
        utterance_id: 0,
        version: 1,
        text: "aaj",
        raw_text: "aaj",
        start_ms: 0,
        end_ms: 900,
      },
      { type: "preview_paused", backlog_ms: 4200 },
      { type: "preview_resumed", backlog_ms: 300 },
      {
        type: "utterance_discarded",
        utterance_id: 1,
        start_ms: 2000,
        end_ms: 2300,
        reason: "no_speech",
      },
      {
        type: "utterance_failed",
        utterance_id: 2,
        start_ms: 3000,
        end_ms: 3500,
        code: "inference_failed",
      },
      { type: "error", code: "persistence_failed", message: "x", fatal: false, seq: 0 },
    ];
    for (const m of sequence) t.ws().receive(m);
    expect(t.messages.map((m) => (m as { type: string }).type)).toEqual([
      "session_started",
      ...sequence.map((m) => m.type),
    ]);
    expect(t.ends).toHaveLength(0); // a non-fatal error does not end anything
  });

  it("ignores invalid JSON and unknown types, with a warning", async () => {
    const t = await started();
    t.ws().receive("{not json");
    t.ws().receive({ type: "surprise" });
    expect(t.messages).toHaveLength(1); // only session_started
    expect(t.warnings).toHaveLength(2); // one per ignored message (the UI de-duplicates)
    expect(t.socket.currentPhase).toBe("streaming");
  });
});

describe("stop workflow", () => {
  it("sends queued audio, then stop, keeps listening, and ends on session_saved", async () => {
    vi.useFakeTimers();
    const t = await started();
    const ws = t.ws();
    ws.bufferedAmount = 64_000;
    t.socket.sendAudio(frame());
    t.socket.finish();
    expect(t.socket.currentPhase).toBe("stopping");
    expect(ws.textMessages.map((m) => m.type)).toEqual(["start"]); // audio still queued
    ws.bufferedAmount = 0;
    t.advance(100);
    vi.advanceTimersByTime(60);
    expect(ws.sent.at(-2)).toBeInstanceOf(ArrayBuffer);
    expect(ws.textMessages.map((m) => m.type)).toEqual(["start", "stop"]); // stop after audio
    expect(ws.closedByClient).toBeNull(); // not closed after stop

    t.socket.sendAudio(frame()); // too late: ignored
    expect(ws.audioFrames).toHaveLength(1);

    ws.receive({
      type: "final",
      seq: 0,
      utterance_id: 0,
      text: "aaj",
      raw_text: "aaj",
      start_ms: 0,
      end_ms: 900,
      language: "hi",
      avg_logprob: -0.2,
      commit_reason: "stop",
      saved: true,
    });
    ws.receive(sessionSaved());
    expect(t.ends).toEqual([{ kind: "saved", saved: sessionSaved(), error: null }]);
    ws.serverClose(1000);
    expect(t.socket.currentPhase).toBe("closed");
    expect(t.ends).toHaveLength(1);
  });

  it("closes the socket itself if the server does not after session_saved", async () => {
    vi.useFakeTimers();
    const t = await started();
    t.socket.finish();
    t.ws().receive(sessionSaved());
    vi.advanceTimersByTime(3_000);
    expect(t.ws().closedByClient).toEqual({ code: 1000 });
  });

  it("gives up waiting for session_saved after the finish timeout", async () => {
    vi.useFakeTimers();
    const t = await started({ finishTimeoutMs: 5_000 });
    t.socket.finish();
    vi.advanceTimersByTime(5_000);
    expect(t.ends[0]).toMatchObject({ kind: "interrupted", error: { code: "finish_timeout" } });
  });
});

describe("unexpected endings", () => {
  it("reports a network disconnect as interrupted, never as completed", async () => {
    const t = await started();
    t.ws().serverClose(1006);
    expect(t.ends).toHaveLength(1);
    expect(t.ends[0]).toMatchObject({
      kind: "interrupted",
      transcriptionId: sessionStarted().transcription_id,
      error: { code: "connection_lost", closeCode: 1006 },
    });
  });

  it("explains close code 1009 (frame too large)", async () => {
    const t = await started();
    t.ws().serverClose(1009);
    expect(t.ends[0]).toMatchObject({
      kind: "interrupted",
      error: { closeCode: 1009, message: expect.stringMatching(/too large/i) },
    });
  });

  it("delivers a fatal server error with the session_saved that follows it", async () => {
    const t = await started();
    const fatal = { type: "error", code: "server_overloaded", message: "x", fatal: true };
    t.ws().receive(fatal);
    t.ws().receive(sessionSaved({ status: "interrupted", end_reason: "overloaded" }));
    t.ws().serverClose(1013);
    expect(t.ends).toEqual([
      {
        kind: "saved",
        saved: sessionSaved({ status: "interrupted", end_reason: "overloaded" }),
        error: fatal,
      },
    ]);
  });

  it("uses the server's error code to explain a close without session_saved", async () => {
    const t = await started();
    t.ws().receive({ type: "error", code: "server_shutdown", message: "x", fatal: true });
    t.ws().serverClose(1012);
    expect(t.ends[0]).toMatchObject({ error: { message: expect.stringMatching(/restarting/i) } });
  });

  it("abort() closes the socket and ends once", async () => {
    const t = await started();
    t.socket.abort();
    t.socket.abort();
    expect(t.ends).toHaveLength(1);
    expect(t.ws().closedByClient).toEqual({ code: 4000 });
  });
});

describe("helpers", () => {
  it("splitFrame keeps whole samples", () => {
    expect(splitFrame(frame(10), 4).map((b) => b.byteLength)).toEqual([4, 4, 2]);
    expect(splitFrame(frame(10), 5).map((b) => b.byteLength)).toEqual([4, 4, 2]);
    expect(splitFrame(frame(10), 64)).toHaveLength(1);
  });

  it("SendPacer refills over time up to its burst", () => {
    let now = 0;
    const pacer = new SendPacer({ rateFactor: 1, burstMs: 100 }, () => now);
    expect(pacer.take(3200)).toBe(true); // 100 ms burst
    expect(pacer.take(2)).toBe(false);
    now = 50;
    expect(pacer.take(1600)).toBe(true);
    now = 10_000;
    expect(pacer.take(3202)).toBe(false); // capped at the burst
  });

  it("parseServerMessage validates the envelope", () => {
    expect(parseServerMessage('{"type":"partial"}')).toEqual({ type: "partial" });
    expect(parseServerMessage("[1]")).toBeNull();
    expect(parseServerMessage("null")).toBeNull();
    expect(parseServerMessage("nope")).toBeNull();
  });
});

// ----- transcript reducer

const partial = (
  utterance_id: number,
  version: number,
  text: string,
  start_ms = utterance_id * 1000,
): PartialMessage => ({
  type: "partial",
  utterance_id,
  version,
  text,
  raw_text: text,
  start_ms,
  end_ms: start_ms + 500,
});
const final = (seq: number, utterance_id: number, text: string, saved = true): FinalMessage => ({
  type: "final",
  seq,
  utterance_id,
  text,
  raw_text: text,
  start_ms: utterance_id * 1000,
  end_ms: utterance_id * 1000 + 800,
  language: "hi",
  avg_logprob: -0.2,
  commit_reason: "silence",
  saved,
});
const run = (...messages: Parameters<typeof transcriptReducer>[1][]): TranscriptState =>
  messages.reduce(transcriptReducer, emptyTranscript);

describe("transcript", () => {
  it("replaces the partial of the same utterance instead of appending", () => {
    const state = run(
      partial(0, 1, "aaj"),
      partial(0, 2, "aaj ki"),
      partial(0, 3, "aaj ki meeting"),
    );
    expect(state.partials.map((p) => p.text)).toEqual(["aaj ki meeting"]);
  });

  it("ignores an older partial version", () => {
    expect(run(partial(0, 3, "new"), partial(0, 2, "old")).partials.map((p) => p.text)).toEqual([
      "new",
    ]);
  });

  it("a final replaces its partial and keeps earlier finals", () => {
    const state = run(
      final(0, 0, "pehla"),
      partial(1, 1, "doos"),
      final(1, 1, "doosra"),
      partial(2, 1, "tee"),
    );
    expect(state.finals.map((f) => f.text)).toEqual(["pehla", "doosra"]);
    expect(state.partials.map((p) => p.utterance_id)).toEqual([2]);
  });

  it("orders finals by seq even if they arrive out of order", () => {
    const state = run(final(2, 5, "c"), final(0, 3, "a"), final(1, 4, "b"));
    expect(state.finals.map((f) => [f.seq, f.text])).toEqual([
      [0, "a"],
      [1, "b"],
      [2, "c"],
    ]);
  });

  it("drops a late partial for an utterance that is already final", () => {
    expect(run(final(0, 0, "done"), partial(0, 9, "late")).partials).toHaveLength(0);
  });

  it("keeps partials of overlapping utterances in time order", () => {
    const state = run(partial(2, 1, "b", 2000), partial(1, 1, "a", 1000));
    expect(state.partials.map((p) => p.text)).toEqual(["a", "b"]);
  });

  it("tracks discarded and failed utterances and preview pauses", () => {
    let state = run(partial(0, 1, "noise"), {
      type: "utterance_discarded",
      utterance_id: 0,
      start_ms: 0,
      end_ms: 1,
      reason: "no_speech",
    });
    expect(state.partials).toHaveLength(0);
    expect(state.discarded).toBe(1);
    state = transcriptReducer(state, {
      type: "utterance_failed",
      utterance_id: 1,
      start_ms: 0,
      end_ms: 1,
      code: "x",
    });
    expect(state.failed).toHaveLength(1);
    state = transcriptReducer(state, { type: "preview_paused", backlog_ms: 5000 });
    expect(state.previewPaused).toBe(true);
    state = transcriptReducer(state, { type: "preview_resumed", backlog_ms: 100 });
    expect(state.previewPaused).toBe(false);
  });

  it("lists finals that were not saved", () => {
    expect(pendingFinals(run(final(0, 0, "a"), final(1, 1, "b", false))).map((f) => f.seq)).toEqual(
      [1],
    );
  });

  it("drops provisional text, but keeps finals, when the connection ends", () => {
    const state = endTranscript(run(final(0, 0, "kept"), partial(1, 1, "lost")));
    expect(state.finals.map((f) => f.text)).toEqual(["kept"]);
    expect(state.partials).toHaveLength(0);
  });

  it("clears provisional text when the session is saved", () => {
    const state = run(
      partial(0, 1, "x"),
      sessionSaved() as Parameters<typeof transcriptReducer>[1],
    );
    expect(state.partials).toHaveLength(0);
  });
});
