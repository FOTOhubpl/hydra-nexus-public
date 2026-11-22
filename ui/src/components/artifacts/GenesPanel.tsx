import { useMemo, useState } from "react";
import { Link } from "react-router";
import type { ArtifactGene, ArtifactGenes } from "../../api/artifacts";
import { FAMILIES } from "../../api/task";
import { pointerText } from "../../lib/artifactEvidence";
import { cx } from "../../lib/cx";
import { formatBits, GENE_STATUS_LABEL, plNumber } from "../../lib/evidence";
import { openGene } from "../../lib/geneExplain";
import { FamChip, GeneStatusChip, GeneValue, Prov } from "../evidence";

const LEVEL_LABEL: Record<string, string> = { campaign: "kampania", kit: "szablon", asset: "zasób", none: "szum (0 b)", unknown: "—" };

function geneKey(g: ArtifactGene, i: number): string {
  return `${g.id ?? i}`;
}

/** "Co wyciągnięto": every gene whose evidence points at this artefact, with rarity and the pairs it fed. */
export function GenesPanel({ data, focusId, onFocus, pairHref }: { data: ArtifactGenes; focusId?: number | null; onFocus?: (g: ArtifactGene) => void; pairHref: (a: string, b: string) => string }) {
  const [fam, setFam] = useState<string | null>(null);
  const [onlyPairs, setOnlyPairs] = useState(false);
  const [q, setQ] = useState("");
  const [all, setAll] = useState(false);
  const rows = useMemo(() => {
    const f = q.trim().toLowerCase();
    return data.genes
      .filter((g) => (!fam || g.family === fam) && (!onlyPairs || g.pairs.length > 0) && (!f || `${g.kind} ${g.value ?? ""} ${g.evidence?.note ?? ""}`.toLowerCase().includes(f)))
      .sort((a, b) => b.pairs.length - a.pairs.length || (b.rarity_bits ?? -1) - (a.rarity_bits ?? -1));
  }, [data.genes, fam, onlyPairs, q]);
  const shown = all ? rows : rows.slice(0, 150);
  const withPairs = data.genes.filter((g) => g.pairs.length > 0).length;

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5 text-xs">
        <button type="button" onClick={() => setFam(null)} aria-pressed={fam === null} className={cx("rounded-[4px] border px-2 py-0.5", fam === null ? "border-accent bg-active" : "border-line hover:bg-hover")}>wszystkie {data.total}</button>
        {FAMILIES.filter((f) => data.by_family[f]).map((f) => (
          <button key={f} type="button" onClick={() => setFam(fam === f ? null : f)} aria-pressed={fam === f} className={cx("inline-flex items-center gap-1 rounded-[4px] border px-1.5 py-0.5", fam === f ? "border-accent bg-active" : "border-line hover:bg-hover")}>
            <FamChip family={f} /> {data.by_family[f]}
          </button>
        ))}
        <label className="ml-1 inline-flex cursor-pointer items-center gap-1 select-none">
          <input type="checkbox" checked={onlyPairs} onChange={(e) => setOnlyPairs(e.target.checked)} className="accent-accent" /> tylko użyte w parach ({withPairs})
        </label>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="szukaj rodzaju / wartości" aria-label="Szukaj genu" className="ml-auto h-7 w-48 rounded-[4px] border border-line bg-raised px-2 focus:border-accent focus:outline-none" />
      </div>
      <div className="text-2xs text-subtle">
        Statusy: {Object.entries(data.by_status).map(([s, n]) => `${GENE_STATUS_LABEL[s] ?? s} ${n}`).join(" · ")}. Rzadkość = log₂(N/df) w {data.incidents_in_run} incydentach przebiegu (waga informacji, nie prawdopodobieństwo).
        {data.truncated && " Lista obcięta do limitu API."}
      </div>
      <div className="max-h-[60vh] overflow-auto rounded-md border border-line">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-raised text-2xs tracking-wider text-subtle uppercase">
            <tr>
              <th className="px-2 py-1 text-left">Rodzina</th><th className="px-2 py-1 text-left">Rodzaj</th><th className="px-2 py-1 text-left">Wartość</th>
              <th className="px-2 py-1 text-left">Poziom</th><th className="px-2 py-1 text-right">df · rzadkość</th><th className="px-2 py-1 text-left">Status</th>
              <th className="px-2 py-1 text-left">Miejsce</th><th className="px-2 py-1 text-left">Pary</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((g, i) => (
              <tr key={geneKey(g, i)} className={cx("border-t border-line align-top", focusId !== undefined && focusId === g.id ? "bg-against-soft" : "hover:bg-hover")}>
                <td className="px-2 py-1 whitespace-nowrap"><FamChip family={g.family} /> <Prov value={g.provenance} /></td>
                <td className="px-2 py-1 whitespace-nowrap text-muted">{g.kind}</td>
                <td className="max-w-[300px] px-2 py-1">
                  {g.value ? <button type="button" className="max-w-full truncate text-left hover:underline" title="dlaczego ten gen się liczy" onClick={() => openGene(g.kind, g.value!)}><GeneValue value={g.value} max={70} /></button>
                    : g.vector_ref ? <span className="text-subtle">wektor (osadzenie)</span> : <span className="text-subtle">—</span>}
                </td>
                <td className="px-2 py-1 whitespace-nowrap text-muted" title={g.tier ? `tier ${g.tier}` : undefined}>{LEVEL_LABEL[g.level] ?? g.level}</td>
                <td className="px-2 py-1 text-right font-mono whitespace-nowrap">{g.doc_freq ? `${g.doc_freq} · ${plNumber(g.rarity_bits ?? 0, 1)} b` : "—"}</td>
                <td className="px-2 py-1">
                  <GeneStatusChip status={g.status} />
                  {g.status !== "observed" && g.evidence?.note && <div className="mt-0.5 max-w-[220px] text-2xs text-subtle">{g.evidence.note}</div>}
                </td>
                <td className="px-2 py-1 text-muted">
                  {onFocus && (g.evidence?.bbox || g.evidence?.timecode_s !== null && g.evidence?.timecode_s !== undefined || g.evidence?.har_entry !== null && g.evidence?.har_entry !== undefined || g.value || g.evidence?.note) ? (
                    <button type="button" onClick={() => onFocus(g)} className="text-left text-accent hover:underline" title="pokaż w podglądzie">{pointerText(g.evidence, g.kind) || "pokaż w podglądzie"}</button>
                  ) : pointerText(g.evidence, g.kind) || "—"}
                </td>
                <td className="px-2 py-1">
                  {g.pairs.length === 0 ? <span className="text-subtle">—</span> : (
                    <ul className="space-y-0.5">
                      {g.pairs.slice(0, 4).map((p) => (
                        <li key={`${p.a}|${p.b}|${p.run_id}`} className="whitespace-nowrap">
                          <Link to={pairHref(p.a, p.b)} className="font-mono text-accent hover:underline" title={p.explanation_pl ?? undefined}>{p.a} ↔ {p.b}</Link>
                          <span className={cx("ml-1 font-mono", (p.bits ?? 0) < 0 ? "text-err" : "text-muted")}>{formatBits(p.bits)}</span>
                        </li>
                      ))}
                      {g.pairs.length > 4 && <li className="text-2xs text-subtle">+{g.pairs.length - 4} kolejnych</li>}
                    </ul>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && <div className="p-3 text-xs text-subtle">Brak genów z dowodem w tym artefakcie (dla wybranego filtra).</div>}
      </div>
      {rows.length > shown.length && <button type="button" onClick={() => setAll(true)} className="text-xs text-accent hover:underline">pokaż wszystkie ({rows.length})</button>}
    </div>
  );
}
