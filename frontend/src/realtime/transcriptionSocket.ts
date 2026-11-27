/**
 * Client for /ws/transcribe (docs/websocket-protocol.md). Framework-free; React binds it in
 * hooks/useTranscriptionSocket.
 *
 * Lifecycle:
 *   connect()   open the socket -> send {"type":"start", token, ...} as the FIRST message ->
 *               resolve on session_started (reject on any failure before that)
 *   sendAudio() binary PCM16 frames, only while streaming; split to <= max_frame_bytes,
 *               paced and bounded (see below)
 *   finish()    after the microphone has flushed: send every queued frame, then
 *               {"type":"stop"}; keep receiving until session_saved
 *   abort()     close now (the server saves the recording as interrupted)
 *
 * onEnd fires exactly once after session_started: with the session_saved message, or with
 * an "interrupted" outcome if the connection ended without one.
 *
 * Sending audio safely. Frames go to the socket only while `bufferedAmount` is below a
 * high-water mark and a token bucket allows it (slightly below the server's own limiter:
 * WS_AUDIO_RATE_FACTOR 1.5, WS_AUDIO_BURST_SECONDS 5). Otherwise they wait in a bounded
 * queue. Nothing is ever dropped: if the queue exceeds its bound, the connection cannot keep
 * up and the recording is stopped with an error instead.
 */
import {
  CLOSE_CODE_MESSAGES,
  ERROR_CODE_MESSAGES,
  parseServerMessage,
  type ServerError,
  type ServerMessage,
  type SessionSaved,
  type SessionStarted,
  type StartMessage,
} from "./protocol";

export const BYTES_PER_MS = 32; // 16 kHz * 2 bytes

export type SocketPhase =
  "idle" | "connecting" | "authenticating" | "streaming" | "stopping" | "closed";

export type SocketErrorCode =
  | "no_token"
  | "connection_failed"
  | "handshake_timeout"
  | "rejected"
  | "backpressure"
  | "finish_timeout"
  | "connection_lost"
  | "aborted";

export class SocketError extends Error {
  override name = "SocketError";
  constructor(
    readonly code: SocketErrorCode,
    message: string,
    readonly closeCode: number | null = null,
    readonly serverCode: string | null = null,
  ) {
    super(message);
  }
}

export type SocketEnd =
  | { kind: "saved"; saved: SessionSaved; error: ServerError | null }
  | {
      kind: "interrupted";
      transcriptionId: string;
      error: SocketError;
    };

export interface SocketLimits {
  /** Keep at most this many bytes inside the browser's socket buffer. */
  highWaterBytes: number;
  /** Largest backlog (in ms of audio) held while the network is slow. */
  maxBacklogMs: number;
  /** Token bucket: sustained rate relative to real time, and burst size. */
  rateFactor: number;
  burstMs: number;
}

export const DEFAULT_LIMITS: SocketLimits = {
  highWaterBytes: 32_000, // ~1 s of audio
  maxBacklogMs: 15_000,
  rateFactor: 1.4, // server allows 1.5
  burstMs: 4_000, // server allows 5 s
};

export interface TranscriptionSocketOptions {
  url: string;
  getToken: () => string | null;
  start: Pick<StartMessage, "speech_mode" | "script_preference" | "title">;
  onMessage?: (message: ServerMessage) => void;
  onPhase?: (phase: SocketPhase) => void;
  onEnd?: (end: SocketEnd) => void;
  /** Non-fatal problems worth showing (unreadable server message, ...). */
  onWarning?: (message: string) => void;
  createSocket?: (url: string) => WebSocket;
  now?: () => number;
  limits?: Partial<SocketLimits>;
  handshakeTimeoutMs?: number;
  /** After stop: how long to wait for session_saved. */
  finishTimeoutMs?: number;
}

const OPEN = 1;
const PUMP_INTERVAL_MS = 50;
const CLIENT_CLOSE_CODE = 4000; // application-defined: client gave up

/** Token bucket in bytes, mirroring the server's AudioRateLimiter (with a safety margin). */
export class SendPacer {
  private tokens: number;
  private last: number;
  private readonly rate: number; // bytes per ms
  private readonly capacity: number;

  constructor(
    limits: Pick<SocketLimits, "rateFactor" | "burstMs">,
    private readonly now: () => number,
  ) {
    this.rate = BYTES_PER_MS * limits.rateFactor;
    this.capacity = BYTES_PER_MS * limits.burstMs;
    this.tokens = this.capacity;
    this.last = now();
  }

  take(bytes: number): boolean {
    const t = this.now();
    this.tokens = Math.min(this.capacity, this.tokens + (t - this.last) * this.rate);
    this.last = t;
    if (bytes > this.tokens) return false;
    this.tokens -= bytes;
    return true;
  }
}

/** Split a PCM16 buffer into chunks of at most `maxBytes` (rounded down to whole samples). */
export function splitFrame(pcm: ArrayBuffer, maxBytes: number): ArrayBuffer[] {
  const limit = Math.max(2, maxBytes - (maxBytes % 2));
  if (pcm.byteLength <= limit) return [pcm];
  const chunks: ArrayBuffer[] = [];
  for (let offset = 0; offset < pcm.byteLength; offset += limit) {
    chunks.push(pcm.slice(offset, Math.min(offset + limit, pcm.byteLength)));
  }
  return chunks;
}

