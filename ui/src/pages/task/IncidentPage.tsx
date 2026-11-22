import { DecisionRule } from "../../components/DecisionRule";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { ArtifactGallery, defaultArtifact } from "../../components/artifacts/ArtifactGallery";
import { ArtifactViewer } from "../../components/artifacts/ArtifactViewer";
import { openGene } from "../../lib/geneExplain";
import { useIncident, useSimilarCases, type FamilyGroup, type Gene, type SimilarItem } from "../../api/task";
import { DecisionBadge, FamChip, GeneStatusChip, GeneValue, HypothesisBadge, KindLabel, Probability, Prov, SectionNote } from "../../components/evidence";
import { LikenessCard } from "../../components/LikenessCard";
import { Panel } from "../../components/Panel";
import { familyLabel, formatBits, formatDatePl, GENE_STATUS_LABEL, plNumber } from "../../lib/evidence";
import { CampaignLink, IncidentLink, Page, pairPath, QueryState } from "./common";
import { polishReason } from "../../lib/displayPl";
import { LabLink, topPair } from "../../components/LabLink";
import { isWholeFile, WHOLE_FILE_TEXT } from "../../lib/displayPl";

function evidenceText(g: Gene): string {
  const e = g.evidence ?? {};
  const parts: string[] = [];
  if (e.artifact_id) parts.push(`artefakt ${e.artifact_id.slice(0, 10)}…`);
  const whole = isWholeFile(e, g.kind);
  if (whole) parts.push(WHOLE_FILE_TEXT);
  if (e.bbox && !whole) parts.push(`ramka ${e.bbox.map((v) => Math.round(v)).join(",")}`);
  if (e.char_range) parts.push(`znaki ${e.char_range[0]}–${e.char_range[1]}`);
  if (e.timecode_s !== null && e.timecode_s !== undefined && !whole) parts.push(`t=${e.timecode_s.toFixed(1).replace(".", ",")} s`);
  if (e.har_entry !== null && e.har_entry !== undefined) parts.push(`HAR #${e.har_entry}`);
  if (e.dom_path) parts.push(`DOM ${e.dom_path}`);
  if (e.enrichment_key) parts.push("wzbogacenie");
  if (e.note) parts.push(e.note);
  return parts.join(" · ");
}

