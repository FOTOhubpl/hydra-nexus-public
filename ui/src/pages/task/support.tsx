/**
 * CR-ASTRA-9 campaign support: shared labels and blocks for the campaign list, inspector and card.
 * Values are evidence bits and heuristic indices from meta.support; never shown as a probability.
 */
import { Link } from "react-router";
import type { CampaignGroup, CampaignSupport } from "../../api/task";
import { formatBits, plNumber } from "../../lib/evidence";
import { IncidentLink, pairPath } from "./common";

export const BITS_TITLE = "mediana bitów dowodów w powiązaniach kampanii; heurystyka, nie prawdopodobieństwo";
export const WEAKEST_TITLE = "najsłabsze ogniwo: najsłabsza krawędź maksymalnego drzewa rozpinającego (bity); heurystyka, nie prawdopodobieństwo";
export const SUPPORT_DISCLAIMER = "Wartości to bity dowodów i heurystyczny ranking; nie są skalibrowanym prawdopodobieństwem.";

/** Group strength in bits: meta.evidence_bits when the run has it, else the sum of the top-3 shared genes. */
export function groupEvidenceBits(g: CampaignGroup): number | null {
  const v = g.meta?.evidence_bits;
  if (typeof v === "number" && Number.isFinite(v)) return v;
  const genes = g.shared_genes ?? [];
  if (genes.length === 0) return null;
  return [...genes].sort((a, b) => b.bits - a.bits).slice(0, 3).reduce((s, x) => s + x.bits, 0);
}

/** "gęstość 0,67 (4/6 powiązań)". */
export function formatDensity(s: CampaignSupport): string {
  return `gęstość ${plNumber(s.density, 2)} (${s.links}/${s.possible_links} powiązań)`;
}

export function BridgeMarker({ count }: { count: number }) {
  if (count <= 0) return null;
  return (
    <span
      className="inline-flex h-[18px] items-center rounded-[3px] border border-abstain bg-abstain-soft px-1 text-2xs font-medium text-abstain-ink"
      title="mosty: powiązania, których usunięcie dzieli kampanię"
    >
      mosty: {count}
    </span>
  );
}

/** ADOPT #7: the weakest member and the bridge links come first on the campaign summary. */
export function WeakestFirst({ s }: { s: CampaignSupport }) {
  if (s.singleton) return <div className="text-xs text-subtle">Kampania jednoelementowa: brak powiązań wewnętrznych do oceny.</div>;
  const weakest = s.members.find((m) => m.id === s.weakest_member);
  return (
    <div className="space-y-1 rounded-md border border-line p-2 text-xs" data-testid="weakest-first">
      <div>
        <span className="text-subtle">Najsłabszy członek: </span>
        {s.weakest_member ? <IncidentLink id={s.weakest_member} /> : <span className="text-subtle">—</span>}
        {weakest && (
          <span className="text-muted">
            {" "}· najsilniejsze ogniwo {formatBits(weakest.anchor_bits)} · indeks {plNumber(weakest.index, 2)} · rodzin {weakest.n_families}
          </span>
        )}
      </div>
      <div>
        <span className="text-subtle">Mosty: </span>
        {s.bridges.length === 0 ? (
          <span className="text-muted">brak (usunięcie jednego powiązania nie dzieli kampanii)</span>
        ) : (
          <ul className="mt-0.5 space-y-0.5">
            {s.bridges.map((b) => (
              <li key={`${b.a}|${b.b}`}>
                <Link className="font-mono text-accent hover:underline" to={pairPath(b.a, b.b)}>{b.a} ↔ {b.b}</Link>
                <span className="text-muted"> · {formatBits(b.bits)} · podział {b.split_sizes[0]} + {b.split_sizes[1]}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <span className="text-subtle">Punkty artykulacji: </span>
        {s.articulation_points.length === 0 ? <span className="text-muted">brak</span> : s.articulation_points.map((id, i) => <span key={id}>{i > 0 && ", "}<IncidentLink id={id} /></span>)}
      </div>
    </div>
  );
}
