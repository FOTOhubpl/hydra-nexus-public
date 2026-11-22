/**
 * "Co się zmieniło": two incidents side by side, gene by gene (GET /v1/pairs/{a}/{b}/diff). Text variants get a
 * word diff, near-duplicate images show the scorer's match values, infrastructure shows same / changed, and the
 * summary separates the invariant core from the decoration that changed. Answers "nowe warianty znanych kampanii".
 */
import { Link, useParams } from "react-router";
import { frameUrl, thumbUrl } from "../../api/artifacts";
import { EvidenceCompare } from "../../components/artifacts/EvidenceCompare";
import { mmss } from "../../lib/artifactEvidence";
import { useIncident, usePair } from "../../api/task";
import { usePairDiff, type DiffRow } from "../../api/visual";
import { FamChip, GeneValue, HypothesisBadge, Thumb } from "../../components/evidence";
import { VideoSegmentStrip } from "../../components/VideoSegmentStrip";
import { GeometryOverlay } from "../../components/GeometryOverlay";
import { cx } from "../../lib/cx";
import { formatBits, plNumber } from "../../lib/evidence";
import { GENE_KIND_PL } from "../../lib/labelsPl";
import { openGene } from "../../lib/geneExplain";
import { sharedRatio, wordDiff } from "../../lib/wordDiff";
import { CampaignLink, IncidentLink, Page, pairPath, QueryState } from "./common";

const STATE: Record<DiffRow["state"], [string, string]> = {
  same: ["bez zmian", "border-for bg-for-soft text-fg"],
  partly: ["częściowo wspólne", "border-fam-meta text-fg"],
  changed: ["zmienione", "border-abstain bg-abstain-soft text-abstain-ink"],
  one_side: ["tylko po jednej stronie", "border-line text-subtle"],
};
const KIND_PL = GENE_KIND_PL;

function StateChip({ s }: { s: DiffRow["state"] }) {
  const [l, c] = STATE[s];
  return <span className={cx("inline-flex h-6 shrink-0 items-center whitespace-nowrap rounded-md border px-2 text-xs", c)}>{l}</span>;
}

function TextRow({ r }: { r: DiffRow }) {
  // pair each changed A value with the most similar B value
  const pairs = r.only_a.slice(0, 4).map((a) => {
    let best = { b: "", ratio: -1 };
    for (const b of r.only_b) { const ratio = sharedRatio(wordDiff(a, b)); if (ratio > best.ratio) best = { b, ratio }; }
    return { a, ...best };
  });
  return (
    <div className="space-y-2 border-t border-line py-3 first:border-t-0">
      <div className="flex items-center gap-2"><span className="text-sm font-medium">{KIND_PL[r.kind] ?? r.kind}</span><StateChip s={r.state} /></div>
      {r.shared.slice(0, 3).map((v) => <div key={v} className="rounded-md bg-for-soft px-3 py-1.5 text-sm"><span className="mr-2 text-xs text-muted">stałe:</span>{v}</div>)}
      {pairs.filter((p) => p.b).map((p, i) => (
        <div key={i} className="rounded-md border border-line px-3 py-2 text-sm leading-7">
          {wordDiff(p.a, p.b).map((o, k) => (
            <span key={k} className={cx(o.op === "same" && "text-fg", o.op === "del" && "rounded-[3px] bg-against-soft px-0.5 text-fg line-through decoration-err", o.op === "add" && "rounded-[3px] bg-abstain-soft px-0.5 text-fg")}>{o.text} </span>
          ))}
          <span className="ml-2 font-mono text-xs text-subtle">wspólne słowa {Math.round(100 * Math.max(0, p.ratio))}%</span>
        </div>
      ))}
      {r.state === "one_side" && <div className="grid grid-cols-2 gap-3 text-sm"><div>{r.only_a.slice(0, 2).join(" · ") || "—"}</div><div>{r.only_b.slice(0, 2).join(" · ") || "—"}</div></div>}
    </div>
  );
}

