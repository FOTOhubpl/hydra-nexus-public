/** "Dlaczego to się liczy": document frequency of one gene value vs its kind, level / tier, caps, real bits. */
import { useEffect } from "react";
import { Link } from "react-router";
import { useGeneExplain } from "../api/visual";
import { closeGene, useOpenGene } from "../lib/geneExplain";
import { formatBits, plNumber } from "../lib/evidence";
import { FamChip, GeneValue, Prov } from "./evidence";
import { Icon } from "./Icon";

const LEVEL: Record<string, string> = {
  campaign: "poziom kampanii / operatora: rzadki wspólny ślad może połączyć incydenty",
  kit: "szablon / kit: wspiera grupę kampanii, do kampanii liczy się z limitem",
  asset: "wspólny zasób: obraz, plik lub osoba, które mogą być skopiowane",
  none: "szum (boilerplate, listy MISP): 0 bitów",
  unknown: "poziom nieznany w tej wersji rdzenia",
};

export function GeneExplainer() {
  const sel = useOpenGene();
  const q = useGeneExplain(sel?.kind, sel?.value);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeGene(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  if (!sel) return null;
  const g = q.data;
  const max = Math.max(1, ...(g?.df_histogram ?? []).map((h) => h.values));
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-fg/30 p-6" role="dialog" aria-label="Dlaczego ten gen się liczy" onClick={closeGene}>
      <div className="max-h-[90vh] w-[720px] max-w-full overflow-y-auto rounded-lg border border-line bg-panel p-6" onClick={(e) => e.stopPropagation()}>
        <div className="mb-3 flex items-start gap-3">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold">Dlaczego ten gen się liczy</h2>
            <div className="mt-1 flex flex-wrap items-center gap-2 text-sm">{g && <><Prov value={g.provenance as never} /><FamChip family={g.family} withName /></>}<span className="text-muted">{sel.kind}</span><GeneValue value={sel.value} max={80} /></div>
          </div>
          <button type="button" onClick={closeGene} aria-label="Zamknij" className="ml-auto flex size-8 items-center justify-center rounded-md hover:bg-hover"><Icon name="close" /></button>
        </div>
        {!g ? <div className="text-sm text-subtle">{q.isError ? "Tej wartości nie ma w genach bieżącego przebiegu." : "Ładowanie…"}</div> : (
          <div className="space-y-4 text-sm">
            <div className="grid grid-cols-3 gap-3">
              <div><div className="text-xs text-subtle">występuje w</div><div className="font-mono text-2xl">{g.df} / {g.incidents_in_run}</div><div className="text-xs text-muted">incydentach przebiegu</div></div>
              <div><div className="text-xs text-subtle">poziom</div><div className="font-mono text-lg">{g.level}</div><div className="text-xs text-muted">{g.tier ? `tier: ${g.tier}` : "bez tieru"}</div></div>
              <div><div className="text-xs text-subtle">limity scorera</div><div className="font-mono text-xs">rodzina ≤ {plNumber(g.caps.family_cap ?? NaN, 0)} b · grupa ≤ {plNumber(g.caps.group_cap ?? NaN, 0)} b · kit→kampania {plNumber(g.caps.platform_kit_cap ?? NaN, 0)} b</div></div>
            </div>
            <p className="rounded-md bg-raised px-3 py-2">{LEVEL[g.level] ?? LEVEL.unknown}. Im mniej incydentów ma tę wartość, tym więcej bitów daje wspólne jej wystąpienie.</p>
            <div>
              <div className="mb-1 text-xs text-muted">rozkład częstości dokumentowej wartości rodzaju „{g.kind}” (słupek tej wartości wyróżniony)</div>
              <div className="flex h-32 items-end gap-1 border-b border-line">
                {g.df_histogram.map((h) => (
                  <div key={h.df} className="flex h-full max-w-16 flex-1 flex-col items-center justify-end gap-1" title={`df=${h.df}: ${h.values} wartości`}>
                    <span className="font-mono text-2xs text-muted">{h.values}</span>
                    <div className={h.df === g.df ? "w-full rounded-t-[3px] bg-accent" : "w-full rounded-t-[3px] bg-unknown"} style={{ height: `${Math.max(4, (100 * h.values) / max)}%` }} />
                  </div>
                ))}
              </div>
              <div className="flex gap-1">{g.df_histogram.map((h) => <span key={h.df} className="max-w-16 flex-1 text-center font-mono text-2xs text-subtle">df {h.df}</span>)}</div>
            </div>
            <div>
              <div className="mb-1 text-xs text-muted">bity, które ta wartość dała w ocenionych parach</div>
              {g.used_in_pairs.length ? (
                <ul className="space-y-1 text-xs">{g.used_in_pairs.map((u, i) => <li key={i} className="flex items-center gap-2"><Link onClick={closeGene} className="font-mono text-accent hover:underline" to={`/pair/${encodeURIComponent(u.a)}/${encodeURIComponent(u.b)}`}>{u.a} ↔ {u.b}</Link><span className="min-w-0 flex-1 truncate text-muted">{u.explanation_pl}</span><span className="font-mono">{formatBits(u.bits)}</span></li>)}</ul>
              ) : <div className="text-xs text-subtle">Nie użyta w żadnej ocenie pary (nikt jej nie dzieli albo to szum).</div>}
            </div>
            <div className="text-xs text-subtle">incydenty: {g.holders.join(", ")} · ekstraktor <span className="font-mono">{g.extractor}</span>{g.dependency_group ? <> · grupa zależności <span className="font-mono">{g.dependency_group}</span></> : null}</div>
          </div>
        )}
      </div>
    </div>
  );
}
