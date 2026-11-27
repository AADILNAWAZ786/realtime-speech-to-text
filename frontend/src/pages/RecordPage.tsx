import {
  AlertTriangle,
  CheckCircle2,
  CircleStop,
  ExternalLink,
  Info,
  Loader2,
  Mic,
  MicOff,
  RotateCcw,
  Wifi,
  WifiOff,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useBlocker } from "react-router";

import { LIMITS, type ScriptPreference, type SpeechMode } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { SCRIPT_OPTIONS, SPEECH_MODE_OPTIONS } from "../components/transcriptions/LanguageOptions";
import { LiveTranscript } from "../components/recording/LiveTranscript";
import { LevelMeter } from "../components/recording/LevelMeter";
import { Button, ButtonLink } from "../components/ui/Button";
import { ConfirmDialog } from "../components/ui/Dialog";
import { InputField, SelectField } from "../components/ui/Field";
import { Badge, Card, PageHeader, type BadgeTone } from "../components/ui/Surface";
import {
  ACTIVE_STATUSES,
  recordingOutcome,
  useLiveRecording,
  type RecordingOutcome,
  type RecordingResult,
  type RecordingStatus,
} from "../hooks/useLiveRecording";
import { cn } from "../lib/cn";
import { formatDuration } from "../lib/format";
import type { SocketPhase } from "../realtime/transcriptionSocket";

const STATUS_TEXT: Record<RecordingStatus, string> = {
  idle: "Ready to record",
  requesting_permission: "Waiting for microphone permission…",
  connecting: "Connecting to the transcription server…",
  recording: "Recording",
  finishing: "Processing final audio…",
  completed: "Recording saved",
  interrupted: "Recording interrupted",
  error: "Could not record",
};

const CONNECTION: Record<SocketPhase, { label: string; tone: BadgeTone; online: boolean }> = {
  idle: { label: "Not connected", tone: "slate", online: false },
  connecting: { label: "Connecting", tone: "amber", online: false },
  authenticating: { label: "Authenticating", tone: "amber", online: false },
  streaming: { label: "Connected", tone: "green", online: true },
  stopping: { label: "Finishing", tone: "indigo", online: true },
  closed: { label: "Disconnected", tone: "slate", online: false },
};

