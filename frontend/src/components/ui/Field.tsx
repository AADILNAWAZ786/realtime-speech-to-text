import {
  useId,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from "react";

import { cn } from "../../lib/cn";

const control =
  "block w-full rounded-lg border-0 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm " +
  "ring-1 ring-inset ring-slate-300 placeholder:text-slate-400 " +
  "focus:ring-2 focus:ring-inset focus:ring-indigo-600 disabled:bg-slate-50 disabled:text-slate-500";
const invalid = "ring-red-400 focus:ring-red-600";

interface FieldProps {
  label: string;
  error?: string | undefined;
  hint?: ReactNode;
  className?: string;
  /** Render prop receiving the ids/aria attributes for the control. */
  children: (props: {
    id: string;
    "aria-invalid": boolean | undefined;
    "aria-describedby": string | undefined;
    className: string;
  }) => ReactNode;
}

/** Label + control + hint/error, wired up for screen readers. */
export function Field({ label, error, hint, className, children }: FieldProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errorId = error ? `${id}-error` : undefined;
  return (
    <div className={cn("space-y-1.5", className)}>
      <label htmlFor={id} className="block text-sm font-medium text-slate-700">
        {label}
      </label>
      {children({
        id,
        "aria-invalid": error ? true : undefined,
        "aria-describedby": [hintId, errorId].filter(Boolean).join(" ") || undefined,
        className: cn(control, error && invalid),
      })}
      {hint && !error && (
        <p id={hintId} className="text-xs text-slate-500">
          {hint}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-xs font-medium text-red-600">
          {error}
        </p>
      )}
    </div>
  );
}

type InputFieldProps = Omit<InputHTMLAttributes<HTMLInputElement>, "className"> & {
  label: string;
  error?: string | undefined;
  hint?: ReactNode;
};

export function InputField({ label, error, hint, ...props }: InputFieldProps) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(aria) => <input {...props} {...aria} />}
    </Field>
  );
}

type TextAreaFieldProps = Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "className"> & {
  label: string;
  error?: string | undefined;
  hint?: ReactNode;
};

export function TextAreaField({ label, error, hint, ...props }: TextAreaFieldProps) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(aria) => <textarea {...props} {...aria} className={cn(aria.className, "min-h-48")} />}
    </Field>
  );
}

export interface SelectOption<T extends string> {
  value: T;
  label: string;
}

type SelectFieldProps<T extends string> = Omit<
  SelectHTMLAttributes<HTMLSelectElement>,
  "className" | "onChange" | "value"
> & {
  label: string;
  value: T;
  options: readonly SelectOption<T>[];
  onChange: (value: T) => void;
  error?: string | undefined;
  hint?: ReactNode;
};

export function SelectField<T extends string>({
  label,
  value,
  options,
  onChange,
  error,
  hint,
  ...props
}: SelectFieldProps<T>) {
  return (
    <Field label={label} error={error} hint={hint}>
      {(aria) => (
        <select
          {...props}
          {...aria}
          value={value}
          onChange={(event) => {
            const option = options.find((o) => o.value === event.target.value);
            if (option) onChange(option.value);
          }}
        >
          {options.map((option) => (
            <option key={option.value} value={option.value}>
              {option.label}
            </option>
          ))}
        </select>
      )}
    </Field>
  );
}
