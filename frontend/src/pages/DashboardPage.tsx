import { CheckCircle2, FilePlus2, FileText, Mic, PenLine, SearchX } from "lucide-react";
import { useEffect, type ReactNode } from "react";
import { useSearchParams } from "react-router";

import { errorMessage } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import {
  TranscriptionFilters,
  type Filters,
} from "../components/transcriptions/TranscriptionFilters";
import {
  TranscriptionListItem,
  TranscriptionListSkeleton,
} from "../components/transcriptions/TranscriptionListItem";
import { ButtonLink, Button } from "../components/ui/Button";
import { EmptyState, ErrorState } from "../components/ui/ErrorState";
import { Pagination } from "../components/ui/Pagination";
import { Card, PageHeader, Skeleton } from "../components/ui/Surface";
import { useTranscriptionList, useTranscriptionStats } from "../hooks/useTranscriptions";
import { DEFAULT_PAGE_SIZE, PAGE_SIZE_OPTIONS, paramsFromSearch } from "../lib/listParams";

function StatCard({
  label,
  value,
  icon,
  loading,
}: {
  label: string;
  value: number | undefined;
  icon: ReactNode;
  loading: boolean;
}) {
  return (
    <Card className="flex items-center gap-4 p-3 sm:p-5">
      <span className="hidden size-10 shrink-0 items-center justify-center rounded-lg bg-indigo-50 text-indigo-600 sm:flex">
        {icon}
      </span>
      <div>
        <p className="text-xs text-slate-600 sm:text-sm">{label}</p>
        {loading ? (
          <Skeleton className="mt-1 h-7 w-12" />
        ) : (
          <p
            className="text-2xl font-semibold text-slate-900"
            aria-label={`${label}: ${value ?? "unavailable"}`}
          >
            {value ?? "—"}
          </p>
        )}
      </div>
    </Card>
  );
}

export function DashboardPage() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();
  const params = paramsFromSearch(searchParams);
  const list = useTranscriptionList(params);
  const stats = useTranscriptionStats();

  const filters: Filters = {
    q: params.q ?? "",
    speech_mode: params.speech_mode ?? "",
    status: params.status ?? "",
    source: params.source ?? "",
  };
  const filtered = Boolean(params.q ?? params.speech_mode ?? params.status ?? params.source);

  const update = (changes: Record<string, string | number | undefined>, resetPage = true) => {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of Object.entries(changes)) {
          if (value === undefined || value === "") next.delete(key);
          else next.set(key, String(value));
        }
        if (resetPage) next.delete("page");
        if (next.get("page") === "1") next.delete("page");
        if (next.get("page_size") === String(DEFAULT_PAGE_SIZE)) next.delete("page_size");
        return next;
      },
      { replace: resetPage },
    );
  };

  // A page past the end (e.g. after deleting the last item on it): go to the last page.
  const pages = list.data?.pages;
  useEffect(() => {
    if (pages !== undefined && pages > 0 && params.page > pages) {
      update({ page: pages }, false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `update` is recreated every render
  }, [pages, params.page]);

  const greeting = user?.display_name ? `Welcome back, ${user.display_name}` : "Dashboard";

  return (
    <div className="space-y-8">
      <PageHeader
        title={greeting}
        description="Your recordings and transcriptions, newest first."
        actions={
          <>
            <ButtonLink to="/record" icon={<Mic className="size-4" aria-hidden="true" />}>
              New recording
            </ButtonLink>
            <ButtonLink
              to="/transcriptions/new"
              variant="secondary"
              icon={<FilePlus2 className="size-4" aria-hidden="true" />}
            >
              New manual transcription
            </ButtonLink>
          </>
        }
      />

      <section aria-label="Statistics" className="grid grid-cols-3 gap-2 sm:gap-4">
        <StatCard
          label="Total transcriptions"
          value={stats.data?.total}
          loading={stats.isPending}
          icon={<FileText className="size-5" aria-hidden="true" />}
        />
        <StatCard
          label="Completed recordings"
          value={stats.data?.completedRecordings}
          loading={stats.isPending}
          icon={<CheckCircle2 className="size-5" aria-hidden="true" />}
        />
        <StatCard
          label="Manual transcriptions"
          value={stats.data?.manual}
          loading={stats.isPending}
          icon={<PenLine className="size-5" aria-hidden="true" />}
        />
      </section>

      <section aria-labelledby="recent-heading" className="space-y-4">
        <h2 id="recent-heading" className="text-lg font-semibold text-slate-900">
          Recent transcriptions
        </h2>
        <TranscriptionFilters value={filters} onChange={(changes) => update({ ...changes })} />

        <Card className="overflow-hidden">
          {list.isPending ? (
            <TranscriptionListSkeleton />
          ) : list.isError ? (
            <ErrorState
              title="Could not load transcriptions"
              message={errorMessage(list.error)}
              onRetry={() => void list.refetch()}
            />
          ) : list.data.items.length === 0 ? (
            filtered ? (
              <EmptyState
                icon={<SearchX className="size-6" aria-hidden="true" />}
                title="No matching transcriptions"
                description="Try a different search term or clear the filters."
                action={
                  <Button
                    variant="secondary"
                    onClick={() =>
                      update({
                        q: undefined,
                        speech_mode: undefined,
                        status: undefined,
                        source: undefined,
                      })
                    }
                  >
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <EmptyState
                icon={<FileText className="size-6" aria-hidden="true" />}
                title="No transcriptions yet"
                description="Record speech in English, Hindi or Hinglish, or type a transcription by hand."
                action={
                  <ButtonLink
                    to="/transcriptions/new"
                    icon={<FilePlus2 className="size-4" aria-hidden="true" />}
                  >
                    New manual transcription
                  </ButtonLink>
                }
              />
            )
          ) : (
            <ul
              aria-label="Transcriptions"
              aria-busy={list.isPlaceholderData || undefined}
              className={
                list.isPlaceholderData
                  ? "divide-y divide-slate-200 opacity-60"
                  : "divide-y divide-slate-200"
              }
            >
              {list.data.items.map((item) => (
                <TranscriptionListItem key={item.id} item={item} />
              ))}
            </ul>
          )}
        </Card>

        {list.data && list.data.total > 0 && (
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="flex-1">
              <Pagination
                page={list.data.page}
                pages={list.data.pages}
                total={list.data.total}
                pageSize={list.data.page_size}
                onPageChange={(page) => {
                  update({ page }, false);
                  window.scrollTo({ top: 0 });
                }}
              />
            </div>
            <label className="flex items-center gap-2 text-sm text-slate-600 sm:ml-4">
              Per page
              <select
                className="h-9 rounded-lg border-0 bg-white px-2 text-sm ring-1 ring-inset ring-slate-300"
                value={params.page_size}
                onChange={(event) => update({ page_size: event.target.value })}
              >
                {PAGE_SIZE_OPTIONS.map((size) => (
                  <option key={size} value={size}>
                    {size}
                  </option>
                ))}
              </select>
            </label>
          </div>
        )}
      </section>
    </div>
  );
}
