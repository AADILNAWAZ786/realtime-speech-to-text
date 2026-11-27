import { cn } from "../../lib/cn";

const BARS = 24;

/** Microphone level, 0..1 (already on a dB scale). Purely data-driven, no animation. */
export function LevelMeter({ level }: { level: number }) {
  const lit = Math.round(level * BARS);
  return (
    <div
      role="meter"
      aria-label="Microphone level"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(level * 100)}
      className="flex h-6 w-full max-w-xs items-end gap-0.5"
    >
      {Array.from({ length: BARS }, (_, i) => (
        <span
          key={i}
          className={cn(
            "flex-1 rounded-sm",
            i < lit
              ? i >= BARS - 3
                ? "bg-red-500"
                : i >= BARS - 8
                  ? "bg-amber-400"
                  : "bg-emerald-500"
              : "bg-slate-200",
          )}
          style={{ height: `${30 + (70 * i) / (BARS - 1)}%` }}
        />
      ))}
    </div>
  );
}
