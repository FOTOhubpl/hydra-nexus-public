/**
 * One analysis of one ad revision, in three separate sections that are never merged into one score:
 * (A) correlation with known campaigns (the open-set decision of the pipeline, with its evidence),
 * (B) fraud triage reasons with the analyst decision (an indication, never an automatic confirmation),
 * (C) manipulation / deepfake signals per modality ("no_signal" is not a statement of authenticity).
 */
import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "../../Button";
import { Badge, type BadgeVariant } from "../../Badge";
import { errorPl, timePl } from "../../../api/watchRt";
import { analysisReportUrl, metaApi, metaKeys, useMetaAnalysis, type Analysis, type ReviewDecision } from "../../../api/metaAds";
import { ModeBadge, numPl } from "./common";

const PRIORITY_VARIANT: Record<string, BadgeVariant> = { unknown: "neutral", low: "info", medium: "warning", high: "danger" };
const STATUS_VARIANT: Record<string, BadgeVariant> = { done: "success", partial: "warning", failed: "danger", running: "info", queued: "neutral" };
const REVIEW_PL: Record<string, string> = {
  unreviewed: "nieprzejrzane", confirmed_by_analyst: "potwierdzone przez analityka", dismissed_by_analyst: "odrzucone przez analityka",
};

function Section({ title, testId, children }: { title: string; testId: string; children: ReactNode }) {
  return (
    <section data-testid={testId} className="rounded-lg border border-line p-3">
      <h4 className="mb-2 text-sm font-semibold text-fg">{title}</h4>
      {children}
    </section>
  );
}

function Correlation({ a }: { a: Analysis }) {
  const c = a.correlation;
  const run = c.base_run_id ?? a.base_run_id;
  return (
    <Section title="A. Powiązanie z kampanią" testId="analysis-correlation">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium text-fg" data-testid="correlation-decision">{c.decision_pl}</span>
        {c.decision && <span className="font-mono text-xs text-muted">({c.decision})</span>}
        {c.campaign_id && (
          <Link to={`/campaigns/${encodeURIComponent(c.campaign_id)}${run ? `?run_id=${encodeURIComponent(run)}` : ""}`} className="font-mono text-accent hover:underline">
            {c.campaign_id}
          </Link>
        )}
        {c.group_id && <span className="text-xs text-muted">grupa {c.group_id}</span>}
      </div>
      {c.reason_pl && <p className="mt-1 text-sm text-muted">{c.reason_pl}</p>}
      {c.candidates.length > 0 && (
        <table className="mt-2 w-full text-left text-xs">
          <caption className="sr-only">Kandydaci kampanii</caption>
          <thead className="text-subtle"><tr><th className="pr-2 font-medium">Kandydat</th><th className="pr-2 font-medium">Bity</th><th className="pr-2 font-medium">Poparcie</th><th className="font-medium">Prawdopodobieństwo</th></tr></thead>
          <tbody>
            {c.candidates.map((k) => (
              <tr key={k.campaign_id} className="border-t border-line"><td className="pr-2 font-mono">{k.campaign_id}</td><td className="pr-2 tabular-nums">{numPl(k.total_bits)}</td><td className="pr-2 tabular-nums">{numPl(k.support)}</td><td className="tabular-nums">{numPl(k.probability, 3)}</td></tr>
            ))}
          </tbody>
        </table>
      )}
      {c.evidence.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-xs" data-testid="correlation-evidence">
          {c.evidence.map((e, i) => (
            <li key={`${e.kind}-${i}`} className="flex flex-wrap gap-2">
              <span className="font-mono text-subtle">{e.kind}</span>
              <span className="min-w-0 break-all text-fg">{e.value}</span>
              {e.bits != null && <span className="tabular-nums text-muted">{numPl(e.bits)} bit</span>}
              {e.level && <span className="text-muted">poziom: {e.level}</span>}
              {e.family && <span className="text-muted">{e.family}</span>}
            </li>
          ))}
        </ul>
      )}
      <div className="mt-2 text-2xs text-muted">
        Przebieg bazowy: <span className="font-mono">{run ?? "—"}</span>
        {c.assign_run_id && <> · przebieg przypisania: <span className="font-mono">{c.assign_run_id}</span></>}
        {a.context_version && <> · kontekst: <span className="font-mono">{a.context_version}</span></>}
      </div>
      {c.note_pl && <p className="mt-1 text-2xs text-muted">{c.note_pl}</p>}
      {c.evidence_tasks && c.evidence_tasks.length > 0 && (
        <div className="mt-2 rounded border border-line p-2" data-testid="evidence-tasks">
          <div className="text-xs font-semibold text-fg">Brakujące obserwacje (zadania dla analityka)</div>
          <ul className="mt-1 space-y-0.5 text-xs">
            {c.evidence_tasks.map((t, i) => (
              <li key={`${t.kind}-${i}`} className="flex flex-wrap gap-2">
                <span className="font-mono text-subtle">{t.kind}</span>
                <span className="text-fg">{t.task_pl}</span>
                <span className="text-muted">{t.adapter ? `adapter: ${t.adapter}` : "brak adaptera: zadanie dla analityka"}</span>
              </li>
            ))}
          </ul>
          <p className="mt-1 text-2xs text-muted">Kolejność heurystyczna, bez zmierzonej przewagi.</p>
        </div>
      )}
    </Section>
  );
}

