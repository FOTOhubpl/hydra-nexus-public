/** Rates, ETA and wording for the /analyse live activity panel (IMPORT-PROGRESS). Pure functions, measured inputs only. */
import type { AnalyseProgress, ProgressSample } from "../api/progress";
import { formatBytes, formatCount } from "./format";

export const STALL_S = 20;
/** ETA needs at least this many samples of the running counter, spanning at least ETA_MIN_SPAN_S. */
export const ETA_MIN_SAMPLES = 5;
export const ETA_MIN_SPAN_S = 3;

export interface Point { t: number; v: number }

/** Units per second between the first and the last point (ms timestamps), or null when not measurable yet. */
export function measuredRate(points: Point[], minSamples = ETA_MIN_SAMPLES, minSpanS = ETA_MIN_SPAN_S): number | null {
  if (points.length < minSamples) return null;
  const a = points[0]!;
  const b = points[points.length - 1]!;
  const span = (b.t - a.t) / 1000;
  if (span < minSpanS || b.v <= a.v) return null;
  return (b.v - a.v) / span;
}

/** Seconds left at the measured rate, or null (no rate, nothing left or no total). */
export function etaSeconds(done: number, total: number | null | undefined, rate: number | null): number | null {
  if (rate === null || rate <= 0 || !total || total <= done) return null;
  return (total - done) / rate;
}

export function formatDuration(s: number): string {
  const sec = Math.max(0, Math.round(s));
  if (sec < 60) return `${sec} s`;
  const m = Math.floor(sec / 60);
  if (m < 60) return `${m} min ${sec % 60} s`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function formatRate(bytesPerS: number): string {
  return `${formatBytes(bytesPerS)}/s`;
}

/** Overall percent over the samples: never decreases, 100 only once the job is done. */
export function overallPercent(samples: ProgressSample[]): number {
  let best = 0;
  for (const { snap } of samples) best = Math.max(best, snap.overall_percent ?? 0);
  const last = samples[samples.length - 1]?.snap;
  return last?.status === "done" ? 100 : Math.min(99, best);
}

/** Points of the running stage's counter (same stage as the last sample), for its rate. */
export function stagePoints(samples: ProgressSample[]): Point[] {
  const last = samples[samples.length - 1]?.snap;
  if (!last) return [];
  const out: Point[] = [];
  for (let i = samples.length - 1; i >= 0; i -= 1) {
    const s = samples[i]!;
    if (s.snap.stage !== last.stage) break;
    const v = s.snap.stage === "import" ? s.snap.import?.files_done : s.snap.counters.done;
    if (typeof v === "number") out.unshift({ t: s.at, v });
  }
  return out;
}

/** Seconds since the last snapshot that changed something (arrival times of distinct snapshots). */
export function silenceSeconds(samples: ProgressSample[], now: number): number | null {
  const last = samples[samples.length - 1];
  return last ? Math.max(0, (now - last.at) / 1000) : null;
}

export function importText(imp: NonNullable<AnalyseProgress["import"]>): string {
  if (imp.files_total === null || imp.phase === "listing") return "Rozpakowywanie…";
  const pct = imp.percent ?? 0;
  return `Import: ${pct}% (${formatCount(imp.files_done)} z ${formatCount(imp.files_total)} plików) · odrzucone: ${formatCount(imp.rejected)}`;
}

export function uploadText(loaded: number, total: number | null, rate: number | null, eta: number | null): string {
  const pct = total ? Math.floor((loaded * 100) / total) : null;
  let s = pct === null ? `Wysyłanie plików: ${formatBytes(loaded)}` : `Wysyłanie plików: ${pct}% (${formatBytes(loaded)} z ${formatBytes(total)})`;
  if (rate !== null) s += ` · ${formatRate(rate)}`;
  if (eta !== null) s += ` · ok. ${formatDuration(eta)}`;
  return s;
}

export interface LogEntry { at: number; text: string }

/** Rolling log of what changed between consecutive snapshots (last `max` entries). */
export function deriveLog(samples: ProgressSample[], max = 50): LogEntry[] {
  const out: LogEntry[] = [];
  let prev: AnalyseProgress | null = null;
  for (const { snap, at } of samples) {
    if (!prev || prev.stage !== snap.stage) out.push({ at, text: `Etap: ${snap.stage_label}` });
    const imp = snap.import;
    if (imp && imp.current && imp.current !== prev?.import?.current) out.push({ at, text: `Import: ${imp.current} (${formatCount(imp.files_done)}/${imp.files_total === null ? "?" : formatCount(imp.files_total)})` });
    if (imp && imp.rejected > (prev?.import?.rejected ?? 0)) out.push({ at, text: `Odrzucone pliki: ${formatCount(imp.rejected)}` });
    const c = snap.counters;
    if (c.item && c.item !== prev?.counters.item) out.push({ at, text: `${snap.stage_label}: ${c.item}` });
    else if (typeof c.done === "number" && c.done !== prev?.counters.done && prev?.stage === snap.stage) out.push({ at, text: `${snap.stage_label}: ${formatCount(c.done)} z ${formatCount(c.total ?? 0)} ${c.unit ?? ""}`.trim() });
    if (prev && prev.status !== snap.status && (snap.status === "done" || snap.status === "failed")) out.push({ at, text: snap.status === "done" ? "Gotowe" : `Błąd: ${snap.error_code ?? ""}`.trim() });
    prev = snap;
  }
  return out.slice(-max);
}
