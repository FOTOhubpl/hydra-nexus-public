/**
 * Bento primitives for the dashboard ("Pulpit"): card, KPI tile, labelled mini-bars and a segmented stack.
 * Layered surface (bg-panel + shadow-card on the canvas), thin marks, every mark carries a text label or title.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { cx } from "../../lib/cx";
import { formatCount } from "../../lib/format";
import { useReducedMotion } from "../../lib/media";
import { SkeletonBlock } from "../Skeleton";
import { Tooltip } from "../Tooltip";

/**
 * Animated number: the first value renders as is (first paint shows the real number), later changes (live deltas)
 * count from the previous value over ~600 ms. Reduced motion: jumps straight to the new value.
 */
export function useCountUp(value: number, ms = 600): number {
  const reduced = useReducedMotion();
  const [shown, setShown] = useState(value);
  const from = useRef(value);
  useEffect(() => {
    if (reduced || typeof requestAnimationFrame === "undefined" || from.current === value) {
      from.current = value;
      setShown(value);
      return;
    }
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const tick = (t: number) => {
      const k = Math.min(1, (t - start) / ms);
      const eased = 1 - (1 - k) ** 3;
      setShown(Math.round(a + (value - a) * eased));
      if (k < 1) raf = requestAnimationFrame(tick);
      else from.current = value;
    };
    raf = requestAnimationFrame(tick);
    return () => { cancelAnimationFrame(raf); from.current = value; };
  }, [value, ms, reduced]);
  return shown;
}

/** True for ~1.6 s after `key` changes (not on mount): drives the `.live-flash` highlight of a card. */
export function useFlash(key: unknown, ms = 1600): boolean {
  const [on, setOn] = useState(false);
  const first = useRef(true);
  const prev = useRef(key);
  useEffect(() => {
    if (first.current) { first.current = false; prev.current = key; return; }
    if (Object.is(prev.current, key)) return;
    const was = prev.current;
    prev.current = key;
    if (was === undefined || was === null) return; // first data after loading is not a live change
    setOn(true);
    const t = setTimeout(() => setOn(false), ms);
    return () => clearTimeout(t);
  }, [key, ms]);
  return on;
}

/** Count-up number formatted like formatCount (Polish grouping). */
export function CountUp({ value, format = formatCount }: { value: number; format?: (n: number) => string }) {
  return <>{format(useCountUp(value))}</>;
}