function describeClose(code: number, serverError: ServerError | null): string {
  if (serverError) {
    const known = ERROR_CODE_MESSAGES[serverError.code as keyof typeof ERROR_CODE_MESSAGES];
    return known ?? serverError.message;
  }
  return CLOSE_CODE_MESSAGES[code] ?? `The connection closed unexpectedly (code ${code}).`;
}

export class TranscriptionSocket {
  private ws: WebSocket | null = null;
  private phase: SocketPhase = "idle";
  private session: SessionStarted | null = null;
  private fatalError: ServerError | null = null;
  private lastError: ServerError | null = null;
  private ended = false;
  private stopSent = false;
  private queue: ArrayBuffer[] = [];
  private queuedBytes = 0;
  private pumpTimer: ReturnType<typeof setTimeout> | null = null;
  private handshakeTimer: ReturnType<typeof setTimeout> | null = null;
  private finishTimer: ReturnType<typeof setTimeout> | null = null;
  private closeTimer: ReturnType<typeof setTimeout> | null = null;
  private pending: {
    resolve: (s: SessionStarted) => void;
    reject: (e: SocketError) => void;
  } | null = null;
  private readonly limits: SocketLimits;
  private readonly pacer: SendPacer;
  private readonly now: () => number;

  /** Bytes of audio handed to the socket so far. */
  sentBytes = 0;

  constructor(private readonly options: TranscriptionSocketOptions) {
    this.limits = { ...DEFAULT_LIMITS, ...options.limits };
    this.now = options.now ?? (() => performance.now());
    this.pacer = new SendPacer(this.limits, this.now);
  }

  get currentPhase(): SocketPhase {
    return this.phase;
  }

  get sessionInfo(): SessionStarted | null {
    return this.session;
  }

  /** Audio waiting in the client queue (ms). */
  get backlogMs(): number {
    return this.queuedBytes / BYTES_PER_MS;
  }

  connect(): Promise<SessionStarted> {
    if (this.phase !== "idle") return Promise.reject(new Error("connect() called twice"));
    const token = this.options.getToken();
    if (!token) {
      this.setPhase("closed");
      return Promise.reject(
        new SocketError("no_token", "You are signed out. Sign in again to record."),
      );
    }
    return new Promise<SessionStarted>((resolve, reject) => {
      this.pending = { resolve, reject };
      this.setPhase("connecting");
      let ws: WebSocket;
      try {
        ws = (this.options.createSocket ?? ((url) => new WebSocket(url)))(this.options.url);
      } catch {
        this.failBeforeStart(
          new SocketError("connection_failed", "Could not connect to the transcription server."),
        );
        return;
      }
      this.ws = ws;
      ws.binaryType = "arraybuffer";
      ws.onopen = () => {
        if (this.phase !== "connecting") return;
        this.setPhase("authenticating");
        // The token travels in this first message, never in the URL (kept out of logs).
        const start: StartMessage = {
          type: "start",
          token,
          speech_mode: this.options.start.speech_mode,
          script_preference: this.options.start.script_preference,
          format: "pcm_s16le",
          sample_rate: 16000,
          channels: 1,
          ...(this.options.start.title ? { title: this.options.start.title } : {}),
        };
        ws.send(JSON.stringify(start));
      };
      ws.onmessage = (event: MessageEvent) => this.onMessage(event);
      ws.onclose = (event: CloseEvent) => this.onClose(event.code);
      ws.onerror = () => undefined; // a close event always follows
      this.handshakeTimer = setTimeout(() => {
        this.failBeforeStart(
          new SocketError("handshake_timeout", "The transcription server did not respond in time."),
        );
      }, this.options.handshakeTimeoutMs ?? 10_000);
    });
  }

  /** Queue one audio frame for sending. Returns false if the recording is not streaming. */
  sendAudio(pcm: ArrayBuffer): boolean {
    if (this.phase !== "streaming" || !this.session || pcm.byteLength === 0) return false;
    for (const chunk of splitFrame(pcm, this.session.max_frame_bytes)) {
      this.queue.push(chunk);
      this.queuedBytes += chunk.byteLength;
    }
    this.pump();
    if (!this.ended && this.backlogMs > this.limits.maxBacklogMs) {
      this.interrupt(
        new SocketError(
          "backpressure",
          "The connection is too slow to stream audio, so the recording was stopped. " +
            "Text received so far is kept.",
        ),
      );
      return false;
    }
    return true;
  }

  /** Send every queued frame, then {"type":"stop"}; session_saved ends the session. */
  finish(): void {
    if (this.phase !== "streaming") return;
    this.setPhase("stopping");
    this.pump();
    this.finishTimer = setTimeout(() => {
      this.interrupt(
        new SocketError(
          "finish_timeout",
          "The server did not confirm the end of the recording in time.",
        ),
      );
    }, this.options.finishTimeoutMs ?? 120_000);
  }

