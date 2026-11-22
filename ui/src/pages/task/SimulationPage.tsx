/**
 * Symulacja potoku: the 8 pipeline stages replayed on the REAL latest run (GET /v1/simulation).
 * Every stage renders deterministically from u ∈ [0, 1] (progress inside the stage), so play, step and scrub
 * all show the same frames. All numbers come from the API; captions only describe them.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { artifactContentUrl } from "../../api/client";
import { FAMILIES, usePair, useSimulation, type SimPoint, type SimulationData } from "../../api/task";
import { FamChip, GeneValue, HypothesisBadge, ProbBar, Thumb, Waterfall } from "../../components/evidence";
import { cx } from "../../lib/cx";
import { FAMILY_COLOR, formatBits, formatProbability, HYPOTHESIS_LABEL, plNumber, ROLE_LABEL } from "../../lib/evidence";
import { formatCount } from "../../lib/format";
import { campaignColors } from "./DashboardPage";
import { NoRun, pairPath, QueryState } from "./common";
import { STAGES, StageRail } from "./StageRail";

type Stages = SimulationData["stages"];

const STAGE_SECONDS = 8;
const SPEEDS = [0.5, 1, 2, 4] as const;

const clamp01 = (x: number) => Math.max(0, Math.min(1, x));
/** Ease so counters settle before the stage ends. */
const ease = (u: number) => 1 - (1 - clamp01(u / 0.8)) ** 3;
const take = <T,>(xs: T[], u: number) => xs.slice(0, Math.ceil(ease(u) * xs.length));

function Big({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="rounded-md border border-line bg-panel px-3 py-2">
      <div className="text-xs text-subtle">{label}</div>
      <div className="font-mono text-2xl font-semibold text-fg">{value}</div>
      {hint && <div className="text-xs text-muted">{hint}</div>}
    </div>
  );
}

function Bar({ label, value, max, color, right }: { label: ReactNode; value: number; max: number; color: string; right?: ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(0,180px)_1fr_64px] items-center gap-2 text-sm">
      <span className="truncate text-muted">{label}</span>
      <span className="h-3 rounded-full bg-hover">
        <span className="block h-3 rounded-full" style={{ width: `${max ? (100 * value) / max : 0}%`, backgroundColor: color }} />
      </span>
      <span className="text-right font-mono text-xs">{right ?? formatCount(Math.round(value))}</span>
    </div>
  );
}