export function RecordPage() {
  const recorder = useLiveRecording();
  const { logout } = useAuth();
  const [speechMode, setSpeechMode] = useState<SpeechMode>("mixed");
  const [script, setScript] = useState<ScriptPreference>("as_recognised");
  const [title, setTitle] = useState("");
  const { status, active, problem, notice, result, transcript, session } = recorder;
  const busyStatus = ACTIVE_STATUSES.includes(status);
  const outcome =
    result && (status === "completed" || status === "interrupted")
      ? recordingOutcome(result)
      : null;

  // In-app navigation while recording: confirm, then stop cleanly before leaving.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      active && currentLocation.pathname !== nextLocation.pathname,
  );
  // Set when the user confirms leaving; cleared only once the router reports the blocker
  // is no longer "blocked". A flag cleared in confirmLeave() itself would let an effect that
  // captured the old "blocked" blocker call proceed() after reset() ("unblocked ->
  // proceeding" error).
  const leaveDecision = useRef(false);
  useEffect(() => {
    if (blocker.state !== "blocked") {
      leaveDecision.current = false;
      return;
    }
    // The recording ended by itself while the dialog was open: nothing left to protect.
    if (!active && !leaveDecision.current) blocker.proceed();
  }, [blocker, active]);

  const confirmLeave = async () => {
    if (blocker.state !== "blocked" || leaveDecision.current) return;
    leaveDecision.current = true;
    if (status === "recording") {
      // A clean stop finishes in the background after we leave. If stopping failed (the
      // last audio could not be flushed), stay so the user sees what happened; the
      // recording is no longer active, so the next navigation is not blocked.
      if (await recorder.stop()) blocker.proceed();
      else blocker.reset();
      return;
    }
    recorder.cancel();
    blocker.proceed();
  };

  const startRecording = () => void recorder.start({ speechMode, scriptPreference: script, title });

  const connection = CONNECTION[recorder.connection];
  const durationMs =
    result?.saved && (status === "completed" || status === "interrupted")
      ? result.saved.duration_ms
      : recorder.capturedMs;

  return (
    <div className="space-y-6">
      <PageHeader
        title="New recording"
        description="Speak in English, Hindi or a mix of both. The transcript appears as you talk."
      />

      <div className="grid gap-6 lg:grid-cols-[20rem_1fr]">
        <Card className="space-y-5 p-6">
          <h2 className="text-base font-semibold text-slate-900">Settings</h2>
          <SelectField
            label="Language"
            value={speechMode}
            options={SPEECH_MODE_OPTIONS}
            onChange={setSpeechMode}
            disabled={busyStatus}
            hint="Hinglish suits speech that switches between Hindi and English."
          />
          <SelectField
            label="Output script"
            value={script}
            options={SCRIPT_OPTIONS}
            onChange={setScript}
            disabled={busyStatus}
            hint="How Hindi words are written in the transcript."
          />
          <InputField
            label="Title (optional)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={LIMITS.titleMax}
            disabled={busyStatus}
            hint="Leave empty for a dated title."
          />
        </Card>

        <Card className="flex min-w-0 flex-col">
          {/* Control panel */}
          <div className="flex flex-col items-center gap-4 border-b border-slate-200 px-6 py-8 text-center">
            <div className="flex w-full flex-wrap items-center justify-between gap-2">
              <span
                className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-600"
                aria-live="polite"
              >
                {connection.online ? (
                  <Wifi className="size-3.5 text-emerald-600" aria-hidden="true" />
                ) : (
                  <WifiOff className="size-3.5 text-slate-400" aria-hidden="true" />
                )}
                <Badge tone={connection.tone}>{connection.label}</Badge>
              </span>
              {session && (
                <span className="text-xs text-slate-500">
                  {recorder.inputSampleRate
                    ? `${(recorder.inputSampleRate / 1000).toFixed(1)} kHz mic → 16 kHz · `
                    : ""}
                  model {session.model_name}
                </span>
              )}
            </div>

            <MicButton
              status={status}
              onStart={startRecording}
              onStop={() => void recorder.stop()}
            />

            <div>
              <p
                className={cn(
                  "font-mono text-3xl tabular-nums",
                  status === "recording" ? "text-slate-900" : "text-slate-400",
                )}
                aria-label="Recording time"
                role="timer"
              >
                {formatDuration(durationMs)}
              </p>
              <p
                className="mt-1 text-sm font-medium text-slate-700"
                role="status"
                data-testid="recording-status"
              >
                {outcome ? OUTCOME_TEXT[outcome] : STATUS_TEXT[status]}
              </p>
              {(status === "idle" || status === "error") && (
                <p className="mt-0.5 text-xs text-slate-500">
                  Press the microphone to start. Your browser will ask for permission.
                </p>
              )}
              {status === "recording" && (
                <p className="mt-0.5 text-xs text-slate-500">Press again to stop.</p>
              )}
            </div>

            <LevelMeter level={status === "recording" ? recorder.level : 0} />

            <div className="flex flex-wrap justify-center gap-2">
              {(status === "requesting_permission" || status === "connecting") && (
                <Button variant="secondary" onClick={recorder.cancel}>
                  Cancel
                </Button>
              )}
              {(status === "completed" || status === "interrupted") && (
                <Button
                  variant="secondary"
                  icon={<RotateCcw className="size-4" aria-hidden="true" />}
                  onClick={recorder.reset}
                >
                  New recording
                </Button>
              )}
            </div>
          </div>

          <div className="space-y-4 px-6 py-6">
            {problem && (
              <div
                role="alert"
                className="flex items-start gap-3 rounded-lg bg-red-50 p-4 text-sm text-red-800 ring-1 ring-inset ring-red-200"
              >
                <AlertTriangle className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
                <div className="space-y-2">
                  <p>{problem.message}</p>
                  {problem.needsSignIn && (
                    <Button size="sm" variant="secondary" onClick={logout}>
                      Sign in again
                    </Button>
                  )}
                </div>
              </div>
            )}
            {notice && (
              <div
                role="note"
                className="flex items-start gap-3 rounded-lg bg-amber-50 p-4 text-sm text-amber-900 ring-1 ring-inset ring-amber-200"
              >
                <Info className="mt-0.5 size-5 shrink-0" aria-hidden="true" />
                <p>{notice}</p>
              </div>
            )}
            {transcript.previewPaused && status === "recording" && (
              <p className="flex items-center gap-2 text-xs text-slate-500" role="status">
                <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
                Live preview paused while the server catches up. Final text will still arrive.
              </p>
            )}
            {recorder.warnings.map((warning) => (
              <p key={warning} className="text-xs text-amber-700">
                {warning}
              </p>
            ))}

            {result && outcome && (
              <ResultSummary
                result={result}
                outcome={outcome}
                confirmedFinals={transcript.finals.filter((f) => f.saved).length}
              />
            )}

            <section aria-labelledby="live-heading">
              <h2 id="live-heading" className="text-sm font-semibold text-slate-900">
                Live transcript
              </h2>
              <LiveTranscript
                transcript={transcript}
                active={status === "recording" || status === "finishing"}
              />
            </section>
          </div>
        </Card>
      </div>

      <ConfirmDialog
        open={blocker.state === "blocked" && active}
        title="Leave while recording?"
        message={
          status === "recording"
            ? "The recording will be stopped. Audio captured so far is still transcribed and saved."
            : status === "finishing"
              ? "The last audio is still being transcribed. It will finish saving in the background."
              : "The recording has not started yet and will be cancelled."
        }
        confirmLabel={status === "recording" ? "Stop and leave" : "Leave"}
        cancelLabel="Keep recording"
        destructive
        onConfirm={() => void confirmLeave()}
        onCancel={() => {
          if (blocker.state === "blocked") blocker.reset();
        }}
      />
    </div>
  );
}

