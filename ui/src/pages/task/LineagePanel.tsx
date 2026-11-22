/** Variant lineage of a campaign (GET /v1/campaigns/{id}/lineage, hydra/lineage.py): who follows whom in observation
 * order, and what changed / stayed on each edge. Hidden for singletons and when the route is unavailable. */
import { Link } from "react-router";
import { useCampaignLineage, type LineageEdge, type LineageItem, type MutationTree } from "../../api/task";
import { Badge } from "../../components/Badge";
import { Panel } from "../../components/Panel";
import { SectionNote } from "../../components/evidence";
import { formatBits, formatDatePl, plNumber } from "../../lib/evidence";
import { IncidentLink } from "./common";

const DIRECTION_BADGE: Record<string, { label: string; variant: "info" | "warning" }> = {
  observation_order: { label: "kolejność obserwacji", variant: "info" },
  mixed: { label: "kolejność obserwacji (część bez dat)", variant: "warning" },
  import_order: { label: "kolejność importu", variant: "warning" },
  undirected: { label: "bez kierunku", variant: "warning" },
};

function cats(items: LineageItem[]): string[] {
  return [...new Set(items.map((i) => i.category_pl))];
}

function Chips({ items, tone }: { items: LineageItem[]; tone: "for" | "against" | "muted" }) {
  if (!items.length) return <span className="text-subtle">—</span>;
  const cls = tone === "for" ? "border-for bg-for-soft" : tone === "against" ? "border-against bg-against-soft" : "border-line bg-hover";
  return (
    <span className="inline-flex flex-wrap gap-1">
      {cats(items).map((c) => {
        const kinds = items.filter((i) => i.category_pl === c).map((i) => i.kind);
        return <span key={c} title={kinds.join(", ")} className={`inline-flex h-[18px] items-center rounded-[3px] border px-1 text-2xs text-fg ${cls}`}>{c}</span>;
      })}
    </span>
  );
}

function EdgeRow({ e, observed }: { e: LineageEdge; observed: string | null }) {
  const keptBits = e.kept.filter((k) => (k.bits ?? 0) > 0);
  return (
    <tr className="border-t border-line align-top">
      <td className="py-1.5 pr-2 whitespace-nowrap"><IncidentLink id={e.child} /><div className="text-2xs text-subtle">{observed ? formatDatePl(observed) : "data nieznana"}</div></td>
      <td className="py-1.5 pr-2 whitespace-nowrap"><IncidentLink id={e.parent} /></td>
      <td className="py-1.5 pr-2 text-right font-mono whitespace-nowrap">{formatBits(e.bits)}</td>
      <td className="py-1.5 pr-2"><Chips items={[...e.changed, ...e.added, ...e.removed]} tone="against" /></td>
      <td className="py-1.5 pr-2" title={keptBits.map((k) => k.explanation_pl ?? k.kind).join("\n")}><Chips items={e.kept} tone="for" /></td>
      <td className="py-1.5 text-muted">
        {e.summary_pl}
        {e.counts.unknown > 0 && <span className="text-subtle"> · nieznane po jednej stronie: {e.counts.unknown}</span>}
        {" "}<Link className="text-accent hover:underline" to={`/diff/${encodeURIComponent(e.parent)}/${encodeURIComponent(e.child)}`}>porównaj</Link>
      </td>
    </tr>
  );
}

