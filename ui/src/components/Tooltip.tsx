/**
 * One accessible tooltip for the whole UI (docs/ui/UI_LOG.md, 2026-11-22 floating chrome).
 * - Opens on hover (short delay) and on keyboard focus (immediately); Esc closes it; it never traps focus.
 * - The trigger gets `aria-describedby` while the tip is shown, so screen readers read the definition after the name.
 * - Rendered in a portal with fixed positioning, flipped / clamped to stay inside the viewport (scrolling rails
 *   would clip an absolutely positioned tip).
 * - Fade only under `motion-safe`; reduced motion shows it instantly.
 */
import { cloneElement, isValidElement, useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type ReactElement, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { cx } from "../lib/cx";

export type TipSide = "top" | "bottom" | "left" | "right";

interface Pos { top: number; left: number; side: TipSide }

const GAP = 8;
const focusVisible = (el: HTMLElement) => {
  try { return el.matches(":focus-visible"); } catch { return true; }
};
const MARGIN = 8;

function place(anchor: DOMRect, tip: DOMRect, side: TipSide): Pos {
  const vw = window.innerWidth, vh = window.innerHeight;
  let s = side;
  if (s === "top" && anchor.top - tip.height - GAP < MARGIN) s = "bottom";
  else if (s === "bottom" && anchor.bottom + tip.height + GAP > vh - MARGIN) s = "top";
  else if (s === "right" && anchor.right + tip.width + GAP > vw - MARGIN) s = "left";
  else if (s === "left" && anchor.left - tip.width - GAP < MARGIN) s = "right";
  let top: number, left: number;
  if (s === "top" || s === "bottom") {
    top = s === "top" ? anchor.top - tip.height - GAP : anchor.bottom + GAP;
    left = anchor.left + anchor.width / 2 - tip.width / 2;
  } else {
    top = anchor.top + anchor.height / 2 - tip.height / 2;
    left = s === "left" ? anchor.left - tip.width - GAP : anchor.right + GAP;
  }
  left = Math.max(MARGIN, Math.min(left, vw - tip.width - MARGIN));
  top = Math.max(MARGIN, Math.min(top, vh - tip.height - MARGIN));
  return { top, left, side: s };
}

/**
 * Wraps exactly one focusable element (button, link, input) or any element when `focusable` is set (a tab stop is
 * then added to the wrapper). `content` may be rich text; keep it short (one or two sentences, Polish).
 */
export function Tooltip({ content, children, side = "top", delay = 350, focusable = false, className }: {
  content: ReactNode;
  children: ReactNode;
  side?: TipSide;
  delay?: number;
  /** The child is not focusable itself (text, a disabled button): the wrapper becomes the tab stop. */
  focusable?: boolean;
  className?: string;
}) {
  const id = useId();
  const wrap = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<Pos | null>(null);

  const clear = () => { if (timer.current) clearTimeout(timer.current); timer.current = null; };
  const show = useCallback((now: boolean) => {
    clear();
    if (now) setOpen(true);
    else timer.current = setTimeout(() => setOpen(true), delay);
  }, [delay]);
  const hide = useCallback(() => { clear(); setOpen(false); setPos(null); }, []);

  useEffect(() => clear, []);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") hide(); };
    const onScroll = () => hide();
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => { window.removeEventListener("keydown", onKey); window.removeEventListener("scroll", onScroll, true); };
  }, [open, hide]);
  useLayoutEffect(() => {
    if (!open || !wrap.current || !tipRef.current) return;
    const anchor = (wrap.current.firstElementChild as HTMLElement | null) ?? wrap.current;
    setPos(place(anchor.getBoundingClientRect(), tipRef.current.getBoundingClientRect(), side));
  }, [open, side, content]);

  // no content: same element tree (a remount would drop focus, e.g. a popover trigger after Esc), no tip
  const enabled = !(content == null || content === "" || content === false);
  useEffect(() => { if (!enabled) hide(); }, [enabled, hide]);
  const child = enabled && !focusable && isValidElement(children)
    ? cloneElement(children as ReactElement<{ "aria-describedby"?: string }>, { "aria-describedby": open ? id : (children as ReactElement<{ "aria-describedby"?: string }>).props["aria-describedby"] })
    : children;
  return (
    <span
      ref={wrap}
      className={className ?? "inline-flex max-w-full min-w-0"}
      tabIndex={focusable && enabled ? 0 : undefined}
      aria-describedby={focusable && open ? id : undefined}
      onPointerEnter={(e) => { if (enabled && e.pointerType !== "touch") show(false); }}
      onPointerLeave={hide}
      onFocus={(e) => { if (enabled && focusVisible(e.target as HTMLElement)) show(true); }}
      onBlur={hide}
      data-tooltip-trigger=""
    >
      {child}
      {open && enabled && typeof document !== "undefined" && createPortal(
        <div
          ref={tipRef}
          id={id}
          role="tooltip"
          data-side={pos?.side ?? side}
          className={cx(
            "pointer-events-none fixed z-[60] max-w-[min(20rem,calc(100vw-1rem))] rounded-lg bg-fg px-2.5 py-1.5 text-xs leading-snug font-medium text-canvas shadow-float",
            pos ? "motion-safe:animate-[fade-in_120ms_ease-out]" : "invisible",
          )}
          style={{ top: pos?.top ?? 0, left: pos?.left ?? 0 }}
        >
          {content}
        </div>,
        document.body,
      )}
    </span>
  );
}

/** Small "i" next to a label: a focusable definition for terms (bit, decision, KPI). */
export function InfoTip({ content, label = "Wyjaśnienie", side = "top" }: { content: ReactNode; label?: string; side?: TipSide }) {
  return (
    <Tooltip content={content} side={side}>
      <button type="button" aria-label={label} className="inline-flex size-4 shrink-0 items-center justify-center rounded-full border border-line-strong text-[9px] leading-none font-semibold text-subtle hover:border-accent hover:text-accent" onClick={(e) => e.preventDefault()}>
        i
      </button>
    </Tooltip>
  );
}
