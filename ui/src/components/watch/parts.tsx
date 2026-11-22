/**
 * Building blocks of the realtime watch page (WATCH-RT): engine pill, replay banner, source health card, the
 * 48-hour timeline and small shared bits (ticking clock, replay tag). Data comes only from /v1/watch/rt/* and the
 * `watch.*` stream events.
 */
import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Badge, type BadgeVariant } from "../Badge";
import { Button } from "../Button";
import { Tooltip } from "../Tooltip";
import { Sparkline } from "../charts";
import { cx } from "../../lib/cx";
import {
  ENGINE_HELP, ENGINE_LABEL, SOURCE_STATE_HELP, bytesPl, durationPl, errorPl, intervalPl, patchStatusSource, relativePl, rtApi, rtKeys, timePl,
  type EngineInfo, type EngineState, type RtSource, type TimelinePoint,
} from "../../api/watchRt";

/** Wall clock that re-renders every `ms` (text only, no animation, so reduced motion is unaffected). */
export function useNow(ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

const ENGINE_VARIANT: Record<EngineState, BadgeVariant> = { live: "danger", offline: "neutral", replay: "warning", paused: "info", stopped: "neutral" };

export function EnginePill({ engine }: { engine: EngineInfo }) {
  const st = engine.state;
  const label = ENGINE_LABEL[st] ?? engine.state_pl ?? st;
  return (
    <Tooltip focusable content={<>{ENGINE_HELP[st] ?? engine.state_pl}{engine.message_pl ? <> {engine.message_pl}</> : null}</>}>
      <span data-testid="engine-pill" data-state={st}>
        <Badge variant={ENGINE_VARIANT[st] ?? "neutral"} dot pulse={st === "live"}>{label}</Badge>
      </span>
    </Tooltip>
  );
}

export const REPLAY_BANNER_SYNTHETIC = "SYNTETYCZNE / ODTWORZENIE — dane nie pochodzą z sieci na żywo";
export const REPLAY_BANNER_RECORDING = "ODTWORZENIE — nagranie, nie dane na żywo";

/** Non-dismissable: replay is never presented as live data. */
export function ReplayBanner({ engine }: { engine: EngineInfo }) {
  const r = engine.replay;
  if (engine.state !== "replay" && !r) return null;
  const synthetic = r?.synthetic !== false;
  return (
    <div role="status" data-testid="replay-banner" className="rounded-xl border-2 border-warn bg-warn/15 px-4 py-3 text-warn">
      <div className="text-sm font-bold tracking-wide uppercase">{synthetic ? REPLAY_BANNER_SYNTHETIC : REPLAY_BANNER_RECORDING}</div>
      {r && (
        <div className="mt-1 text-xs text-fg">
          zestaw <span className="font-mono">{r.fixture}</span> · prędkość {r.speed}×
          {r.ticks_total != null ? <> · postęp {r.ticks_done ?? 0}/{r.ticks_total}</> : null}
          {r.done ? " · zakończono" : ""}
          {r.label_pl && r.label_pl !== engine.message_pl ? <> · {r.label_pl}</> : null}
        </div>
      )}
    </div>
  );
}

export function ReplayTag() {
  return <Badge variant="warning" title="pozycja z odtworzenia, nie z sieci na żywo">odtworzenie</Badge>;
}

/** A label with a definition on hover / keyboard focus. */
export function Term({ children, tip }: { children: ReactNode; tip: ReactNode }) {
  return (
    <Tooltip focusable content={tip}>
      <span className="cursor-help border-b border-dotted border-line-strong">{children}</span>
    </Tooltip>
  );
}

const SOURCE_VARIANT: Record<string, BadgeVariant> = {
  ok: "success", no_new: "success", idle: "neutral", disabled: "neutral", offline: "neutral", backoff: "warning", circuit_open: "danger",
  needs_key: "warning", error: "danger", replay: "warning", paused: "info",
};

const SOURCE_STATE_PL: Record<string, string> = {
  ok: "działa", no_new: "brak nowych wpisów", idle: "oczekuje", disabled: "wyłączone", offline: "offline", backoff: "opóźnienie po błędach", circuit_open: "bezpiecznik",
  needs_key: "brak klucza", error: "błąd", replay: "odtworzenie", paused: "wstrzymane",
};

/** Seconds to the next poll, ticking client-side from `next_due_in_s` at the time the status was fetched. */
export function countdown(src: RtSource, fetchedAt: number, now: number): number | null {
  if (src.next_due_in_s != null && Number.isFinite(src.next_due_in_s)) return src.next_due_in_s - (now - fetchedAt) / 1000;
  if (src.next_due) {
    const t = Date.parse(src.next_due);
    if (!Number.isNaN(t)) return (t - now) / 1000;
  }
  return null;
}

export function SourceCard({ src, fetchedAt, now }: { src: RtSource; fetchedAt: number; now: number }) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ tone: "ok" | "err"; text: string } | null>(null);
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => rtApi.setSource(src.id, { enabled }),
    onSuccess: (s) => { patchStatusSource(qc, s); setMsg(null); },
    onError: (e) => setMsg({ tone: "err", text: errorPl(e) }),
  });
  const poll = useMutation({
    mutationFn: () => rtApi.poll(src.id),
    onSuccess: (r) => {
      const run = r?.run;
      setMsg(run ? { tone: run.error_pl ? "err" : "ok", text: run.error_pl ?? run.result_pl ?? `Sprawdzono: pobrano ${run.fetched}, nowe ${run.new}, dopasowania ${run.matched}${run.not_modified ? " (bez zmian od ostatniego razu)" : ""}.` } : { tone: "ok", text: "Sprawdzono." });
      void qc.invalidateQueries({ queryKey: rtKeys.all });
    },
    onError: (e) => setMsg({ tone: "err", text: errorPl(e) }),
  });
  const left = countdown(src, fetchedAt, now);
  const waiting = !src.enabled || ["disabled", "offline", "paused", "needs_key"].includes(src.state);
  const used = src.rate_limit?.used_60s ?? null;
  const limit = src.rate_limit?.limit_per_min ?? null;
  const share = used != null && limit ? Math.min(1, used / limit) : null;
  const stateLabel = src.state_pl || SOURCE_STATE_PL[src.state] || src.state;
  const headId = `src-${src.id}-name`;
  return (
    <article className="surface-card flex min-w-0 flex-col gap-3 p-4" data-testid={`source-${src.id}`} data-state={src.state} aria-labelledby={headId}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <h3 id={headId} className="min-w-0 flex-1 truncate text-sm font-semibold text-fg">{src.name_pl}</h3>
        <Tooltip focusable content={SOURCE_STATE_HELP[src.state] ?? stateLabel}>
          <span data-testid={`source-state-${src.id}`}><Badge variant={SOURCE_VARIANT[src.state] ?? "neutral"} dot>{stateLabel}</Badge></span>
        </Tooltip>
        <Tooltip content={src.enabled ? "Wyłącz odpytywanie tego źródła" : "Włącz odpytywanie tego źródła"}>
          <button
            type="button"
            role="switch"
            aria-checked={src.enabled}
            aria-label={`Źródło ${src.name_pl} włączone`}
            disabled={toggle.isPending}
            onClick={() => toggle.mutate(!src.enabled)}
            className={cx("relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:opacity-60", src.enabled ? "border-accent-strong bg-accent-strong" : "border-line-strong bg-hover")}
          >
            <span aria-hidden="true" className={cx("inline-block size-3.5 rounded-full bg-canvas shadow transition-transform", src.enabled ? "translate-x-[18px]" : "translate-x-[2px]")} />
          </button>
        </Tooltip>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs">
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip="Kiedy silnik ostatnio wysłał zapytanie do tego źródła (niezależnie od wyniku).">Ostatnie odpytanie</Term></dt>
          <dd className="truncate font-mono text-fg" title={timePl(src.last_poll)}>{relativePl(src.last_poll, now)}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip="Czas odpowiedzi źródła przy ostatnim odpytaniu, w milisekundach.">Opóźnienie</Term></dt>
          <dd className="font-mono text-fg">{src.last_latency_ms != null ? `${Math.round(src.last_latency_ms)} ms` : "—"}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip="Liczba nieudanych odpytań z rzędu; po kilku błędach silnik odsuwa kolejne próby, a potem odcina źródło (bezpiecznik).">Błędy z rzędu</Term></dt>
          <dd className={cx("font-mono", src.consecutive_errors > 0 ? "text-err" : "text-fg")}>{src.consecutive_errors}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip="Zapytania wysłane w ostatnich 60 s wobec limitu na minutę, który silnik sam sobie narzuca dla tego źródła.">Limit zapytań</Term></dt>
          <dd className="font-mono text-fg">
            {used ?? "—"} / {limit ?? "—"} na min
            {share != null && (
              <span className="mt-1 block h-1 w-full overflow-hidden rounded-full bg-hover" aria-hidden="true">
                <span className={cx("block h-full rounded-full", share >= 0.9 ? "bg-err" : share >= 0.6 ? "bg-warn" : "bg-accent")} style={{ width: `${share * 100}%` }} />
              </span>
            )}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip="Odliczanie do następnego zaplanowanego odpytania (liczone w przeglądarce od ostatniego stanu z serwera).">Następne odpytanie</Term></dt>
          <dd className="font-mono text-fg" data-testid={`source-next-${src.id}`}>
            {waiting || left == null ? "—" : left <= 0 ? "teraz" : `za ${durationPl(left)}`}
          </dd>
        </div>
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip={`Jak często silnik odpytuje źródło${src.min_interval_s ? `; najkrócej co ${src.min_interval_s} s, aby nie obciążać serwisu` : ""}.`}>Interwał</Term></dt>
          <dd className="font-mono text-fg">{intervalPl(src.interval_s)}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip="Wszystkie odpytania od startu silnika.">Odpytania</Term></dt>
          <dd className="font-mono text-fg">{src.total_polls ?? "—"}</dd>
        </div>
        <div className="min-w-0">
          <dt className="text-subtle"><Term tip="Pozycje (adresy, domeny, certyfikaty, reklamy) zapisane z tego źródła.">Pozycje</Term></dt>
          <dd className="font-mono text-fg">{src.total_items ?? "—"}</dd>
        </div>
      </dl>
      <Freshness src={src} />
      {src.last_error_pl && <p className="text-xs break-words text-err" data-testid={`source-error-${src.id}`}>Ostatni błąd: {src.last_error_pl}</p>}
      <div className="mt-auto flex flex-wrap items-center gap-2">
        <Button size="sm" onClick={() => poll.mutate()} disabled={poll.isPending}>{poll.isPending ? "Odpytuję…" : "Odpytaj teraz"}</Button>
        {src.sends_pl && (
          <Tooltip content={src.sends_pl}>
            <button type="button" className="text-xs text-accent underline-offset-2 hover:underline">Co jest wysyłane</button>
          </Tooltip>
        )}
      </div>
      <p role="status" aria-live="polite" className={cx("min-h-0 text-xs break-words", msg?.tone === "err" ? "text-err" : "text-muted")} data-testid={`source-msg-${src.id}`}>{msg?.text}</p>
    </article>
  );
}

