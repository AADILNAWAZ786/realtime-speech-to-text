import { FileAudio } from "lucide-react";

import type { Segment, TranscriptionSource } from "../../api/types";
import { formatTimestamp } from "../../lib/format";

/** Raw recognised segments: read-only model output, kept separate from the editable text. */
export function SegmentList({
  segments,
  source,
}: {
  segments: Segment[];
  source: TranscriptionSource;
}) {
  if (segments.length === 0) {
    return (
      <div className="flex items-center gap-3 px-6 py-8 text-sm text-slate-500">
        <FileAudio className="size-5 text-slate-400" aria-hidden="true" />
        {source === "manual"
          ? "No recognised segments. Manual transcriptions have no audio."
          : "No speech was recognised in this recording."}
      </div>
    );
  }
  return (
    <ol aria-label="Recognised segments" className="divide-y divide-slate-200">
      {segments.map((segment) => (
        <li
          key={segment.seq}
          className="grid gap-1 px-6 py-3 sm:grid-cols-[7rem_1fr_auto] sm:gap-4"
        >
          <span className="font-mono text-xs text-slate-500">
            {formatTimestamp(segment.start_ms)}–{formatTimestamp(segment.end_ms)}
          </span>
          <p className="text-sm text-slate-800" lang={segment.language ?? undefined}>
            {segment.raw_text}
          </p>
          <span className="text-xs text-slate-400">
            #{segment.seq}
            {segment.language ? ` · ${segment.language}` : ""}
          </span>
        </li>
      ))}
    </ol>
  );
}
