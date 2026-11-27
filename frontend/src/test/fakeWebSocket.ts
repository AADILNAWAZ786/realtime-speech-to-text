/**
 * WebSocket double that plays the server side of /ws/transcribe in tests.
 * Install with vi.stubGlobal("WebSocket", FakeWebSocket).
 */
export class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  binaryType: BinaryType = "blob";
  bufferedAmount = 0;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  /** Everything the client sent, in order. */
  sent: (string | ArrayBuffer)[] = [];
  closedByClient: { code?: number; reason?: string } | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  static latest(): FakeWebSocket {
    const ws = FakeWebSocket.instances.at(-1);
    if (!ws) throw new Error("no WebSocket was created");
    return ws;
  }

  send(data: string | ArrayBuffer): void {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error("WebSocket is not open");
    this.sent.push(data);
  }

  close(code?: number, reason?: string): void {
    if (this.readyState >= FakeWebSocket.CLOSING) return;
    this.closedByClient = {
      ...(code === undefined ? {} : { code }),
      ...(reason === undefined ? {} : { reason }),
    };
    this.readyState = FakeWebSocket.CLOSING;
  }

  // ----- server side

  /** Accept the connection. */
  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event("open"));
  }

  /** Server sends a JSON message (or raw text). */
  receive(message: object | string): void {
    const data = typeof message === "string" ? message : JSON.stringify(message);
    this.onmessage?.({ data } as MessageEvent);
  }

  /** Server (or network) closes the connection. */
  serverClose(code = 1000): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason: "", wasClean: code === 1000 } as CloseEvent);
  }

  get textMessages(): Record<string, unknown>[] {
    return this.sent
      .filter((m): m is string => typeof m === "string")
      .map((m) => JSON.parse(m) as Record<string, unknown>);
  }

  get audioFrames(): ArrayBuffer[] {
    return this.sent.filter((m): m is ArrayBuffer => typeof m !== "string");
  }

  get audioBytes(): number {
    return this.audioFrames.reduce((n, f) => n + f.byteLength, 0);
  }
}

export const sessionStarted = (overrides: Record<string, unknown> = {}) => ({
  type: "session_started",
  transcription_id: "11111111-2222-4333-8444-555555555555",
  speech_mode: "mixed",
  script_preference: "roman",
  model_name: "timeline-fake",
  audio: { format: "pcm_s16le", sample_rate: 16000, channels: 1 },
  max_frame_bytes: 65536,
  max_duration_ms: 1_800_000,
  ...overrides,
});

export const sessionSaved = (overrides: Record<string, unknown> = {}) => ({
  type: "session_saved",
  transcription_id: "11111111-2222-4333-8444-555555555555",
  status: "completed",
  end_reason: "stopped",
  duration_ms: 6900,
  final_count: 2,
  saved_segments: 2,
  unsaved_segments: [],
  failed_utterances: 0,
  record_saved: true,
  text: "aaj ki meeting cancel ho gai kal phir call karate hain",
  ...overrides,
});
