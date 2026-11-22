/**
 * Live activity of an analyse run on /analyse (IMPORT-PROGRESS): stepper (Wysyłanie → Import → ... → Gotowe),
 * overall and per-stage bars, current item, counters, elapsed / rate / ETA ("szacunek", only after enough measured
 * samples), a stall notice after 20 s without a changed snapshot, and a rolling log of the last 50 events.
 * Indeterminate stages are shown as such; no movement is ever simulated.
 */
import { useEffect, useState } from "react";
import type { AnalyseProgress, ProgressSample } from "../api/progress";
import type { UploadProgress } from "../api/upload";
import { cx } from "../lib/cx";
import { formatBytes, formatCount } from "../lib/format";
import { useReducedMotion } from "../lib/media";
import {
  deriveLog, etaSeconds, formatDuration, importText, measuredRate, overallPercent, silenceSeconds, STALL_S, stagePoints,
  uploadText, type Point,
} from "../lib/progressMath";
import { Button } from "./Button";

export type UploadPhase = "uploading" | "processing" | "done" | "error" | "cancelled";

export interface UploadState {
  phase: UploadPhase;
  samples: UploadProgress[];
  error?: string | null;
}

const DEFAULT_STAGES = [{ key: "import", label: "Import" }];

export function ProgressBar({ value, label, valueText, reducedMotion, testId, tone = "accent" }: {
  value: number | null; label: string; valueText: string; reducedMotion: boolean; testId?: string; tone?: "accent" | "ok" | "err";
}) {
  const fill = tone === "ok" ? "bg-ok" : tone === "err" ? "bg-err" : "bg-accent";
  return (
    <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value === null ? undefined : Math.round(value)}
      aria-valuetext={valueText} aria-busy={value === null ? true : undefined} data-testid={testId}
      className="relative h-2 overflow-hidden rounded-full bg-hover">
      {value === null ? (
        <div className={cx("absolute inset-y-0 w-1/3 rounded-full", fill, reducedMotion ? "left-0 opacity-60" : "animate-indeterminate")} />
      ) : (
        <div className={cx("absolute inset-y-0 left-0 rounded-full", fill, !reducedMotion && "transition-[width] duration-300")} style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
      )}
    </div>
  );
}

function Step({ label, state }: { label: string; state: "done" | "running" | "waiting" | "failed" }) {
  return (
    <li className={cx("flex min-w-0 items-center gap-1.5 rounded-md border px-2 py-1 text-xs",
      state === "done" ? "border-ok/50 text-fg" : state === "running" ? "border-accent bg-for-soft font-semibold text-fg" : state === "failed" ? "border-err text-err" : "border-line text-subtle")}
      aria-current={state === "running" ? "step" : undefined}>
      <span aria-hidden="true">{state === "done" ? "✓" : state === "failed" ? "✕" : state === "running" ? "●" : "○"}</span>
      <span className="truncate">{label}</span>
      <span className="sr-only">{state === "done" ? " (zakończony)" : state === "running" ? " (w toku)" : state === "failed" ? " (błąd)" : " (oczekuje)"}</span>
    </li>
  );
}

function uploadMeasure(samples: UploadProgress[]) {
  const last = samples[samples.length - 1];
  const pts: Point[] = samples.map((s) => ({ t: s.at, v: s.loaded }));
  const rate = measuredRate(pts.slice(-20));
  const eta = last ? etaSeconds(last.loaded, last.total, rate) : null;
  return { last, rate, eta };
}

export interface AnalyseActivityProps {
  upload: UploadState | null;
  samples: ProgressSample[];
  onCancel?: () => void;
  onRetry?: () => void;
  /** Link (href) to the import report on the page, shown once the import phase is done. */
  reportHref?: string | null;
  /** Fixed clock for tests; otherwise a 1 s ticker. */
  now?: number;
  compact?: boolean;
}

