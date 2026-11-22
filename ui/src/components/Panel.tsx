import type { ReactNode } from "react";
import { cx } from "../lib/cx";

export interface PanelProps {
  title?: ReactNode;
  /** Right-aligned header content (counts, actions). */
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
  /** Remove body padding (tables, canvases). */
  flush?: boolean;
}

/** Shared card surface (`.surface-card`) with an optional header row: title left, counts / actions right. */
export function Panel({ title, actions, children, className, bodyClassName, flush = false }: PanelProps) {
  return (
    <section className={cx("surface-card flex min-h-0 min-w-0 flex-col", className)}>
      {(title || actions) && (
        <header className="flex h-12 shrink-0 items-center justify-between gap-3 border-b border-line px-4 sm:px-5">
          <h2 className="truncate text-sm font-semibold text-fg">{title}</h2>
          {actions && <div className="flex items-center gap-2">{actions}</div>}
        </header>
      )}
      <div className={cx("min-h-0 flex-1", flush ? "overflow-hidden rounded-b-[calc(var(--radius-card)-1px)]" : "p-3 sm:p-4", bodyClassName)}>{children}</div>
    </section>
  );
}
