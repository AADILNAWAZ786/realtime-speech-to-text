import { useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, FileQuestion, Pencil, Save, Trash2 } from "lucide-react";
import { useState, type SyntheticEvent, type ReactNode } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router";

import { ApiError, errorMessage } from "../api/client";
import { LIMITS, type Transcription, type TranscriptionUpdate } from "../api/types";
import { ModeBadge, SourceBadge, StatusBadge } from "../components/transcriptions/Badges";
import { SegmentList } from "../components/transcriptions/SegmentList";
import { Button, ButtonLink } from "../components/ui/Button";
import { ConfirmDialog } from "../components/ui/Dialog";
import { EmptyState, ErrorState } from "../components/ui/ErrorState";
import { InputField, TextAreaField } from "../components/ui/Field";
import { FormAlert } from "../components/ui/FormAlert";
import { Badge, Card, Skeleton } from "../components/ui/Surface";
import { useToast } from "../components/ui/Toast";
import {
  forgetTranscription,
  useDeleteTranscription,
  useTranscription,
  useUpdateTranscription,
} from "../hooks/useTranscriptions";
import {
  formatDateTime,
  formatDuration,
  languageName,
  SCRIPT_LABELS,
  SPEECH_MODE_LABELS,
} from "../lib/format";
import { hasErrors, validateText, validateTitle, type Errors } from "../lib/validation";

function BackLink() {
  return (
    <ButtonLink
      to="/"
      variant="ghost"
      size="sm"
      icon={<ArrowLeft className="size-4" aria-hidden="true" />}
    >
      Back to dashboard
    </ButtonLink>
  );
}

function Meta({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium tracking-wide text-slate-500 uppercase">{label}</dt>
      <dd className="mt-1 text-sm text-slate-900">{children}</dd>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading transcription" className="space-y-6">
      <Skeleton className="h-8 w-1/2" />
      <Card className="grid gap-6 p-6 sm:grid-cols-3">
        {Array.from({ length: 6 }, (_, i) => (
          <Skeleton key={i} className="h-10" />
        ))}
      </Card>
      <Card className="space-y-3 p-6">
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-3/4" />
      </Card>
    </div>
  );
}

type FieldName = "title" | "text";

function EditForm({ transcription, onDone }: { transcription: Transcription; onDone: () => void }) {
  const toast = useToast();
  const update = useUpdateTranscription(transcription.id);
  const [title, setTitle] = useState(transcription.title);
  const [text, setText] = useState(transcription.text);
  const [errors, setErrors] = useState<Errors<FieldName>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);

  const changes: TranscriptionUpdate = {
    ...(title.trim() !== transcription.title ? { title: title.trim() } : {}),
    ...(text !== transcription.text ? { text } : {}),
  };
  const dirty = Object.keys(changes).length > 0;

  const onSubmit = (event: SyntheticEvent) => {
    event.preventDefault();
    const next = { title: validateTitle(title, { required: true }), text: validateText(text) };
    setErrors(next);
    setFormError(null);
    if (hasErrors(next)) return;
    if (!dirty) {
      onDone();
      return;
    }
    update.mutate(changes, {
      onSuccess: () => {
        toast.show({ kind: "success", title: "Changes saved" });
        onDone();
      },
      onError: (error) => {
        if (error instanceof ApiError && error.status === 422) {
          setErrors({ title: error.fieldErrors.title, text: error.fieldErrors.text });
        }
        setFormError(errorMessage(error));
      },
    });
  };

  return (
    <form noValidate onSubmit={onSubmit} className="space-y-5 p-6" aria-label="Edit transcription">
      {formError && <FormAlert message={formError} />}
      <InputField
        label="Title"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        maxLength={LIMITS.titleMax}
        error={errors.title}
      />
      <TextAreaField
        label="Text"
        value={text}
        onChange={(e) => setText(e.target.value)}
        error={errors.text}
        hint="Your edits replace the transcript text. The recognised segments below stay unchanged."
      />
      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <Button
          variant="secondary"
          disabled={update.isPending}
          onClick={() => (dirty ? setConfirmDiscard(true) : onDone())}
        >
          Cancel
        </Button>
        <Button
          type="submit"
          loading={update.isPending}
          icon={<Save className="size-4" aria-hidden="true" />}
        >
          Save changes
        </Button>
      </div>
      <ConfirmDialog
        open={confirmDiscard}
        title="Discard your changes?"
        message="The edits you made to this transcription will be lost."
        confirmLabel="Discard changes"
        cancelLabel="Keep editing"
        destructive
        onConfirm={() => {
          setConfirmDiscard(false);
          onDone();
        }}
        onCancel={() => setConfirmDiscard(false)}
      />
    </form>
  );
}

