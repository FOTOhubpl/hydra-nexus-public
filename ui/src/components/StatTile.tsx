import type { ReactNode } from "react";
import { cx } from "../lib/cx";

export interface StatTileProps {
  label: string;
  /** null/undefined renders an em dash: unknown is shown as unknown, never as 0. */
  value: ReactNode | null | undefined;
  hint?: ReactNode;
  /** Small trailing element (badge, unit, spark). */
  aside?: ReactNode;
  loading?: boolean;
  className?: string;
}

export function StatTile({ label, value, hint, aside, loading = false, className }: StatTileProps) {
  const empty = value === null || value === undefined || value === "";
  return (
    <div className={cx("surface-card flex min-w-0 flex-col gap-1.5 px-4 py-3", className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate text-xs font-medium text-subtle">{label}</span>
        {aside}
      </div>
      <div className="truncate font-mono text-2xl leading-tight font-semibold text-fg tabular-nums" aria-busy={loading || undefined}>
        {loading ? <span className="inline-block h-6 w-16 rounded-md bg-hover align-middle motion-safe:animate-pulse" /> : empty ? <span className="text-subtle">—</span> : value}
      </div>
      {hint && <div className="truncate text-xs text-muted">{hint}</div>}
    </div>
  );
}
