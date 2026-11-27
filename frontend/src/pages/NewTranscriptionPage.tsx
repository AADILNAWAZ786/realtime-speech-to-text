import { Save } from "lucide-react";
import { useState, type SyntheticEvent } from "react";
import { useNavigate } from "react-router";

import { ApiError, errorMessage } from "../api/client";
import { LIMITS, type ScriptPreference, type SpeechMode } from "../api/types";
import { SCRIPT_OPTIONS, SPEECH_MODE_OPTIONS } from "../components/transcriptions/LanguageOptions";
import { Button, ButtonLink } from "../components/ui/Button";
import { InputField, SelectField, TextAreaField } from "../components/ui/Field";
import { FormAlert } from "../components/ui/FormAlert";
import { Card, PageHeader } from "../components/ui/Surface";
import { useToast } from "../components/ui/Toast";
import { useCreateTranscription } from "../hooks/useTranscriptions";
import { hasErrors, validateText, validateTitle, type Errors } from "../lib/validation";

type FieldName = "title" | "text";

export function NewTranscriptionPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const create = useCreateTranscription();
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [speechMode, setSpeechMode] = useState<SpeechMode>("auto");
  const [script, setScript] = useState<ScriptPreference>("as_recognised");
  const [errors, setErrors] = useState<Errors<FieldName>>({});
  const [formError, setFormError] = useState<string | null>(null);

  const onSubmit = (event: SyntheticEvent) => {
    event.preventDefault();
    const next = { title: validateTitle(title), text: validateText(text) };
    setErrors(next);
    setFormError(null);
    if (hasErrors(next)) return;

    create.mutate(
      {
        ...(title.trim() ? { title: title.trim() } : {}),
        text,
        speech_mode: speechMode,
        script_preference: script,
      },
      {
        onSuccess: (created) => {
          toast.show({ kind: "success", title: "Transcription saved" });
          void navigate(`/transcriptions/${created.id}`, { replace: true });
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 422) {
            setErrors({ title: error.fieldErrors.title, text: error.fieldErrors.text });
          }
          setFormError(errorMessage(error));
        },
      },
    );
  };

  return (
    <div className="space-y-6">
      <PageHeader
        title="New manual transcription"
        description="Type or paste text. You can edit it later from its detail page."
      />
      <Card className="p-6">
        <form noValidate onSubmit={onSubmit} className="space-y-5">
          {formError && <FormAlert message={formError} />}
          <InputField
            label="Title (optional)"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            maxLength={LIMITS.titleMax}
            error={errors.title}
            hint="Leave empty to use a dated title such as “Transcription - 26 Nov 2026, 10:15 UTC”."
          />
          <div className="grid gap-5 sm:grid-cols-2">
            <SelectField
              label="Language mode"
              value={speechMode}
              options={SPEECH_MODE_OPTIONS}
              onChange={setSpeechMode}
            />
            <SelectField
              label="Output script"
              value={script}
              options={SCRIPT_OPTIONS}
              onChange={setScript}
            />
          </div>
          <TextAreaField
            label="Text"
            value={text}
            onChange={(e) => setText(e.target.value)}
            error={errors.text}
            hint={`${text.length.toLocaleString()} / ${LIMITS.textMax.toLocaleString()} characters`}
          />
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <ButtonLink to="/" variant="secondary">
              Cancel
            </ButtonLink>
            <Button
              type="submit"
              loading={create.isPending}
              icon={<Save className="size-4" aria-hidden="true" />}
            >
              Save transcription
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
