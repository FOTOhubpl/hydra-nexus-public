/** Side panels of the graph explorer: inspector (pair / incident / campaign), evidence path, lasso selection, stats. */
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { artifactContentUrl } from "../../api/client";
import { datasetExportUrl, neo4jExportUrl, useAiBrief, usePair, useReview, useSigningKey } from "../../api/task";
import { cx } from "../../lib/cx";
import { familyLabel, formatBits, plNumber } from "../../lib/evidence";
import { Button } from "../Button";
import { FamChip, Waterfall } from "../evidence";
import { ReviewEffectNote } from "../ReviewEffectNote";
import { downloadSubgraph, LEVEL_LABEL, LEVELS, useCampaignSummary, type EvidencePath, type Explore, type GEdge, type GNode, type Level } from "./api";
import { shortTitle } from "./build";

export function Section({ title, children, right }: { title: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="border-b border-line px-4 py-3">
      <header className="mb-2 flex items-center gap-2">
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        <span className="ml-auto">{right}</span>
      </header>
      {children}
    </section>
  );
}

export function LineKey({ style, color = "var(--color-muted)", width = 2 }: { style: "solid" | "dashed" | "dotted"; color?: string; width?: number }) {
  return (
    <svg aria-hidden="true" width="22" height="8" className="shrink-0">
      <line x1="1" y1="4" x2="21" y2="4" stroke={color} strokeWidth={width} strokeLinecap="round" strokeDasharray={style === "dashed" ? "5 3" : style === "dotted" ? "0.5 3.5" : undefined} />
    </svg>
  );
}

export function RobustVerdict({ e }: { e: GEdge }) {
  if (!e.robustness) return <span className="text-xs text-subtle">odporność: nie oceniono w tym przebiegu</span>;
  if (e.robustness.robust) return <span className="inline-flex h-6 items-center rounded-md border border-ok/50 px-2 text-xs text-fg">odporne: utrzymuje się bez każdej pojedynczej rodziny</span>;
  return (
    <span className="inline-flex h-6 items-center rounded-md border border-abstain bg-abstain-soft px-2 text-xs text-abstain-ink">
      zależy od: {e.robustness.critical.map((f) => `${f} (${familyLabel(f)})`).join(", ") || "jednej rodziny"}
    </span>
  );
}

export function ReviewBadgeG({ r }: { r: GEdge["review"] }) {
  if (!r) return null;
  if (r === "unsure") return <span className="inline-flex h-5 items-center rounded-[4px] border border-abstain px-1.5 text-2xs text-abstain-ink" title="oznaczenie nie zmienia kampanii">analityk: niepewne</span>;
  return r === "must_link" ? (
    <span className="inline-flex h-5 items-center rounded-[4px] border border-ok/60 px-1.5 text-2xs text-fg">analityk: musi łączyć</span>
  ) : (
    <span className="inline-flex h-5 items-center rounded-[4px] border border-err/60 px-1.5 text-2xs text-fg">analityk: nie może łączyć</span>
  );
}

