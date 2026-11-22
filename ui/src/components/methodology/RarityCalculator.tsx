/**
 * "Kalkulator ilustracyjny": the HYDRA-RGE discrete formula computed client-side (./rarity.ts mirrors
 * hydra/entropy.py). Inputs are user-chosen; nothing here is a measurement on any corpus.
 */
import { useId, useMemo, useState } from "react";
import { plNumber } from "../../lib/evidence";
import { DISCRETE_CAP, KIND_U0_EXAMPLES, discreteBits, hubLimit } from "./rarity";

function Slider({ label, value, min, max, step = 1, onChange, fmt }: { label: string; value: number; min: number; max: number; step?: number; onChange: (v: number) => void; fmt?: (v: number) => string }) {
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="flex justify-between text-xs text-muted"><span>{label}</span><span className="font-mono text-fg">{fmt ? fmt(value) : value}</span></label>
      <input id={id} type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} className="w-full accent-[var(--color-for)]" />
    </div>
  );
}

/** Bits vs holders for the current corpus size and kind (single series; current point marked). */
function Curve({ n, u0, q, holders }: { n: number; u0: number; q: number; holders: number }) {
  const maxH = Math.min(n, Math.max(10, hubLimit(n) + 2));
  const pts = Array.from({ length: maxH - 1 }, (_, i) => i + 2).map((h) => ({ h, b: discreteBits({ n, holders: h, u0, quality: q }).bits }));
  const W = 320, H = 120, px = 28, py = 10;
  const x = (h: number) => px + ((h - 2) / Math.max(1, maxH - 2)) * (W - px - 6);
  const y = (b: number) => H - py - (b / DISCRETE_CAP) * (H - 2 * py);
  const cur = pts.find((p) => p.h === holders);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full" role="img" aria-label={`Bity w zależności od liczby incydentów z wartością, korpus ${n} incydentów`}>
      {[0, 7, 14].map((b) => <g key={b}><line x1={px} x2={W - 6} y1={y(b)} y2={y(b)} stroke="var(--color-line)" strokeWidth={1} /><text x={px - 4} y={y(b) + 3} textAnchor="end" fontSize={9} fill="var(--color-subtle)">{b}</text></g>)}
      <polyline fill="none" stroke="var(--color-for)" strokeWidth={2} strokeLinejoin="round" points={pts.map((p) => `${x(p.h)},${y(p.b)}`).join(" ")} />
      {pts.map((p) => <circle key={p.h} cx={x(p.h)} cy={y(p.b)} r={2.5} fill="var(--color-for)"><title>{`${p.h} incydentów: ${plNumber(p.b, 1)} b`}</title></circle>)}
      {cur && <circle cx={x(cur.h)} cy={y(cur.b)} r={5} fill="var(--color-panel)" stroke="var(--color-for)" strokeWidth={2} />}
      <text x={W - 6} y={8} textAnchor="end" fontSize={9} fill="var(--color-subtle)">oś X: incydenty z wartością (para wliczona) →</text>
    </svg>
  );
}

export function RarityCalculator() {
  const [n, setN] = useState(10);
  const [holders, setHolders] = useState(2);
  const [kind, setKind] = useState("domain");
  const [q, setQ] = useState(1);
  const [bgOn, setBgOn] = useState(false);
  const [bgDf, setBgDf] = useState(20);
  const kindId = useId();
  const k = KIND_U0_EXAMPLES.find((e) => e.kind === kind) ?? KIND_U0_EXAMPLES[0]!;
  const u0 = 2 ** k.log2u0;
  const h = Math.min(holders, n);
  const r = useMemo(() => discreteBits({ n, holders: h, u0, quality: q, background: bgOn ? { df: bgDf, n: 3000 } : null }), [n, h, u0, q, bgOn, bgDf]);
  return (
    <div className="rounded-2xl border border-line bg-panel p-4" data-testid="rarity-calculator">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-fg">Kalkulator ilustracyjny</h3>
        <span className="rounded-full border border-abstain bg-abstain-soft px-2 text-2xs text-abstain-ink">ilustracja wzoru, nie wynik</span>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-3">
          <Slider label="Rozmiar korpusu N (incydenty)" value={n} min={3} max={2000} onChange={(v) => { setN(v); if (holders > v) setHolders(v); }} />
          <Slider label="Incydenty z tą wartością (para wliczona)" value={h} min={2} max={Math.min(n, 200)} onChange={setHolders} />
          <div className="flex flex-col gap-1">
            <label htmlFor={kindId} className="text-xs text-muted">Rodzaj genu (prior u₀ z <span className="font-mono">KIND_U0</span>)</label>
            <select id={kindId} value={kind} onChange={(e) => setKind(e.target.value)} className="h-8 rounded-lg border border-line bg-panel px-2 text-sm text-fg">
              {KIND_U0_EXAMPLES.map((e) => <option key={e.kind} value={e.kind}>{e.label} · u₀ = 2^{e.log2u0}</option>)}
            </select>
          </div>
          <Slider label="Jakość słabszej obserwacji q" value={q} min={0} max={1} step={0.05} onChange={setQ} fmt={(v) => plNumber(v, 2)} />
          <label className="flex items-center gap-2 text-xs text-muted"><input type="checkbox" checked={bgOn} onChange={(e) => setBgOn(e.target.checked)} /> wartość widziana w tle (prior zewnętrzny, 3000 jednostek)</label>
          {bgOn && <Slider label="Jednostki tła z tą wartością" value={bgDf} min={1} max={600} onChange={setBgDf} />}
        </div>
        <div className="flex flex-col gap-3">
          <div className="grid grid-cols-3 gap-2 text-center">
            <div className="rounded-xl bg-raised p-2"><div className="text-2xs text-subtle">û</div><div className="font-mono text-sm text-fg">{r.u < 0.001 ? r.u.toExponential(2).replace(".", ",") : plNumber(r.u, 4)}</div></div>
            <div className="rounded-xl bg-raised p-2"><div className="text-2xs text-subtle">log₂(m/û)</div><div className="font-mono text-sm text-fg">{plNumber(r.raw, 2)}</div></div>
            <div className="rounded-xl bg-for-soft p-2"><div className="text-2xs text-subtle">bity b(v)</div><div className="font-mono text-xl font-semibold text-fg" data-testid="calc-bits">{plNumber(r.bits, 1)}</div></div>
          </div>
          <p className="text-xs text-muted" aria-live="polite">
            df<sub>poz</sub> = {r.dfOthers}, N<sub>poz</sub> = {r.nOthers} (para wyłączona).
            {r.hub && <> Wartość ma więcej posiadaczy niż limit huba max(5, ⌈0,5·N⌉) = {hubLimit(n)}: <b>0 bitów</b> (formułka).</>}
            {r.capped && <> Ucięte do 14 bitów.</>}
            {r.weight < 1 && <> Waga jakości {plNumber(r.weight, 2)}{r.weight === 0 && " (q < 0,25: nie jest dowodem)"}.</>}
          </p>
          <Curve n={n} u0={u0} q={q} holders={h} />
        </div>
      </div>
      <p className="mt-3 text-2xs text-subtle">Liczone w przeglądarce tym samym wzorem co <span className="font-mono">Rarity.discrete_bits</span> (jednostki = incydenty, bez jednostek operatora, stop-genów i reguł rodzin). Przykład z METHODOLOGY §5: N = 10, domena tylko w parze → 10,5 b; jeden posiadacz więcej → 3,8 b.</p>
    </div>
  );
}