/** Freshness of one source: the plain sentence, CERT file facts and new items per hour (last 24 h). */
export function Freshness({ src }: { src: RtSource }) {
  const hourly = src.hourly_new ?? [];
  const isCert = src.id === "cert-pl";
  const facts: { k: string; v: string; tip: string }[] = [];
  if (isCert && src.file_size) facts.push({ k: "Rozmiar pliku", v: bytesPl(src.file_size), tip: "Rozmiar pliku actions_<rok>.log na serwerze CERT Polska przy ostatnim odpytaniu (z nagłówka Content-Range)." });
  if (isCert && src.server_last_modified) facts.push({ k: "Plik na serwerze", v: timePl(src.server_last_modified), tip: "Nagłówek Last-Modified serwera CERT. Plik jest generowany na nowo co kilka minut, więc ta data zmienia się także wtedy, gdy nie ma nowych wpisów." });
  if (src.newest_entry) facts.push({ k: "Najnowszy wpis", v: timePl(src.newest_entry), tip: isCert ? "Najpóźniejszy czas akcji (ActionTime) widziany na liście CERT." : "Najpóźniejszy czas źródła wśród zapisanych pozycji." });
  if (src.last_new_at) facts.push({ k: "Ostatni nowy wpis", v: timePl(src.last_new_at), tip: "Kiedy odpytanie ostatnio zapisało nową pozycję z tego źródła." });
  if (src.new_24h != null && (src.last_ok || src.new_24h > 0)) facts.push({ k: "Nowe w 24 h", v: String(src.new_24h), tip: "Nowe pozycje zapisane z tego źródła w ostatnich 24 godzinach." });
  if (!src.summary_pl && !facts.length && !hourly.some((h) => h.new > 0)) return null;
  return (
    <div className="flex flex-col gap-2" data-testid={`source-fresh-${src.id}`}>
      {src.summary_pl && <p className="text-xs break-words text-fg" data-testid={`source-summary-${src.id}`}>{src.summary_pl}</p>}
      {src.state_detail_pl && <p className="text-xs break-words text-muted">{src.state_detail_pl}</p>}
      {facts.length > 0 && (
        <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
          {facts.map((f) => (
            <div key={f.k} className="min-w-0">
              <dt className="text-subtle"><Term tip={f.tip}>{f.k}</Term></dt>
              <dd className="truncate font-mono text-fg" title={f.v}>{f.v}</dd>
            </div>
          ))}
        </dl>
      )}
      {hourly.length > 0 && (
        <div className="flex items-center gap-2 text-xs text-subtle">
          <Tooltip content="Nowe pozycje z tego źródła w każdej z ostatnich 24 godzin (z zapisanych odpytań).">
            <span tabIndex={0}>Nowe / godz. (24 h)</span>
          </Tooltip>
          <Sparkline values={hourly.map((h) => h.new)} width={120} height={22} label={`nowe pozycje na godzinę, ostatnie 24 h, źródło ${src.name_pl}`} />
        </div>
      )}
    </div>
  );
}