export function PairInspector({ edge, data, onClose }: { edge: GEdge; data: Explore; onClose: () => void }) {
  const pair = usePair(edge.source, edge.target);
  const review = useReview();
  const nodes = useMemo(() => new Map(data.nodes.map((n) => [n.id, n])), [data.nodes]);
  const a = nodes.get(edge.source);
  const b = nodes.get(edge.target);
  const s = pair.data?.score;
  return (
    <div>
      <Section title="Para incydentów" right={<Button variant="ghost" size="sm" onClick={onClose} aria-label="Zamknij inspektor">zamknij</Button>}>
        <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-2">
          {[a, b].map((n, i) => n && (
            <div key={n.id} className="min-w-0">
              {n.thumbnail && <img src={artifactContentUrl(n.thumbnail)} alt="" className="mb-1 aspect-video w-full rounded-md border border-line object-cover" />}
              <div className="truncate font-mono text-xs text-subtle">{i === 0 ? "A" : "B"}: {n.id}</div>
              <div className="line-clamp-2 text-xs text-fg">{n.title ?? "bez tytułu"}</div>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded-[4px] border border-line-strong px-1.5 py-0.5 text-fg">poziom: {LEVEL_LABEL[edge.level]}</span>
          <span className="font-mono text-fg">{plNumber(edge.bits, 1)} b</span>
          {edge.family && <FamChip family={edge.family} withName />}
          {edge.inferred && <span className="text-subtle">wywnioskowane z wzbogacenia</span>}
          <ReviewBadgeG r={edge.review} />
        </div>
        <div className="mt-2"><RobustVerdict e={edge} /></div>
        <div className="mt-2 grid grid-cols-3 gap-2 text-2xs text-subtle">
          <div>kampania <span className="block font-mono text-xs text-fg">{plNumber(edge.campaign_bits, 1)} b</span></div>
          <div>zestaw <span className="block font-mono text-xs text-fg">{plNumber(edge.kit_bits, 1)} b</span></div>
          <div>zasób <span className="block font-mono text-xs text-fg">{plNumber(edge.asset_bits, 1)} b</span></div>
        </div>
      </Section>
      <Section title="Kaskada bitów">
        {s ? (
          <Waterfall familyBits={s.family_bits} totalBits={s.total_bits} missing={s.missing_families} reasons={s.reasons} contradictions={s.contradictions} />
        ) : pair.isError ? (
          <p className="text-xs text-err">Nie udało się pobrać dowodu pary.</p>
        ) : (
          <p className="text-xs text-subtle">wczytywanie dowodu…</p>
        )}
      </Section>
      <Section title="Działania">
        <div className="flex flex-wrap gap-2">
          <Link className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-raised px-3 text-sm text-fg hover:bg-hover" to={`/pair/${encodeURIComponent(edge.source)}/${encodeURIComponent(edge.target)}`}>Dowód pary</Link>
          <Link className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-raised px-3 text-sm text-fg hover:bg-hover" to={`/diff/${encodeURIComponent(edge.source)}/${encodeURIComponent(edge.target)}`}>Co się zmieniło</Link>
        </div>
        <div className="mt-2 flex flex-wrap gap-2">
          <Button size="sm" disabled={review.isPending} onClick={() => review.mutate({ action: "accept_link", target: { a: edge.source, b: edge.target } })}>Potwierdź powiązanie</Button>
          <Button size="sm" variant="danger" disabled={review.isPending} onClick={() => review.mutate({ action: "reject_link", target: { a: edge.source, b: edge.target } })}>Odrzuć powiązanie</Button>
        </div>
        {review.isSuccess && <p className="mt-1 text-xs text-ok">Decyzja zapisana.</p>}
        {review.isError && <p className="mt-1 text-xs text-err">Nie udało się zapisać decyzji.</p>}
        <div className="mt-1"><ReviewEffectNote /></div>
      </Section>
    </div>
  );
}

export function ClusterExplain({ campaignId }: { campaignId: string }) {
  const ai = useAiBrief("campaign", campaignId);
  const rule = useCampaignSummary(ai.data && ai.data.status !== "ok" ? campaignId : ai.isError ? campaignId : null);
  if (ai.isLoading) return <p className="text-xs text-subtle">pytam analityka AI…</p>;
  if (ai.data?.status === "ok") {
    return (
      <div className="text-sm text-fg">
        <p className="whitespace-pre-line">{ai.data.text_pl}</p>
        <p className="mt-1 text-2xs text-subtle">analityk AI ({ai.data.model ?? "model"}): tylko przeredagowuje dowody, nie decyduje</p>
      </div>
    );
  }
  return (
    <div className="text-sm text-fg">
      <p className="mb-1 text-2xs text-subtle">{ai.data?.reason_pl ?? "Analityk AI niedostępny."} Poniżej podsumowanie regułowe.</p>
      {rule.data ? (
        <>
          <p>{rule.data.text_pl}</p>
          {rule.data.core_genes.length > 0 && (
            <ul className="mt-2 space-y-1">
              {rule.data.core_genes.map((g, i) => (
                <li key={i} className="flex gap-2 text-xs"><FamChip family={g.family} /><span className="min-w-0 text-muted">{g.text}</span></li>
              ))}
            </ul>
          )}
        </>
      ) : rule.isError ? <p className="text-xs text-err">Brak podsumowania dla tej kampanii.</p> : <p className="text-xs text-subtle">wczytywanie…</p>}
    </div>
  );
}

export function NodeInspector({ node, data, onClose, onPickEdge, onPath }: {
  node: GNode;
  data: Explore;
  onClose: () => void;
  onPickEdge: (id: string) => void;
  onPath: (role: "a" | "b", id: string) => void;
}) {
  const [explain, setExplain] = useState(false);
  const camp = data.campaigns.find((c) => c.id === node.campaign_id);
  const nb = useMemo(() => data.edges.filter((e) => e.source === node.id || e.target === node.id).sort((x, y) => y.bits - x.bits), [data.edges, node.id]);
  const title = new Map(data.nodes.map((n) => [n.id, n.title]));
  return (
    <div>
      <Section title="Incydent" right={<Button variant="ghost" size="sm" onClick={onClose} aria-label="Zamknij inspektor">zamknij</Button>}>
        {node.thumbnail && <img src={artifactContentUrl(node.thumbnail)} alt="" className="mb-2 aspect-video w-full rounded-md border border-line object-cover" />}
        <div className="font-mono text-xs text-subtle">{node.id}</div>
        <div className="text-sm text-fg">{node.title ?? "bez tytułu"}</div>
        <dl className="mt-2 grid grid-cols-[110px_minmax(0,1fr)] gap-y-1 text-xs">
          <dt className="text-subtle">kampania</dt>
          <dd className="min-w-0 truncate">{camp ? <Link className="text-accent hover:underline" to={`/campaigns/${encodeURIComponent(camp.id)}`}>{shortTitle(camp.label, camp.id, 40)} ({camp.size})</Link> : "brak"}</dd>
          <dt className="text-subtle">obserwacja</dt><dd>{node.observed_at ?? "brak daty"}</dd>
          <dt className="text-subtle">rejestracja domeny</dt><dd>{node.registered_at ?? "brak danych"}</dd>
          <dt className="text-subtle">korpus</dt><dd>{node.corpus}</dd>
        </dl>
        <div className="mt-3 flex flex-wrap gap-2">
          <Link className="inline-flex h-6 items-center rounded-[4px] border border-line-strong bg-raised px-2 text-xs text-fg hover:bg-hover" to={`/incident/${encodeURIComponent(node.id)}`}>Karta incydentu</Link>
          <Button size="sm" onClick={() => onPath("a", node.id)}>Ścieżka od tego</Button>
          <Button size="sm" onClick={() => onPath("b", node.id)}>Ścieżka do tego</Button>
        </div>
      </Section>
      {camp && camp.size > 1 && (
        <Section title="Dlaczego ten klaster" right={!explain && <Button size="sm" variant="primary" onClick={() => setExplain(true)}>Wyjaśnij ten klaster</Button>}>
          {explain ? <ClusterExplain campaignId={camp.id} /> : <p className="text-xs text-subtle">Analityk AI (jeśli włączony) albo podsumowanie regułowe z dowodów kampanii.</p>}
        </Section>
      )}
      <Section title={`Powiązania (${nb.length})`}>
        {nb.length === 0 ? <p className="text-xs text-subtle">Brak powiązań na poziomie kampanii, zestawu, zasobu ani słabych.</p> : (
          <ul className="space-y-1">
            {nb.slice(0, 30).map((e) => {
              const other = e.source === node.id ? e.target : e.source;
              return (
                <li key={e.id}>
                  <button type="button" onClick={() => onPickEdge(e.id)} className="flex w-full items-center gap-2 rounded-[4px] px-1 py-1 text-left text-xs hover:bg-hover">
                    <span className="w-14 shrink-0 text-right font-mono text-fg">{plNumber(e.bits, 1)} b</span>
                    {e.family && <FamChip family={e.family} />}
                    <span className="min-w-0 flex-1 truncate text-muted">{shortTitle(title.get(other), other, 40)}</span>
                    <span className="shrink-0 text-2xs text-subtle">{LEVEL_LABEL[e.level]}</span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </Section>
    </div>
  );
}

export function PathPanel({ a, b, path, loading, data, onClear, onStep }: { a: string | null; b: string | null; path: EvidencePath | undefined; loading: boolean; data: Explore; onClear: () => void; onStep: (edgeId: string) => void }) {
  const title = new Map(data.nodes.map((n) => [n.id, n.title]));
  return (
    <Section title="Ścieżka dowodowa" right={<Button variant="ghost" size="sm" onClick={onClear}>wyczyść</Button>}>
      <p className="text-xs text-muted">
        od <span className="font-mono text-fg">{a ?? "wybierz węzeł"}</span> do <span className="font-mono text-fg">{b ?? "wybierz węzeł"}</span>
      </p>
      {loading && <p className="mt-2 text-xs text-subtle">liczę najsilniejszą ścieżkę…</p>}
      {path && !path.found && <p className="mt-2 text-xs text-muted">{path.reason_pl}</p>}
      {path?.found && (
        <>
          <p className="mt-2 text-xs text-fg">{path.summary_pl}</p>
          <ol className="mt-2 space-y-2">
            {path.steps.map((s, i) => (
              <li key={s.edge_id} className="rounded-md border border-line p-2">
                <button type="button" className="w-full text-left" onClick={() => onStep(s.edge_id)}>
                  <div className="flex items-center gap-2 text-xs">
                    <span className="font-mono text-subtle">{i + 1}.</span>
                    <span className="min-w-0 flex-1 truncate text-fg">{shortTitle(title.get(s.from), s.from, 22)} → {shortTitle(title.get(s.to), s.to, 22)}</span>
                    <span className="font-mono text-fg">{plNumber(s.bits, 1)} b</span>
                  </div>
                  <div className="mt-0.5 text-2xs text-subtle">{s.level_pl}{s.review ? `, decyzja analityka: ${s.review === "must_link" ? "musi łączyć" : s.review === "unsure" ? "niepewne" : "nie może"}` : ""}</div>
                </button>
                <ul className="mt-1 space-y-0.5">
                  {s.genes.slice(0, 3).map((g, j) => (
                    <li key={j} className="flex items-start gap-1.5 text-2xs text-muted">
                      {g.family && <FamChip family={g.family} />}
                      <span className="min-w-0 [overflow-wrap:anywhere]">{g.text}</span>
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ol>
        </>
      )}
    </Section>
  );
}

export function SelectionPanel({ ids, data, onClear, onCompare }: { ids: string[]; data: Explore; onClear: () => void; onCompare: (a: string, b: string) => void }) {
  const review = useReview();
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const set = useMemo(() => new Set(ids), [ids]);
  const nodes = data.nodes.filter((n) => set.has(n.id));
  const camps = [...new Set(nodes.map((n) => n.campaign_id).filter((c): c is string => Boolean(c)))];
  const internal = data.edges.filter((e) => set.has(e.source) && set.has(e.target));
  const oneCampaign = camps.length === 1 ? data.campaigns.find((c) => c.id === camps[0]) : undefined;
  const exp = async (f: "json" | "cypher") => {
    setBusy(f);
    setErr(null);
    try {
      await downloadSubgraph(ids, f);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };
  return (
    <Section title={`Zaznaczenie: ${ids.length} incydentów`} right={<Button variant="ghost" size="sm" onClick={onClear}>wyczyść</Button>}>
      <p className="text-xs text-muted">{camps.length} kampanii, {internal.length} powiązań wewnątrz zaznaczenia</p>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button size="sm" disabled={ids.length !== 2} onClick={() => onCompare(ids[0]!, ids[1]!)} title={ids.length !== 2 ? "Porównanie pary wymaga dokładnie dwóch incydentów" : undefined}>Porównaj parę</Button>
        <Button size="sm" disabled={ids.length !== 2 || review.isPending} onClick={() => review.mutate({ action: "accept_link", target: { a: ids[0], b: ids[1] } })}>Potwierdź powiązanie</Button>
        <Button size="sm" disabled={camps.length < 2 || review.isPending} onClick={() => review.mutate({ action: "merge_campaigns", target: { campaign_ids: camps } })} title={camps.length < 2 ? "Scalanie wymaga co najmniej dwóch kampanii w zaznaczeniu" : undefined}>Scal kampanie ({camps.length})</Button>
        <Button size="sm" disabled={!oneCampaign || ids.length >= (oneCampaign?.size ?? 0) || review.isPending} onClick={() => oneCampaign && review.mutate({ action: "split_campaign", target: { campaign_id: oneCampaign.id, incident_ids: ids } })} title="Wydziel zaznaczone incydenty z ich kampanii">Wydziel z kampanii</Button>
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button size="sm" disabled={busy !== null} onClick={() => void exp("json")}>Eksport JSON</Button>
        <Button size="sm" disabled={busy !== null} onClick={() => void exp("cypher")}>Eksport Neo4j Cypher</Button>
      </div>
      {review.isSuccess && <p className="mt-1 text-xs text-ok">Decyzja zapisana w dzienniku audytu.</p>}
      {(review.isError || err) && <p className="mt-1 text-xs text-err">{err ?? "Nie udało się zapisać decyzji."}</p>}
      <ul className="mt-2 max-h-48 space-y-0.5 overflow-auto">
        {nodes.slice(0, 60).map((n) => <li key={n.id} className="truncate text-2xs text-muted"><span className="font-mono text-subtle">{n.id}</span> {shortTitle(n.title, n.id, 50)}</li>)}
      </ul>
    </Section>
  );
}

export interface VisibleStats { nodes: number; edges: number; components: number; levels: Partial<Record<Level, number>>; families: Record<string, number>; robust: number; scored: number }

export function StatsPanel({ stats, data, onFocusCampaign }: { stats: VisibleStats; data: Explore; onFocusCampaign: (id: string) => void }) {
  const total = Math.max(1, stats.edges);
  const largest = data.stats?.largest_campaign;
  const fams = Object.entries(stats.families).sort((a, b) => b[1] - a[1]);
  return (
    <>
      <Section title="Statystyki widoku">
        <div className="grid grid-cols-3 gap-2">
          {[["węzły", stats.nodes], ["krawędzie", stats.edges], ["składowe", stats.components]].map(([l, v]) => (
            <div key={String(l)}><div className="text-2xs text-subtle">{l}</div><div className="font-mono text-lg text-fg">{Number(v).toLocaleString("pl-PL")}</div></div>
          ))}
        </div>
        {largest && (
          <p className="mt-2 text-xs text-muted">
            największa kampania:{" "}
            <button type="button" className="text-accent hover:underline" onClick={() => onFocusCampaign(largest.id)}>{shortTitle(largest.label, largest.id, 36)}</button>{" "}
            <span className="font-mono text-fg">({largest.size})</span>
          </p>
        )}
        <div className="mt-3 text-2xs text-subtle">gęstość poziomów dowodu (krawędzie w widoku)</div>
        <ul className="mt-1 space-y-1">
          {LEVELS.map((l) => {
            const v = stats.levels[l] ?? 0;
            return (
              <li key={l} className="grid grid-cols-[110px_minmax(0,1fr)_44px] items-center gap-2 text-xs">
                <span className="truncate text-muted">{LEVEL_LABEL[l]}</span>
                <span className="h-2 rounded-[2px] bg-hover"><span className="block h-2 rounded-[2px] bg-for" style={{ width: `${(100 * v) / total}%` }} /></span>
                <span className="text-right font-mono text-fg">{v}</span>
              </li>
            );
          })}
        </ul>
        <p className="mt-2 text-2xs text-subtle">
          odporność: {stats.scored === 0 ? "nie oceniono w tym przebiegu" : `${stats.robust} z ${stats.scored} ocenionych powiązań odpornych`}
        </p>
      </Section>
      <RunExports runId={data.run_id} />
      <Section title="Dlaczego te klastry">
        <ul className="space-y-1 text-xs text-muted">
          <li className="flex items-center gap-2"><LineKey style="solid" />ciągła: dowód na poziomie kampanii (≥ {data.thresholds?.link_bits ?? 10} b) lub decyzja analityka</li>
          <li className="flex items-center gap-2"><LineKey style="dashed" />przerywana: wspólny zestaw / szablon (tylko grupa)</li>
          <li className="flex items-center gap-2"><LineKey style="dotted" width={2.5} />kropkowana: wywnioskowane z wzbogacenia (WHOIS, DNS, TLS)</li>
          <li>grubość = bity dowodu, kolor = dominująca rodzina genów, blade = poniżej progu</li>
          <li>kolor węzła i tło = kampania; przerywany okrąg = grupa kampanii</li>
        </ul>
        {fams.length > 0 && (
          <ul className="mt-2 space-y-1">
            {fams.map(([f, n]) => (
              <li key={f} className="flex items-center gap-2 text-xs"><FamChip family={f} withName /><span className="ml-auto font-mono text-fg">{n}</span><span className="w-10 text-right text-2xs text-subtle">{Math.round((100 * n) / total)}%</span></li>
            ))}
          </ul>
        )}
      </Section>
    </>
  );
}

const DL = "inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover";

/** Whole-run exports: the Neo4j graph (same as `hydra export-neo4j`) and the enriched dataset ZIP. */
export function RunExports({ runId }: { runId: string | null }) {
  const key = useSigningKey();
  if (!runId) return null;
  const fp = key.data?.fingerprint;
  return (
    <Section title="Eksport przebiegu">
      <div className="flex flex-wrap gap-2">
        <a className={DL} href={neo4jExportUrl("cypher", 0, runId)} download={`hydra-${runId}.cypher`} title="Samodzielny skrypt Cypher: incydenty, kampanie, grupy, geny wspólne, powiązania LINKED_TO">↓ Neo4j Cypher</a>
        <a className={DL} href={neo4jExportUrl("zip", 0, runId)} download={`neo4j-${runId}.zip`} title="Pliki CSV i import.cypher (LOAD CSV) do katalogu import/ serwera Neo4j">↓ Neo4j CSV (ZIP)</a>
        <a className={DL} href={datasetExportUrl(runId)} download={`hydra-dataset-${runId}.zip`} title="Wzbogacony zbiór danych: incydenty, geny, wzbogacenie, powiązania, przypisania (z duplikatami), kampanie, manifest podpisany Ed25519">↓ Zbiór danych (ZIP)</a>
      </div>
      <p className="mt-1 text-2xs text-subtle">cały przebieg, nie tylko widok; zawiera materiał organizatora, nie do publikacji</p>
      <p className="mt-1 text-2xs text-subtle" data-testid="export-signing-key">
        Zbiór danych podpisany Ed25519 (SIGNED_MANIFEST.json + .sig), weryfikacja offline: <code>hydra verify-export</code>.
        {" "}Odcisk klucza: <code className="break-all">{fp ?? (key.isError ? "niedostępny" : "…")}</code>
        {" "}(podpis potwierdza integralność, nie prawdziwość materiału)
      </p>
    </Section>
  );
}

export function TooltipCard({ children, x, y, w, h }: { children: ReactNode; x: number; y: number; w: number; h: number }) {
  const left = Math.min(x + 14, w - 320);
  const top = Math.max(8, Math.min(y + 14, h - 300));
  return (
    <div role="tooltip" className={cx("pointer-events-none absolute z-20 w-[300px] rounded-md border border-line-strong bg-panel p-2.5 text-xs shadow-sm")} style={{ left, top }}>
      {children}
    </div>
  );
}

export function ReasonList({ reasons }: { reasons: GEdge["reasons"] }) {
  if (reasons.length === 0) return <p className="text-subtle">brak zapisanych powodów</p>;
  return (
    <ul className="space-y-1">
      {reasons.slice(0, 3).map((r, i) => (
        <li key={i} className="flex items-start gap-1.5">
          <span className="w-12 shrink-0 text-right font-mono text-fg">{formatBits(r.bits)}</span>
          <span className="min-w-0 text-muted [overflow-wrap:anywhere]">{r.text.length > 180 ? `${r.text.slice(0, 179)}…` : r.text}</span>
        </li>
      ))}
    </ul>
  );
}
