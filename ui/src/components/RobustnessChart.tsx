/** Leave-one-family-out robustness (pairs.detail.robustness): support left after removing each family. */
import type { RobustnessDetail } from "../api/visual";
import { familyLabel, plNumber } from "../lib/evidence";
import { RobustnessBadge } from "./RobustnessBadge";

export function RobustnessChart({ r, support, threshold }: { r: RobustnessDetail | null | undefined; support?: number | null; threshold?: number | null }) {
  const fams = Object.entries(r?.families ?? {});
  if (!r || !fams.length) return <div className="text-xs text-subtle">Brak testu odporności dla tej pary (liczony dla powiązanych par).</div>;
  const max = Math.max(1, support ?? 0, threshold ?? 0, ...fams.map(([, v]) => v.support_without ?? 0));
  const x = (v: number) => `${(100 * v) / max}%`;
  return (
    <div className="space-y-2">
      <RobustnessBadge r={r} />
      <div className="space-y-1.5">
        {support !== null && support !== undefined && (
          <div className="grid grid-cols-[110px_1fr_64px] items-center gap-2 text-xs">
            <span className="font-medium">wszystkie rodziny</span>
            <span className="relative h-3.5"><span className="absolute inset-y-0 left-0 rounded-r-[3px] bg-fg" style={{ width: x(support) }} />{threshold !== null && threshold !== undefined && <span className="absolute -top-1 -bottom-1 w-[2px] bg-err" style={{ left: x(threshold) }} />}</span>
            <span className="text-right font-mono">{plNumber(support, 1)} b</span>
          </div>
        )}
        {fams.map(([f, v]) => (
          <div key={f} className="grid grid-cols-[110px_1fr_64px] items-center gap-2 text-xs" title={`bez ${f}: wsparcie ${plNumber(v.support_without ?? 0, 1)} b, ${v.still_linked ? "powiązanie zostaje" : "powiązanie znika"}`}>
            <span className="text-muted">bez {f} <span className="text-subtle">{familyLabel(f)}</span></span>
            <span className="relative h-3.5">
              <span className="absolute inset-y-0 left-0 rounded-r-[3px]" style={{ width: x(v.support_without ?? 0), backgroundColor: v.still_linked ? "var(--color-for)" : "var(--color-against)" }} />
              {threshold !== null && threshold !== undefined && <span className="absolute -top-1 -bottom-1 w-[2px] bg-err" style={{ left: x(threshold) }} />}
            </span>
            <span className="text-right font-mono">{plNumber(v.support_without ?? 0, 1)} b</span>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap gap-3 text-xs text-muted">
        <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-for" />powiązanie zostaje</span>
        <span className="inline-flex items-center gap-1"><span className="inline-block size-2.5 rounded-sm bg-against" />powiązanie znika</span>
        {threshold !== null && threshold !== undefined && <span className="inline-flex items-center gap-1"><span className="inline-block h-3 w-[2px] bg-err" />próg powiązania {plNumber(threshold, 0)} b</span>}
      </div>
    </div>
  );
}
