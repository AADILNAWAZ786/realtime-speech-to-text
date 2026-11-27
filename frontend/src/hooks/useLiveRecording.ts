import { useCallback, useEffect, useRef, useState } from "react";

import type { ScriptPreference, SpeechMode } from "../api/types";
import { MicrophoneError, type AudioEnvironment } from "../audio/microphone";
import type { SessionSaved } from "../realtime/protocol";
import { SocketError, type SocketEnd } from "../realtime/transcriptionSocket";
import { useMicrophone } from "./useMicrophone";
import { useTranscriptionSocket, type SocketFactoryOptions } from "./useTranscriptionSocket";

export type RecordingStatus =
  | "idle"
  | "requesting_permission"
  | "connecting"
  | "recording"
  | "finishing"
  | "completed"
  | "interrupted"
  | "error";

export const ACTIVE_STATUSES: readonly RecordingStatus[] = [
  "requesting_permission",
  "connecting",
  "recording",
  "finishing",
];

export interface RecordingSettings {
  speechMode: SpeechMode;
  scriptPreference: ScriptPreference;
  title?: string;
}

export interface RecordingProblem {
  message: string;
  /** "Sign in again" is the fix (expired or invalid token). */
  needsSignIn?: boolean;
  /** Permission denied: the user must change a browser setting. */
  microphone?: MicrophoneError["code"];
}

export interface RecordingResult {
  transcriptionId: string;
  saved: SessionSaved | null; // null when the connection ended without session_saved
}

/**
 * What the server actually stored, from session_saved (most severe first):
 *
 * - not_finalised:    record_saved is false - the database could not finalise the record
 * - interrupted:      status "interrupted", or no session_saved at all
 * - partially_saved:  completed, but some segments are in unsaved_segments
 * - saved:            completed, record finalised, every segment saved
 */
export type RecordingOutcome = "saved" | "partially_saved" | "not_finalised" | "interrupted";

export function recordingOutcome(result: RecordingResult): RecordingOutcome {
  const saved = result.saved;
  if (!saved) return "interrupted";
  if (!saved.record_saved) return "not_finalised";
  if (saved.status !== "completed") return "interrupted";
  if (saved.unsaved_segments.length > 0) return "partially_saved";
  return "saved";
}

/**
 * Orchestrates one live recording:
 *
 *   Start  -> microphone permission + audio graph (no audio flows yet)
 *          -> WebSocket open, {"type":"start", token, ...}, wait for session_started
 *          -> connect the microphone: PCM16 frames stream to the server
 *   Stop   -> stop capturing, flush the worklet's last samples, send queued frames,
 *             {"type":"stop"}, keep receiving until session_saved
 *   End    -> microphone and AudioContext released, history queries invalidated
 *
 * Permission is requested before connecting, so a denied microphone never creates an empty
 * recording on the server.
 */