export function AnalyseActivity({ upload, samples, onCancel, onRetry, reportHref, now: fixedNow, compact }: AnalyseActivityProps) {
  const reducedMotion = useReducedMotion();
  const [tick, setTick] = useState(() => Date.now());
  useEffect(() => {
    if (fixedNow !== undefined) return;
    const t = setInterval(() => setTick(Date.now()), 1000);
    return () => clearInterval(t);
  }, [fixedNow]);
  const now = fixedNow ?? tick;
  const snap: AnalyseProgress | undefined = samples[samples.length - 1]?.snap;
  const terminal = snap?.status === "done" || snap?.status === "failed";
  const up = upload ? uploadMeasure(upload.samples) : null;

  // ---- stepper
  const steps: { key: string; label: string; state: "done" | "running" | "waiting" | "failed" }[] = [];
  if (upload) {
    steps.push({ key: "upload", label: "Wysyłanie", state: upload.phase === "done" ? "done" : upload.phase === "error" || upload.phase === "cancelled" ? "failed" : "running" });
  }
  const serverStages = snap?.stages ?? DEFAULT_STAGES.map((s) => ({ ...s, state: "waiting" as const, weight: 0, percent: null }));
  steps.push(...serverStages.map((s) => ({ key: s.key, label: s.label, state: s.state })));

  // ---- current stage bar
  let stageValue: number | null = null;
  let stageText = "";
  let stageLabel = "Etap";
  let item: string | null = null;
  let rateText: string | null = null;
  let etaText: string | null = null;
  const uploading = upload && (upload.phase === "uploading" || upload.phase === "processing");
  if (uploading && up) {
    stageLabel = "Wysyłanie plików";
    if (upload.phase === "processing") {
      stageText = "Rozpakowywanie… (serwer zapisuje i sprawdza przesłane pliki)";
    } else {
      const total = up.last?.total ?? null;
      stageValue = total ? Math.floor(((up.last?.loaded ?? 0) * 100) / total) : null;
      stageText = uploadText(up.last?.loaded ?? 0, total, up.rate, up.eta);
    }
  } else if (snap) {
    stageLabel = snap.stage_label;
    if (snap.stage === "import" && snap.import) {
      stageText = importText(snap.import);
      stageValue = snap.import.files_total === null || snap.import.phase === "listing" ? null : snap.import.percent ?? 0;
      item = snap.import.current;
    } else if (snap.status === "done") {
      stageValue = 100;
      stageText = "Gotowe";
    } else {
      stageValue = snap.stage_percent;
      const c = snap.counters;
      stageText = stageValue === null
        ? `${snap.stage_label}… (ten etap nie raportuje licznika)`
        : `${snap.stage_label}: ${Math.floor(stageValue)}%${typeof c.done === "number" ? ` (${formatCount(c.done)} z ${formatCount(c.total ?? 0)} ${c.unit ?? ""})` : ""}`.trim();
      if (c.item) item = c.item;
      if (c.sub) stageText += ` · ${c.sub}`;
    }
    const pts = stagePoints(samples);
    const rate = measuredRate(pts);
    const lastV = pts[pts.length - 1]?.v ?? 0;
    const total = snap.stage === "import" ? snap.import?.files_total ?? null : snap.counters.total ?? null;
    if (rate !== null && !terminal) {
      rateText = `${rate >= 10 ? Math.round(rate) : rate.toFixed(1).replace(".", ",")} ${snap.stage === "import" ? "plików" : snap.counters.unit ?? ""}/s`;
      const eta = etaSeconds(lastV, total, rate);
      if (eta !== null) etaText = `ok. ${formatDuration(eta)} do końca etapu (szacunek)`;
    }
  } else {
    stageText = "Oczekiwanie na serwer…";
  }

  const overall = snap ? overallPercent(samples) : null;
  const started = snap?.started_at ? Date.parse(snap.started_at) : NaN;
  const finished = snap?.finished_at ? Date.parse(snap.finished_at) : NaN;
  const elapsed = Number.isNaN(started) ? null : ((Number.isNaN(finished) ? now : finished) - started) / 1000;
  const silent = snap && !terminal && !uploading ? silenceSeconds(samples, now) : null;
  const stalled = silent !== null && silent > STALL_S;
  const log = deriveLog(samples);
  const imp = snap?.import;
  const core = snap?.core ?? {};
  const failed = upload?.phase === "error" || snap?.status === "failed";
  const cancelled = upload?.phase === "cancelled";

  return (
    <section data-testid="analyse-activity" aria-label="Postęp analizy" className={cx("space-y-2 rounded-md border border-line bg-raised p-3 text-xs", compact && "p-2")}>
      <ol className="flex flex-wrap gap-1" aria-label="Etapy">
        {steps.map((s) => <Step key={s.key} label={s.label} state={s.state} />)}
      </ol>

      {!cancelled && !(upload?.phase === "error") && (
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            <span className="font-medium text-fg" data-testid="activity-stage-text">{stageText}</span>
            {uploading && upload.phase === "uploading" && onCancel && <Button className="ml-auto" onClick={onCancel}>Anuluj</Button>}
          </div>
          <ProgressBar testId="activity-stage-bar" label={stageLabel} value={stageValue} reducedMotion={reducedMotion}
            valueText={stageValue === null ? `${stageLabel}: w toku, bez licznika` : `${stageLabel}: ${Math.floor(stageValue)}%`}
            tone={snap?.status === "done" ? "ok" : snap?.status === "failed" ? "err" : "accent"} />
          {item && <div className="truncate font-mono text-muted" title={item} data-testid="activity-item">{snap?.stage === "import" ? "Import" : stageLabel}: {snap?.mode === "single" && typeof core.incident_id === "string" && snap.stage !== "import" ? `incydent ${core.incident_id} · ` : ""}{item}</div>}
        </div>
      )}

      {overall !== null && !compact && (
        <div className="space-y-1">
          <div className="flex flex-wrap items-baseline gap-x-2 text-muted">
            <span className="font-medium text-fg">Całość: {Math.floor(overall)}%</span>
            <span className="text-subtle" title={snap?.weights_source}>wagi etapów z pomiarów ({snap?.weights_source})</span>
          </div>
          <ProgressBar testId="activity-overall-bar" label="Całość analizy" value={overall} reducedMotion={reducedMotion}
            valueText={`Całość: ${Math.floor(overall)}% (etap: ${snap?.stage_label ?? ""})`} tone={snap?.status === "done" ? "ok" : snap?.status === "failed" ? "err" : "accent"} />
        </div>
      )}

      {snap && (
        <dl className="flex flex-wrap gap-x-4 gap-y-0.5 text-muted" data-testid="activity-counters">
          {imp && <><dt className="sr-only">pliki</dt><dd>pliki: <span className="font-mono text-fg">{formatCount(imp.files_done)}{imp.files_total !== null ? ` / ${formatCount(imp.files_total)}` : ""}</span>{imp.bytes_total ? ` (${formatBytes(imp.bytes_done)} z ${formatBytes(imp.bytes_total)})` : ""}</dd></>}
          {imp && <><dt className="sr-only">odrzucone</dt><dd>odrzucone: <span className="font-mono text-fg">{formatCount(imp.rejected)}</span></dd></>}
          {typeof core.incidents_total === "number" && core.incidents_total > 0 && <><dt className="sr-only">incydenty</dt><dd>incydenty: <span className="font-mono text-fg">{formatCount(Number(core.incidents_done ?? 0))} / {formatCount(core.incidents_total)}</span></dd></>}
          {typeof core.genes === "number" && core.genes > 0 && <><dt className="sr-only">geny</dt><dd>geny: <span className="font-mono text-fg">{formatCount(core.genes)}</span></dd></>}
          {typeof core.pairs_scored === "number" && core.pairs_scored > 0 && <><dt className="sr-only">pary</dt><dd>pary ocenione: <span className="font-mono text-fg">{formatCount(core.pairs_scored)}{typeof core.pairs_total === "number" && core.pairs_total ? ` / ${formatCount(core.pairs_total)}` : ""}</span></dd></>}
          {elapsed !== null && <><dt className="sr-only">czas</dt><dd>czas: <span className="font-mono text-fg">{formatDuration(elapsed)}</span></dd></>}
          {rateText && <><dt className="sr-only">tempo</dt><dd>tempo: <span className="font-mono text-fg">{rateText}</span></dd></>}
          {etaText && <><dt className="sr-only">pozostało</dt><dd data-testid="activity-eta">{etaText}</dd></>}
        </dl>
      )}

      {stalled && (
        <div role="status" data-testid="activity-stall" className="rounded-md border border-abstain bg-abstain-soft px-2 py-1 text-abstain-ink">
          brak zdarzeń od {Math.floor(silent ?? 0)} s — praca trwa: {snap?.stage_label}
        </div>
      )}

      {snap?.status === "done" && imp && (
        <div data-testid="activity-done" className="text-fg">
          Import zakończony: {formatCount(imp.files_done)} plików{imp.incidents_done ? `, ${formatCount(imp.incidents_done)} incydentów` : ""}, odrzucone: {formatCount(imp.rejected)}.
          {reportHref && <> <a href={reportHref} className="text-accent hover:underline">Raport importu →</a></>}
        </div>
      )}

      {(failed || cancelled) && (
        <div role="alert" data-testid="activity-error" className="flex flex-wrap items-center gap-2 rounded-md border border-err/40 bg-against-soft px-2 py-1 text-fg">
          <span>{cancelled ? "Wysyłanie anulowane. Nic nie zostało zaimportowane." : upload?.phase === "error" ? `Wysyłanie nie powiodło się: ${upload.error ?? "błąd sieci"}` : `Analiza przerwana błędem${snap?.error_code ? ` (${snap.error_code})` : ""}${imp?.phase === "failed" ? " podczas importu" : ""}.`}</span>
          {onRetry && <Button onClick={onRetry}>Spróbuj ponownie</Button>}
        </div>
      )}

      {log.length > 0 && !compact && (
        <details className="text-muted">
          <summary className="cursor-pointer select-none">Dziennik zdarzeń ({log.length})</summary>
          <ol className="mt-1 max-h-40 overflow-y-auto font-mono text-2xs [content-visibility:auto]" data-testid="activity-log">
            {log.map((e, i) => <li key={`${e.at}-${i}`}><span className="text-subtle">{new Date(e.at).toLocaleTimeString("pl-PL")}</span> {e.text}</li>)}
          </ol>
        </details>
      )}
    </section>
  );
}
