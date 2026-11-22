import type { ReactNode } from "react";
import { cx } from "../lib/cx";

export type BadgeVariant = "neutral" | "info" | "success" | "warning" | "danger";

const VARIANT_CLASSES: Record<BadgeVariant, string> = {
  neutral: "border-line-strong bg-hover text-muted",
  info: "border-info/35 bg-info/10 text-info",
  success: "border-ok/35 bg-ok/10 text-ok",
  warning: "border-warn/35 bg-warn/10 text-warn",
  danger: "border-err/35 bg-err/10 text-err",
};

const DOT_CLASSES: Record<BadgeVariant, string> = {
  neutral: "bg-subtle",
  info: "bg-info",
  success: "bg-ok",
  warning: "bg-warn",
  danger: "bg-err",
};

export interface BadgeProps {
  variant?: BadgeVariant;
  children: ReactNode;
  /** Leading status dot, so state never relies on colour alone in dense rows. */
  dot?: boolean;
  /** Pulse the dot (e.g. an in-flight job). */
  pulse?: boolean;
  title?: string;
  className?: string;
}

export function Badge({ variant = "neutral", children, dot = false, pulse = false, title, className }: BadgeProps) {
  return (
    <span
      data-variant={variant}
      title={title}
      className={cx(
        "inline-flex h-[18px] max-w-full shrink-0 items-center gap-1 rounded-full border px-2 text-2xs font-medium uppercase tracking-wide whitespace-nowrap",
        VARIANT_CLASSES[variant],
        className,
      )}
    >
      {dot && (
        <span
          aria-hidden="true"
          className={cx("size-1.5 shrink-0 rounded-full", DOT_CLASSES[variant], pulse && "animate-pulse")}
        />
      )}
      <span className="truncate">{children}</span>
    </span>
  );
}
