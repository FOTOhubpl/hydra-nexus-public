/** Spec M6 consistency gate report of a campaign (GET /v1/campaigns/{id}/consistency, hydra/consistency.py):
 * cohesion, weakest link, members held by a single pair, strong single bridges, contradictions and what the gate
 * would do. Read-only telemetry; hidden for singletons and when the route is unavailable. */
import { useCampaignConsistency, type ConsistencyMember } from "../../api/task";
import { Badge, type BadgeVariant } from "../../components/Badge";
import { Panel } from "../../components/Panel";
import { SectionNote } from "../../components/evidence";
import { formatBits, plNumber } from "../../lib/evidence";
import { IncidentLink } from "./common";

const VERDICT: Record<string, { label: string; variant: BadgeVariant }> = {
  consistent: { label: "spójna", variant: "success" },
  weak_members: { label: "słabi członkowie", variant: "warning" },
  gate_would_split: { label: "bramka rozdzieliłaby", variant: "danger" },
  pair_campaign: { label: "jedna para", variant: "info" },
  contradiction: { label: "sprzeczność", variant: "danger" },
};

const STATUS_PL: Record<ConsistencyMember["status"], string> = {
  ok: "≥ 2 pary wspierające",
  strong_single: "jedno silne powiązanie",
  single_pair: "trzyma go jedna para",
  no_direct_link: "brak bezpośredniego powiązania",
  analyst_link: "powiązanie analityka",
  pair_campaign: "kampania z jednej pary",
};

function Ids({ ids }: { ids: string[] | undefined }) {
  if (!ids?.length) return <span className="text-subtle">—</span>;
  return <span>{ids.map((i, k) => <span key={i}>{k > 0 && ", "}<IncidentLink id={i} /></span>)}</span>;
}

export function ConsistencyPanel({ campaignId, runId }: { campaignId: string; runId?: string | null }) {
  const q = useCampaignConsistency(campaignId, runId);
  const r = q.data;
  if (!r || !r.applicable || !Array.isArray(r.members)) return null;
  const v = VERDICT[r.verdict] ?? { label: r.verdict, variant: "neutral" as const };
  const weak = r.members.filter((m) => m.status !== "ok" && m.status !== "pair_campaign");
  return (
    <Panel
      title="Bramka spójności (M6)"
      actions={<span className="flex items-center gap-2"><Badge variant={v.variant} dot title={r.verdict_pl}>{v.label}</Badge><span className="text-xs text-subtle">{r.gate_enabled ? "bramka włączona w grupowaniu" : "raport; bramka nie zmienia grupowania"}</span></span>}
      flush
    >
      <div className="space-y-2 p-3 text-xs" data-testid="consistency-report">
        <div className="grid gap-2 sm:grid-cols-4">
          <div><div className="text-2xs text-subtle uppercase">Spójność</div><div className="font-mono">{r.cohesion != null ? plNumber(r.cohesion, 2) : "—"}</div></div>
          <div><div className="text-2xs text-subtle uppercase">Najsłabsze ogniwo</div><div className="font-mono">{r.weakest_link_bits != null ? formatBits(r.weakest_link_bits) : "—"}</div></div>
          <div><div className="text-2xs text-subtle uppercase">Mediana S ocenionych par</div><div className="font-mono">{r.median_scored_bits != null ? formatBits(r.median_scored_bits) : "—"} <span className="text-subtle">(próg {formatBits(r.low_threshold_bits)}; {r.scored_pairs}/{r.possible_pairs} par)</span></div></div>
          <div><div className="text-2xs text-subtle uppercase">Trzymani przez jedną parę</div><div><Ids ids={r.single_pair_members} /></div></div>
        </div>
        <div className="text-muted">{r.verdict_pl}{r.gate_applied ? ` · bramka rozdzieliła składową ${r.gate_applied.from_size} incydentów na ${r.gate_applied.parts} części` : ""}</div>
        {r.gate_parts && r.gate_parts.length > 1 && (
          <div>Części po bramce: {r.gate_parts.map((p, i) => <span key={i} className="mr-2">[<Ids ids={p} />]</span>)}</div>
        )}
        {r.contradiction_pairs && r.contradiction_pairs.length > 0 && (
          <div className="text-against">Twarde sprzeczności: {r.contradiction_pairs.map((p) => p.join(" – ")).join("; ")}</div>
        )}
        {weak.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs" data-testid="consistency-members">
              <thead className="text-2xs tracking-wider text-subtle uppercase">
                <tr><th className="py-1 text-left">Członek</th><th className="py-1 text-left">Stan</th><th className="py-1 text-right">Pary wspierające</th><th className="py-1 text-right">Mediana S</th><th className="py-1 text-left">Najsilniejsza para / powód</th></tr>
              </thead>
              <tbody>
                {weak.map((m) => (
                  <tr key={m.id} className="border-t border-line align-top">
                    <td className="py-1.5 pr-2"><IncidentLink id={m.id} /></td>
                    <td className="py-1.5 pr-2">{STATUS_PL[m.status] ?? m.status}</td>
                    <td className="py-1.5 pr-2 text-right font-mono">{m.supporting_pairs}</td>
                    <td className="py-1.5 pr-2 text-right font-mono">{m.median_bits != null ? formatBits(m.median_bits) : "—"}</td>
                    <td className="py-1.5 text-muted">{m.best_pair ? <>z <IncidentLink id={m.best_pair.with} /> {formatBits(m.best_pair.bits)}{m.best_pair.reasons_pl.length ? `: ${m.best_pair.reasons_pl.join("; ")}` : ""}</> : "—"}{m.reason_pl ? <div className="text-for">{m.reason_pl}</div> : null}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <SectionNote>Warunki bramki (spec M6): co najmniej 2 pary wspierające albo jedna para ≥ {formatBits(r.strong_bits)}; mediana S ≥ {formatBits(r.low_threshold_bits)}; brak twardej sprzeczności. Bity to wagi dowodu, nie prawdopodobieństwa; pary nieocenione są nieznane.</SectionNote>
      </div>
    </Panel>
  );
}
