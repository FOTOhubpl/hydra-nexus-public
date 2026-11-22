/**
 * "Reklamy Meta" tab of /watch (`?view=meta&mode=live|recorded|synthetic`): the Meta Ad Library collector
 * (META-COLLECT, /v1/watch/rt/meta/*). Collector state and controls, own budget, separate counters, configuration,
 * collected ads with revisions and artefacts, and per-revision analyses (correlation, fraud triage, manipulation
 * signals as three separate results) with reports. The three data modes are separate spaces and are always labelled.
 */
import { useRef, type KeyboardEvent } from "react";
import { useSearchParams } from "react-router";
import { useCanWrite, useCurrentUser } from "../../api/auth";
import { META_MODES, MODE_TAB, parseMode, periodReportUrl, useMetaConfig, useMetaStatus, type MetaMode } from "../../api/metaAds";
import { cx } from "../../lib/cx";
import { MetaAds } from "../../components/watch/meta/MetaAds";
import { MetaCollector, MetaCounters } from "../../components/watch/meta/MetaCollector";
import { MetaConfigForm } from "../../components/watch/meta/MetaConfigForm";
import { ModeBadge, ModeBanner } from "../../components/watch/meta/common";
import { QueryState } from "./common";

function ModeSwitch({ mode, onChange }: { mode: MetaMode; onChange: (m: MetaMode) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKey = (e: KeyboardEvent, i: number) => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    e.preventDefault();
    const j = (i + (e.key === "ArrowRight" ? 1 : META_MODES.length - 1)) % META_MODES.length;
    onChange(META_MODES[j]!);
    refs.current[j]?.focus();
  };
  return (
    <div role="radiogroup" aria-label="Tryb danych" className="flex flex-wrap gap-1">
      {META_MODES.map((m, i) => (
        <button
          key={m}
          ref={(el) => { refs.current[i] = el; }}
          type="button"
          role="radio"
          aria-checked={mode === m}
          tabIndex={mode === m ? 0 : -1}
          data-testid={`meta-mode-${m}`}
          onClick={() => onChange(m)}
          onKeyDown={(e) => onKey(e, i)}
          className={cx(
            "h-7 rounded-full border px-3 text-xs font-medium whitespace-nowrap",
            mode === m ? "border-accent-strong bg-accent-strong text-canvas" : "border-line-strong bg-raised text-fg hover:bg-hover",
          )}
        >
          {MODE_TAB[m]}
        </button>
      ))}
    </div>
  );
}

export function WatchMeta() {
  const [params, setParams] = useSearchParams();
  const mode = parseMode(params.get("mode"));
  const status = useMetaStatus(mode);
  const config = useMetaConfig();
  const user = useCurrentUser();
  const canWrite = useCanWrite();
  const isAdmin = !user || user.role === "admin";
  const setMode = (m: MetaMode) => {
    const next = new URLSearchParams(params);
    next.set("view", "meta");
    if (m === "live") next.delete("mode");
    else next.set("mode", m);
    setParams(next, { replace: true });
  };
  const s = status.data;
  return (
    <div className="flex flex-col gap-4 p-4" data-testid="watch-meta">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-xl font-semibold text-fg">Reklamy Meta</h2>
        <ModeBadge mode={mode} />
        <ModeSwitch mode={mode} onChange={setMode} />
        <span className="ml-auto flex gap-3 text-xs">
          <span className="text-subtle">Raport okresu:</span>
          <a href={periodReportUrl(mode, "json")} data-testid="period-report-json" className="text-accent hover:underline" download>JSON</a>
          <a href={periodReportUrl(mode, "md")} data-testid="period-report-md" className="text-accent hover:underline" download>Markdown</a>
        </span>
      </div>
      <ModeBanner mode={mode} />
      <p className="text-xs text-muted">
        Reklamy z publicznej biblioteki reklam Meta (kraj dotarcia, nie siedziba reklamodawcy). Kampanie rozpoznaje HYDRA na podstawie
        materiału; biblioteka nie ujawnia wewnętrznych kampanii reklamodawcy. Raport to raport obserwacji, nie potwierdzone zgłoszenie
        nadużycia; nic nie jest wysyłane automatycznie do Meta, CERT ani poczty.
      </p>
      <QueryState query={status} what="stan kolektora Meta" endpoint="/v1/watch/rt/meta/status" />
      {s && (
        <>
          <MetaCollector s={s} isAdmin={isAdmin} />
          <MetaCounters s={s} />
        </>
      )}
      <MetaAds mode={mode} canWrite={canWrite} />
      {config.data && <MetaConfigForm config={config.data} isAdmin={isAdmin} />}
    </div>
  );
}
