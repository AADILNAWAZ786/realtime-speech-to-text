import { Languages, Mic, PenLine } from "lucide-react";

import type { SpeechMode, TranscriptionSource, TranscriptionStatus } from "../../api/types";
import { SOURCE_LABELS, SPEECH_MODE_SHORT, STATUS_LABELS } from "../../lib/format";
import { Badge, type BadgeTone } from "../ui/Surface";

const STATUS_TONES: Record<TranscriptionStatus, BadgeTone> = {
  recording: "amber",
  completed: "green",
  interrupted: "red",
};

export function StatusBadge({ status }: { status: TranscriptionStatus }) {
  return (
    <Badge tone={STATUS_TONES[status]}>
      <span className="size-1.5 rounded-full bg-current" aria-hidden="true" />
      {STATUS_LABELS[status]}
    </Badge>
  );
}

export function ModeBadge({ mode }: { mode: SpeechMode }) {
  return (
    <Badge tone="indigo" icon={<Languages className="size-3" aria-hidden="true" />}>
      {SPEECH_MODE_SHORT[mode]}
    </Badge>
  );
}

export function SourceBadge({ source }: { source: TranscriptionSource }) {
  const Icon = source === "live" ? Mic : PenLine;
  return (
    <Badge icon={<Icon className="size-3" aria-hidden="true" />}>{SOURCE_LABELS[source]}</Badge>
  );
}
