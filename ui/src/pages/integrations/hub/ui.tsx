/** Small building blocks shared by the hub and the webhooks tab: switch, modal sheet with focus trap, copy, code. */
import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { Badge, type BadgeVariant } from "../../../components/Badge";
import { Icon } from "../../../components/Icon";
import { Tooltip } from "../../../components/Tooltip";
import { cx } from "../../../lib/cx";

/** Accessible on/off switch (role="switch", Space/Enter via the native button). */
export function Switch({ checked, onChange, label, disabled, busy, testId }: {
  checked: boolean;
  onChange: (next: boolean) => void;
  label: string;
  disabled?: boolean;
  busy?: boolean;
  testId?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      aria-busy={busy || undefined}
      disabled={disabled}
      data-testid={testId}
      onClick={() => onChange(!checked)}
      className={cx(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:cursor-not-allowed disabled:opacity-50",
        checked ? "border-accent bg-accent" : "border-line-strong bg-hover",
      )}
    >
      <span
        aria-hidden="true"
        className={cx("inline-block size-3.5 rounded-full bg-canvas shadow transition-transform motion-reduce:transition-none", checked ? "translate-x-4" : "translate-x-0.5")}
      />
    </button>
  );
}

/** Pill with a tooltip (rounded-full Badge + Tooltip, focusable so keyboard users get the definition). */
export function TipPill({ variant, tip, children, testId }: { variant: BadgeVariant; tip: ReactNode; children: ReactNode; testId?: string }) {
  return (
    <Tooltip content={tip} focusable className="inline-flex max-w-full min-w-0 rounded-full">
      <span data-testid={testId} className="inline-flex max-w-full"><Badge variant={variant} dot>{children}</Badge></span>
    </Tooltip>
  );
}

const FOCUSABLE = "a[href],button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex]:not([tabindex='-1'])";

/**
 * Modal sheet: `side="right"` is the detail drawer (full height, max 36rem, full width on phones), `"center"` a
 * dialog. Focus moves in on open, Tab is trapped, Esc and the backdrop close it, focus returns to the opener.
 */
export function Sheet({ open, onClose, title, children, side = "right", testId, wide }: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  side?: "right" | "center";
  testId?: string;
  wide?: boolean;
}) {
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const prev = document.activeElement as HTMLElement | null;
    const el = panel.current;
    (el?.querySelector<HTMLElement>("[data-autofocus]") ?? el?.querySelector<HTMLElement>(FOCUSABLE))?.focus({ preventScroll: true });
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); closeRef.current(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      const f = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((n) => n.offsetParent !== null || n === document.activeElement);
      if (f.length === 0) { e.preventDefault(); return; }
      const first = f[0]!, last = f[f.length - 1]!;
      if (e.shiftKey && (document.activeElement === first || !panel.current.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !panel.current.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      prev?.focus?.({ preventScroll: true });
    };
  }, [open]);

  if (!open || typeof document === "undefined") return null;
  return createPortal(
    <div className={cx("fixed inset-0 z-50 flex", side === "right" ? "justify-end" : "items-center justify-center p-3")}>
      <div aria-hidden="true" className="absolute inset-0 bg-black/40 motion-safe:animate-[fade-in_120ms_ease-out]" onClick={onClose} />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        data-testid={testId}
        className={cx(
          "relative flex min-w-0 flex-col border-line bg-panel shadow-float",
          side === "right"
            ? cx("h-full w-full border-l", wide ? "sm:max-w-[40rem]" : "sm:max-w-[34rem]")
            : "max-h-[calc(100dvh-1.5rem)] w-full max-w-lg rounded-2xl border",
        )}
      >
        <div className="flex min-h-14 shrink-0 items-center gap-2 border-b border-line px-4 py-2">
          <h2 id={id} className="min-w-0 flex-1 truncate text-base font-semibold text-fg">{title}</h2>
          <button type="button" onClick={onClose} aria-label="Zamknij" className="flex size-8 shrink-0 items-center justify-center rounded-md text-muted hover:bg-hover hover:text-fg">
            <Icon name="close" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through */
  }
  return false;
}

export function CopyButton({ text, label = "Kopiuj", copiedLabel = "Skopiowano", className, testId }: { text: string; label?: string; copiedLabel?: string; className?: string; testId?: string }) {
  const [state, setState] = useState<"idle" | "ok" | "err">("idle");
  useEffect(() => {
    if (state === "idle") return;
    const t = setTimeout(() => setState("idle"), 2000);
    return () => clearTimeout(t);
  }, [state]);
  return (
    <button
      type="button"
      data-testid={testId}
      onClick={async () => setState((await copyText(text)) ? "ok" : "err")}
      className={cx("inline-flex h-6 shrink-0 items-center gap-1 rounded-[4px] border border-line-strong bg-raised px-2 text-xs font-medium text-fg hover:bg-hover", className)}
    >
      {state === "ok" ? copiedLabel : state === "err" ? "Schowek niedostępny" : label}
      <span className="sr-only" aria-live="polite">{state === "ok" ? copiedLabel : ""}</span>
    </button>
  );
}

/** Pretty JSON (or text) viewer with a copy button; long payloads scroll inside the box. */
export function CodeViewer({ value, label, testId, maxHeight = "20rem" }: { value: unknown; label: string; testId?: string; maxHeight?: string }) {
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2) ?? "";
  return (
    <div className="min-w-0 overflow-hidden rounded-lg border border-line bg-raised">
      <div className="flex items-center justify-between gap-2 border-b border-line px-3 py-1.5">
        <span className="truncate text-xs font-medium text-subtle">{label}</span>
        <CopyButton text={text} />
      </div>
      <pre data-testid={testId} tabIndex={0} aria-label={label} className="overflow-auto p-3 font-mono text-xs leading-relaxed break-words whitespace-pre-wrap text-fg" style={{ maxHeight }}>
        {text}
      </pre>
    </div>
  );
}

/** Label + InfoTip-style tooltip on the term (used for every field in the cards and the drawer). */
export function Term({ children, tip }: { children: ReactNode; tip: ReactNode }) {
  return (
    <Tooltip content={tip} focusable className="inline-flex min-w-0">
      <span className="cursor-help truncate border-b border-dotted border-line-strong text-xs text-subtle">{children}</span>
    </Tooltip>
  );
}

export function StepIcon({ ok }: { ok: boolean }) {
  return (
    <span aria-hidden="true" className={cx("flex size-5 shrink-0 items-center justify-center rounded-full border text-xs font-bold", ok ? "border-ok/40 bg-ok/10 text-ok" : "border-err/40 bg-err/10 text-err")}>
      {ok ? "✓" : "✕"}
    </span>
  );
}
