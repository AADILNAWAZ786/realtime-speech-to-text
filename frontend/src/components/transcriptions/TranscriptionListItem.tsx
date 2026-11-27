import { Clock, Eye, Pencil } from "lucide-react";
import { Link } from "react-router";

import type { TranscriptionSummary } from "../../api/types";
import { formatDateTime, formatDuration } from "../../lib/format";
import { ButtonLink } from "../ui/Button";
import { Skeleton } from "../ui/Surface";
import { ModeBadge, SourceBadge, StatusBadge } from "./Badges";

export function TranscriptionListItem({ item }: { item: TranscriptionSummary }) {
  return (
    <li className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-start sm:gap-6 sm:px-6">
      <div className="min-w-0 flex-1">
        <Link
          to={`/transcriptions/${item.id}`}
          className="text-sm font-semibold text-slate-900 hover:text-indigo-700"
        >
          {item.title}
        </Link>
        <p className="mt-1 line-clamp-2 text-sm text-slate-600">
          {item.preview || <span className="italic text-slate-400">No text yet</span>}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
          <StatusBadge status={item.status} />
          <ModeBadge mode={item.speech_mode} />
          <SourceBadge source={item.source} />
          <span className="inline-flex items-center gap-1">
            <time dateTime={item.created_at}>{formatDateTime(item.created_at)}</time>
          </span>
          <span className="inline-flex items-center gap-1" title="Duration">
            <Clock className="size-3.5" aria-hidden="true" />
            {formatDuration(item.duration_ms)}
          </span>
        </div>
      </div>
      <div className="flex shrink-0 gap-2">
        <ButtonLink
          to={`/transcriptions/${item.id}`}
          variant="secondary"
          size="sm"
          icon={<Eye className="size-4" aria-hidden="true" />}
          aria-label={`View ${item.title}`}
        >
          View
        </ButtonLink>
        <ButtonLink
          to={`/transcriptions/${item.id}?edit=1`}
          variant="ghost"
          size="sm"
          icon={<Pencil className="size-4" aria-hidden="true" />}
          aria-label={`Edit ${item.title}`}
        >
          Edit
        </ButtonLink>
      </div>
    </li>
  );
}

export function TranscriptionListSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <ul aria-label="Loading transcriptions" aria-busy="true" className="divide-y divide-slate-200">
      {Array.from({ length: rows }, (_, i) => (
        <li key={i} className="space-y-2 px-4 py-4 sm:px-6">
          <Skeleton className="h-4 w-1/3" />
          <Skeleton className="h-3 w-full" />
          <Skeleton className="h-3 w-2/3" />
          <div className="flex gap-2 pt-1">
            <Skeleton className="h-5 w-20 rounded-full" />
            <Skeleton className="h-5 w-16 rounded-full" />
          </div>
        </li>
      ))}
    </ul>
  );
}
