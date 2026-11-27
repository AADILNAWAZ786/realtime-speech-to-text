/**
 * Types for /ws/transcribe, mirroring docs/websocket-protocol.md and
 * backend/app/ws/protocol.py + connection.py. Field names are identical.
 */
import type { ScriptPreference, SpeechMode, TranscriptionStatus } from "../api/types";

export const WS_PATH = "/ws/transcribe";

// ----- client -> server

export interface StartMessage {
  type: "start";
  token: string;
  speech_mode: SpeechMode;
  script_preference: ScriptPreference;
  format: "pcm_s16le";
  sample_rate: 16000;
  channels: 1;
  title?: string;
}

export interface StopMessage {
  type: "stop";
}

// ----- server -> client

export interface SessionStarted {
  type: "session_started";
  transcription_id: string;
  speech_mode: SpeechMode;
  script_preference: ScriptPreference;
  model_name: string;
  audio: { format: "pcm_s16le"; sample_rate: number; channels: number };
  max_frame_bytes: number;
  max_duration_ms: number;
}

export interface PartialMessage {
  type: "partial";
  utterance_id: number;
  version: number;
  text: string;
  raw_text: string;
  start_ms: number;
  end_ms: number;
}

export interface FinalMessage {
  type: "final";
  seq: number;
  utterance_id: number;
  text: string;
  raw_text: string;
  start_ms: number;
  end_ms: number;
  language: string | null;
  avg_logprob: number | null;
  commit_reason: string;
  saved: boolean;
}

export interface PreviewPaused {
  type: "preview_paused";
  backlog_ms: number;
}

export interface PreviewResumed {
  type: "preview_resumed";
  backlog_ms: number;
}

export interface UtteranceDiscarded {
  type: "utterance_discarded";
  utterance_id: number;
  start_ms: number;
  end_ms: number;
  reason: string;
}

export interface UtteranceFailed {
  type: "utterance_failed";
  utterance_id: number;
  start_ms: number;
  end_ms: number;
  code: string;
}

export type EndReason =
  | "stopped"
  | "recording_limit"
  | "disconnected"
  | "idle_timeout"
  | "rate_limited"
  | "protocol_error"
  | "overloaded"
  | "shutdown"
  | "internal_error"
  | "provider_error"; // cloud speech provider (Gemini Live) failed mid-recording

export interface SessionSaved {
  type: "session_saved";
  transcription_id: string;
  status: Exclude<TranscriptionStatus, "recording">;
  end_reason: EndReason;
  duration_ms: number;
  final_count: number;
  saved_segments: number;
  unsaved_segments: number[];
  failed_utterances: number;
  record_saved: boolean;
  text: string;
}

export type ServerErrorCode =
  | "auth_timeout"
  | "auth_required"
  | "invalid_token"
  | "token_expired"
  | "invalid_message"
  | "invalid_audio"
  | "frame_too_large"
  | "rate_limited"
  | "recording_in_progress"
  | "server_busy"
  | "server_overloaded"
  | "idle_timeout"
  | "server_shutdown"
  | "persistence_failed"
  | "internal_error"
  | "transcription_unavailable"
  | "transcription_quota_exceeded";

export interface ServerError {
  type: "error";
  code: ServerErrorCode | (string & {});
  message: string;
  fatal: boolean;
  seq?: number;
  unsaved_segments?: number[];
}

export type ServerMessage =
  | SessionStarted
  | PartialMessage
  | FinalMessage
  | PreviewPaused
  | PreviewResumed
  | UtteranceDiscarded
  | UtteranceFailed
  | SessionSaved
  | ServerError;

const TYPES = new Set<ServerMessage["type"]>([
  "session_started",
  "partial",
  "final",
  "preview_paused",
  "preview_resumed",
  "utterance_discarded",
  "utterance_failed",
  "session_saved",
  "error",
]);

/** Parse a text frame. Returns null for invalid JSON or an unknown message type. */
export function parseServerMessage(text: string): ServerMessage | null {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof data !== "object" || data === null) return null;
  const type = (data as { type?: unknown }).type;
  return typeof type === "string" && TYPES.has(type as ServerMessage["type"])
    ? (data as ServerMessage)
    : null;
}

/**
 * The WebSocket URL from the REST base URL (VITE_API_BASE_URL). The WebSocket route is
 * mounted at the server root, next to the REST prefix:
 *   http://localhost:8000/api/v1        -> ws://localhost:8000/ws/transcribe
 *   https://example.com/stt/api/v1      -> wss://example.com/stt/ws/transcribe
 */
export function webSocketUrl(apiBaseUrl: string): string {
  const url = new URL(apiBaseUrl);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error(`Unsupported API URL protocol: ${url.protocol}`);
  }
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  const root = url.pathname.replace(/\/+$/, "").replace(/\/api\/v\d+$/, "");
  url.pathname = `${root}${WS_PATH}`;
  url.search = "";
  url.hash = "";
  return url.toString();
}

// ----- close codes (docs/websocket-protocol.md, "Errors and close codes")

export const CLOSE_CODE_MESSAGES: Record<number, string> = {
  1006: "The connection to the transcription server was lost.",
  1008: "The transcription server refused this site (origin not allowed).",
  1009: "An audio frame was too large for the server.",
  1011: "The transcription server had an internal error.",
  1012: "The transcription server is restarting. Try again in a moment.",
  1013: "The transcription server is busy. Try again in a moment.",
  4400: "The server rejected a message from this page.",
  4401: "Your session has expired. Sign in again to record.",
  4408: "The connection timed out.",
  4409: "You already have a recording in progress (perhaps in another tab).",
  4429: "Audio was sent faster than real time.",
};

/** User-facing text for a server error code. */
export const ERROR_CODE_MESSAGES: Partial<Record<ServerErrorCode, string>> = {
  auth_timeout: "The server did not receive the start message in time.",
  auth_required: "The recording was not authenticated.",
  invalid_token: "Your session is no longer valid. Sign in again to record.",
  token_expired: "Your session has expired. Sign in again to record.",
  invalid_message: "The server rejected a message from this page.",
  invalid_audio: "The server rejected the audio format.",
  frame_too_large: "An audio frame was too large for the server.",
  rate_limited: "Audio was sent faster than real time.",
  recording_in_progress: "You already have a recording in progress (perhaps in another tab).",
  server_busy: "Too many recordings are running on the server. Try again in a moment.",
  server_overloaded: "Transcription fell too far behind, so the server stopped the recording.",
  idle_timeout: "No audio reached the server for too long, so the recording was stopped.",
  server_shutdown: "The server is restarting, so the recording was stopped.",
  persistence_failed: "Some transcript segments could not be saved.",
  internal_error: "The transcription server had an internal error.",
  transcription_unavailable:
    "The speech recognition service is unavailable right now. Try again in a moment.",
  transcription_quota_exceeded: "The speech recognition service is at capacity. Try again later.",
};
