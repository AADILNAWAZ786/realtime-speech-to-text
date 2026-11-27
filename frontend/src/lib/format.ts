import type {
  ScriptPreference,
  SpeechMode,
  TranscriptionSource,
  TranscriptionStatus,
} from "../api/types";

export const SPEECH_MODE_LABELS: Record<SpeechMode, string> = {
  auto: "Auto-detect",
  en: "English",
  hi: "Hindi",
  mixed: "Hindi + English (Hinglish)",
};

export const SPEECH_MODE_SHORT: Record<SpeechMode, string> = {
  auto: "Auto",
  en: "English",
  hi: "Hindi",
  mixed: "Hinglish",
};

export const SCRIPT_LABELS: Record<ScriptPreference, string> = {
  as_recognised: "As recognised",
  roman: "Roman (Latin script)",
  devanagari: "Devanagari",
};

export const STATUS_LABELS: Record<TranscriptionStatus, string> = {
  recording: "Recording",
  completed: "Completed",
  interrupted: "Interrupted",
};

export const SOURCE_LABELS: Record<TranscriptionSource, string> = {
  live: "Live recording",
  manual: "Manual",
};

const LANGUAGE_NAMES: Record<string, string> = { en: "English", hi: "Hindi", ur: "Urdu" };

export function languageName(code: string | null): string {
  if (!code) return "—";
  return LANGUAGE_NAMES[code] ? `${LANGUAGE_NAMES[code]} (${code})` : code;
}

/** 0 -> "0:00", 65_000 -> "1:05", 3_725_000 -> "1:02:05"; null -> "—". */
export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  const total = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`;
}

/** Segment timestamp: 83_450 -> "1:23.4". */
export function formatTimestamp(ms: number): string {
  const minutes = Math.floor(ms / 60_000);
  const seconds = ((ms % 60_000) / 1000).toFixed(1).padStart(4, "0");
  return `${minutes}:${seconds}`;
}

const dateTime = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

export function formatDateTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : dateTime.format(date);
}