function FamilyPanel({ group }: { group: FamilyGroup }) {
  const total = Object.values(group.counts).reduce((s, n) => s + (n ?? 0), 0);
  // Big families (hundreds of DOM/asset genes) start collapsed; the header already shows counts per kind.
  const [open, setOpen] = useState(group.status === "observed" && total <= 60);
  return (
    <section className="rounded-md border border-line bg-panel">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="flex h-9 w-full items-center gap-3 px-3 text-left hover:bg-hover">
        <span aria-hidden="true" className="w-3 text-2xs text-subtle">{open ? "▾" : "▸"}</span>
        <FamChip family={group.family} withName />
        <GeneStatusChip status={group.status} />
        <span className="text-xs text-subtle">
          {total === 0 ? "brak genów: nieznane (nie dowód przeciw)" : Object.entries(group.counts).map(([s, n]) => `${GENE_STATUS_LABEL[s] ?? s}: ${n}`).join(" · ")}
        </span>
        <span className="ml-auto truncate text-2xs text-subtle">{Object.entries(group.kinds).map(([k, n]) => `${k} ${n}`).join(" · ")}</span>
      </button>
      {open && group.genes.length > 0 && (
        <div className="overflow-x-auto border-t border-line">
          <table className="w-full text-xs">
            <thead className="bg-raised text-2xs tracking-wider text-subtle uppercase">
              <tr>
                <th className="px-2 py-1.5 text-left">Poch.</th>
                <th className="px-2 py-1.5 text-left">Rodzaj</th>
                <th className="px-2 py-1.5 text-left">Wartość</th>
                <th className="px-2 py-1.5 text-left">Status</th>
                <th className="px-2 py-1.5 text-right">Jakość</th>
                <th className="px-2 py-1.5 text-left">Dowód (wskaźnik)</th>
                <th className="px-2 py-1.5 text-left">Ekstraktor</th>
              </tr>
            </thead>
            <tbody>
              {group.genes.map((g, i) => (
                <tr key={g.id ?? i} className="border-t border-line">
                  <td className="px-2 py-1"><Prov value={g.provenance} /></td>
                  <td className="px-2 py-1 whitespace-nowrap text-muted"><KindLabel kind={g.kind} /></td>
                  <td className="max-w-[420px] truncate px-2 py-1">
                    {g.value ? <button type="button" className="max-w-full truncate text-left hover:underline" title="dlaczego ten gen się liczy" onClick={() => openGene(g.kind, g.value!)}><GeneValue value={g.value} max={90} /></button> : g.vector_ref ? <span className="text-subtle">wektor (osadzenie)</span> : <span className="text-subtle">—</span>}
                  </td>
                  <td className="px-2 py-1"><GeneStatusChip status={g.status} /></td>
                  <td className="px-2 py-1 text-right font-mono">{g.quality.toFixed(2).replace(".", ",")}</td>
                  <td className="px-2 py-1 text-muted">
                    {g.evidence?.artifact_id ? (
                      <Link className="text-accent hover:underline" to={`?art=${g.evidence.artifact_id}`} preventScrollReset={false}>{evidenceText(g)}</Link>
                    ) : evidenceText(g) || "—"}
                  </td>
                  <td className="px-2 py-1 font-mono text-subtle">{g.extractor}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {group.truncated > 0 && <div className="px-3 py-1.5 text-2xs text-subtle">… oraz {group.truncated} kolejnych genów (pełna lista w eksporcie CSV kampanii)</div>}
        </div>
      )}
    </section>
  );
}

function SimilarRow({ item }: { item: SimilarItem }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="border-t border-line py-1.5 first:border-t-0" data-testid="similar-row">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs">
        <IncidentLink id={item.incident_id} title={item.title} />
        <CampaignLink id={item.campaign_id} />
        {item.duplicate && <span className="inline-flex h-[18px] items-center rounded-[3px] border border-abstain bg-abstain-soft px-1 text-2xs font-medium text-abstain-ink" title="ten sam materiał (duplikat), nie niezależny dowód">duplikat</span>}
        <span className="ml-auto font-mono text-muted" title="wspólne rzadkie wartości / wszystkie wartości tego incydentu">wspólne {item.shared}/{item.total}</span>
      </div>
      <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-muted">
        <span className="font-mono" title="wspólne wartości operatora (infrastruktura, identyfikatory) / wszystkie wartości operatora">operatora {item.shared_operator}/{item.total_operator}</span>
        <span className="font-mono" title="suma bitów rzadkości wspólnych wartości; heurystyka, nie prawdopodobieństwo">rzadkość {plNumber(item.rare_bits, 1)} bit</span>
        {(item.values.length > 0 || item.reasons_pl.length > 0) && (
          <button type="button" onClick={() => setOpen(!open)} aria-expanded={open} className="ml-auto text-accent hover:underline">{open ? "▾ ukryj powody" : "▸ powody"}</button>
        )}
      </div>
      {item.title && <div className="truncate text-2xs text-subtle">{item.title}</div>}
      {open && (
        <div className="mt-1 space-y-1 rounded-md bg-raised p-1.5 text-xs">
          {item.reasons_pl.length > 0 && <ul className="list-disc space-y-0.5 pl-4 text-muted">{item.reasons_pl.map((r, i) => <li key={i}>{r}</li>)}</ul>}
          {item.values.length > 0 && (
            <ul className="space-y-0.5">
              {item.values.map((v, i) => (
                <li key={i} className="flex min-w-0 items-center gap-1.5" title={v.explanation_pl}>
                  <FamChip family={v.family} /><KindLabel kind={v.kind} className="text-muted" />
                  <span className="min-w-0 flex-1 truncate"><GeneValue value={v.value} max={50} /></span>
                  <span className="whitespace-nowrap text-subtle">w {v.df} {v.df === 1 ? "incydencie" : "incydentach"}</span>
                  <span className="font-mono">{plNumber(v.bits, 1)} bit</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </li>
  );
}

/** TheHive-style similar cases: shared rare values with this incident (explanation card, not a linking result). */
function SimilarCasesPanel({ incidentId }: { incidentId: string }) {
  const q = useSimilarCases(incidentId, 10);
  const d = q.data;
  return (
    <Panel title="Podobne przypadki" actions={<span className="text-xs text-subtle">wspólne rzadkie wartości; karta wyjaśniająca, nie wynik powiązania</span>}>
      {!d ? <QueryState query={q} what="podobne przypadki" endpoint={`/v1/incidents/${incidentId}/similar`} /> : d.items.length === 0 ? (
        <div className="text-xs text-subtle">{d.empty_reason_pl ?? "Brak incydentów ze wspólnymi rzadkimi wartościami."}</div>
      ) : (
        <div className="space-y-1.5">
          <ul>{d.items.map((it) => <SimilarRow key={it.incident_id} item={it} />)}</ul>
          <SectionNote>
            {d.method_pl} Wartości tego incydentu: {d.total_values} (operatora {d.operator_values}); pominięte jako zbyt częste: {d.excluded_common}.
          </SectionNote>
        </div>
      )}
    </Panel>
  );
}
export function IncidentPage() {
  const { id } = useParams();
  const q = useIncident(id);
  const [sp, setSp] = useSearchParams();
  if (!q.data) return <div className="p-4"><QueryState query={q} what="incydent" endpoint={`/v1/incidents/${id}`} /></div>;
  const inc = q.data;
  const wanted = sp.get("art");
  const current = inc.artifacts.find((a) => a.artifact_id === wanted) ?? defaultArtifact(inc.artifacts);
  const select = (artifactId: string) => {
    const next = new URLSearchParams(sp);
    next.set("art", artifactId);
    setSp(next, { replace: true, preventScrollReset: true });
  };

  return (
    <Page
      title={<span className="inline-flex items-center gap-2"><span className="font-mono">{inc.id}</span>{inc.title && <span className="text-muted">· {inc.title}</span>}</span>}
      sub={<>źródło {inc.source} · <span className="font-mono">{inc.source_ref}</span> · {formatDatePl(inc.observed_at)}</>}
      actions={<Link to={`/lab/${encodeURIComponent(inc.id)}`} data-testid="incident-lab-link" className="inline-flex h-7 items-center rounded-md border border-line-strong px-2.5 text-xs hover:bg-hover">Laboratorium hipotez</Link>}
    >
      <div className="grid gap-3 xl:grid-cols-[1fr_380px]">
        <div className="flex min-w-0 flex-col gap-3">
          <Panel title={`Materiały incydentu (${inc.artifacts.length})`} actions={<span className="text-xs text-subtle">kliknij artefakt, aby zobaczyć podgląd i wyciągnięte geny</span>}>
            {inc.artifacts.length === 0 ? <div className="text-xs text-subtle">Incydent nie ma zarejestrowanych artefaktów.</div> : (
              <ArtifactGallery artifacts={inc.artifacts} selected={current?.artifact_id ?? null} onSelect={(a) => select(a.artifact_id)} />
            )}
          </Panel>
          {current && (
            <Panel title={<span className="inline-flex min-w-0 items-center gap-2">Artefakt <span className="truncate font-mono text-xs font-normal text-muted">{current.path_in_incident}</span></span>}>
              <ArtifactViewer key={current.artifact_id} id={current.artifact_id} role={current.role} path={current.path_in_incident} pairHref={pairPath} />
            </Panel>
          )}
          <div className="flex flex-col gap-2">
            <h3 className="text-xs font-semibold tracking-wider text-muted uppercase">Geny według rodzin</h3>
            {inc.families.map((g) => <FamilyPanel key={g.family} group={g} />)}
          </div>
        </div>
        <div className="flex min-w-0 flex-col gap-3">
          <Panel title="Kampania i przypisanie">
            <div className="space-y-2 text-sm">
              <div><span className="text-subtle">Kampania w przebiegu: </span><CampaignLink id={inc.campaign_id} /></div>
              {inc.assignment && (
                <div className="space-y-1">
                  <DecisionBadge value={inc.assignment.decision} />
                  <div className="text-xs text-muted">{polishReason(inc.assignment.reason_pl)}</div>
                  <DecisionRule meta={inc.assignment.meta} bestPair={topPair(inc.assignment)} />
                  <LabLink incidentId={inc.id} />
                </div>
              )}
              {!inc.run_id && <div className="text-xs text-subtle">Brak przebiegu analizy.</div>}
            </div>
          </Panel>
          <LikenessCard incidentId={inc.id} runId={inc.run_id} />
          <Panel title="Najbliższe incydenty">
            {inc.neighbours.length === 0 ? <div className="text-xs text-subtle">Brak ocenionych par z tym incydentem.</div> : (
              <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
                <tbody>
                  {inc.neighbours.map((n) => (
                    <tr key={n.other} className="border-t border-line first:border-t-0">
                      <td className="py-1"><IncidentLink id={n.other} /></td>
                      <td className="py-1"><HypothesisBadge value={n.hypothesis} /></td>
                      <td className="py-1 text-right font-mono">{formatBits(n.total_bits)}</td>
                      <td className="py-1 text-right"><Link to={pairPath(inc.id, n.other)} className="text-accent hover:underline"><Probability p={n.probability} /> →</Link></td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </Panel>
          <SimilarCasesPanel incidentId={inc.id} />
          <Panel title={`Wzbogacenie (${inc.enrichment.length})`} actions={<span className="text-xs text-subtle">fakty z publicznych źródeł</span>}>
            {inc.enrichment.length === 0 ? (
              <div className="text-xs text-subtle">Brak rekordów wzbogacenia (tryb offline lub brak domen). Brak = nieznane.</div>
            ) : (
              <ul className="space-y-2">
                {inc.enrichment.map((e) => (
                  <li key={e.key} className="rounded-md border border-line p-2 text-xs">
                    <div className="flex items-center gap-2">
                      <Prov value="OBS" />
                      <span className="font-semibold">{e.source}</span>
                      <span className="truncate font-mono">{e.query}</span>
                      <GeneStatusChip status={e.status} />
                    </div>
                    <div className="mt-1 text-2xs text-subtle">
                      {e.derived_from_genes
                        ? "zestawione z genów (brak rekordu w tabeli wzbogacenia: data pobrania nieznana)"
                        : <>pobrano {formatDatePl(e.fetched_at)}{e.response_sha256 && <> · sha256 {e.response_sha256.slice(0, 12)}…</>}</>}
                    </div>
                    {Object.keys(e.summary).length > 0 && (
                      <dl className="mt-1 grid grid-cols-[110px_1fr] gap-x-2">
                        {Object.entries(e.summary).slice(0, 12).map(([k, v]) => (
                          <div key={k} className="contents"><dt className="text-subtle">{k}</dt><dd className="break-all"><GeneValue value={typeof v === "object" ? JSON.stringify(v) : String(v)} max={80} /></dd></div>
                        ))}
                      </dl>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          <SectionNote>Rodziny: {["VIS", "TXT", "INF", "ID", "NAR", "META"].map((f) => `${f} = ${familyLabel(f)}`).join(", ")}.</SectionNote>
        </div>
      </div>
    </Page>
  );
}
