import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";

import { useApi } from "../api/ApiProvider";
import { tokenStorage } from "../auth/tokenStorage";
import {
  webSocketUrl,
  type ServerMessage,
  type SessionStarted,
  type StartMessage,
} from "../realtime/protocol";
import {
  emptyTranscript,
  endTranscript,
  transcriptReducer,
  type TranscriptState,
} from "../realtime/transcript";
import {
  TranscriptionSocket,
  type SocketEnd,
  type SocketPhase,
  type TranscriptionSocketOptions,
} from "../realtime/transcriptionSocket";
import { transcriptionKeys } from "./useTranscriptions";

type TranscriptAction =
  { type: "reset" } | { type: "ended" } | { type: "message"; message: ServerMessage };

function reducer(state: TranscriptState, action: TranscriptAction): TranscriptState {
  switch (action.type) {
    case "reset":
      return emptyTranscript;
    case "ended":
      return endTranscript(state);
    case "message":
      return transcriptReducer(state, action.message);
  }
}

export interface ConnectOptions {
  start: Pick<StartMessage, "speech_mode" | "script_preference" | "title">;
  onEnd: (end: SocketEnd) => void;
}

export interface UseTranscriptionSocket {
  phase: SocketPhase;
  session: SessionStarted | null;
  transcript: TranscriptState;
  warnings: string[];
  /** Open, authenticate (JWT in the first message) and wait for session_started. */
  connect: (options: ConnectOptions) => Promise<SessionStarted>;
  sendAudio: (pcm: ArrayBuffer) => boolean;
  /** Flush queued audio, send stop, keep listening until session_saved. */
  finish: () => void;
  abort: (message?: string) => void;
  backlogMs: () => number;
}

/** Test seam: replace socket construction / limits. */
export type SocketFactoryOptions = Pick<
  TranscriptionSocketOptions,
  "createSocket" | "now" | "limits" | "handshakeTimeoutMs" | "finishTimeoutMs"
>;

/**
 * React binding for TranscriptionSocket. The URL comes from VITE_API_BASE_URL (ws:// or
 * wss:// to match http:// or https://) and the token from tokenStorage: there is no second
 * copy of the credentials.
 *
 * On unmount an active recording is aborted (the server keeps it as interrupted), except
 * one that is already stopping: that one finishes in the background so the last audio is
 * transcribed and saved. Recording history queries are invalidated whenever a recording
 * ends, mounted or not.
 */
export function useTranscriptionSocket(factory?: SocketFactoryOptions): UseTranscriptionSocket {
  const { baseUrl } = useApi();
  const queryClient = useQueryClient();
  const url = useMemo(() => webSocketUrl(baseUrl), [baseUrl]);
  const socketRef = useRef<TranscriptionSocket | null>(null);
  const mounted = useRef(true);
  const [phase, setPhase] = useState<SocketPhase>("idle");
  const [session, setSession] = useState<SessionStarted | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [transcript, dispatch] = useReducer(reducer, emptyTranscript);

  const connect = useCallback(
    ({ start, onEnd }: ConnectOptions) => {
      socketRef.current?.abort();
      dispatch({ type: "reset" });
      setWarnings([]);
      setSession(null);
      const socket = new TranscriptionSocket({
        url,
        getToken: () => tokenStorage.get(),
        start,
        ...factory,
        onPhase: (next) => {
          if (mounted.current && socketRef.current === socket) setPhase(next);
        },
        onMessage: (message) => {
          if (!mounted.current || socketRef.current !== socket) return;
          if (message.type === "session_started") setSession(message);
          dispatch({ type: "message", message });
        },
        onWarning: (text) => {
          if (mounted.current) setWarnings((w) => (w.includes(text) ? w : [...w, text]));
        },
        onEnd: (end) => {
          void queryClient.invalidateQueries({ queryKey: transcriptionKeys.all });
          if (mounted.current && socketRef.current === socket) {
            dispatch({ type: "ended" });
            onEnd(end);
          }
        },
      });
      socketRef.current = socket;
      return socket.connect();
    },
    [url, factory, queryClient],
  );

  const sendAudio = useCallback(
    (pcm: ArrayBuffer) => socketRef.current?.sendAudio(pcm) ?? false,
    [],
  );
  const finish = useCallback(() => socketRef.current?.finish(), []);
  const abort = useCallback((message?: string) => socketRef.current?.abort(message), []);
  const backlogMs = useCallback(() => socketRef.current?.backlogMs ?? 0, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const socket = socketRef.current;
      if (socket && socket.currentPhase !== "stopping") socket.abort();
    };
  }, []);

  return { phase, session, transcript, warnings, connect, sendAudio, finish, abort, backlogMs };
}
