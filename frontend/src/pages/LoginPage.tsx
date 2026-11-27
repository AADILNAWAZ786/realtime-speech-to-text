import { LogIn } from "lucide-react";
import { useState, type SyntheticEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import { ApiError, errorMessage } from "../api/client";
import { useAuth } from "../auth/AuthProvider";
import type { LocationState } from "../auth/RouteGuards";
import { AuthLayout } from "../components/layout/AuthLayout";
import { Button } from "../components/ui/Button";
import { InputField } from "../components/ui/Field";
import { FormAlert } from "../components/ui/FormAlert";
import { safeRedirect } from "../lib/redirect";
import { hasErrors, validateEmail, type Errors } from "../lib/validation";

type FieldName = "email" | "password";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const from = safeRedirect((location.state as LocationState | null)?.from);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<Errors<FieldName>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (event: SyntheticEvent) => {
    event.preventDefault();
    const next: Errors<FieldName> = {
      email: validateEmail(email),
      password: password ? undefined : "Enter your password.",
    };
    setErrors(next);
    setFormError(null);
    if (hasErrors(next)) return;

    setSubmitting(true);
    try {
      await login(email.trim(), password);
      void navigate(from, { replace: true });
    } catch (error) {
      if (error instanceof ApiError && error.status === 422) {
        setErrors({ email: error.fieldErrors.email, password: error.fieldErrors.password });
      }
      setFormError(
        error instanceof ApiError && error.code === "invalid_credentials"
          ? "Incorrect email or password."
          : errorMessage(error),
      );
      setSubmitting(false);
    }
  };

  return (
    <AuthLayout
      title="Sign in"
      subtitle="Welcome back. Sign in to see your transcriptions."
      footer={
        <>
          New here?{" "}
          <Link to="/register" className="font-medium text-indigo-600 hover:text-indigo-700">
            Create an account
          </Link>
        </>
      }
    >
      <form noValidate onSubmit={onSubmit} className="space-y-5">
        {formError && <FormAlert message={formError} />}
        <InputField
          label="Email"
          type="email"
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          error={errors.email}
        />
        <InputField
          label="Password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          error={errors.password}
        />
        <Button
          type="submit"
          className="w-full"
          loading={submitting}
          icon={<LogIn className="size-4" aria-hidden="true" />}
        >
          Sign in
        </Button>
      </form>
    </AuthLayout>
  );
}
