import { UserPlus } from "lucide-react";
import { useState, type SyntheticEvent } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, errorMessage } from "../api/client";
import { LIMITS } from "../api/types";
import { useAuth } from "../auth/AuthProvider";
import { AuthLayout } from "../components/layout/AuthLayout";
import { Button } from "../components/ui/Button";
import { InputField } from "../components/ui/Field";
import { FormAlert } from "../components/ui/FormAlert";
import { hasErrors, validateEmail, validateNewPassword, type Errors } from "../lib/validation";

type FieldName = "display_name" | "email" | "password" | "confirm";

export function RegisterPage() {
  const { register } = useAuth();
  const navigate = useNavigate();
  const [values, setValues] = useState({ display_name: "", email: "", password: "", confirm: "" });
  const [errors, setErrors] = useState<Errors<FieldName>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const set = (field: FieldName) => (event: { target: { value: string } }) =>
    setValues((v) => ({ ...v, [field]: event.target.value }));

  const onSubmit = async (event: SyntheticEvent) => {
    event.preventDefault();
    const displayName = values.display_name.trim();
    const next: Errors<FieldName> = {
      display_name:
        displayName.length > LIMITS.displayNameMax
          ? `Use at most ${LIMITS.displayNameMax} characters.`
          : undefined,
      email: validateEmail(values.email),
      password: validateNewPassword(values.password),
      confirm: values.confirm === values.password ? undefined : "The passwords do not match.",
    };
    setErrors(next);
    setFormError(null);
    if (hasErrors(next)) return;

    setSubmitting(true);
    try {
      // An empty name is simply omitted (the API would also accept null or "").
      await register({
        email: values.email.trim(),
        password: values.password,
        ...(displayName ? { display_name: displayName } : {}),
      });
      void navigate("/", { replace: true });
    } catch (error) {
      if (error instanceof ApiError && error.code === "email_already_registered") {
        setErrors({ email: "An account with this email already exists." });
        setFormError("An account with this email already exists. Sign in instead.");
      } else if (error instanceof ApiError && error.status === 422) {
        setErrors({
          display_name: error.fieldErrors.display_name,
          email: error.fieldErrors.email,
          password: error.fieldErrors.password,
        });
        setFormError("Please correct the highlighted fields.");
      } else {
        setFormError(errorMessage(error));
      }
      setSubmitting(false);
    }
  };

  return (
    <AuthLayout
      title="Create your account"
      subtitle="Transcribe English, Hindi and Hinglish speech."
      footer={
        <>
          Already have an account?{" "}
          <Link to="/login" className="font-medium text-indigo-600 hover:text-indigo-700">
            Sign in
          </Link>
        </>
      }
    >
      <form noValidate onSubmit={onSubmit} className="space-y-5">
        {formError && <FormAlert message={formError} />}
        <InputField
          label="Name (optional)"
          autoComplete="name"
          value={values.display_name}
          onChange={set("display_name")}
          error={errors.display_name}
          maxLength={LIMITS.displayNameMax}
        />
        <InputField
          label="Email"
          type="email"
          autoComplete="email"
          value={values.email}
          onChange={set("email")}
          error={errors.email}
        />
        <InputField
          label="Password"
          type="password"
          autoComplete="new-password"
          value={values.password}
          onChange={set("password")}
          error={errors.password}
          hint={`${LIMITS.passwordMin}–${LIMITS.passwordMax} characters.`}
        />
        <InputField
          label="Confirm password"
          type="password"
          autoComplete="new-password"
          value={values.confirm}
          onChange={set("confirm")}
          error={errors.confirm}
        />
        <Button
          type="submit"
          className="w-full"
          loading={submitting}
          icon={<UserPlus className="size-4" aria-hidden="true" />}
        >
          Create account
        </Button>
      </form>
    </AuthLayout>
  );
}
