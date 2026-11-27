import { Search, X } from "lucide-react";
import { useEffect, useState } from "react";

import {
  LIMITS,
  SPEECH_MODES,
  TRANSCRIPTION_SOURCES,
  TRANSCRIPTION_STATUSES,
  type SpeechMode,
  type TranscriptionSource,
  type TranscriptionStatus,
} from "../../api/types";
import { useDebouncedValue } from "../../hooks/useDebouncedValue";
import { SOURCE_LABELS, SPEECH_MODE_SHORT, STATUS_LABELS } from "../../lib/format";

export interface Filters {
  q: string;
  speech_mode: SpeechMode | "";
  status: TranscriptionStatus | "";
  source: TranscriptionSource | "";
}

interface Props {
  value: Filters;
  onChange: (next: Partial<Filters>) => void;
}

const select =
  "h-10 rounded-lg border-0 bg-white px-3 text-sm text-slate-700 shadow-sm ring-1 ring-inset ring-slate-300 " +
  "focus:ring-2 focus:ring-indigo-600";

export const SEARCH_DEBOUNCE_MS = 300;

export function TranscriptionFilters({ value, onChange }: Props) {
  const [search, setSearch] = useState(value.q);
  const debounced = useDebouncedValue(search, SEARCH_DEBOUNCE_MS);

  // Follow external changes to the query (back/forward, "Clear filters").
  const [syncedQ, setSyncedQ] = useState(value.q);
  if (syncedQ !== value.q) {
    setSyncedQ(value.q);
    setSearch(value.q);
  }
  useEffect(() => {
    if (debounced.trim() !== value.q) onChange({ q: debounced.trim() });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only react to the debounced text
  }, [debounced]);

  return (
    <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
      <div className="relative flex-1">
        <Search
          className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-slate-400"
          aria-hidden="true"
        />
        <input
          type="search"
          aria-label="Search transcriptions"
          placeholder="Search titles and text…"
          value={search}
          maxLength={LIMITS.searchMax}
          onChange={(event) => setSearch(event.target.value)}
          className="h-10 w-full rounded-lg border-0 bg-white pr-9 pl-9 text-sm shadow-sm ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 focus:ring-2 focus:ring-indigo-600"
        />
        {search && (
          <button
            type="button"
            onClick={() => {
              setSearch("");
              onChange({ q: "" });
            }}
            className="absolute top-1/2 right-2 -translate-y-1/2 rounded p-1 text-slate-400 hover:text-slate-600"
            aria-label="Clear search"
          >
            <X className="size-4" />
          </button>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 lg:flex">
        <select
          aria-label="Filter by language mode"
          className={select}
          value={value.speech_mode}
          onChange={(event) =>
            onChange({ speech_mode: event.target.value as Filters["speech_mode"] })
          }
        >
          <option value="">All modes</option>
          {SPEECH_MODES.map((mode) => (
            <option key={mode} value={mode}>
              {SPEECH_MODE_SHORT[mode]}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by status"
          className={select}
          value={value.status}
          onChange={(event) => onChange({ status: event.target.value as Filters["status"] })}
        >
          <option value="">All statuses</option>
          {TRANSCRIPTION_STATUSES.map((status) => (
            <option key={status} value={status}>
              {STATUS_LABELS[status]}
            </option>
          ))}
        </select>
        <select
          aria-label="Filter by source"
          className={select}
          value={value.source}
          onChange={(event) => onChange({ source: event.target.value as Filters["source"] })}
        >
          <option value="">All sources</option>
          {TRANSCRIPTION_SOURCES.map((source) => (
            <option key={source} value={source}>
              {SOURCE_LABELS[source]}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