export function useLiveRecording(test?: {
  audio?: AudioEnvironment;
  socket?: SocketFactoryOptions;
}) {
  const mic = useMicrophone(test?.audio);
  const socket = useTranscriptionSocket(test?.socket);
  const [status, setStatus] = useState<RecordingStatus>("idle");
  const [problem, setProblem] = useState<RecordingProblem | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [result, setResult] = useState<RecordingResult | null>(null);
  const busy = useRef(false); // synchronous guard against double starts
  const statusRef = useRef<RecordingStatus>("idle");
  const stopRef = useRef<() => Promise<boolean>>(() => Promise.resolve(false));

  const update = useCallback((next: RecordingStatus) => {
    statusRef.current = next;
    setStatus(next);
  }, []);

  const onEnd = useCallback(
    (end: SocketEnd) => {
      mic.close();
      busy.current = false;
      if (end.kind === "saved") {
        setResult({ transcriptionId: end.saved.transcription_id, saved: end.saved });
        if (end.saved.status === "completed") {
          update("completed");
          if (end.saved.end_reason === "recording_limit") {
            setNotice("The maximum recording length was reached, so the recording was stopped.");
          }
        } else {
          update("interrupted");
          setProblem({
            message:
              end.error?.message && end.error.fatal
                ? describeServerError(end.error.code, end.error.message)
                : "The recording was interrupted. Everything transcribed before that point was saved.",
          });
        }
      } else {
        setResult(
          end.transcriptionId ? { transcriptionId: end.transcriptionId, saved: null } : null,
        );
        update("interrupted");
        setProblem({ message: end.error.message });
      }
    },
    [mic, update],
  );

  const start = useCallback(
    async (settings: RecordingSettings) => {
      if (busy.current) return; // one recording at a time
      busy.current = true;
      setProblem(null);
      setNotice(null);
      setResult(null);

      update("requesting_permission");
      try {
        await mic.open({
          onFrame: (frame) => {
            socket.sendAudio(frame.pcm);
          },
          onDeviceLost: () => {
            setNotice("The microphone was disconnected, so the recording was stopped.");
            void stopRef.current();
          },
        });
      } catch (error) {
        busy.current = false;
        const micError = error instanceof MicrophoneError ? error : new MicrophoneError("unknown");
        if (micError.code === "aborted") return;
        update("error");
        setProblem({ message: micError.message, microphone: micError.code });
        return;
      }

      update("connecting");
      try {
        await socket.connect({
          start: {
            speech_mode: settings.speechMode,
            script_preference: settings.scriptPreference,
            ...(settings.title?.trim() ? { title: settings.title.trim() } : {}),
          },
          onEnd,
        });
      } catch (error) {
        mic.close();
        busy.current = false;
        if (error instanceof SocketError && error.code === "aborted") {
          update("idle");
          return;
        }
        update("error");
        setProblem({
          message: error instanceof Error ? error.message : "Could not start the recording.",
          needsSignIn:
            error instanceof SocketError && (error.code === "no_token" || error.closeCode === 4401),
        });
        return;
      }

      mic.begin(); // audio flows only after session_started
      update("recording");
    },
    [mic, socket, onEnd, update],
  );

  /**
   * Stop the recording. Resolves true when the last audio was flushed and `stop` was sent
   * (the result follows with session_saved). Resolves false when the flush failed: then no
   * `stop` is sent - a normal stop would make the server mark the recording "completed"
   * although its last audio never arrived. The socket is aborted instead, so the server
   * saves it as interrupted, and the page shows why.
   */
  const stop = useCallback(async (): Promise<boolean> => {
    if (statusRef.current !== "recording") return false;
    update("finishing");
    try {
      await mic.finish(); // the worklet's tail frames are sent through onFrame first
    } catch (error) {
      const reason =
        error instanceof MicrophoneError ? error.message : "The microphone could not be stopped.";
      socket.abort(
        `${reason} The recording was ended as interrupted: its final seconds were not sent ` +
          "or transcribed. Lines already shown below were saved by the server, unless " +
          "marked “not saved yet”.",
      );
      return false;
    }
    socket.finish(); // queued frames, then {"type":"stop"}; session_saved arrives later
    return true;
  }, [mic, socket, update]);
  useEffect(() => {
    stopRef.current = stop;
  }, [stop]);

  /** Cancel while asking for permission or connecting. */
  const cancel = useCallback(() => {
    if (statusRef.current === "requesting_permission" || statusRef.current === "connecting") {
      socket.abort();
      mic.close();
      busy.current = false;
      update("idle");
    }
  }, [mic, socket, update]);

  const reset = useCallback(() => {
    if (ACTIVE_STATUSES.includes(statusRef.current)) return;
    setProblem(null);
    setNotice(null);
    setResult(null);
    update("idle");
  }, [update]);

  // Browser back/forward and in-app navigation are handled by the page (useBlocker);
  // closing the tab can only be warned about.
  const active = ACTIVE_STATUSES.includes(status);
  useEffect(() => {
    if (!active) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault(); // shows the browser's "Leave site?" prompt
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [active]);

  return {
    status,
    active,
    problem,
    notice,
    result,
    warnings: socket.warnings,
    transcript: socket.transcript,
    session: socket.session,
    connection: socket.phase,
    level: mic.level,
    capturedMs: mic.capturedMs,
    inputSampleRate: mic.inputSampleRate,
    start,
    stop,
    cancel,
    reset,
  };
}

function describeServerError(code: string, fallback: string): string {
  switch (code) {
    case "server_overloaded":
      return "Transcription fell too far behind, so the server stopped the recording. Everything transcribed was saved.";
    case "idle_timeout":
      return "No audio reached the server for too long, so the recording was stopped.";
    case "server_shutdown":
      return "The server restarted, so the recording was stopped. Everything transcribed was saved.";
    case "rate_limited":
      return "Audio reached the server faster than real time, so the recording was stopped.";
    case "frame_too_large":
      return "An audio frame was too large for the server (close code 1009), so the recording was stopped.";
    case "transcription_unavailable":
      return "The speech recognition service stopped responding, so the recording was stopped. Everything transcribed before that was saved.";
    case "transcription_quota_exceeded":
      return "The speech recognition service reached its usage limit, so the recording was stopped. Everything transcribed before that was saved.";
    default:
      return fallback;
  }
}