function ValuesTable({ rows, a, b }: { rows: DiffRow[]; a: string; b: string }) {
  return (
    <div className="overflow-x-auto"><table className="w-full table-fixed text-xs"><colgroup><col className="w-[110px]" /><col className="w-[150px]" /><col /><col /><col /></colgroup>
      <thead className="text-left text-subtle"><tr><th className="py-1.5 font-normal">gen</th><th className="py-1.5 font-normal">stan</th><th className="py-1.5 font-normal">wspólne</th><th className="py-1.5 font-normal">tylko {a}</th><th className="py-1.5 font-normal">tylko {b}</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.kind} className="border-t border-line align-top">
            <td className="py-1.5 pr-3 text-muted">{KIND_PL[r.kind] ?? r.kind}</td>
            <td className="py-1.5 pr-3"><StateChip s={r.state} /></td>
            {[r.shared, r.only_a, r.only_b].map((vals, i) => (
              <td key={i} className="min-w-0 py-1.5 pr-3">
                {vals.slice(0, 3).map((v) => <button key={v} type="button" onClick={() => openGene(r.kind, v)} className="block max-w-full truncate text-left hover:underline" title="dlaczego ten gen się liczy"><GeneValue value={v} max={22} /></button>)}
                {vals.length > 3 && <span className="text-subtle">+{vals.length - 3}</span>}
                {vals.length === 0 && <span className="text-subtle">—</span>}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

export function VariantDiffPage() {
  const { a = "", b = "" } = useParams();
  const q = usePairDiff(a, b);
  const ia = useIncident(q.data?.a);
  const ib = useIncident(q.data?.b);
  const pair = usePair(q.data?.a, q.data?.b);
  const d = q.data;
  if (!d) return <div className="p-6"><QueryState query={q} what="porównanie wariantów" endpoint={`/v1/pairs/${a}/${b}/diff`} /></div>;
  const sides = [{ id: d.a, inc: ia.data }, { id: d.b, inc: ib.data }];
  return (
    <Page
      title="Co się zmieniło"
      sub={<span className="inline-flex flex-wrap items-center gap-2"><span className="font-mono">{d.a} ↔ {d.b}</span>{d.hypothesis && <HypothesisBadge value={d.hypothesis} />}{d.total_bits !== null && <span className="font-mono">{formatBits(d.total_bits)}</span>}{d.campaign_id && <>kampania <CampaignLink id={d.campaign_id} /></>}</span>}
      actions={<Link to={pairPath(d.a, d.b)} className="inline-flex h-9 items-center rounded-md border border-line-strong px-3 text-sm hover:bg-hover">Dowód pary</Link>}
    >
      <div className="grid gap-4 md:grid-cols-2">
        {sides.map(({ id, inc }) => (
          <section key={id} className="surface-card flex gap-4 p-4">
            <Thumb artifactId={inc?.thumbnail} alt={id} size={140} />
            <div className="min-w-0"><IncidentLink id={id} /><div className="mt-1 text-sm text-fg">{inc?.title ?? "—"}</div><div className="text-xs text-muted">{inc?.observed_at ?? "data nieznana"}</div></div>
          </section>
        ))}
      </div>

      <section className="surface-card grid gap-4 p-5 md:grid-cols-3">
        <div><div className="text-xs text-subtle">wspólne wartości genów</div><div className="font-mono text-2xl">{d.summary.shared_values}</div></div>
        <div><div className="text-xs text-subtle">tylko w {d.a}</div><div className="font-mono text-2xl">{d.summary.only_a}</div></div>
        <div><div className="text-xs text-subtle">tylko w {d.b}</div><div className="font-mono text-2xl">{d.summary.only_b}</div></div>
        <div className="md:col-span-3 grid gap-4 md:grid-cols-2">
          <div>
            <div className="mb-1 text-sm font-semibold">Stałe geny (rdzeń wariantu)</div>
            <div className="flex flex-wrap gap-1.5">{d.summary.invariant_kinds.map(([k, n]) => <span key={k} title={`kod: ${k}`} className="rounded-md border border-for bg-for-soft px-2 py-0.5 text-xs">{KIND_PL[k] ?? k} · {n}</span>)}</div>
            {d.summary.campaign_core.length > 0 && <div className="mt-2 text-xs text-muted">genom rdzenia kampanii: {d.summary.campaign_core.slice(0, 6).map((g) => KIND_PL[g.kind] ?? g.kind).join(", ")}</div>}
          </div>
          <div>
            <div className="mb-1 text-sm font-semibold">Zmieniona dekoracja</div>
            <div className="flex flex-wrap gap-1.5">{d.summary.changed_kinds.map((k) => <span key={k} title={`kod: ${k}`} className="rounded-md border border-abstain bg-abstain-soft px-2 py-0.5 text-xs text-abstain-ink">{KIND_PL[k] ?? k}</span>)}{d.summary.changed_kinds.length === 0 && <span className="text-xs text-subtle">brak rodzajów genów zmienionych po obu stronach</span>}</div>
          </div>
        </div>
      </section>

      <div className="grid gap-4 xl:grid-cols-2">
        <section className="surface-card p-5">
          <h2 className="mb-1 text-sm font-semibold">Tekst</h2>
          <p className="mb-2 text-xs text-subtle">przekreślone: tylko w {d.a}; podświetlone: tylko w {d.b}</p>
          {d.text.length ? d.text.map((r) => <TextRow key={r.kind} r={r} />) : <div className="text-sm text-subtle">Brak genów tekstowych po żadnej stronie.</div>}
        </section>
        <div className="flex min-w-0 flex-col gap-4">
          <section className="surface-card p-5">
            <h2 className="mb-3 text-sm font-semibold">Obrazy dopasowane przez scorer</h2>
            {d.image_matches.length ? (
              <ul className="space-y-3">
                {d.image_matches.slice(0, 6).map((m, i) => (
                  <li key={i} className="flex items-center gap-3">
                    {[m.artifact_a, m.artifact_b].map((art, k) => art ? (
                      <Link key={k} to={`/incident/${encodeURIComponent(k === 0 ? d.a : d.b)}?art=${art}`} title="otwórz artefakt z genami" className="relative block">
                        {m.kind.startsWith("audio")
                          ? <span className="flex size-20 items-center justify-center rounded-md border border-line bg-hover text-2xs text-muted">▶ audio</span>
                          : <img src={thumbUrl(art, 160)} alt={`${k === 0 ? "A" : "B"}: ${/^(video|av_)/.test(m.kind) ? "wideo" : "obraz"} dopasowany`} className="size-20 rounded-md border border-line object-cover" />}
                        {/^(video|av_)/.test(m.kind) && <span className="absolute right-1 bottom-1 rounded-[2px] bg-fg/80 px-1 text-2xs text-canvas">▶</span>}
                      </Link>
                    ) : <span key={k} className="flex size-20 items-center justify-center rounded-md bg-hover text-2xs text-subtle">—</span>)}
                    <div className="min-w-0 flex-1 text-xs">
                      <div className="flex items-center gap-2"><FamChip family="VIS" /><span className="text-muted">{KIND_PL[m.kind] ?? m.kind}</span><span className="ml-auto font-mono">{formatBits(m.bits)}</span></div>
                      <div className="mt-1 text-muted">{m.explanation_pl}</div>
                      {m.similarity !== null && <div className="font-mono text-subtle">podobieństwo {plNumber(m.similarity, 3)}</div>}
                    </div>
                  </li>
                ))}
              </ul>
            ) : <div className="text-sm text-subtle">Scorer nie dopasował obrazów tej pary.</div>}
            {d.visual.length > 0 && <div className="mt-4"><ValuesTable rows={d.visual} a={d.a} b={d.b} /></div>}
            <div className="mt-4 border-t border-line pt-3">
              <h3 className="mb-2 text-xs font-semibold">Weryfikacja geometryczna: dopasowane punkty kluczowe</h3>
              <GeometryOverlay a={d.a} b={d.b} />
            </div>
          </section>
          {((d.video_segments?.length ?? 0) > 0 || (d.av_sync?.length ?? 0) > 0) && (
            <section className="surface-card p-5">
              <h2 className="mb-1 text-sm font-semibold">Wspólny materiał wideo</h2>
              <p className="mb-3 text-xs text-subtle">najdłuższy ciąg niemal identycznych klatek (PDQ / DINOv2) z czasem w obu nagraniach</p>
              <div className="space-y-4">
                {(d.video_segments ?? []).map((s, i) => (
                  <div key={i}>
                    <VideoSegmentStrip segment={s} a={d.a} b={d.b} />
                    {s.explanation_pl && <div className="mt-1 text-xs text-muted">{s.explanation_pl}</div>}
                    {s.artifact_a && s.artifact_b && (
                      <div className="mt-2 grid grid-cols-2 gap-2" aria-label="Klatki wspólnego fragmentu">
                        {([[s.artifact_a, s.start_a, s.end_a, "A"], [s.artifact_b, s.start_b, s.end_b, "B"]] as const).map(([art, st, en, L]) => (
                          <div key={L} className="flex gap-1 overflow-x-auto">
                            {[st, (st + en) / 2, en].map((t) => (
                              <figure key={t} className="shrink-0">
                                <img src={frameUrl(art, t, 160)} alt={`${L}: klatka ${mmss(t)}`} loading="lazy" className="h-24 w-auto rounded-[3px] border border-line bg-black object-contain" />
                                <figcaption className="font-mono text-2xs text-subtle">{L} {mmss(t)}</figcaption>
                              </figure>
                            ))}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
                {(d.video_segments?.length ?? 0) === 0 && <div className="text-sm text-subtle">Scorer nie znalazł wspólnego fragmentu wideo.</div>}
              </div>
              {(d.av_sync?.length ?? 0) > 0 && (
                <ul className="mt-4 space-y-1 border-t border-line pt-3 text-xs text-muted">
                  {(d.av_sync ?? []).map((g, i) => <li key={i}><span className="font-mono">{g.incident_id}</span>: {g.note}</li>)}
                  <li className="text-subtle">sygnał pomocniczy (0 bit), nigdy reguła powiązania</li>
                </ul>
              )}
            </section>
          )}
          <section className="surface-card p-5">
            <h2 className="mb-3 text-sm font-semibold">Infrastruktura</h2>
            {d.infrastructure.length ? <ValuesTable rows={d.infrastructure} a={d.a} b={d.b} /> : <div className="text-sm text-subtle">Brak genów infrastruktury.</div>}
          </section>
        </div>
      </div>
      {pair.data && (
        <section className="surface-card p-5">
          <h2 className="mb-1 text-sm font-semibold">Te same elementy w obu materiałach</h2>
          <p className="mb-3 text-xs text-subtle">powody scorera ze wskazaniem miejsca: ramka na obrazie lub klatce, fragment źródła strony, żądanie HAR</p>
          <EvidenceCompare a={d.a} b={d.b} reasons={[...pair.data.score.reasons, ...pair.data.score.contradictions]} />
        </section>
      )}
    </Page>
  );
}
