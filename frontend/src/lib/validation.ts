import { LIMITS } from "../api/types";

// Pragmatic email check; the backend performs full validation (email-validator).
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export type Errors<K extends string> = Partial<Record<K, string>>;

export function validateEmail(email: string): string | undefined {
  const value = email.trim();
  if (!value) return "Enter your email address.";
  if (value.length > LIMITS.emailMax || !EMAIL.test(value)) return "Enter a valid email address.";
  return undefined;
}

export function validateNewPassword(password: string): string | undefined {
  if (!password) return "Enter a password.";
  if (!password.trim()) return "The password cannot be only spaces.";
  if (password.length < LIMITS.passwordMin) return `Use at least ${LIMITS.passwordMin} characters.`;
  if (password.length > LIMITS.passwordMax) return `Use at most ${LIMITS.passwordMax} characters.`;
  return undefined;
}

export function validateTitle(title: string, { required = false } = {}): string | undefined {
  const value = title.trim();
  if (!value) return required ? "Enter a title." : undefined;
  if (value.length > LIMITS.titleMax) return `Use at most ${LIMITS.titleMax} characters.`;
  return undefined;
}

export function validateText(text: string): string | undefined {
  if (!text.trim()) return "Enter the transcription text.";
  if (text.trim().length > LIMITS.textMax)
    return `Use at most ${LIMITS.textMax.toLocaleString()} characters.`;
  return undefined;
}

export function hasErrors(errors: Record<string, string | undefined>): boolean {
  return Object.values(errors).some(Boolean);
}
