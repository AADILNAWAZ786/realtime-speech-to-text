import { CloudOff } from "lucide-react";
import { useEffect, useRef } from "react";

import { formatTimestamp } from "../../lib/format";
import type { TranscriptState } from "../../realtime/transcript";

/**
 * Finals (committed, in seq order) in normal text; the current partials after them in
 * lighter italics. A partial line is replaced in place until its final arrives.
 */
export function LiveTranscript({
  transcript,
  active,
}: {
  transcript: TranscriptState;
  active: boolean;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const count = transcript.finals.length + transcript.partials.length;
  const lastPartial = transcript.partials.at(-1)?.text;
  useEffect(() => {
    const end = endRef.current;
    // scrollIntoView is missing in some non-browser environments (jsdom).
    if (end && "scrollIntoView" in end) end.scrollIntoView({ block: "nearest" });
  }, [count, lastPartial]);

  const empty = transcript.finals.length === 0 && transcript.partials.length === 0;
  return (
    <div
      className="mt-3 max-h-[28rem] min-h-40 overflow-y-auto rounded-lg bg-slate-50 p-4 ring-1 ring-inset ring-slate-200"
      aria-live="polite"
      aria-label="Transcript"
    >
      {empty ? (
        <p className="text-sm text-slate-400 italic">
          {active
            ? "Listening… text appears after you speak."
            : "The transcript will appear here while you speak."}
        </p>
      ) : (
        <ol className="space-y-2">
          {transcript.finals.map((final) => (
            <li
              key={`f-${final.seq}`}
              data-kind="final"
              data-seq={final.seq}
              className="flex gap-3"
            >
              <span className="w-12 shrink-0 pt-0.5 font-mono text-[11px] text-slate-400">
                {formatTimestamp(final.start_ms)}
              </span>
              <p
                className="min-w-0 text-[15px] leading-6 break-words text-slate-900"
                lang={final.language ?? undefined}
              >
                {final.text}
                {!final.saved && (
                  <span
                    className="ml-2 inline-flex items-center gap-1 align-middle text-xs font-medium text-amber-700"
                    title="The server could not store this segment yet; it retries before finishing."
                  >
                    <CloudOff className="size-3.5" aria-hidden="true" />
                    not saved yet
                  </span>
                )}
              </p>
            </li>
          ))}
          {transcript.partials.map((partial) => (
            <li
              key={`p-${partial.utterance_id}`}
              data-kind="partial"
              data-utterance={partial.utterance_id}
              className="flex gap-3"
            >
              <span className="w-12 shrink-0 pt-0.5 font-mono text-[11px] text-slate-300">
                {formatTimestamp(partial.start_ms)}
              </span>
              <p className="min-w-0 text-[15px] leading-6 break-words text-slate-500 italic">
                {partial.text}
                <span className="sr-only"> (provisional)</span>
              </p>
            </li>
          ))}
        </ol>
      )}
      {transcript.failed.length > 0 && (
        <p className="mt-3 text-xs text-red-700">
          {transcript.failed.length} part{transcript.failed.length > 1 ? "s" : ""} of the audio
          could not be transcribed.
        </p>
      )}
      <div ref={endRef} />
    </div>
  );
}
