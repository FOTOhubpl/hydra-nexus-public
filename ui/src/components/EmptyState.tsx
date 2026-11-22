import type { ReactNode } from "react";
import { cx } from "../lib/cx";

export type EmptyStateTone = "neutral" | "offline" | "error" | "pending";

const TONE_ICON: Record<EmptyStateTone, string> = {
  neutral: "border-line-strong text-subtle",
  pending: "border-info/40 text-info",
  offline: "border-warn/40 text-warn",
  error: "border-err/40 text-err",
};

const TONE_GLYPH: Record<EmptyStateTone, string> = {
  neutral: "∅",
  pending: "…",
  offline: "⏻",
  error: "!",
};

export interface EmptyStateProps {
  title: string;
  description?: ReactNode;
  tone?: EmptyStateTone;
  /** Optional call to action (button/link). */
  action?: ReactNode;
  /** Optional technical detail, e.g. the endpoint involved. */
  detail?: ReactNode;
  compact?: boolean;
  className?: string;
  testId?: string;
}

export function EmptyState({ title, description, tone = "neutral", action, detail, compact = false, className, testId }: EmptyStateProps) {
  return (
    <div
      role="status"
      data-testid={testId}
      className={cx(
        "flex flex-col items-center justify-center gap-2 text-center",
        compact ? "px-4 py-6" : "px-6 py-12",
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cx("flex size-8 items-center justify-center rounded-full border font-mono text-sm", TONE_ICON[tone])}
      >
        {TONE_GLYPH[tone]}
      </span>
      <div className="text-sm font-semibold text-fg">{title}</div>
      {description && <div className="max-w-md text-sm text-muted">{description}</div>}
      {detail && <div className="max-w-md font-mono text-xs break-all text-subtle">{detail}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}