/** Spec M7 variant tree by mutation distance (HYDRA_ADV_LINEAGE=1): mutations named on every edge. */
function MutationTreeView({ t }: { t: MutationTree }) {
  return (
    <div className="space-y-1 border-t border-line pt-2" data-testid="mutation-tree">
      <div className="text-xs font-medium text-fg">
        Drzewo mutacji (Edmonds){" "}
        <span className="text-subtle">· korzeń: {t.root ? <IncidentLink id={t.root} /> : "—"} · koszt {plNumber(t.total_cost, 2)}
          {t.agreement_with_link_tree !== undefined && ` · zgodność z linią powyżej: ${plNumber(t.agreement_with_link_tree * 100, 0)}%`}
          {!t.spanning && " · las (nie wszyscy członkowie osiągalni)"}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs" data-testid="mutation-edges">
          <thead className="text-2xs tracking-wider text-subtle uppercase">
            <tr><th className="py-1 text-left">Wariant</th><th className="py-1 text-left">Od</th><th className="py-1 text-right" title="odległość mutacji genów (heurystyka), nie bity">Mutacja</th><th className="py-1 text-left">Mutacje na krawędzi</th></tr>
          </thead>
          <tbody>
            {t.edges.map((e) => (
              <tr key={`${e.parent}|${e.child}`} className="border-t border-line align-top">
                <td className="py-1.5 pr-2 whitespace-nowrap"><IncidentLink id={e.child} /></td>
                <td className="py-1.5 pr-2 whitespace-nowrap"><IncidentLink id={e.parent} />{e.against_observation_order && <div className="text-2xs text-warn">wbrew kolejności dat</div>}</td>
                <td className="py-1.5 pr-2 text-right font-mono">{plNumber(e.mutation_distance, 2)}</td>
                <td className="py-1.5 text-muted">
                  <span className="inline-flex flex-wrap gap-1">
                    {e.mutations.slice(0, 6).map((m) => (
                      <span key={`${m.kind}|${m.type}`} title={`${m.kind}: ${(m.before ?? []).join(", ")} → ${(m.after ?? []).join(", ")}`} className="inline-flex h-[18px] items-center rounded-[3px] border border-against bg-against-soft px-1 text-2xs text-fg">{m.type_pl}: {m.category_pl}</span>
                    ))}
                  </span>
                  <div>{e.summary_pl}</div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {t.note_pl && <SectionNote>{t.note_pl}</SectionNote>}
    </div>
  );
}

export function LineagePanel({ campaignId, runId }: { campaignId: string; runId?: string | null }) {
  const q = useCampaignLineage(campaignId, runId);
  const lin = q.data;
  if (!lin || !Array.isArray(lin.nodes) || !Array.isArray(lin.edges) || lin.nodes.length < 2) return null;
  const observed = Object.fromEntries(lin.nodes.map((n) => [n.incident_id, n.observed_at]));
  const dir = DIRECTION_BADGE[lin.direction] ?? { label: lin.direction, variant: "warning" as const };
  return (
    <Panel
      title="Linia wariantów"
      actions={<span className="flex items-center gap-2"><Badge variant={dir.variant} dot title={lin.direction_pl}>{dir.label}</Badge><span className="text-xs text-subtle">{lin.edges.length} krawędzi · korzenie: {lin.roots.length}</span></span>}
      flush
    >
      <div className="space-y-2 p-3">
        <div className="text-xs text-muted">
          Początek linii: {lin.roots.map((r, i) => <span key={r}>{i > 0 && ", "}<IncidentLink id={r} /></span>)} · {lin.direction_pl}
        </div>
        {lin.edges.length === 0 ? <div className="text-xs text-subtle">Brak ocenionych powiązań między członkami.</div> : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs" data-testid="lineage-edges">
              <thead className="text-2xs tracking-wider text-subtle uppercase">
                <tr>
                  <th className="py-1 text-left">Wariant</th>
                  <th className="py-1 text-left">Po</th>
                  <th className="py-1 text-right" title="bity poziomu kampanii (wsparcie powiązania), nie prawdopodobieństwo">Bity</th>
                  <th className="py-1 text-left">Zmiany</th>
                  <th className="py-1 text-left">Bez zmian</th>
                  <th className="py-1 text-left">Opis</th>
                </tr>
              </thead>
              <tbody>
                {lin.edges.map((e) => <EdgeRow key={`${e.parent}|${e.child}`} e={e} observed={observed[e.child] ?? null} />)}
              </tbody>
            </table>
          </div>
        )}
        <SectionNote>{lin.note_pl}</SectionNote>
        {lin.mutation_tree && lin.mutation_tree.edges.length > 0 && <MutationTreeView t={lin.mutation_tree} />}
      </div>
    </Panel>
  );
}