export function TranscriptionDetailPage() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const editing = searchParams.get("edit") === "1";
  const [deleted, setDeleted] = useState(false);
  const query = useTranscription(id, { enabled: !deleted });
  const remove = useDeleteTranscription();
  const queryClient = useQueryClient();
  const [confirmDelete, setConfirmDelete] = useState(false);

  const setEditing = (on: boolean) => setSearchParams(on ? { edit: "1" } : {}, { replace: true });

  if (query.isPending) return <DetailSkeleton />;
  if (query.isError) {
    const notFound =
      query.error instanceof ApiError && (query.error.status === 404 || query.error.status === 422);
    return (
      <div className="space-y-4">
        <BackLink />
        <Card>
          {notFound ? (
            <EmptyState
              icon={<FileQuestion className="size-6" aria-hidden="true" />}
              title="Transcription not found"
              description="It may have been deleted, or the link is wrong."
            />
          ) : (
            <ErrorState
              title="Could not load the transcription"
              message={errorMessage(query.error)}
              onRetry={() => void query.refetch()}
            />
          )}
        </Card>
      </div>
    );
  }

  const t = query.data;
  const onDelete = () =>
    remove.mutate(t.id, {
      onSuccess: () => {
        toast.show({ kind: "success", title: "Transcription deleted" });
        setDeleted(true);
        void Promise.resolve(navigate("/", { replace: true })).then(() =>
          forgetTranscription(queryClient, t.id),
        );
      },
      onError: (error) => {
        setConfirmDelete(false);
        toast.show({ kind: "error", title: "Could not delete", description: errorMessage(error) });
      },
    });

  return (
    <div className="space-y-6">
      <BackLink />
      <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h1 className="text-2xl font-semibold tracking-tight break-words text-slate-900">
            {t.title}
          </h1>
          <div className="mt-2 flex flex-wrap gap-2">
            <StatusBadge status={t.status} />
            <ModeBadge mode={t.speech_mode} />
            <SourceBadge source={t.source} />
            {t.is_edited && (
              <Badge icon={<Pencil className="size-3" aria-hidden="true" />}>Edited</Badge>
            )}
          </div>
        </div>
        {!editing && (
          <div className="flex shrink-0 gap-2">
            <Button
              variant="secondary"
              icon={<Pencil className="size-4" aria-hidden="true" />}
              onClick={() => setEditing(true)}
            >
              Edit
            </Button>
            <Button
              variant="danger"
              icon={<Trash2 className="size-4" aria-hidden="true" />}
              onClick={() => setConfirmDelete(true)}
            >
              Delete
            </Button>
          </div>
        )}
      </div>

      <Card className="p-6">
        <dl className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3">
          <Meta label="Language mode">{SPEECH_MODE_LABELS[t.speech_mode]}</Meta>
          <Meta label="Output script">{SCRIPT_LABELS[t.script_preference]}</Meta>
          <Meta label="Detected language">{languageName(t.detected_language)}</Meta>
          <Meta label="Created">
            <time dateTime={t.created_at}>{formatDateTime(t.created_at)}</time>
          </Meta>
          <Meta label="Last updated">
            <time dateTime={t.updated_at}>{formatDateTime(t.updated_at)}</time>
          </Meta>
          <Meta label="Duration">{formatDuration(t.duration_ms)}</Meta>
        </dl>
      </Card>

      <section aria-labelledby="text-heading">
        <Card>
          <div className="border-b border-slate-200 px-6 py-4">
            <h2 id="text-heading" className="text-base font-semibold text-slate-900">
              Transcript
            </h2>
          </div>
          {editing ? (
            <EditForm transcription={t} onDone={() => setEditing(false)} />
          ) : t.text ? (
            <p className="px-6 py-5 text-[15px] leading-7 whitespace-pre-wrap break-words text-slate-800">
              {t.text}
            </p>
          ) : (
            <p className="px-6 py-5 text-sm text-slate-500 italic">No text was recognised.</p>
          )}
        </Card>
      </section>

      <section aria-labelledby="segments-heading">
        <Card>
          <div className="border-b border-slate-200 px-6 py-4">
            <h2 id="segments-heading" className="text-base font-semibold text-slate-900">
              Recognised segments
            </h2>
            <p className="mt-0.5 text-sm text-slate-500">
              Raw speech-recognition output with timestamps. Read-only, even after you edit the
              text.
            </p>
          </div>
          <SegmentList segments={t.segments ?? []} source={t.source} />
        </Card>
      </section>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete this transcription?"
        message={
          <>
            “{t.title}” and its recognised segments will be permanently deleted. This cannot be
            undone.
          </>
        }
        confirmLabel="Delete"
        destructive
        loading={remove.isPending}
        onConfirm={onDelete}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}