function Triage({ a, canWrite }: { a: Analysis; canWrite: boolean }) {
  const qc = useQueryClient();
  const [msg, setMsg] = useState<string | null>(null);
  const t = a.triage;
  const review = useMutation({
    mutationFn: (d: ReviewDecision) => metaApi.review(a.id, d),
    onSuccess: (r) => { qc.setQueryData(metaKeys.analysis(a.id), r); setMsg(null); void qc.invalidateQueries({ queryKey: metaKeys.all }); },
    onError: (e) => setMsg(errorPl(e)),
  });
  return (
    <Section title="B. Przesłanki oszustwa (triage)" testId="analysis-triage">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant={PRIORITY_VARIANT[t.triage_priority] ?? "neutral"} dot><span data-testid="triage-priority">{t.triage_priority_pl}</span></Badge>
        <span className="text-xs text-muted" data-testid="review-status">przegląd: {REVIEW_PL[t.review_status] ?? t.review_status}</span>
      </div>
      {t.reasons.length > 0 ? (
        <ul className="mt-2 space-y-1 text-xs">
          {t.reasons.map((r, i) => (
            <li key={`${r.code}-${i}`} className="flex flex-wrap gap-2">
              <span className="font-mono text-subtle">{r.code}</span>
              <span className="text-fg">{r.description_pl}</span>
              <span className="text-muted">źródło: {r.source}</span>
              <span className="text-muted">reguła {r.rule_version}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-muted">Brak przesłanek według reguł.</p>
      )}
      {t.note_pl && <p className="mt-2 text-2xs text-muted">{t.note_pl}</p>}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Button size="sm" variant="primary" disabled={!canWrite || review.isPending || t.review_status === "confirmed_by_analyst"} onClick={() => review.mutate("confirmed_by_analyst")}>Potwierdź podejrzenie</Button>
        <Button size="sm" disabled={!canWrite || review.isPending || t.review_status === "dismissed_by_analyst"} onClick={() => review.mutate("dismissed_by_analyst")}>Odrzuć</Button>
        <Button size="sm" variant="ghost" disabled={!canWrite || review.isPending || t.review_status === "unreviewed"} onClick={() => review.mutate("unreviewed")}>Cofnij do nieprzejrzanych</Button>
        {msg && <span role="alert" className="text-xs text-err">{msg}</span>}
      </div>
      {a.review.history.length > 0 && (
        <ol className="mt-2 space-y-0.5 text-2xs text-muted" data-testid="review-history">
          {a.review.history.map((h, i) => (
            <li key={`${h.created_at}-${i}`}>{timePl(h.created_at)} · {h.analyst}: {REVIEW_PL[h.decision] ?? h.decision}{h.note ? ` — ${h.note}` : ""}</li>
          ))}
        </ol>
      )}
    </Section>
  );
}

function Manipulation({ a }: { a: Analysis }) {
  return (
    <Section title="C. Sygnały manipulacji (obraz, wideo, dźwięk)" testId="analysis-manipulation">
      <ul className="space-y-2 text-xs">
        {a.manipulation.map((m) => (
          <li key={m.modality} data-testid={`manip-${m.modality}`} data-status={m.status} className="rounded border border-line p-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-semibold text-fg">{m.modality === "image" ? "Obraz" : m.modality === "video" ? "Wideo" : m.modality === "audio" ? "Dźwięk" : m.modality}</span>
              <Badge variant={m.status === "signal_detected" ? "warning" : "neutral"}>{m.status_pl}</Badge>
              <span className="text-muted">{m.method} {m.version}</span>
            </div>
            <div className="mt-1 text-muted">Zakres: {m.scope || "—"} · Pokrycie: {m.quality_and_coverage || "—"}</div>
            {m.evidence_refs.length > 0 && <div className="mt-1 font-mono text-2xs text-subtle">{m.evidence_refs.join(", ")}</div>}
            <div className="mt-1 text-fg">{m.limitations_pl}</div>
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function MetaAnalysisView({ id, canWrite }: { id: number; canWrite: boolean }) {
  const q = useMetaAnalysis(id);
  if (q.isPending) return <p className="text-sm text-muted">Wczytywanie analizy…</p>;
  if (q.isError) return <p role="alert" className="text-sm text-err">{errorPl(q.error, "Nie udało się wczytać analizy.")}</p>;
  const a = q.data;
  return (
    <div data-testid="meta-analysis" data-status={a.status} className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-semibold text-fg">Analiza #{a.id}</span>
        <ModeBadge mode={a.mode} />
        <Badge variant={STATUS_VARIANT[a.status] ?? "neutral"} dot pulse={a.status === "running"}><span data-testid="analysis-status">{a.status_pl}</span></Badge>
        {a.incident_id && <Link to={`/incident/${encodeURIComponent(a.incident_id)}`} className="font-mono text-xs text-accent hover:underline">{a.incident_id}</Link>}
        <span className="ml-auto flex gap-3 text-xs">
          <a href={analysisReportUrl(a.id, "json")} data-testid="report-json" className="text-accent hover:underline" download>Raport JSON</a>
          <a href={analysisReportUrl(a.id, "md")} data-testid="report-md" className="text-accent hover:underline" download>Raport Markdown</a>
        </span>
      </div>
      <p className="text-xs text-muted" data-testid="analysis-coverage">Pokrycie analizy: {a.coverage_pl}</p>
      {a.reason_pl && <p className="text-xs text-muted">{a.reason_pl}</p>}
      <Correlation a={a} />
      <Triage a={a} canWrite={canWrite} />
      <Manipulation a={a} />
    </div>
  );
}
