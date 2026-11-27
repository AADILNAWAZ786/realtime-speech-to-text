/**
 * Live transcript state, driven by server messages (pure reducer, no I/O).
 *
 * - A partial replaces the previous partial with the same utterance_id (newer version only).
 * - A final is placed by seq and removes the partial of its utterance_id; later partials
 *   for an utterance that already has a final are ignored.
 * - Discarded and failed utterances remove their partial.
 */
import type { FinalMessage, PartialMessage, ServerMessage, UtteranceFailed } from "./protocol";

export interface TranscriptState {
  finals: FinalMessage[]; // ordered by seq
  partials: PartialMessage[]; // ordered by start_ms, at most one per utterance
  finalizedUtterances: number[];
  failed: UtteranceFailed[];
  discarded: number;
  previewPaused: boolean;
  backlogMs: number;
}

export const emptyTranscript: TranscriptState = {
  finals: [],
  partials: [],
  finalizedUtterances: [],
  failed: [],
  discarded: 0,
  previewPaused: false,
  backlogMs: 0,
};

function withoutPartial(partials: PartialMessage[], utteranceId: number) {
  return partials.filter((p) => p.utterance_id !== utteranceId);
}

export function transcriptReducer(state: TranscriptState, message: ServerMessage): TranscriptState {
  switch (message.type) {
    case "partial": {
      if (state.finalizedUtterances.includes(message.utterance_id)) return state;
      const existing = state.partials.find((p) => p.utterance_id === message.utterance_id);
      if (existing && existing.version > message.version) return state;
      const partials = [...withoutPartial(state.partials, message.utterance_id), message].sort(
        (a, b) => a.start_ms - b.start_ms || a.utterance_id - b.utterance_id,
      );
      return { ...state, partials };
    }
    case "final": {
      const finals = [...state.finals.filter((f) => f.seq !== message.seq), message].sort(
        (a, b) => a.seq - b.seq,
      );
      return {
        ...state,
        finals,
        partials: withoutPartial(state.partials, message.utterance_id),
        finalizedUtterances: [...state.finalizedUtterances, message.utterance_id],
      };
    }
    case "utterance_discarded":
      return {
        ...state,
        partials: withoutPartial(state.partials, message.utterance_id),
        discarded: state.discarded + 1,
      };
    case "utterance_failed":
      return {
        ...state,
        partials: withoutPartial(state.partials, message.utterance_id),
        failed: [...state.failed, message],
      };
    case "preview_paused":
      return { ...state, previewPaused: true, backlogMs: message.backlog_ms };
    case "preview_resumed":
      return { ...state, previewPaused: false, backlogMs: message.backlog_ms };
    case "session_saved":
      // The recording is over: provisional text is no longer meaningful.
      return { ...state, partials: [], previewPaused: false };
    default:
      return state;
  }
}

/** Finals whose `saved` flag was false (database unavailable when they were produced). */
export function pendingFinals(state: TranscriptState): FinalMessage[] {
  return state.finals.filter((f) => !f.saved);
}

/**
 * The connection ended without session_saved: provisional text was never stored, so it is
 * removed (finals stay: the server saves each one before sending it).
 */
export function endTranscript(state: TranscriptState): TranscriptState {
  return state.partials.length === 0 && !state.previewPaused
    ? state
    : { ...state, partials: [], previewPaused: false };
}