/** Hour label "14:00" from "YYYY-MM-DDTHH". */
const hourLabel = (h: string) => `${h.slice(11, 13)}:00`;
/** Axis label "21.11 14:00" (48 h span two days, the hour alone is ambiguous). */
const axisLabel = (h: string) => `${h.slice(8, 10)}.${h.slice(5, 7)} ${hourLabel(h)}`;

/** 48 hourly columns, new vs matched side by side, 2 px gaps; hover title on each column and a table view. */
export function TimelineChart({ points }: { points: TimelinePoint[] }) {
  const max = Math.max(1, ...points.map((p) => Math.max(p.new, p.matched)));
  const totalNew = points.reduce((s, p) => s + p.new, 0);
  const totalMatched = points.reduce((s, p) => s + p.matched, 0);
  const H = 112;
  if (points.length === 0) return <p className="text-sm text-subtle">Brak danych z przebiegów.</p>;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3 text-xs text-muted">
        <span className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rounded-sm" style={{ backgroundColor: "var(--color-accent)" }} />nowe pozycje ({totalNew})</span>
        <span className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rounded-sm" style={{ backgroundColor: "var(--color-fam-nar)" }} />dopasowane ({totalMatched})</span>
        <span className="text-subtle">maks. {max} na godzinę</span>
      </div>
      <div role="img" aria-label={`Ostatnie 48 godzin: ${totalNew} nowych pozycji, ${totalMatched} dopasowanych`} className="flex items-end gap-[2px] border-b border-line" style={{ height: H }}>
        {points.map((p) => (
          <div key={p.hour} title={`${p.hour.slice(0, 10)} ${hourLabel(p.hour)}: nowe ${p.new}, dopasowane ${p.matched}`} className="flex h-full min-w-0 flex-1 items-end gap-px hover:bg-hover">
            <span className="block min-w-0 flex-1 rounded-t-[2px]" style={{ height: `${(p.new / max) * 100}%`, backgroundColor: "var(--color-accent)" }} />
            <span className="block min-w-0 flex-1 rounded-t-[2px]" style={{ height: `${(p.matched / max) * 100}%`, backgroundColor: "var(--color-fam-nar)" }} />
          </div>
        ))}
      </div>
      <div className="flex justify-between font-mono text-2xs text-subtle">
        <span>{axisLabel(points[0]!.hour)}</span>
        <span>{axisLabel(points[Math.floor(points.length / 2)]!.hour)}</span>
        <span>{axisLabel(points[points.length - 1]!.hour)}</span>
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-muted">Tabela godzinowa</summary>
        <div className="mt-2 max-h-48 overflow-auto">
          <table className="w-full text-xs">
            <thead><tr className="text-left text-subtle"><th className="font-medium">godzina</th><th className="text-right font-medium">nowe</th><th className="text-right font-medium">dopasowane</th></tr></thead>
            <tbody>
              {points.filter((p) => p.new || p.matched).map((p) => (
                <tr key={p.hour}><td className="font-mono">{p.hour.replace("T", " ")}:00</td><td className="text-right font-mono">{p.new}</td><td className="text-right font-mono">{p.matched}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