function MicButton({
  status,
  onStart,
  onStop,
}: {
  status: RecordingStatus;
  onStart: () => void;
  onStop: () => void;
}) {
  const recording = status === "recording";
  const waiting =
    status === "requesting_permission" || status === "connecting" || status === "finishing";
  const blocked = status === "error";
  return (
    <button
      type="button"
      onClick={recording ? onStop : onStart}
      disabled={waiting}
      aria-label={recording ? "Stop recording" : "Start recording"}
      aria-pressed={recording}
      className={cn(
        "flex size-24 items-center justify-center rounded-full shadow-sm ring-1 transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-indigo-600",
        recording
          ? "bg-red-600 text-white ring-red-700 hover:bg-red-700"
          : waiting
            ? "cursor-wait bg-slate-100 text-slate-400 ring-slate-200"
            : "bg-indigo-600 text-white ring-indigo-700 hover:bg-indigo-700",
      )}
    >
      {waiting ? (
        <Loader2 className="size-9 animate-spin" aria-hidden="true" />
      ) : recording ? (
        <CircleStop className="size-10" aria-hidden="true" />
      ) : blocked ? (
        <MicOff className="size-10" aria-hidden="true" />
      ) : (
        <Mic className="size-10" aria-hidden="true" />
      )}
    </button>
  );
}

const OUTCOME_TEXT: Record<RecordingOutcome, string> = {
  saved: "Recording saved",
  partially_saved: "Recording completed, but not fully saved",
  not_finalised: "Recording finished, but saving failed",
  interrupted: "Recording interrupted",
};

const OUTCOME_STYLE: Record<RecordingOutcome, { box: string; icon: ReactNode }> = {
  saved: {
    box: "bg-emerald-50 ring-emerald-200",
    icon: <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-emerald-600" aria-hidden="true" />,
  },
  partially_saved: {
    box: "bg-amber-50 ring-amber-200",
    icon: <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-600" aria-hidden="true" />,
  },
  interrupted: {
    box: "bg-amber-50 ring-amber-200",
    icon: <AlertTriangle className="mt-0.5 size-5 shrink-0 text-amber-600" aria-hidden="true" />,
  },
  not_finalised: {
    box: "bg-red-50 ring-red-200",
    icon: <XCircle className="mt-0.5 size-5 shrink-0 text-red-600" aria-hidden="true" />,
  },
};

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * The stored result, strictly from session_saved: record_saved, status, saved_segments,
 * unsaved_segments, failed_utterances. Success is only claimed when all of them agree.
 */
function ResultSummary({
  result,
  outcome,
  confirmedFinals,
}: {
  result: RecordingResult;
  outcome: RecordingOutcome;
  /** Finals the server confirmed as saved (saved: true), for endings without session_saved. */
  confirmedFinals: number;
}) {
  const saved = result.saved;
  const style = OUTCOME_STYLE[outcome];
  const unsaved = saved?.unsaved_segments ?? [];
  return (
    <div
      className={cn("rounded-lg p-4 ring-1 ring-inset", style.box)}
      aria-label="Recording result"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex items-start gap-3">
          {style.icon}
          <div className="space-y-1 text-sm text-slate-700">
            <p className="font-semibold text-slate-900">
              {
                {
                  saved: "Saved",
                  partially_saved: "Completed, but some segments were not saved",
                  not_finalised: "Finished, but the recording could not be finalised",
                  interrupted: saved ? "Interrupted" : "Interrupted (not confirmed by the server)",
                }[outcome]
              }
            </p>
            {saved ? (
              <p>
                Duration {formatDuration(saved.duration_ms)} ·{" "}
                {plural(saved.saved_segments, "segment")} saved
                {unsaved.length > 0 && ` · ${unsaved.length} not saved`}
              </p>
            ) : (
              <p>
                The connection ended before the server sent its final summary.{" "}
                {confirmedFinals > 0
                  ? `${plural(confirmedFinals, "line")} shown below ${confirmedFinals === 1 ? "was" : "were"} confirmed saved before that.`
                  : "No transcript lines had been confirmed saved."}
              </p>
            )}
            {unsaved.length > 0 && (
              <p className="text-red-700">
                Not saved (database unavailable):{" "}
                {unsaved.map((seq) => `segment #${seq + 1}`).join(", ")}. Their text is shown below
                but is not in the stored transcript.
              </p>
            )}
            {saved && saved.failed_utterances > 0 && (
              <p className="text-amber-800">
                {plural(saved.failed_utterances, "part")} of the audio could not be transcribed.
              </p>
            )}
            {outcome === "not_finalised" && (
              <p className="text-red-700">
                The server could not finish storing this recording, so its status or duration may be
                wrong. Segments counted as saved above are stored.
              </p>
            )}
          </div>
        </div>
        {result.transcriptionId && (
          <ButtonLink
            to={`/transcriptions/${result.transcriptionId}`}
            variant="secondary"
            size="sm"
            className="shrink-0"
            icon={<ExternalLink className="size-4" aria-hidden="true" />}
          >
            Open transcription
          </ButtonLink>
        )}
      </div>
    </div>
  );
}
