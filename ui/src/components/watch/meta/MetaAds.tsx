/**
 * Collected ads of one mode: filter chips (all / unprocessed / new revisions / alerts), search, paging, and the ad
 * detail (revision timeline, artefacts with explicit gaps, queries that found it, link to the public library entry
 * without any token, re-analysis and analyst file attachment) with its analyses.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button } from "../../Button";
import { Panel } from "../../Panel";
import { Badge } from "../../Badge";
import { EmptyState } from "../../EmptyState";
import { errorPl, timePl } from "../../../api/watchRt";
import {
  VIEW_LABEL, metaApi, metaKeys, useMetaAd, useMetaAds,
  type AdRow, type AdView, type MetaMode,
} from "../../../api/metaAds";
import { cx } from "../../../lib/cx";
import { ModeBadge, bytesPl, short } from "./common";
import { MetaAnalysisView } from "./MetaAnalysis";
import { MetaScheme } from "./MetaScheme";

const VIEWS: AdView[] = ["all", "unprocessed", "new_revisions", "alerts"];

function AdCard({ ad, selected, onOpen }: { ad: AdRow; selected: boolean; onOpen: () => void }) {
  return (
    <li>
      <button
        type="button"
        data-testid="meta-ad-row"
        data-ad={ad.ad_id}
        aria-pressed={selected}
        onClick={onOpen}
        className={cx("w-full rounded-lg border p-3 text-left transition-colors", selected ? "border-accent bg-hover" : "border-line bg-raised hover:bg-hover")}
      >
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <ModeBadge mode={ad.mode} />
          <span className="font-mono text-xs text-fg">{ad.ad_id}</span>
          {ad.page_name && <span className="truncate text-xs text-muted">{ad.page_name}</span>}
          {ad.alert && <Badge variant="danger" dot>alert do sprawdzenia</Badge>}
          {ad.revisions > 1 && <Badge variant="info">rewizje: {ad.revisions}</Badge>}
        </div>
        {ad.text_excerpt && <p className="mt-1 line-clamp-2 text-sm text-fg">{ad.text_excerpt}</p>}
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-muted">
          <span>obserwacja: {timePl(ad.first_observed_at)} – {timePl(ad.last_observed_at)} ({ad.seen_count}×)</span>
          {ad.link_hosts.length > 0 && <span>linki: {ad.link_hosts.join(", ")}</span>}
          {ad.queries.length > 0 && <span>zapytania: {ad.queries.join(", ")}</span>}
          {ad.current_revision && <span>zbieranie: {ad.current_revision.collection_status_pl}</span>}
        </div>
        <div className="mt-1 text-2xs text-muted" data-testid="ad-coverage">
          {ad.analysis ? <>analiza: {ad.analysis.status_pl}{ad.analysis.decision_pl ? ` · ${ad.analysis.decision_pl}` : ""} · triage: {ad.analysis.triage_priority_pl} · pokrycie: {ad.analysis.coverage_pl}</> : "analiza: jeszcze nie wykonana"}
        </div>
      </button>
    </li>
  );
}

function Th({ children }: { children: ReactNode }) {
  return <th className="pr-2 font-medium">{children}</th>;
}

function AdDetail({ mode, adId, canWrite }: { mode: MetaMode; adId: string; canWrite: boolean }) {
  const qc = useQueryClient();
  const q = useMetaAd(mode, adId);
  const [msg, setMsg] = useState<string | null>(null);
  const [analysisId, setAnalysisId] = useState<number | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const analyses = q.data?.analyses ?? [];
  useEffect(() => { setAnalysisId(null); }, [adId]);
  const shownId = analysisId ?? analyses[0]?.id ?? null;
  const after = (text: string) => { setMsg(text); void qc.invalidateQueries({ queryKey: metaKeys.all }); };
  const analyse = useMutation({
    mutationFn: () => metaApi.analyse(mode, adId),
    onSuccess: (r) => after(r.created ? `Zlecono analizę (zadanie #${r.job.id}).` : `Analiza tej rewizji już istnieje lub czeka w kolejce (zadanie #${r.job.id}: ${r.job.status_pl}).`),
    onError: (e) => setMsg(errorPl(e)),
  });
  const attach = useMutation({
    mutationFn: (f: File) => metaApi.attach(mode, adId, f),
    onSuccess: (r) => after(`Dołączono plik jako nową rewizję (#${r.revision_id}); analiza w kolejce.`),
    onError: (e) => setMsg(errorPl(e)),
  });
  if (q.isPending) return <p className="text-sm text-muted">Wczytywanie reklamy…</p>;
  if (q.isError) return <p role="alert" className="text-sm text-err">{errorPl(q.error, "Nie udało się wczytać reklamy.")}</p>;
  const d = q.data;
  return (
    <div data-testid="meta-ad-detail" className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <ModeBadge mode={d.ad.mode} />
        <span className="font-mono text-sm text-fg">{d.ad.ad_id}</span>
        {d.ad.page_name && <span className="text-sm text-muted">{d.ad.page_name}{d.ad.page_id ? ` (strona ${d.ad.page_id})` : ""}</span>}
        {d.source_link && (
          <a href={d.source_link} target="_blank" rel="noopener noreferrer" className="text-sm text-accent hover:underline">Otwórz w bibliotece reklam Meta</a>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="sm" disabled={!canWrite || analyse.isPending} onClick={() => analyse.mutate()} data-testid="meta-analyse">Analizuj ponownie</Button>
        <label htmlFor="meta-attach" className="text-xs text-subtle">Dołącz plik (obraz/wideo)</label>
        <input
          id="meta-attach"
          ref={fileRef}
          type="file"
          accept="image/*,video/*"
          disabled={!canWrite || attach.isPending}
          onChange={(e) => { const f = e.target.files?.[0]; if (f) attach.mutate(f); if (fileRef.current) fileRef.current.value = ""; }}
          className="text-xs"
        />
        {msg && <span role="status" className="text-xs text-muted">{msg}</span>}
      </div>

      <section>
        <h4 className="mb-1 text-sm font-semibold text-fg">Rewizje i materiały</h4>
        <ol className="flex flex-col gap-3" data-testid="meta-revisions">
          {d.revisions.map((r) => (
            <li key={r.id} className="rounded-lg border border-line p-2">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <span className="font-semibold text-fg">rewizja {r.rev}</span>
                <span className="text-muted">{timePl(r.observed_at)}</span>
                <span className="font-mono text-subtle" title={r.content_sha256}>{short(r.content_sha256)}</span>
                <span className="text-fg">{r.collection_status_pl}</span>
                {r.collection_reason_pl && <span className="text-muted">{r.collection_reason_pl}</span>}
              </div>
              {r.artifacts.length > 0 ? (
                <table className="mt-1 w-full text-left text-2xs">
                  <caption className="sr-only">Materiały rewizji {r.rev}</caption>
                  <thead className="text-subtle"><tr><Th>Rola</Th><Th>Status</Th><Th>SHA-256</Th><Th>MIME</Th><Th>Rozmiar</Th><Th>Źródło</Th></tr></thead>
                  <tbody>
                    {r.artifacts.map((a, i) => (
                      <tr key={`${a.role}-${i}`} className="border-t border-line" data-testid="meta-artifact" data-status={a.status}>
                        <td className="pr-2">{a.role}{a.derived && <span className="ml-1 text-warn">pochodny</span>}</td>
                        <td className="pr-2">{a.status_pl}{a.retry_possible && <span className="ml-1 text-muted">(można ponowić)</span>}</td>
                        <td className="pr-2 font-mono" title={a.sha256 ?? undefined}>{short(a.sha256)}</td>
                        <td className="pr-2">{a.mime ?? "—"}</td>
                        <td className="pr-2 tabular-nums">{bytesPl(a.size)}</td>
                        <td>{a.source}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="mt-1 text-2xs text-muted">Brak zapisanych materiałów dla tej rewizji.</p>
              )}
            </li>
          ))}
        </ol>
      </section>

      {d.queries.length > 0 && (
        <section>
          <h4 className="mb-1 text-sm font-semibold text-fg">Zapytania, które znalazły reklamę</h4>
          <ul className="text-xs text-muted">
            {d.queries.map((x) => <li key={x.query_id}><span className="text-fg">{x.label}</span> · {timePl(x.first_at)} – {timePl(x.last_at)} · trafień {x.hits}</li>)}
          </ul>
        </section>
      )}

      <MetaScheme mode={mode} adId={adId} />

      <section>
        <div className="mb-1 flex flex-wrap items-center gap-2">
          <h4 className="text-sm font-semibold text-fg">Analizy</h4>
          {analyses.length > 1 && (
            <>
              <label htmlFor="meta-analysis-pick" className="sr-only">Wybierz analizę</label>
              <select id="meta-analysis-pick" value={shownId ?? ""} onChange={(e) => setAnalysisId(Number(e.target.value))} className="h-7 rounded-[4px] border border-line bg-panel px-2 text-xs">
                {analyses.map((a) => <option key={a.id} value={a.id}>#{a.id} · {a.status_pl}</option>)}
              </select>
            </>
          )}
        </div>
        {shownId != null ? <MetaAnalysisView id={shownId} canWrite={canWrite} /> : <p className="text-xs text-muted" data-testid="no-analysis">Brak analizy tej reklamy (w kolejce lub poza limitem analiz).</p>}
      </section>
    </div>
  );
}

export function MetaAds({ mode, canWrite }: { mode: MetaMode; canWrite: boolean }) {
  const [view, setView] = useState<AdView>("all");
  const [q, setQ] = useState("");
  const [term, setTerm] = useState("");
  const [extra, setExtra] = useState<AdRow[]>([]);
  const [next, setNext] = useState<string | null | undefined>(undefined);
  const [selected, setSelected] = useState<string | null>(null);
  const list = useMetaAds(mode, view, term);
  useEffect(() => { setExtra([]); setNext(undefined); setSelected(null); }, [mode, view, term]);
  const more = useMutation({
    mutationFn: (before: string) => metaApi.ads(mode, view, term, before),
    onSuccess: (r) => { setExtra((p) => [...p, ...r.ads]); setNext(r.next_before); },
  });
  const base = list.data?.ads ?? [];
  const seen = new Set(base.map((a) => a.ad_id));
  const ads = [...base, ...extra.filter((a) => !seen.has(a.ad_id))];
  const cursor = next === undefined ? list.data?.next_before ?? null : next;
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
      <Panel title="Reklamy" actions={<span className="text-xs text-muted">{ads.length}</span>}>
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <div role="group" aria-label="Filtr reklam" className="flex flex-wrap gap-1">
            {VIEWS.map((v) => (
              <button
                key={v}
                type="button"
                aria-pressed={view === v}
                data-testid={`meta-view-${v}`}
                onClick={() => setView(v)}
                className={cx("h-7 rounded-full border px-3 text-xs font-medium", view === v ? "border-accent-strong bg-accent-strong text-canvas" : "border-line-strong bg-raised text-fg hover:bg-hover")}
              >
                {VIEW_LABEL[v]}
              </button>
            ))}
          </div>
          <form className="ml-auto flex gap-1" onSubmit={(e) => { e.preventDefault(); setTerm(q.trim()); }}>
            <label htmlFor="meta-q" className="sr-only">Szukaj w reklamach</label>
            <input id="meta-q" value={q} onChange={(e) => setQ(e.target.value)} placeholder="szukaj (tekst, strona, ID)" className="h-7 w-48 min-w-0 rounded-[4px] border border-line bg-panel px-2 text-sm" />
            <Button size="sm" type="submit">Szukaj</Button>
          </form>
        </div>
        {list.isPending ? (
          <p className="text-sm text-muted">Wczytywanie reklam…</p>
        ) : list.isError ? (
          <p role="alert" className="text-sm text-err">{errorPl(list.error, "Nie udało się wczytać reklam.")}</p>
        ) : ads.length === 0 ? (
          <EmptyState
            testId="meta-empty"
            compact
            title="Brak reklam w tym trybie"
            description={mode === "live"
              ? "Brak reklam w tym trybie: skonfiguruj hasła lub strony, skonfiguruj token w Integracjach i uruchom kolektor albo kliknij „Skanuj teraz”."
              : "Brak reklam w tym trybie: uruchom odtworzenie zestawu (zakładka „Na żywo” → Tryb demonstracyjny) albo zmień tryb."}
          />
        ) : (
          <ul className="flex flex-col gap-2" data-testid="meta-ads">
            {ads.map((a) => <AdCard key={`${a.mode}-${a.ad_id}`} ad={a} selected={selected === a.ad_id} onOpen={() => setSelected(a.ad_id)} />)}
          </ul>
        )}
        {cursor && (
          <Button size="sm" className="mt-3" disabled={more.isPending} onClick={() => more.mutate(cursor)}>Pokaż więcej</Button>
        )}
      </Panel>
      <Panel title="Szczegóły reklamy">
        {selected ? <AdDetail mode={mode} adId={selected} canWrite={canWrite} /> : <p className="text-sm text-muted">Wybierz reklamę z listy, aby zobaczyć rewizje, materiały i wynik analizy.</p>}
      </Panel>
    </div>
  );
}
