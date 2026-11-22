import { useEffect, useState } from "react";
import { cx } from "../lib/cx";

export const SLOW_HINT_MS = 1500;
export const SLOW_HINT_TEXT = "Wczytywanie danych przebiegu… (duży zbiór, pierwsze otwarcie może potrwać)";

/** True once `ms` elapsed since mount; for a "still loading" hint that does not flicker on fast responses. */
export function useElapsed(ms = SLOW_HINT_MS): boolean {
  const [done, setDone] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => setDone(true), ms);
    return () => window.clearTimeout(t);
  }, [ms]);
  return done;
}

/** Grey placeholder block; pulses only when the user allows motion. */
export function SkeletonBlock({ className }: { className?: string }) {
  return <span aria-hidden="true" className={cx("block rounded-md bg-hover motion-safe:animate-pulse", className)} />;
}

export function SlowHint({ className }: { className?: string }) {
  const slow = useElapsed();
  return slow ? <div className={cx("text-xs text-muted", className)}>{SLOW_HINT_TEXT}</div> : null;
}

/** Lightweight loading placeholder for a section: a few grey lines, the label, then the slow-load hint. */
export function Skeleton({ label, lines = 3, className }: { label: string; lines?: number; className?: string }) {
  return (
    <div role="status" aria-busy="true" aria-live="polite" data-testid="skeleton" className={cx("flex flex-col gap-2 px-4 py-4", className)}>
      <span className="text-xs text-subtle">{label}</span>
      {Array.from({ length: lines }, (_, i) => (
        <SkeletonBlock key={i} className={cx("h-3", i === lines - 1 ? "w-2/3" : "w-full")} />
      ))}
      <SlowHint />
    </div>
  );
}
