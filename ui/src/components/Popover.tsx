/**
 * Floating popover for the top bar (run details, notifications, shortcuts): a trigger button and a panel in the
 * floating-chrome style (rounded card, soft depth). Click or Enter toggles, Esc and an outside click close it and
 * return focus to the trigger. The panel is a non-modal dialog labelled by `label`.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { cx } from "../lib/cx";
import { Tooltip } from "./Tooltip";

export function Popover({ label, tip, trigger, children, triggerClassName, panelClassName, align = "end", testId, onOpenChange, badge }: {
  /** Accessible name of the trigger and of the dialog. */
  label: string;
  /** Tooltip of the trigger (defaults to `label`). */
  tip?: ReactNode;
  trigger: ReactNode;
  children: ReactNode | ((close: () => void) => ReactNode);
  triggerClassName?: string;
  panelClassName?: string;
  align?: "start" | "end";
  testId?: string;
  onOpenChange?: (open: boolean) => void;
  /** Extra text for screen readers on the trigger (e.g. unread count). */
  badge?: string;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const btn = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const set = (v: boolean) => { setOpen(v); onOpenChange?.(v); };
  const close = () => { set(false); btn.current?.focus(); };

  useEffect(() => {
    if (!open) return;
    panel.current?.querySelector<HTMLElement>("[data-autofocus],a[href],button:not(:disabled),select,input")?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); setOpen(false); onOpenChange?.(false); btn.current?.focus(); }
    };
    const onDown = (e: PointerEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) { setOpen(false); onOpenChange?.(false); }
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onDown);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("pointerdown", onDown); };
  }, [open, onOpenChange]);

  return (
    <div ref={root} className="relative shrink-0" data-testid={testId}>
      <Tooltip content={open ? null : (tip ?? label)} side="bottom">
        <button
          ref={btn}
          type="button"
          aria-label={badge ? `${label} (${badge})` : label}
          aria-expanded={open}
          aria-haspopup="dialog"
          aria-controls={open ? id : undefined}
          onClick={() => set(!open)}
          className={cx("relative flex size-9 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-hover hover:text-fg aria-expanded:bg-hover aria-expanded:text-fg", triggerClassName)}
        >
          {trigger}
        </button>
      </Tooltip>
      {open && (
        <div
          ref={panel}
          id={id}
          role="dialog"
          aria-label={label}
          className={cx(
            "absolute top-[calc(100%+10px)] z-40 w-[min(22rem,calc(100vw-1.5rem))] overflow-hidden rounded-2xl border border-line bg-panel text-left shadow-float motion-safe:animate-[pop-in_160ms_var(--ease-out-soft)]",
            align === "end" ? "right-0" : "left-0",
            panelClassName,
          )}
        >
          {typeof children === "function" ? children(close) : children}
        </div>
      )}
    </div>
  );
}

export function PopoverHeader({ title, sub, action }: { title: string; sub?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex items-start gap-2 border-b border-line px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="text-sm font-semibold text-fg">{title}</div>
        {sub && <div className="mt-0.5 text-xs text-subtle">{sub}</div>}
      </div>
      {action}
    </div>
  );
}
