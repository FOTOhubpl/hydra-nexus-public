/**
 * /watch "Monitoring domen": three views in one route.
 * - "Na żywo" (default): the realtime watch engine over public sources (WatchLive.tsx, /v1/watch/rt/*).
 * - "Odkrywanie domen (nagrania)": the classic CERT/CT domain ticker and alerts (WatchClassic.tsx, /v1/watch/*),
 *   unchanged; `?view=classic` (or the classic `?tab=alerts`) opens it directly.
 * - "Reklamy Meta": the Meta Ad Library collector (WatchMeta.tsx, /v1/watch/rt/meta/*); `?view=meta`.
 * - "Automatyczne wyszukiwanie": the whole automatic search loop (WatchAuto.tsx, BONUS-AUTO); `?view=auto`.
 */
import { useRef, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router";
import { cx } from "../../lib/cx";
import { WatchClassic } from "./WatchClassic";
import { WatchLive } from "./WatchLive";
import { WatchAuto } from "./WatchAuto";
import { WatchMeta } from "./WatchMeta";

const TABS = [
  { id: "live", label: "Na żywo" },
  { id: "classic", label: "Odkrywanie domen (nagrania)" },
  { id: "meta", label: "Reklamy Meta" },
  { id: "auto", label: "Automatyczne wyszukiwanie" },
] as const;
type TabId = (typeof TABS)[number]["id"];

export function WatchPage() {
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const tab: TabId = view === "auto" ? "auto" : view === "meta" ? "meta" : view === "classic" || params.get("tab") === "alerts" ? "classic" : "live";
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const go = (id: TabId) => {
    const next = new URLSearchParams(params);
    if (id === "live") { next.delete("view"); next.delete("tab"); next.delete("mode"); }
    else { next.set("view", id); if (id !== "meta" && id !== "auto") next.delete("mode"); }
    setParams(next, { replace: true });
  };
  const onKey = (e: KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const j = (i + (e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length;
    go(TABS[j]!.id);
    refs.current[j]?.focus();
  };
  return (
    <div className="flex min-w-0 flex-col">
      <div role="tablist" aria-label="Widok monitoringu" className="flex flex-wrap gap-1 px-4 pt-4">
        {TABS.map((t, i) => (
          <button
            key={t.id}
            ref={(el) => { refs.current[i] = el; }}
            type="button"
            role="tab"
            id={`watch-tab-${t.id}`}
            aria-selected={tab === t.id}
            aria-controls={`watch-panel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => go(t.id)}
            onKeyDown={(e) => onKey(e, i)}
            className={cx(
              "h-8 rounded-full border px-3 text-sm font-medium whitespace-nowrap transition-colors",
              tab === t.id ? "border-accent-strong bg-accent-strong text-canvas" : "border-line-strong bg-raised text-fg hover:bg-hover",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`watch-panel-${tab}`} aria-labelledby={`watch-tab-${tab}`} className="min-w-0">
        {tab === "live" ? <WatchLive /> : tab === "meta" ? <WatchMeta /> : tab === "auto" ? <WatchAuto /> : <WatchClassic />}
      </div>
    </div>
  );
}
