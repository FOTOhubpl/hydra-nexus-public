import type { Ref } from "react";
import { cx } from "../../lib/cx";

export const STAGES = ["Import", "Geny", "Rzadkość", "Kandydaci", "Dowody", "Decyzja", "Grupowanie", "Wynik"] as const;

/** Stage tabs: 8 equal columns from md up; below md one scrollable row (tabs overlapped at 390 px). */
export function StageRail({ ref, stage, u, go }: { ref?: Ref<HTMLOListElement>; stage: number; u: number; go: (t: number) => void }) {
  return (
    <ol ref={ref} className="flex snap-x gap-2 overflow-x-auto md:grid md:grid-cols-8 md:overflow-visible" aria-label="Etapy" data-testid="stage-rail">
      {STAGES.map((name, i) => (
        <li key={name} className="w-28 shrink-0 snap-start md:w-auto md:min-w-0">
          <button type="button" onClick={() => go(i)} aria-current={i === stage ? "step" : undefined} className={cx("w-full rounded-md px-2 py-1.5 text-left", i === stage ? "bg-active" : "hover:bg-hover")}>
            <span className="flex items-center gap-2 text-sm">
              <span className={cx("flex size-5 items-center justify-center rounded-full font-mono text-xs", i <= stage ? "bg-fg text-canvas" : "border border-line-strong text-subtle")}>{i + 1}</span>
              <span className={cx("truncate", i === stage ? "font-semibold" : "text-muted")}>{name}</span>
            </span>
            <span className="mt-1 block h-1 rounded-full bg-hover"><span className="block h-1 rounded-full bg-accent" style={{ width: `${i < stage ? 100 : i === stage ? u * 100 : 0}%` }} /></span>
          </button>
        </li>
      ))}
    </ol>
  );
}
