import {
  SCRIPT_PREFERENCES,
  SPEECH_MODES,
  type ScriptPreference,
  type SpeechMode,
} from "../../api/types";
import { SCRIPT_LABELS, SPEECH_MODE_LABELS } from "../../lib/format";
import type { SelectOption } from "../ui/Field";

export const SPEECH_MODE_OPTIONS: readonly SelectOption<SpeechMode>[] = SPEECH_MODES.map(
  (value) => ({
    value,
    label: SPEECH_MODE_LABELS[value],
  }),
);

export const SCRIPT_OPTIONS: readonly SelectOption<ScriptPreference>[] = SCRIPT_PREFERENCES.map(
  (value) => ({ value, label: SCRIPT_LABELS[value] }),
);
