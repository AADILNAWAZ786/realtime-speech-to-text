/**
 * Types mirroring the backend contracts (backend/app/schemas/*.py, app/models/enums.py).
 * Keep in sync with the API: field names and value sets are identical.
 */

export type SpeechMode = "auto" | "en" | "hi" | "mixed";
export type ScriptPreference = "as_recognised" | "roman" | "devanagari";
export type TranscriptionSource = "live" | "manual";
export type TranscriptionStatus = "recording" | "completed" | "interrupted";

export const SPEECH_MODES: readonly SpeechMode[] = ["auto", "en", "hi", "mixed"];
export const SCRIPT_PREFERENCES: readonly ScriptPreference[] = [
  "as_recognised",
  "roman",
  "devanagari",
];
export const TRANSCRIPTION_SOURCES: readonly TranscriptionSource[] = ["live", "manual"];
export const TRANSCRIPTION_STATUSES: readonly TranscriptionStatus[] = [
  "recording",
  "completed",
  "interrupted",
];

/** Validation limits enforced by the backend (schemas/auth.py, models/enums.py). */
export const LIMITS = {
  emailMax: 320,
  passwordMin: 8,
  passwordMax: 128,
  displayNameMax: 100,
  titleMax: 200,
  textMax: 100_000,
  searchMax: 200,
  pageSizeMax: 100,
} as const;

// ----- auth (schemas/auth.py, schemas/user.py)

export interface User {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string;
}

export interface RegisterRequest {
  email: string;
  password: string;
  /** Optional; the API also accepts null / "" / whitespace (stored as null). The form omits it. */
  display_name?: string | null;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  /** Token lifetime in seconds. */
  expires_in: number;
}

// ----- transcriptions (schemas/transcription.py)

interface TranscriptionBase {
  id: string;
  title: string;
  speech_mode: SpeechMode;
  script_preference: ScriptPreference;
  source: TranscriptionSource;
  status: TranscriptionStatus;
  detected_language: string | null;
  duration_ms: number | null;
  is_edited: boolean;
  created_at: string;
  updated_at: string;
}

/** Dashboard list item: a 200-character `preview`, not the full text. */
export interface TranscriptionSummary extends TranscriptionBase {
  preview: string;
}

/** Raw, immutable model output for one finalised utterance. */
export interface Segment {
  seq: number;
  start_ms: number;
  end_ms: number;
  raw_text: string;
  language: string | null;
  avg_logprob: number | null;
}

export interface Transcription extends TranscriptionBase {
  /** The user-editable text (script-rendered for live recordings). */
  text: string;
  decode_strategy: string | null;
  model_name: string | null;
  /** Present only when requested with include_segments=true. */
  segments: Segment[] | null;
}

export interface TranscriptionPage {
  items: TranscriptionSummary[];
  total: number;
  page: number;
  page_size: number;
  pages: number;
}

export interface TranscriptionCreate {
  title?: string;
  text: string;
  speech_mode?: SpeechMode;
  script_preference?: ScriptPreference;
}

/** At least one field is required; null is rejected by the API. */
export interface TranscriptionUpdate {
  title?: string;
  text?: string;
}

export interface TranscriptionListParams {
  page?: number;
  page_size?: number;
  q?: string;
  speech_mode?: SpeechMode;
  status?: TranscriptionStatus;
  source?: TranscriptionSource;
}

// ----- errors (core/exceptions.py): {"detail", "code", "errors"?}

export interface ApiValidationIssue {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface ApiErrorBody {
  detail: string;
  code: string;
  errors?: ApiValidationIssue[];
}
