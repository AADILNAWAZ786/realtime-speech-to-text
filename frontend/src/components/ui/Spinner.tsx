import { LoaderCircle } from "lucide-react";

import { cn } from "../../lib/cn";

export function Spinner({ className, label }: { className?: string; label?: string }) {
  return (
    <LoaderCircle
      className={cn("size-4 animate-spin", className)}
      aria-label={label ?? "Loading"}
      role="status"
    />
  );
}

export function FullPageSpinner({ label }: { label: string }) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-3 text-slate-500">
      <Spinner className="size-6 text-indigo-600" label={label} />
      <p className="text-sm">{label}</p>
    </div>
  );
}
