import {
  LIMITS,
  SPEECH_MODES,
  TRANSCRIPTION_SOURCES,
  TRANSCRIPTION_STATUSES,
  type TranscriptionListParams,
} from "../api/types";

export const DEFAULT_PAGE_SIZE = 10;
export const PAGE_SIZE_OPTIONS = [10, 20, 50] as const;

function oneOf<T extends string>(allowed: readonly T[], value: string | null): T | undefined {
  return allowed.find((v) => v === value);
}

function positiveInt(value: string | null, fallback: number, max = Number.MAX_SAFE_INTEGER) {
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 ? Math.min(n, max) : fallback;
}

/** Dashboard URL (?page=2&q=...) -> API list params. Invalid values fall back to defaults. */
export function paramsFromSearch(
  search: URLSearchParams,
): Required<Pick<TranscriptionListParams, "page" | "page_size">> & TranscriptionListParams {
  const q = search.get("q")?.trim().slice(0, LIMITS.searchMax);
  const speech_mode = oneOf(SPEECH_MODES, search.get("speech_mode"));
  const status = oneOf(TRANSCRIPTION_STATUSES, search.get("status"));
  const source = oneOf(TRANSCRIPTION_SOURCES, search.get("source"));
  return {
    page: positiveInt(search.get("page"), 1),
    page_size: positiveInt(search.get("page_size"), DEFAULT_PAGE_SIZE, LIMITS.pageSizeMax),
    ...(q ? { q } : {}),
    ...(speech_mode ? { speech_mode } : {}),
    ...(status ? { status } : {}),
    ...(source ? { source } : {}),
  };
}
