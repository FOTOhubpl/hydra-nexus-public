/**
 * Code block with a copy button (console snippets, response body, docs). Long lines scroll inside the block, never
 * the page (390 px). Copy uses the async Clipboard API with a textarea fallback; the result is announced politely.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { cx } from "../../../lib/cx";
import { METHOD_STYLES } from "./methods";

export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the legacy path */
  }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch {
    return false;
  }
}

export function CopyButton({ text, label = "Kopiuj", className }: { text: string; label?: string; className?: string }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <button
      type="button"
      onClick={async () => {
        const ok = await copyText(text);
        setState(ok ? "ok" : "fail");
        window.clearTimeout(timer.current);
        timer.current = window.setTimeout(() => setState("idle"), 1600);
      }}
      className={cx(
        "inline-flex h-6 shrink-0 items-center rounded-full border border-line-strong bg-panel px-2 text-xs font-medium text-muted transition-colors hover:bg-hover hover:text-fg focus-visible:outline-2 focus-visible:outline-accent",
        className,
      )}
      aria-label={state === "ok" ? "Skopiowano do schowka" : `${label} do schowka`}
    >
      <span aria-live="polite">{state === "ok" ? "Skopiowano" : state === "fail" ? "Nie udało się" : label}</span>
    </button>
  );
}

export function CodeBlock({
  code,
  label,
  className,
  maxHeight = "28rem",
  testId,
  actions,
}: {
  code: string;
  label?: ReactNode;
  className?: string;
  maxHeight?: string;
  testId?: string;
  actions?: ReactNode;
}) {
  return (
    <div className={cx("min-w-0 overflow-hidden rounded-lg border border-line bg-raised", className)} data-testid={testId}>
      <div className="flex min-w-0 items-center justify-between gap-2 border-b border-line px-3 py-1.5">
        <span className="min-w-0 truncate text-xs font-medium text-subtle">{label ?? "kod"}</span>
        <div className="flex shrink-0 items-center gap-1.5">
          {actions}
          <CopyButton text={code} />
        </div>
      </div>
      <pre
        tabIndex={0}
        className="overflow-auto p-3 font-mono text-xs leading-relaxed text-fg focus-visible:outline-2 focus-visible:outline-accent"
        style={{ maxHeight }}
      >
        <code>{code}</code>
      </pre>
    </div>
  );
}

export function MethodPill({ method, className }: { method: string; className?: string }) {
  const m = method.toUpperCase();
  return (
    <span
      className={cx(
        "inline-flex h-5 min-w-[3.25rem] shrink-0 items-center justify-center rounded-full border px-1.5 font-mono text-[10px] font-semibold tracking-wide",
        METHOD_STYLES[m] ?? "border-line-strong bg-hover text-muted",
        className,
      )}
    >
      {m}
    </span>
  );
}

export function StatusPill({ status }: { status: number | null }) {
  const tone =
    status === null
      ? "border-err/40 bg-err/10 text-err"
      : status < 300
        ? "border-ok/40 bg-ok/10 text-ok"
        : status < 400
          ? "border-info/40 bg-info/10 text-info"
          : status < 500
            ? "border-warn/40 bg-warn/10 text-warn"
            : "border-err/40 bg-err/10 text-err";
  return (
    <span data-testid="response-status" className={cx("inline-flex h-6 items-center rounded-full border px-2.5 font-mono text-xs font-semibold", tone)}>
      {status === null ? "błąd" : status}
    </span>
  );
}