  /** Close immediately. Before session_started the connect() promise rejects. */
  abort(message = "Recording was cancelled."): void {
    if (this.ended) return;
    const error = new SocketError("aborted", message);
    if (!this.session) this.failBeforeStart(error);
    else this.interrupt(error);
  }

  // ----- internals

  private setPhase(phase: SocketPhase): void {
    if (this.phase === phase) return;
    this.phase = phase;
    this.options.onPhase?.(phase);
  }

  private pump(): void {
    const ws = this.ws;
    if (!ws || ws.readyState !== OPEN) return;
    while (this.queue.length > 0) {
      const chunk = this.queue[0];
      if (!chunk) break;
      if (ws.bufferedAmount >= this.limits.highWaterBytes) break;
      if (!this.pacer.take(chunk.byteLength)) break;
      this.queue.shift();
      this.queuedBytes -= chunk.byteLength;
      ws.send(chunk);
      this.sentBytes += chunk.byteLength;
    }
    if (this.queue.length > 0) {
      this.pumpTimer ??= setTimeout(() => {
        this.pumpTimer = null;
        this.pump();
      }, PUMP_INTERVAL_MS);
      return;
    }
    if (this.phase === "stopping" && !this.stopSent) {
      this.stopSent = true;
      ws.send(JSON.stringify({ type: "stop" }));
    }
  }

  private onMessage(event: MessageEvent): void {
    if (typeof event.data !== "string") return; // the server only sends JSON text
    const message = parseServerMessage(event.data);
    if (!message) {
      this.options.onWarning?.("Received an unreadable message from the server; it was ignored.");
      return;
    }
    switch (message.type) {
      case "session_started":
        if (this.phase !== "authenticating") return;
        this.clearTimer("handshake");
        this.session = message;
        this.setPhase("streaming");
        this.options.onMessage?.(message);
        this.pending?.resolve(message);
        this.pending = null;
        return;
      case "error":
        this.lastError = message;
        if (message.fatal) this.fatalError = message;
        break;
      case "session_saved":
        this.options.onMessage?.(message);
        this.complete(message);
        return;
      default:
        break;
    }
    this.options.onMessage?.(message);
  }

  private onClose(code: number): void {
    this.ws = null;
    this.clearAllTimers();
    if (this.ended) {
      this.setPhase("closed");
      return;
    }
    const message = describeClose(code, this.fatalError);
    if (!this.session) {
      const rejected = this.fatalError !== null || (code >= 4000 && code < 5000) || code === 1008;
      this.failBeforeStart(
        new SocketError(
          rejected ? "rejected" : "connection_failed",
          code === 1006 && !this.fatalError
            ? "Could not connect to the transcription server."
            : message,
          code,
          this.fatalError?.code ?? null,
        ),
      );
      return;
    }
    this.interrupt(
      new SocketError("connection_lost", message, code, this.fatalError?.code ?? null),
    );
  }

  private complete(saved: SessionSaved): void {
    if (this.ended) return;
    this.ended = true;
    this.clearAllTimers();
    this.queue = [];
    this.queuedBytes = 0;
    this.options.onEnd?.({ kind: "saved", saved, error: this.fatalError ?? this.lastError });
    // The server closes right after session_saved; close ourselves if it does not.
    const ws = this.ws;
    if (ws) {
      this.closeTimer = setTimeout(() => {
        ws.close(1000);
      }, 3_000);
    } else {
      this.setPhase("closed");
    }
  }

  private interrupt(error: SocketError): void {
    if (this.ended) return;
    this.ended = true;
    this.clearAllTimers();
    this.queue = [];
    this.queuedBytes = 0;
    this.closeSocket();
    this.setPhase("closed");
    this.options.onEnd?.({
      kind: "interrupted",
      transcriptionId: this.session?.transcription_id ?? "",
      error,
    });
  }

  private failBeforeStart(error: SocketError): void {
    if (this.ended) return;
    this.ended = true;
    this.clearAllTimers();
    this.closeSocket();
    this.setPhase("closed");
    this.pending?.reject(error);
    this.pending = null;
  }

  private closeSocket(): void {
    const ws = this.ws;
    this.ws = null;
    if (!ws) return;
    ws.onopen = null;
    ws.onmessage = null;
    ws.onclose = null;
    ws.onerror = null;
    try {
      ws.close(CLIENT_CLOSE_CODE);
    } catch {
      // already closing
    }
  }

  private clearTimer(which: "handshake" | "finish"): void {
    if (which === "handshake" && this.handshakeTimer) {
      clearTimeout(this.handshakeTimer);
      this.handshakeTimer = null;
    }
    if (which === "finish" && this.finishTimer) {
      clearTimeout(this.finishTimer);
      this.finishTimer = null;
    }
  }

  private clearAllTimers(): void {
    this.clearTimer("handshake");
    this.clearTimer("finish");
    if (this.pumpTimer) clearTimeout(this.pumpTimer);
    this.pumpTimer = null;
    if (this.closeTimer) clearTimeout(this.closeTimer);
    this.closeTimer = null;
  }
}