// ---------------------------------------------------------------- 1 Import
function StageImport({ s, u }: { s: Stages["import"]; u: number }) {
  const e = ease(u);
  const roles = Object.entries(s.artifacts_by_role).sort((a, b) => b[1] - a[1]);
  const maxRole = Math.max(1, ...roles.map(([, n]) => n));
  const items = s.job_items_by_status;
  return (
    <div className="grid gap-4 xl:grid-cols-[1.3fr_1fr]">
      <div className="grid grid-cols-5 gap-2">
        {s.incidents_list.map((inc, i) => (
          <div key={inc.id} className="transition-opacity duration-300" style={{ opacity: i < Math.ceil(e * s.incidents_list.length) ? 1 : 0.08 }}>
            <Thumb artifactId={inc.thumbnail} alt={inc.id} size={120} className="!h-24 !w-full" />
            <div className="mt-0.5 flex justify-between text-xs"><span className="font-mono">{inc.id}</span><span className="text-subtle">{inc.files} pl.</span></div>
          </div>
        ))}
      </div>
      <div className="space-y-3">
        <div className="grid grid-cols-3 gap-2">
          <Big label="Incydenty" value={Math.round(e * s.incidents)} />
          <Big label="Zapisane (unikalne)" value={formatCount(Math.round(e * (items.stored ?? 0)))} hint="SHA-256, oryginał tylko do odczytu" />
          <Big label="Duplikaty" value={formatCount(Math.round(e * (items.duplicate ?? 0)))} hint={`kwarantanna: ${items.quarantined ?? 0} · błędy: ${items.failed ?? 0}`} />
        </div>
        <div className="space-y-1.5">
          {roles.map(([r, n]) => <Bar key={r} label={ROLE_LABEL[r] ?? r} value={e * n} max={maxRole} color="var(--color-for)" />)}
        </div>
        <div className="text-xs text-muted">Ukryte sekrety (tokeny, klucze, nazwy użytkowników): <span className="font-mono text-fg">{s.redacted_genes}</span> wartości w {s.redacted_incidents} incydentach. Czas importu w przebiegu: <span className="font-mono">{s.timings_s?.import ?? "—"} s</span>.</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 2 Genes
function StageGenes({ s, u }: { s: Stages["genes"]; u: number }) {
  const e = ease(u);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const boxes = take(s.boxes, u);
  const ex = Object.entries(s.by_extractor);
  const maxEx = Math.max(1, ...ex.map(([, n]) => n));
  const famTotal = (f: string) => Object.values(s.by_family[f] ?? {}).reduce((a, b) => a + b, 0);
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_1.1fr]">
      <div>
        <div className="mb-1 text-sm"><span className="font-mono">{s.incident_id}</span> <span className="text-muted">{s.title}</span></div>
        <div className="relative overflow-hidden rounded-md border border-line bg-raised">
          {s.creative ? (
            <>
              <img src={artifactContentUrl(s.creative)} alt="kreacja incydentu" className="block max-h-[52vh] w-full object-contain" onLoad={(ev) => setSize({ w: ev.currentTarget.naturalWidth, h: ev.currentTarget.naturalHeight })} />
              {size && (
                <svg viewBox={`0 0 ${size.w} ${size.h}`} className="pointer-events-none absolute inset-0 h-full w-full" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
                  {boxes.map((b, i) => (
                    <rect key={i} x={b.bbox[0]} y={b.bbox[1]} width={b.bbox[2]} height={b.bbox[3]} fill="none" strokeWidth={Math.max(2, size.w / 300)}
                      stroke={b.kind === "face" ? FAMILY_COLOR.ID : b.family === "VIS" ? FAMILY_COLOR.VIS : FAMILY_COLOR.TXT} />
                  ))}
                </svg>
              )}
            </>
          ) : <div className="p-6 text-sm text-subtle">brak obrazu</div>}
        </div>
        <div className="mt-1 flex flex-wrap gap-3 text-xs text-muted">
          <span>ramki z genów (wskaźniki dowodu): {boxes.length}/{s.boxes.length}</span>
          <span><span className="inline-block size-2 rounded-sm" style={{ backgroundColor: FAMILY_COLOR.TXT }} /> tekst OCR</span>
          <span><span className="inline-block size-2 rounded-sm" style={{ backgroundColor: FAMILY_COLOR.ID }} /> twarz</span>
        </div>
      </div>
      <div className="space-y-3">
        <div className="grid grid-cols-3 gap-2">
          {FAMILIES.map((f) => (
            <div key={f} className="rounded-md border border-line bg-panel px-3 py-2">
              <FamChip family={f} withName />
              <div className="font-mono text-xl">{famTotal(f) ? formatCount(Math.round(e * famTotal(f))) : <span className="text-base text-subtle">nieznane</span>}</div>
            </div>
          ))}
        </div>
        <div className="space-y-1">
          <div className="text-xs font-semibold text-muted">geny wg ekstraktora</div>
          {ex.slice(0, 12).map(([name, n]) => <Bar key={name} label={<span className="font-mono text-xs">{name}</span>} value={e * n} max={maxEx} color="var(--color-accent)" />)}
        </div>
        <div className="text-xs text-muted">
          {s.extract ? (
            <>Ekstrakcja tego incydentu: <span className="font-mono text-fg">{s.extract.measured_seconds ?? "—"} s</span>
              {s.extract.measured_run_id && <> (zmierzone w przebiegu <span className="font-mono">{s.extract.measured_run_id}</span>{s.extract.cached ? "; bieżący przebieg użył cache" : ""})</>}
              · {formatCount(s.extract.genes ?? 0)} genów{s.extract.vectors ? ` · ${s.extract.vectors} wektorów` : ""}</>
          ) : "Brak statystyk ekstrakcji w metrykach przebiegu."}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 3 Rarity
const LEVEL_COLOR: Record<string, string> = { campaign: "var(--color-for)", kit: "var(--color-unknown)", asset: "var(--color-fam-vis)", none: "var(--color-line-strong)", unknown: "var(--color-line)" };
const LEVEL_LABEL: Record<string, string> = { campaign: "kampania / operator", kit: "szablon / kit", asset: "zasób", none: "szum (boilerplate)", unknown: "nieznany poziom" };

function StageRarity({ s, u }: { s: Stages["rarity"]; u: number }) {
  const coloured = u > 0.35;
  const levels = ["campaign", "asset", "kit", "none", "unknown"];
  const rows = s.histogram.filter((h) => h.df >= 1);
  const max = Math.max(1, ...rows.map((h) => levels.reduce((a, l) => a + (h[l] ?? 0), 0)));
  return (
    <div className="grid gap-4 xl:grid-cols-[1.2fr_1fr]">
      <div>
        <div className="mb-1 text-xs text-muted">liczba różnych wartości genów wg liczby incydentów, które je mają (df) · {formatCount(s.distinct_values)} wartości</div>
        <div className="space-y-1">
          {rows.map((h) => {
            const total = levels.reduce((a, l) => a + (h[l] ?? 0), 0);
            return (
              <div key={h.df} className="grid grid-cols-[60px_1fr_70px] items-center gap-2 text-xs">
                <span className="font-mono">df = {h.df}</span>
                <span className="flex h-4 overflow-hidden rounded-[3px] bg-hover" style={{ width: `${Math.max(2, (100 * Math.sqrt(total)) / Math.sqrt(max))}%` }}>
                  {levels.map((l) => (h[l] ? <span key={l} title={`${LEVEL_LABEL[l]}: ${h[l]}`} style={{ width: `${(100 * h[l]) / total}%`, backgroundColor: coloured ? LEVEL_COLOR[l] : "var(--color-unknown)", opacity: coloured && l === "none" ? 0.35 : 1 }} /> : null))}
                </span>
                <span className="text-right font-mono">{formatCount(total)}</span>
              </div>
            );
          })}
        </div>
        <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted">
          {levels.slice(0, 4).map((l) => <span key={l} className="inline-flex items-center gap-1"><span className="inline-block h-2 w-3 rounded-sm" style={{ backgroundColor: LEVEL_COLOR[l] }} />{LEVEL_LABEL[l]}</span>)}
          <span>szerokość ∝ √liczby (df = 1 dominuje)</span>
        </div>
      </div>
      <div className="grid gap-3">
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">rzadkie wspólne geny (poziom kampanii) zapalają się</div>
          <ul className="space-y-0.5 text-xs">
            {s.rare_shared.map((g, i) => (
              <li key={i} className="flex items-center gap-1.5 rounded-[3px] px-1 transition-colors" style={{ backgroundColor: u > 0.55 + i * 0.03 ? "var(--color-for-soft)" : "transparent" }}>
                <FamChip family={g.family} /><span className="text-muted">{g.kind}</span><span className="min-w-0 flex-1 truncate"><GeneValue value={g.value} max={40} /></span><span className="font-mono">df {g.df}</span>
              </li>
            ))}
          </ul>
        </div>
        <div>
          <div className="mb-1 text-xs font-semibold text-muted">szum: wspólne prawie wszystkim lub poziom „none” gasną (0 bitów)</div>
          <ul className="space-y-0.5 text-xs">
            {s.boilerplate.map((g, i) => (
              <li key={i} className={cx("flex items-center gap-1.5 px-1 transition-opacity", u > 0.5 && "line-through")} style={{ opacity: u > 0.5 ? 0.45 : 1 }}>
                <FamChip family={g.family} /><span className="text-muted">{g.kind}</span><span className="min-w-0 flex-1 truncate"><GeneValue value={g.value} max={40} /></span><span className="font-mono">df {g.df}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 4 Candidates
function StageCandidates({ s, u, thumbs }: { s: Stages["candidates"]; u: number; thumbs: Record<string, string | null> }) {
  const others = s.pairs;
  const R = 40;
  const shown = take(others, u);
  const idx = Object.entries(s.by_index).sort((a, b) => b[1] - a[1]);
  const maxIdx = Math.max(1, ...idx.map(([, n]) => n));
  return (
    <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
      <div className="relative mx-auto aspect-square w-full max-w-[520px]">
        <svg viewBox="-50 -50 100 100" className="absolute inset-0 h-full w-full" aria-hidden="true">
          {shown.map((p, i) => {
            const a = (2 * Math.PI * i) / Math.max(1, others.length) - Math.PI / 2;
            return <line key={p.other} x1={0} y1={0} x2={R * Math.cos(a)} y2={R * Math.sin(a)} stroke={p.hypothesis === "same_campaign" ? "var(--color-for)" : p.hypothesis === "abstain" ? "var(--color-abstain)" : "var(--color-line-strong)"} strokeWidth={p.hypothesis === "same_campaign" ? 1.2 : 0.5} />;
          })}
        </svg>
        {others.map((p, i) => {
          const a = (2 * Math.PI * i) / Math.max(1, others.length) - Math.PI / 2;
          return (
            <div key={p.other} className="absolute -translate-x-1/2 -translate-y-1/2 text-center transition-opacity" style={{ left: `${50 + R * Math.cos(a)}%`, top: `${50 + R * Math.sin(a)}%`, opacity: i < shown.length ? 1 : 0.15 }}>
              <Thumb artifactId={thumbs[p.other]} alt={p.other} size={48} />
              <div className="font-mono text-2xs">{p.other}</div>
              <div className="text-2xs text-subtle">{p.indexes.join(" · ")}</div>
            </div>
          );
        })}
        <div className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 text-center">
          <Thumb artifactId={thumbs[s.incident_id ?? ""]} alt={s.incident_id ?? ""} size={80} className="ring-2 ring-fg" />
          <div className="font-mono text-xs font-semibold">{s.incident_id}</div>
        </div>
      </div>
      <div className="space-y-3">
        <div className="grid grid-cols-2 gap-2">
          <Big label="Pary kandydatów w przebiegu" value={formatCount(s.candidate_pairs_total ?? 0)} hint={`z ${formatCount(s.all_pairs)} możliwych par`} />
          <Big label={`Kandydaci dla ${s.incident_id}`} value={shown.length} hint={`z ${others.length}`} />
        </div>
        <div className="space-y-1">
          <div className="text-xs font-semibold text-muted">który indeks zaproponował parę (dla tego incydentu)</div>
          {idx.map(([name, n]) => <Bar key={name} label={<span className="font-mono">{name}</span>} value={ease(u) * n} max={maxIdx} color="var(--color-accent)" />)}
        </div>
        <ul className="max-h-48 space-y-0.5 overflow-auto text-xs">
          {shown.map((p) => (
            <li key={p.other} className="flex items-center gap-2"><span className="w-16 font-mono">{p.other}</span><HypothesisBadge value={p.hypothesis} /><span className="ml-auto font-mono">{p.n_sources} źródeł</span></li>
          ))}
        </ul>
        <div className="text-xs text-subtle">Na małej próbce indeks „all_pairs” obejmuje wszystkie pary; przy dużym korpusie decydują indeksy dokładnych genów, kNN osadzeń i bliskich hashy.</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 5 Evidence
function StageEvidence({ s, u, preset, setPreset, thumbs }: { s: Stages["evidence"]; u: number; preset: number; setPreset: (i: number) => void; thumbs: Record<string, string | null> }) {
  const cur = s.presets[preset];
  const pair = usePair(cur?.a, cur?.b);
  const sc = pair.data?.score;
  if (!cur) return <div className="text-sm text-subtle">Brak par w przebiegu.</div>;
  const steps = 6 + 2;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1">
        {s.presets.map((p, i) => (
          <button key={`${p.a}|${p.b}`} type="button" onClick={() => setPreset(i)} className={cx("h-7 rounded-[4px] border px-2.5 text-sm", i === preset ? "border-fg bg-fg text-canvas" : "border-line-strong bg-panel hover:bg-hover")}>
            <span className="font-mono">{String.fromCharCode(65 + i)}</span> · {p.a} ↔ {p.b} · {p.why}
          </button>
        ))}
      </div>
      <div className="grid gap-4 xl:grid-cols-[0.9fr_1.1fr]">
        <div className="grid grid-cols-2 gap-2">
          {[cur.a, cur.b].map((id, k) => {
            const side = k === 0 ? pair.data?.incident_a : pair.data?.incident_b;
            const v = side?.visuals?.find((x) => x.mime?.startsWith("image/"));
            return (
              <div key={id} className="min-w-0">
                <div className="flex aspect-[4/3] items-center justify-center overflow-hidden rounded-md border border-line bg-raised">
                  {v ? <img src={artifactContentUrl(v.artifact_id)} alt={id} className="h-full w-full object-contain" /> : <Thumb artifactId={thumbs[id]} alt={id} size={160} />}
                </div>
                <div className="mt-0.5 text-xs"><span className="font-mono">{id}</span> <span className="text-muted">{side?.title?.slice(0, 60)}</span></div>
              </div>
            );
          })}
          {sc && (
            <ul className="col-span-2 space-y-0.5 text-xs">
              {sc.reasons.filter((r) => r.kind !== "cap_correction").slice(0, Math.ceil(ease(u) * 6)).map((r, i) => (
                <li key={i} className="flex items-center gap-1.5"><FamChip family={r.family} /><span className="min-w-0 flex-1 truncate text-muted" title={r.explanation_pl}>{r.explanation_pl}</span><span className="font-mono">{formatBits(r.bits)}</span></li>
              ))}
            </ul>
          )}
        </div>
        <div className="space-y-3">
          {!sc ? <QueryState query={pair} what="parę" /> : (
            <>
              <Waterfall familyBits={sc.family_bits} totalBits={sc.total_bits} missing={Array.from(new Set([...(sc.unknown_families ?? []), ...sc.missing_families]))} reasons={sc.reasons} contradictions={sc.contradictions} reveal={Math.ceil(ease(u) * steps)} />
              <div className="flex flex-wrap items-center gap-3 text-sm" style={{ opacity: u > 0.7 ? 1 : 0.15 }}>
                <span className="font-mono text-3xl font-semibold">{formatProbability(sc.probability)}</span>
                <HypothesisBadge value={sc.hypothesis} />
                {sc.campaign_bits !== undefined && <span className="text-xs text-muted">kampania {formatBits(sc.campaign_bits)} · kit {formatBits(sc.kit_bits)} · zasób {formatBits(sc.asset_bits)}</span>}
                <Link to={pairPath(cur.a, cur.b)} className="ml-auto text-xs text-accent hover:underline">pełny dowód →</Link>
              </div>
              <div style={{ opacity: u > 0.7 ? 1 : 0.15 }}><ProbBar p={sc.probability} calibrated={sc.calibrated} /></div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 6 Decision
const HYP_COLOR: Record<string, string> = { same_campaign: "var(--color-for)", abstain: "var(--color-abstain)", shared_template: "var(--color-fam-vis)", shared_asset: "var(--color-fam-id)", independent: "var(--color-unknown)" };

function StageDecision({ s, u }: { s: Stages["decision"]; u: number }) {
  const pts = useMemo(() => [...s.points].sort((a, b) => a.p - b.p || a.a.localeCompare(b.a)), [s.points]);
  const n = Math.ceil(ease(u) * pts.length);
  const stacks: Record<number, number> = {};
  const maxStack = Math.max(1, ...Object.values(pts.reduce<Record<number, number>>((acc, p) => {
    const col = Math.min(49, Math.floor(p.p * 50));
    acc[col] = (acc[col] ?? 0) + 1;
    return acc;
  }, {})));
  const step = Math.min(13, 240 / maxStack);
  return (
    <div className="grid gap-4 xl:grid-cols-[1.4fr_1fr]">
      <div>
        <div className="relative h-72 rounded-md border border-line bg-panel">
          <div className="absolute inset-y-0 bg-unknown/20" style={{ left: 0, width: `${s.band[0] * 100}%` }} />
          <div className="hatch-abstain absolute inset-y-0 opacity-30" style={{ left: `${s.band[0] * 100}%`, width: `${(s.band[1] - s.band[0]) * 100}%` }} />
          <div className="absolute inset-y-0 bg-for/15" style={{ left: `${s.band[1] * 100}%`, right: 0 }} />
          {pts.slice(0, n).map((p) => {
            const col = Math.min(49, Math.floor(p.p * 50));
            const k = (stacks[col] = (stacks[col] ?? 0) + 1);
            return <span key={`${p.a}|${p.b}`} title={`${p.a} ↔ ${p.b}: wynik nieskalibrowany ${formatProbability(p.p)} · ${HYPOTHESIS_LABEL[p.hypothesis] ?? p.hypothesis}`} className="absolute size-3 -translate-x-1/2 rounded-full border border-panel" style={{ left: `${(col + 0.5) * 2}%`, bottom: `${4 + (k - 1) * step}px`, backgroundColor: HYP_COLOR[p.hypothesis] ?? "var(--color-unknown)" }} />;
          })}
          <div className="absolute inset-x-2 top-2 flex justify-between text-xs text-muted"><span>odrzuć</span><span>kolejka analityka</span><span>połącz</span></div>
        </div>
        <div className="relative mt-1 h-4 font-mono text-2xs text-subtle">
          {[0, s.band[0], s.band[1], 1].map((v, i) => <span key={i} className="absolute -translate-x-1/2" style={{ left: `${v * 100}%` }}>{i === 0 ? "p = 0" : plNumber(v, 1)}</span>)}
        </div>
      </div>
      <div className="space-y-2">
        {Object.entries(s.hypotheses).sort((a, b) => b[1] - a[1]).map(([h, c]) => (
          <Bar key={h} label={HYPOTHESIS_LABEL[h] ?? h} value={ease(u) * c} max={Math.max(...Object.values(s.hypotheses))} color={HYP_COLOR[h] ?? "var(--color-unknown)"} />
        ))}
        <HypLegend />
        {s.thresholds && <div className="text-xs text-muted">progi scorera (bity): {Object.entries(s.thresholds).filter(([, v]) => typeof v === "number").map(([k, v]) => `${k} ${v}`).join(" · ")}</div>}
        <div className="text-xs text-subtle">wynik liczony ze wsparcia w bitach względem progu powiązania; to wynik, nie prawdopodobieństwo (ranking nieskalibrowany), dopóki EVAL nie dopasuje kalibracji.</div>
      </div>
    </div>
  );
}

function HypLegend() {
  return (
    <div className="flex flex-wrap gap-3 text-xs text-muted">
      {Object.entries(HYP_COLOR).map(([h, c]) => <span key={h} className="inline-flex items-center gap-1"><span className="inline-block h-2 w-3 rounded-sm" style={{ backgroundColor: c }} />{HYPOTHESIS_LABEL[h] ?? h}</span>)}
    </div>
  );
}

// ---------------------------------------------------------------- 7 Grouping
function StageGrouping({ s, u, thumbs, truthOn }: { s: Stages["grouping"]; u: number; thumbs: Record<string, string | null>; truthOn: boolean }) {
  const colors = useMemo(() => campaignColors(s.campaigns.map((c) => ({ id: c.id, size: c.members.length }))), [s.campaigns]);
  const pos = useMemo(() => {
    // campaigns packed on a circle, members on a small circle around each campaign centre
    // multi-member campaigns on an inner ring, singletons on an outer ring (no overlap with group hulls)
    const out: Record<string, { x: number; y: number; c: string }> = {};
    const multi = s.campaigns.filter((c) => c.members.length > 1).sort((a, b) => b.members.length - a.members.length);
    const single = s.campaigns.filter((c) => c.members.length === 1);
    multi.forEach((c, i) => {
      const a = multi.length === 1 ? 0 : (2 * Math.PI * i) / multi.length + Math.PI;
      const r0 = multi.length === 1 ? 0 : 15;
      const cx0 = 50 + r0 * Math.cos(a);
      const cy0 = 50 + r0 * Math.sin(a);
      c.members.forEach((m, j) => {
        const b = (2 * Math.PI * j) / c.members.length - Math.PI / 2;
        out[m] = { x: cx0 + 7 * Math.cos(b), y: cy0 + 7 * Math.sin(b), c: c.id };
      });
    });
    single.forEach((c, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, single.length) - Math.PI / 2;
      out[c.members[0]!] = { x: 50 + 43 * Math.cos(a), y: 50 + 43 * Math.sin(a), c: c.id };
    });
    return out;
  }, [s.campaigns]);
  const edges = s.edges.filter((e) => e.p >= 0.05 && pos[e.a] && pos[e.b]);
  const shown = edges.slice(0, Math.ceil(ease(u) * edges.length));
  const formed = u > 0.85;
  const groupOf: Record<string, string> = {};
  for (const g of s.groups) for (const cid of g.campaign_ids ?? []) groupOf[cid] = g.id;
  return (
    <div className="grid gap-4 xl:grid-cols-[1.5fr_1fr]">
      <div className="relative mx-auto aspect-square w-full max-w-[620px]">
        <svg viewBox="0 0 100 100" className="absolute inset-0 h-full w-full" aria-hidden="true">
          {formed && s.groups.map((g) => {
            const ms = (g.campaign_ids ?? []).flatMap((cid) => s.campaigns.find((c) => c.id === cid)?.members ?? []).map((m) => pos[m]).filter(Boolean);
            if (ms.length < 2) return null;
            const gx = ms.reduce((a, p) => a + p!.x, 0) / ms.length;
            const gy = ms.reduce((a, p) => a + p!.y, 0) / ms.length;
            const r = Math.max(...ms.map((p) => Math.hypot(p!.x - gx, p!.y - gy))) + 10;
            return <circle key={g.id} cx={gx} cy={gy} r={r} fill="none" stroke="var(--color-muted)" strokeDasharray="1.5 1.5" strokeWidth={0.3} />;
          })}
          {shown.map((e: SimPoint) => (
            <line key={`${e.a}|${e.b}`} x1={pos[e.a]!.x} y1={pos[e.a]!.y} x2={pos[e.b]!.x} y2={pos[e.b]!.y} stroke={HYP_COLOR[e.hypothesis] ?? "var(--color-unknown)"} strokeWidth={0.2 + 1.2 * e.p} strokeDasharray={e.hypothesis === "abstain" ? "1 1" : undefined} opacity={e.hypothesis === "independent" ? 0.35 : 0.9} />
          ))}
          {formed && s.campaigns.filter((c) => c.members.length > 1).map((c) => {
            const ms = c.members.map((m) => pos[m]!);
            const cx0 = ms.reduce((a, p) => a + p.x, 0) / ms.length;
            const cy0 = ms.reduce((a, p) => a + p.y, 0) / ms.length;
            return <circle key={c.id} cx={cx0} cy={cy0} r={12} fill={colors[c.id]} fillOpacity={0.08} stroke={colors[c.id]} strokeWidth={0.4} />;
          })}
        </svg>
        {Object.entries(pos).map(([id, p]) => {
          const truth = truthOn && s.truth ? s.truth[id] : null;
          return (
            <div key={id} className="absolute -translate-x-1/2 -translate-y-1/2 text-center" style={{ left: `${p.x}%`, top: `${p.y}%` }}>
              <span className="block rounded-[5px] p-0.5" style={{ backgroundColor: formed ? colors[p.c] : "var(--color-line-strong)" }}>
                <Thumb artifactId={thumbs[id]} alt={id} size={38} />
              </span>
              <div className="font-mono text-2xs">{id}</div>
              {truth && <div className="text-2xs text-abstain-ink">prawda: {truth}</div>}
            </div>
          );
        })}
      </div>
      <div className="space-y-2 text-sm">
        <div className="text-xs text-muted">krawędzie pojawiają się od najsilniejszej: {shown.length}/{edges.length} · grubość = p · kolor = hipoteza</div>
        <HypLegend />
        <ul className="space-y-1">
          {s.campaigns.filter((c) => c.members.length > 1).map((c) => (
            <li key={c.id} className="flex items-start gap-2" style={{ opacity: formed ? 1 : 0.2 }}>
              <span className="mt-1 size-3 shrink-0 rounded-full" style={{ backgroundColor: colors[c.id] }} />
              <span className="min-w-0"><span className="font-mono text-xs">{c.id}</span> {c.label.slice(0, 70)}<div className="font-mono text-xs text-muted">{c.members.join(", ")}{groupOf[c.id] ? ` · grupa ${groupOf[c.id]}` : ""}</div></span>
            </li>
          ))}
        </ul>
        <div className="text-xs text-subtle" style={{ opacity: formed ? 1 : 0.2 }}>pojedyncze incydenty: {s.campaigns.filter((c) => c.members.length === 1).map((c) => c.members[0]).join(", ") || "—"}</div>
        {s.groups.map((g) => <div key={g.id} className="text-xs text-muted" style={{ opacity: formed ? 1 : 0.2 }}>grupa (przerywany okrąg) <span className="font-mono">{g.id}</span>: {g.label}</div>)}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- 8 Result
function StageResult({ s, grouping, u, thumbs }: { s: Stages["result"]; grouping: Stages["grouping"]; u: number; thumbs: Record<string, string | null> }) {
  const h = s.eval_headline as Record<string, unknown> | null;
  const perCorpus = h ? Object.entries(h).filter(([, v]) => typeof v === "object" && v !== null) as [string, Record<string, unknown>][] : [];
  const m = s.run_metrics as Record<string, unknown>;
  return (
    <div className="space-y-4" style={{ opacity: 0.2 + 0.8 * ease(u) }}>
      <div className="grid grid-cols-2 gap-2 md:grid-cols-5">
        <Big label="Incydenty" value={String(m.incidents ?? "—")} />
        <Big label="Geny" value={formatCount(Number(m.genes ?? 0))} />
        <Big label="Kampanie" value={s.campaigns} hint={`${s.multi_member} z ≥ 2 incydentami`} />
        <Big label="Grupy kampanii" value={s.groups} />
        <Big label="Pary ocenione" value={String(m.pairs_scored ?? "—")} hint={`scorer ${String(m.scorer ?? "—")}`} />
      </div>
      {h ? (
        <div className="rounded-md border border-line bg-panel p-3">
          <div className="mb-2 text-sm font-semibold">Ewaluacja <span className="font-mono text-xs font-normal">{s.eval_run_id}</span>{s.eval_backend && <span className="ml-2 text-xs font-normal text-muted">backend: {s.eval_backend}</span>}</div>
          {/stub/i.test(s.eval_backend ?? "") && <div className="mb-2 text-xs text-abstain-ink">Backend ewaluacji to scorer zastępczy, nie rdzeń.</div>}
          <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
            <thead className="text-2xs text-subtle"><tr><th className="py-1 text-left">korpus</th><th className="py-1 text-right">F1 par kampanii</th><th className="py-1 text-right">B³ F1</th><th className="py-1 text-right">fałszywe scalenia</th><th className="py-1 text-right">przypisanie (LOO)</th><th className="py-1 text-right">odrzucanie spoza zbioru</th><th className="py-1 text-right">twarde negatywy</th></tr></thead>
            <tbody>
              {perCorpus.map(([c, v]) => (
                <tr key={c} className="border-t border-line font-mono">
                  <td className="py-1 font-sans">{c}</td>
                  {["campaign_pairwise_f1", "campaign_bcubed_f1", "false_merge_pairs", "loo_accuracy", "open_set_rejection", "hard_negative_violations"].map((k) => <td key={k} className="py-1 text-right">{typeof v[k] === "number" ? (k === "false_merge_pairs" || k === "hard_negative_violations" ? formatCount(v[k] as number) : plNumber(v[k] as number, 3)) : "—"}</td>)}
                </tr>
              ))}
            </tbody>
          </table></div>
          <Link to="/methodology" className="mt-2 inline-block text-xs text-accent hover:underline">pełne wyniki: Metodyka →</Link>
        </div>
      ) : (
        <div className="rounded-md border border-line bg-panel p-3 text-sm text-muted">Brak opublikowanego przebiegu ewaluacji (runs.kind = eval). Pokazujemy tylko liczby z przebiegu analizy; jakości nie podajemy bez pomiaru.</div>
      )}
      <div className="grid gap-3 md:grid-cols-2">
        {grouping.campaigns.filter((c) => c.members.length > 1).map((c) => (
          <Link key={c.id} to={`/campaigns/${encodeURIComponent(c.id)}`} className="rounded-md border border-line bg-panel p-3 hover:border-accent">
            <div className="text-xs"><span className="font-mono">{c.id}</span> · {c.members.length} incydentów{grouping.groups.find((g) => (g.campaign_ids ?? []).includes(c.id)) ? ` · grupa ${grouping.groups.find((g) => (g.campaign_ids ?? []).includes(c.id))!.id}` : ""}</div>
            <div className="mt-1 line-clamp-2 text-sm font-medium">{c.label}</div>
            <div className="mt-2 flex gap-1.5">{c.members.map((m) => <Thumb key={m} artifactId={thumbs[m]} alt={m} size={72} />)}</div>
          </Link>
        ))}
      </div>
      {grouping.groups.map((g) => <div key={g.id} className="text-sm text-muted">Grupa podobnych kampanii <span className="font-mono">{g.id}</span>: {g.label} ({(g.campaign_ids ?? []).join(", ")})</div>)}
    </div>
  );
}

// ---------------------------------------------------------------- captions
function captions(st: Stages, preset: number): { what: string; why: string }[] {
  const imp = st.import, g = st.genes, r = st.rarity, c = st.candidates, d = st.decision, gr = st.grouping, res = st.result;
  const p = st.evidence.presets[preset];
  const famCount = FAMILIES.filter((f) => Object.values(g.by_family[f] ?? {}).some((n) => n > 0)).length;
  const rare = r.histogram.filter((h) => h.df >= 2).reduce((a, h) => a + (h.campaign ?? 0), 0);
  const multi = gr.campaigns.filter((x) => x.members.length > 1);
  return [
    { what: `${imp.incidents} incydentów, ${formatCount(Object.values(imp.artifacts_by_role).reduce((a, b) => a + b, 0))} plików wg roli; ${formatCount(imp.job_items_by_status.stored ?? 0)} zapisanych, ${formatCount(imp.job_items_by_status.duplicate ?? 0)} duplikatów.`, why: "Każdy plik ma adres SHA-256: od pierwszej sekundy wiadomo, z czego pochodzi każdy dowód. Sekrety są ukrywane przy ekstrakcji." },
    { what: `Incydent ${g.incident_id} rozłożony na geny w ${famCount} rodzinach; ramki OCR i twarzy pochodzą ze wskaźników dowodu genów.`, why: "Wiele niezależnych śladów (obraz, tekst, infrastruktura, tożsamość, narracja) zamiast jednego hasha." },
    { what: `${formatCount(r.distinct_values)} różnych wartości genów; ${formatCount(rare)} rzadkich wspólnych na poziomie kampanii, szum i szablony gasną.`, why: "Wspólny hosting czy szablon to nie kampania: waga genu zależy od jego rzadkości w korpusie." },
    { what: `${formatCount(c.candidate_pairs_total ?? 0)} par kandydatów z ${formatCount(c.all_pairs)} możliwych; dla ${c.incident_id}: ${c.pairs.length}.`, why: "Kandydatów proponują indeksy dokładnych genów, osadzeń i bliskich hashy; każde źródło jest zapisane przy parze." },
    { what: p ? `Para ${p.a} ↔ ${p.b} (${p.why}): rachunek bitów per rodzina, poziomy kampania / szablon / zasób.` : "Brak par.", why: "Każde powiązanie ma audytowalne wyjaśnienie w bitach; brak obserwacji = nieznane, nie dowód przeciw." },
    { what: `Hipotezy: ${Object.entries(d.hypotheses).map(([h, n]) => `${HYPOTHESIS_LABEL[h] ?? h} ${n}`).join(", ")}.`, why: "System umie powiedzieć „nie wiem”: pasmo niepewności trafia do kolejki analityka." },
    { what: `Powiązania łączą się w ${multi.length} kampanie wieloincydentowe i ${gr.groups.length} grupę podobnych kampanii.`, why: "Wynik jest widoczny i sprawdzalny: każdą krawędź można otworzyć jako dowód pary." },
    { what: res.eval_headline ? `Wyniki ewaluacji z przebiegu ${res.eval_run_id}.` : "Brak opublikowanej ewaluacji; tylko liczby z przebiegu analizy.", why: "Liczby pochodzą z bazy przebiegu; jakości nie podajemy bez pomiaru." },
  ];
}

// ---------------------------------------------------------------- page
export function SimulationPage() {
  const q = useSimulation();
  const [t, setT] = useState(0); // 0..8
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<(typeof SPEEDS)[number]>(1);
  const [present, setPresent] = useState(false);
  const [preset, setPreset] = useState(0);
  const [truthOn, setTruthOn] = useState(false);
  const raf = useRef<number | null>(null);
  const last = useRef<number | null>(null);
  const stage = Math.min(7, Math.floor(t));
  const u = t >= 8 ? 1 : t - stage;
  const railRef = useRef<HTMLOListElement>(null);
  // narrow screens scroll the rail horizontally: keep the current stage tab visible
  useEffect(() => {
    const cur = railRef.current?.querySelector<HTMLElement>('[aria-current="step"]');
    if (cur && railRef.current && railRef.current.scrollWidth > railRef.current.clientWidth) cur.scrollIntoView?.({ block: "nearest", inline: "nearest" });
  }, [stage]);

  useEffect(() => {
    if (!playing) return;
    const tick = (now: number) => {
      if (last.current !== null) {
        const dt = ((now - last.current) / 1000) * speed / STAGE_SECONDS;
        setT((x) => {
          const nx = x + dt;
          if (nx >= 8) { setPlaying(false); return 8; }
          return nx;
        });
      }
      last.current = now;
      raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => { if (raf.current) cancelAnimationFrame(raf.current); last.current = null; };
  }, [playing, speed]);

  const go = useCallback((s: number) => { setT(Math.max(0, Math.min(7, s))); }, []);
  const togglePresent = useCallback((on: boolean) => {
    setPresent(on);
    if (on) { setPlaying(true); void document.documentElement.requestFullscreen?.().catch(() => undefined); }
    else if (document.fullscreenElement) void document.exitFullscreen?.().catch(() => undefined);
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tg = e.target as HTMLElement;
      if (tg && (tg.tagName === "INPUT" || tg.tagName === "SELECT" || tg.tagName === "TEXTAREA")) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === " ") { e.preventDefault(); setPlaying((p) => { if (!p && t >= 8) setT(0); return !p; }); }
      else if (e.key === "ArrowRight") go(e.shiftKey ? t + 0.1 : stage + 1);
      else if (e.key === "ArrowLeft") go(e.shiftKey ? t - 0.1 : (u > 0.05 ? stage : stage - 1));
      else if (/^[1-8]$/.test(e.key)) go(Number(e.key) - 1);
      else if (e.key === "+" || e.key === "=") setSpeed((s) => SPEEDS[Math.min(SPEEDS.length - 1, SPEEDS.indexOf(s) + 1)]!);
      else if (e.key === "-") setSpeed((s) => SPEEDS[Math.max(0, SPEEDS.indexOf(s) - 1)]!);
      else if (e.key.toLowerCase() === "p") togglePresent(!present);
      else if (e.key === "Escape" && present) togglePresent(false);
      else if (stage === 4 && /^[a-e]$/i.test(e.key)) setPreset(e.key.toLowerCase().charCodeAt(0) - 97);
      else if (e.key.toLowerCase() === "t") setTruthOn((v) => !v);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [t, stage, u, go, present, togglePresent]);

  if (!q.data) return <div className="p-4"><QueryState query={q} what="symulację" endpoint="/v1/simulation" /></div>;
  if (!q.data.run_id) return <div className="p-4"><NoRun what="Symulacja" /></div>;
  const st = q.data.stages;
  const thumbs: Record<string, string | null> = Object.fromEntries(st.import.incidents_list.map((i) => [i.id, i.thumbnail]));
  const caps = captions(st, Math.min(preset, Math.max(0, st.evidence.presets.length - 1)));
  const body = (
    <>
      {stage === 0 && <StageImport s={st.import} u={u} />}
      {stage === 1 && <StageGenes s={st.genes} u={u} />}
      {stage === 2 && <StageRarity s={st.rarity} u={u} />}
      {stage === 3 && <StageCandidates s={st.candidates} u={u} thumbs={thumbs} />}
      {stage === 4 && <StageEvidence s={st.evidence} u={u} preset={Math.min(preset, Math.max(0, st.evidence.presets.length - 1))} setPreset={setPreset} thumbs={thumbs} />}
      {stage === 5 && <StageDecision s={st.decision} u={u} />}
      {stage === 6 && <StageGrouping s={st.grouping} u={u} thumbs={thumbs} truthOn={truthOn} />}
      {stage === 7 && <StageResult s={st.result} grouping={st.grouping} u={u} thumbs={thumbs} />}
    </>
  );

  const rail = <StageRail ref={railRef} stage={stage} u={u} go={go} />;

  const transport = (
    <div className="flex flex-wrap items-center gap-2 border-t border-line bg-panel px-4 py-2">
      <button type="button" onClick={() => { setT(0); setPlaying(false); }} aria-label="Na początek" className="h-8 w-8 rounded-[4px] hover:bg-hover">⏮</button>
      <button type="button" onClick={() => go(u > 0.05 ? stage : stage - 1)} aria-label="Poprzedni etap" className="h-8 w-8 rounded-[4px] hover:bg-hover">◀</button>
      <button type="button" onClick={() => { if (t >= 8) setT(0); setPlaying(!playing); }} className="h-8 shrink-0 rounded-[4px] bg-fg px-4 text-sm whitespace-nowrap text-canvas">{playing ? "❚❚ Pauza" : "▶ Odtwórz"}</button>
      <button type="button" onClick={() => go(stage + 1)} aria-label="Następny etap" className="h-8 w-8 rounded-[4px] hover:bg-hover">▶</button>
      <input type="range" min={0} max={8} step={0.01} value={t} onChange={(e) => { setPlaying(false); setT(Number(e.target.value)); }} aria-label="Przewijanie" className="mx-2 min-w-32 flex-1" />
      <span className="w-24 text-right font-mono text-xs">{Math.floor((t * STAGE_SECONDS) / 60)}:{String(Math.floor((t * STAGE_SECONDS) % 60)).padStart(2, "0")} / 1:04</span>
      <span className="flex overflow-hidden rounded-[4px] border border-line-strong">
        {SPEEDS.map((s) => <button key={s} type="button" onClick={() => setSpeed(s)} className={cx("h-7 px-2 text-xs", s === speed ? "bg-active font-semibold" : "hover:bg-hover")}>{String(s).replace(".", ",")}×</button>)}
      </span>
      {st.grouping.truth && <button type="button" onClick={() => setTruthOn(!truthOn)} className={cx("h-7 rounded-[4px] border px-2 text-xs", truthOn ? "border-abstain bg-abstain-soft" : "border-line-strong")}>Prawda (T)</button>}
      <button type="button" onClick={() => togglePresent(!present)} className="h-7 rounded-[4px] border border-line-strong px-2 text-xs">{present ? "Zamknij prezentację (Esc)" : "⛶ Prezentacja (P)"}</button>
    </div>
  );

  if (present) {
    return (
      <div className="fixed inset-0 z-40 flex flex-col bg-canvas">
        <div className="border-b border-line bg-panel px-6 py-3">{rail}</div>
        <div className="min-h-0 flex-1 overflow-auto px-8 py-5 text-[15px]">
          <h2 className="mb-4 text-2xl font-semibold">{stage + 1}. {STAGES[stage]}</h2>
          {body}
        </div>
        <div className="border-t border-line bg-fg px-8 py-4 text-lg text-canvas" aria-live="polite">
          <div>{caps[stage]?.what}</div>
          <div className="text-base text-canvas/80">{caps[stage]?.why}</div>
        </div>
        {transport}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-line bg-panel px-4 py-2">{rail}</div>
      <div className="grid min-h-0 flex-1 gap-4 overflow-auto p-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div className="min-w-0">
          <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <span className="font-mono text-sm text-subtle">{stage + 1}/8</span>
            <h2 className="shrink-0 text-xl font-semibold">{STAGES[stage]}</h2>
            <span className="min-w-0 text-sm text-muted [overflow-wrap:anywhere]">przebieg <span className="font-mono">{q.data.run_id}</span></span>
          </div>
          {body}
        </div>
        <aside className="space-y-3 text-sm">
          <section className="rounded-md border border-line bg-panel p-3"><h3 className="mb-1 text-xs font-semibold text-muted">Co widać</h3><p>{caps[stage]?.what}</p></section>
          <section className="rounded-md border border-line bg-panel p-3"><h3 className="mb-1 text-xs font-semibold text-muted">Dlaczego to przekonuje</h3><p>{caps[stage]?.why}</p></section>
          <section className="rounded-md border border-line bg-panel p-3 text-xs text-muted">
            <h3 className="mb-1 font-semibold">Sterowanie</h3>
            Spacja odtwórz/pauza · ←/→ etap · Shift+←/→ przewijanie · 1–8 skok · +/− tempo · A–E para (etap 5){st.grouping.truth ? " · T prawda" : ""} · P prezentacja · Esc wyjście
          </section>
          <section className="rounded-md border border-line bg-panel p-3 text-xs text-subtle">Wszystkie liczby pochodzą z bazy przebiegu (GET /v1/simulation); animacja tylko je odsłania.</section>
        </aside>
      </div>
      {transport}
    </div>
  );
}