export function Card({ title, sub, to, toLabel = "szczegóły", children, className, testId, tip, flashKey }: {
  title: string;
  sub?: ReactNode;
  /** Definition shown in a tooltip on the card title. */
  tip?: ReactNode;
  /** When this value changes (live delta), the card briefly highlights. */
  flashKey?: unknown;
  to?: string;
  toLabel?: string;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  const flash = useFlash(flashKey);
  return (
    <section data-testid={testId} data-flash={flash ? "true" : undefined} className={cx("surface-card flex min-w-0 flex-col", flash && "live-flash", className)}>
      <div className="flex min-h-11 items-baseline gap-3 px-4 pt-4 pb-2.5 sm:px-5">
        {tip ? (
          <Tooltip content={tip} focusable className="inline-flex shrink-0 rounded-sm">
            <h2 className="shrink-0 cursor-help text-sm font-semibold whitespace-nowrap text-fg decoration-line-strong decoration-dotted underline-offset-4 hover:underline">{title}</h2>
          </Tooltip>
        ) : <h2 className="shrink-0 text-sm font-semibold whitespace-nowrap text-fg">{title}</h2>}
        {sub && <span className="min-w-0 truncate text-xs text-subtle">{sub}</span>}
        {to && <Link to={to} className="ml-auto shrink-0 rounded-sm text-xs text-accent hover:underline">{toLabel}</Link>}
      </div>
      <div className="min-h-0 min-w-0 flex-1 px-4 pb-4 sm:px-5">{children}</div>
    </section>
  );
}

/** KPI tile. When `to` is set the whole tile is one link whose accessible name starts with "<label> <value>". */
export function Kpi({ label, value, count, format, tip, hint, aside, foot, to, accent = false, testId }: {
  label: string;
  value?: ReactNode;
  /** Numeric value: rendered with a count-up on change and a short highlight (live deltas). Overrides `value`. */
  count?: number;
  format?: (n: number) => string;
  /** Definition of the KPI (tooltip on hover and keyboard focus). */
  tip?: ReactNode;
  hint?: ReactNode;
  aside?: ReactNode;
  foot?: ReactNode;
  to?: string;
  accent?: boolean;
  /** `data-testid` of the tile (e2e, CR-E2E-UI). */
  testId?: string;
}) {
  const flash = useFlash(count ?? value);
  // divs, not spans: block children give the link an accessible name with spaces ("Incydenty 20 …")
  const shown = count !== undefined ? <CountUp value={count} format={format} /> : value;
  const body = (
    <>
      <div className="text-xs font-medium text-subtle">{label}</div>
      <div className="mt-2 flex items-end justify-between gap-2">
        <span className={cx("font-mono text-[26px] leading-none font-semibold tracking-tight tabular-nums", accent ? "text-accent" : "text-fg")}>{shown}</span>
        {aside}
      </div>
      {hint && <div className="mt-1.5 truncate text-xs text-muted">{hint}</div>}
      {foot && <div className="mt-2">{foot}</div>}
    </>
  );
  const cls = cx("surface-card block w-full min-w-0 px-4 py-3.5", flash && "live-flash");
  const tile = to ? (
    <Link to={to} data-testid={testId} data-flash={flash ? "true" : undefined} className={cx(cls, "surface-hover")}>{body}</Link>
  ) : (
    <div data-testid={testId} data-flash={flash ? "true" : undefined} className={cls}>{body}</div>
  );
  if (!tip) return tile;
  // a plain tile is not focusable: the tooltip wrapper becomes the tab stop so the definition is keyboard-reachable
  return <Tooltip content={tip} side="bottom" focusable={!to} className="flex w-full rounded-[var(--radius-card)]">{tile}</Tooltip>;
}

export interface BarRow {
  id: string;
  label: ReactNode;
  value: number;
  /** Text next to the bar; defaults to the value. */
  valueLabel?: string;
  color?: string;
  title?: string;
  extra?: ReactNode;
  onPick?: () => void;
}

/** Horizontal labelled bars relative to the largest value (or `max`). Rows are buttons when `onPick` is set. */
export function MiniBars({ rows, max, unit }: { rows: BarRow[]; max?: number; unit?: string }) {
  const top = Math.max(1, max ?? Math.max(0, ...rows.map((r) => r.value)));
  return (
    <ul className="space-y-1.5">
      {rows.map((r) => {
        const pct = Math.max(2, Math.round((Math.max(0, r.value) / top) * 100));
        const inner = (
          <>
            <span className="flex items-center gap-2 text-xs">
              <span className="min-w-0 flex-1 truncate text-left">{r.label}</span>
              {r.extra}
              <span className="shrink-0 font-mono text-muted tabular-nums">{r.valueLabel ?? r.value}{unit ? ` ${unit}` : ""}</span>
            </span>
            <span aria-hidden="true" className="mt-1 block h-1.5 overflow-hidden rounded-full bg-hover">
              <span className="block h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: r.color ?? "var(--color-accent)" }} />
            </span>
          </>
        );
        return (
          <li key={r.id} title={r.title}>
            {r.onPick ? (
              <button type="button" onClick={r.onPick} className="block min-h-9 w-full rounded-md px-1 py-1 text-left hover:bg-hover sm:min-h-0">{inner}</button>
            ) : (
              <div className="px-1 py-1">{inner}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** One thin segmented bar (2 px gaps), e.g. genes by family. Labelled via aria-label and per-segment titles. */
export function StackBar({ parts, label, className }: { parts: { key: string; value: number; color: string; title: string }[]; label: string; className?: string }) {
  const total = parts.reduce((s, p) => s + Math.max(0, p.value), 0);
  if (total <= 0) return null;
  return (
    <span role="img" aria-label={label} className={cx("flex h-1.5 w-full gap-[2px] overflow-hidden rounded-full", className)}>
      {parts.filter((p) => p.value > 0).map((p) => (
        <span key={p.key} title={p.title} className="block h-full first:rounded-l-full last:rounded-r-full" style={{ flexGrow: p.value, flexBasis: 0, backgroundColor: p.color }} />
      ))}
    </span>
  );
}

export function CardSkeleton({ lines = 4 }: { lines?: number }) {
  return (
    <div aria-hidden="true" className="space-y-2 pt-1">
      {Array.from({ length: lines }, (_, i) => <SkeletonBlock key={i} className={cx("h-4", i === lines - 1 ? "w-2/3" : "w-full")} />)}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div role="status" className="rounded-lg border border-dashed border-line px-3 py-5 text-center text-sm text-subtle">{children}</div>;
}
