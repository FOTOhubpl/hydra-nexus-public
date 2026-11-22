import { familyLabel } from "../lib/evidence";

export interface Robustness {
  robust_link?: boolean;
  critical_families?: string[];
  link?: string;
}

/** Leave-one-family-out result from CORE (pairs.detail.robustness): does the link survive without any one family? */
export function RobustnessBadge({ r }: { r: Robustness | null | undefined }) {
  if (!r || r.link === undefined) return null;
  const crit = r.critical_families ?? [];
  if (r.robust_link) {
    return <span className="inline-flex h-6 items-center rounded-md border border-ok/50 bg-panel px-2 text-xs text-fg" title="Powiązanie zostaje po usunięciu dowolnej jednej rodziny genów">✓ odporne</span>;
  }
  return (
    <span className="inline-flex h-6 items-center rounded-md border border-abstain bg-abstain-soft px-2 text-xs text-abstain-ink" title="Bez tej rodziny para traci powiązanie (test pomijania rodzin, CORE)">
      zależy od jednej rodziny: {crit.map((f) => `${f} (${familyLabel(f)})`).join(", ") || "—"}
    </span>
  );
}
