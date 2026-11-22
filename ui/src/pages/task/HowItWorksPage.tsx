/**
 * "Jak to działa": the real pipeline as a flow diagram. Every box shows live numbers of the current run
 * (GET /v1/system/flow) and links to the screen that shows the detail. Particles on each arrow are proportional
 * (log scale) to the real volume that crosses it; with prefers-reduced-motion they are not drawn.
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router";
import { FAMILIES } from "../../api/task";
import { useCorpusStats, useSystemFlow, type SystemFlow } from "../../api/visual";
import { FamChip } from "../../components/evidence";
import { cx } from "../../lib/cx";
import { familyColors, familyLabel, formatDatePl, HYPOTHESIS_LABEL, plNumber } from "../../lib/evidence";
import { formatCount } from "../../lib/format";
import { useTheme } from "../../lib/theme";
import { MD_UP, useMediaQuery, useReducedMotion } from "../../lib/media";
import { NoRun, QueryState } from "./common";
import { SkeletonBlock, SlowHint } from "../../components/Skeleton";
import { HowExplainer } from "./HowExplainer";

type Boxes = NonNullable<SystemFlow["boxes"]>;

interface BoxDef { key: string; title: string; to: string; body: (b: Boxes) => ReactNode }

const Num = ({ v, l }: { v: ReactNode; l: string }) => (
  <div className="min-w-0"><div className="font-mono text-lg leading-6 text-fg">{v}</div><div className="truncate text-xs text-subtle">{l}</div></div>
);

function MiniBars({ data, color }: { data: [string, number][]; color: (k: string) => string }) {
  const max = Math.max(1, ...data.map(([, v]) => v));
  return (
    <div className="mt-2 space-y-0.5">
      {data.slice(0, 6).map(([k, v]) => (
        <div key={k} className="grid grid-cols-[64px_1fr_48px] items-center gap-1.5 text-2xs" title={`${k}: ${v}`}>
          <span className="truncate text-muted">{k}</span>
          <span className="h-1.5 rounded-r-[2px]" style={{ width: `${Math.max(2, (100 * v) / max)}%`, backgroundColor: color(k) }} />
          <span className="text-right font-mono text-muted">{formatCount(Math.round(v))}</span>
        </div>
      ))}
    </div>
  );
}

const pct = (v: number | null | undefined) => (v == null ? "—" : `${plNumber(100 * v, 0)}%`);
const Hint = ({ text }: { text?: string | null }) => (text ? <div className="mt-1 text-2xs text-subtle">{text}</div> : null);

function boxes(fam: Record<string, string>): BoxDef[] {
  const neutral = () => "var(--color-accent)";
  return [
    { key: "input", title: "Wejście", to: "/analyse", body: (b) => <><div className="grid grid-cols-2 gap-2"><Num v={b.input.incidents} l="incydenty w przebiegu" /><Num v={Object.keys(b.input.by_corpus).length} l="korpusy" /></div><MiniBars data={Object.entries(b.input.by_corpus)} color={neutral} /></> },
    { key: "adapters", title: "Adaptery i import", to: "/data", body: (b) => <><Num v={formatCount(b.adapters.artifacts)} l="artefaktów (SHA-256, rola)" /><MiniBars data={Object.entries(b.adapters.by_role).sort((x, y) => y[1] - x[1])} color={neutral} /></> },
    { key: "genes", title: "Geny: 6 rodzin", to: "/incidents", body: (b) => <><div className="grid grid-cols-2 gap-2"><Num v={formatCount(b.genes.total)} l="genów" /><Num v={`${b.genes.extractors_cached}/${b.genes.extractors_cached + b.genes.extractors_run}`} l="ekstraktorów z cache" /></div><MiniBars data={FAMILIES.map((f) => [f, b.genes.by_family[f] ?? 0] as [string, number])} color={(k) => fam[k] ?? "var(--color-muted)"} /></> },
    { key: "enrichment", title: "Wzbogacanie", to: "/incidents", body: (b) => <><Num v={b.enrichment.genes_with_enrichment} l="genów z faktów RDAP/DNS/CT" /><MiniBars data={Object.entries(b.enrichment.records_by_source)} color={neutral} />{Object.keys(b.enrichment.records_by_source).length === 0 && <div className="mt-1 text-2xs text-subtle">brak rekordów z datą pobrania (tryb replay)</div>}</> },
    { key: "rarity", title: "Rzadkość i poziomy", to: "/simulation", body: (b) => <><Num v={formatCount(Object.values(b.rarity.distinct_values_by_level).reduce((s, v) => s + v, 0))} l="różnych wartości genów" /><MiniBars data={Object.entries(b.rarity.distinct_values_by_level).sort((x, y) => y[1] - x[1])} color={(k) => (k === "campaign" ? "var(--color-for)" : k === "none" ? "var(--color-unknown)" : k === "kit" ? "var(--color-fam-meta)" : "var(--color-fam-vis)")} /></> },
    { key: "candidates", title: "Indeksy kandydatów", to: "/pairs", body: (b) => <><div className="grid grid-cols-2 gap-2"><Num v={b.candidates.candidate_pairs ?? "—"} l="par kandydatów" /><Num v={b.candidates.all_pairs} l="wszystkich par" /></div><MiniBars data={Object.entries(b.candidates.by_index).sort((x, y) => y[1] - x[1])} color={neutral} /></> },
    { key: "scoring", title: "Ocena par (bity)", to: "/pairs", body: (b) => <><Num v={b.scoring.pairs_scored} l={`ocenionych par · ${b.scoring.scorer ?? ""}`} /><MiniBars data={Object.entries(b.scoring.bits_by_level).map(([k, v]) => [k.replace("_bits", ""), v] as [string, number])} color={(k) => (k === "campaign" ? "var(--color-for)" : k === "kit" ? "var(--color-fam-meta)" : "var(--color-fam-vis)")} /></> },
    { key: "clustering", title: "Kampanie i grupy", to: "/campaigns", body: (b) => <div className="grid grid-cols-3 gap-2"><Num v={b.clustering.campaigns} l="kampanie" /><Num v={b.clustering.multi_member} l="≥ 2 incydenty" /><Num v={b.clustering.groups} l="grupy" /></div> },
    { key: "assignment", title: "Przypisanie (zbiór otwarty)", to: "/incidents", body: (b) => { const a = b.assignment, sm = a.summary; return <><div className="grid grid-cols-3 gap-2"><Num v={Object.values(a.by_decision).reduce((s, v) => s + v, 0)} l={`przypisań${a.conformal ? " · konformalnie" : ""}`} /><Num v={pct(sm?.agreement_with_clustering)} l="zgodne z grupowaniem" /><Num v={a.new_material ?? 0} l="nowy materiał" /></div><MiniBars data={Object.entries(a.by_decision)} color={neutral} />{sm && <div className="mt-1 text-2xs text-subtle">leave-one-out: {sm.assignments} · wstrzymane {pct(sm.abstain_rate)} · odrzucone odłożone kampanie {pct(sm.held_out_campaign_rejection_rate)}</div>}<Hint text={a.hint_pl} /></>; } },
    { key: "reviews", title: "Recenzje i rewizje", to: "/queue", body: (b) => <><div className="grid grid-cols-3 gap-2"><Num v={b.reviews.reviews} l="recenzje" /><Num v={b.reviews.revisions} l="rewizje" /><Num v={b.reviews.constraints_in_run} l="ograniczeń" /></div><Hint text={b.reviews.hint_pl} /></> },
    { key: "outputs", title: "Eksport · Neo4j · AI", to: "/graph", body: (b) => { const e = b.outputs.exports; return <><div className="grid grid-cols-2 gap-2"><Num v={e?.total ?? 0} l="eksportów przebiegu" /><Num v={b.outputs.llm_enabled ? "włączony" : "wyłączony"} l="analityk AI" /></div><MiniBars data={Object.entries(e?.by_kind ?? {}).sort((x, y) => y[1] - x[1])} color={neutral} /><Hint text={b.outputs.hint_pl} /></>; } },
    { key: "watch", title: "Obserwacja (Watch)", to: "/scheduler", body: () => <div className="text-xs text-muted">Harmonogram, listy obserwowanych, biblioteki reklam: osobny ekran.</div> },
  ];
}

// grid positions (col, row) of the snake; arrows follow the order of the list
const POS: [number, number][] = [[0, 0], [1, 0], [2, 0], [3, 0], [3, 1], [2, 1], [1, 1], [0, 1], [0, 2], [1, 2], [2, 2], [3, 2]];

function volumes(b: Boxes): number[] {
  const sum = (o: Record<string, number>) => Object.values(o).reduce((s, v) => s + v, 0);
  return [b.adapters.artifacts, b.genes.total, b.enrichment.genes_with_enrichment, sum(b.rarity.distinct_values_by_level), b.candidates.candidate_pairs ?? b.candidates.all_pairs,
    b.scoring.pairs_scored, b.clustering.campaigns, sum(b.assignment.by_decision), b.reviews.reviews, b.outputs.exports?.total ?? 0, 1];
}

function Diagram({ b }: { b: Boxes }) {
  const theme = useTheme();
  const defs = boxes(familyColors(theme));
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 1600, h: 720 });
  const reduced = useReducedMotion();
  const wide = useMediaQuery(MD_UP, true);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [wide]);
  const cols = 4, rows = 3, gapX = 56, gapY = 48;
  const bw = (size.w - gapX * (cols - 1)) / cols, bh = (size.h - gapY * (rows - 1)) / rows;
  const rect = (i: number) => { const [c, r] = POS[i]!; return { x: c * (bw + gapX), y: r * (bh + gapY) }; };
  const anchor = (i: number, j: number) => {
    const a = rect(i), z = rect(j);
    if (a.y === z.y) { const right = z.x > a.x; return { x1: right ? a.x + bw : a.x, y1: a.y + bh / 2, x2: right ? z.x : z.x + bw, y2: z.y + bh / 2 }; }
    return { x1: a.x + bw / 2, y1: a.y + bh, x2: z.x + bw / 2, y2: z.y };
  };
  const vol = volumes(b);
  if (!wide) {
    // Phone / narrow: the 4×3 snake would squeeze 12 boxes into ~80 px columns; one column, arrows between.
    return (
      <ol className="space-y-0" aria-label="Etapy potoku" data-testid="flow-column">
        {defs.map((d, i) => (
          <li key={d.key}>
            <Link to={d.to} className="surface-card surface-hover flex flex-col p-4 hover:border-accent">
              <div className="mb-2 flex items-baseline gap-2"><span className="font-mono text-xs text-subtle">{i + 1}</span><span className="text-sm font-semibold">{d.title}</span><span className="ml-auto text-xs text-accent">zobacz</span></div>
              <div className="min-w-0">{d.body(b)}</div>
            </Link>
            {i < defs.length - 1 && (
              <div className="flex items-center gap-2 py-1.5 pl-6 text-xs text-subtle" aria-label={`dalej: ${formatCount(vol[i] ?? 0)}`}>
                <span aria-hidden="true" className="text-base leading-none">↓</span>
                <span className="font-mono">{formatCount(vol[i] ?? 0)}</span>
              </div>
            )}
          </li>
        ))}
      </ol>
    );
  }
  return (
    <div ref={ref} className="relative h-[760px] w-full">
      <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
        <defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="var(--color-line-strong)" /></marker></defs>
        {defs.slice(0, -1).map((_, i) => {
          const { x1, y1, x2, y2 } = anchor(i, i + 1);
          const d = `M${x1},${y1} L${x2},${y2}`;
          const n = Math.max(1, Math.min(8, Math.round(Math.log10((vol[i] ?? 0) + 1) * 2)));
          return (
            <g key={i}>
              <path d={d} stroke="var(--color-line-strong)" strokeWidth={2} fill="none" markerEnd="url(#arr)" />
              {!reduced && (vol[i] ?? 0) > 0 && Array.from({ length: n }, (_, k) => (
                <circle key={k} r={3.5} fill="var(--color-accent)">
                  <animateMotion dur="2.4s" repeatCount="indefinite" begin={`${(k * 2.4) / n}s`} path={d} />
                </circle>
              ))}
              <text x={(x1 + x2) / 2 + (y1 === y2 ? 0 : 8)} y={(y1 + y2) / 2 + (y1 === y2 ? -8 : 4)} fontSize={11} textAnchor={y1 === y2 ? "middle" : "start"} className="fill-subtle font-mono">{formatCount(vol[i] ?? 0)}</text>
            </g>
          );
        })}
      </svg>
      {defs.map((d, i) => {
        const r = rect(i);
        return (
          <Link key={d.key} to={d.to} className="surface-card surface-hover absolute flex flex-col p-4 hover:border-accent" style={{ left: r.x, top: r.y, width: bw, height: bh }}>
            <div className="mb-2 flex items-baseline gap-2"><span className="font-mono text-xs text-subtle">{i + 1}</span><span className="text-sm font-semibold">{d.title}</span><span className="ml-auto text-xs text-accent">zobacz</span></div>
            <div className="min-h-0 flex-1 overflow-hidden">{d.body(b)}</div>
          </Link>
        );
      })}
    </div>
  );
}

export function HowItWorksPage() {
  const q = useSystemFlow();
  const cs = useCorpusStats();
  const theme = useTheme();
  const fam = familyColors(theme);
  const f = q.data;
  if (!f || !f.run_id || !f.boxes) {
    return (
      <div className="mx-auto max-w-[1840px] space-y-8 p-4 sm:p-6">
        <HowExplainer />
        <section aria-labelledby="how-live-h" className="space-y-4">
          <h2 id="how-live-h" className="text-lg font-semibold tracking-tight">Ten przebieg na żywo</h2>
          {!f && q.isError ? <QueryState query={q} what="przepływ" endpoint="/v1/system/flow" />
            : !f ? (
              <div className="space-y-4" role="status" aria-busy="true" aria-live="polite" data-testid="flow-skeleton">
                <p className="text-sm text-muted">Ładowanie: przepływ…</p>
                <SlowHint className="text-sm" />
                <SkeletonBlock className="h-[420px] rounded-lg" />
              </div>
            ) : <NoRun what="Schemat działania" />}
        </section>
      </div>
    );
  }
  const b = f.boxes;
  const timings = Object.entries(f.timings_s ?? {}).sort((x, y) => y[1] - x[1]);
  const tmax = Math.max(1e-6, ...timings.map(([, v]) => v));
  const orig = b.genes.extractor_original_seconds ?? {};
  const stats = b.genes.extractors ?? {};
  const ex = (Object.keys(stats).length ? Object.keys(stats) : Object.keys({ ...orig, ...b.genes.extractor_seconds }))
    .map((k) => ({ k, run: stats[k]?.run_seconds ?? b.genes.extractor_seconds[k] ?? 0, orig: stats[k]?.original_seconds ?? orig[k] ?? 0, s: stats[k] }))
    .sort((x, y) => y.orig - x.orig || y.run - x.run || x.k.localeCompare(y.k)).slice(0, 16);
  const exMax = Math.max(1e-6, ...ex.map((x) => Math.max(x.orig, x.run)));
  const corp = cs.data?.items ?? [];
  const corpMax = Math.max(1, ...corp.map((c) => c.incidents));
  return (
    <div className="mx-auto max-w-[1840px] space-y-8 p-4 sm:p-6">
      <HowExplainer b={b} runId={f.run_id} />
      <section aria-labelledby="how-live-h" className="space-y-4">
      <div>
        <h2 id="how-live-h" className="text-lg font-semibold tracking-tight">Ten przebieg na żywo</h2>
        <p className="text-sm text-muted">Rzeczywisty potok na przebiegu <span className="font-mono">{f.run_id}</span> ({formatDatePl(f.created_at)}){f.revision_of ? <> · rewizja przebiegu <span className="font-mono">{f.revision_of}</span></> : null}. Liczby na strzałkach: ile przeszło dalej; kropki są proporcjonalne do tej liczby (skala logarytmiczna).</p>
      </div>
      <Diagram b={b} />
      <div className="grid gap-4 xl:grid-cols-3">
        <section className="surface-card min-w-0 p-5">
          <h2 className="mb-1 text-sm font-semibold">Czas etapów przebiegu</h2>
          <p className="mb-3 text-xs text-subtle">runs.metrics.timings_s, sekundy</p>
          {timings.map(([k, v]) => (
            <div key={k} className="grid grid-cols-[minmax(0,170px)_1fr_60px] items-center gap-2 py-0.5 text-xs">
              <span className="truncate text-muted">{k}</span><span className="h-2.5 rounded-r-[3px] bg-accent" style={{ width: `${Math.max(1, (100 * v) / tmax)}%` }} /><span className="text-right font-mono">{plNumber(v, 2)} s</span>
            </div>
          ))}
        </section>
        <section className="surface-card min-w-0 p-5">
          <h2 className="mb-1 text-sm font-semibold">Czas ekstraktorów</h2>
          <p className="mb-3 text-xs text-subtle">suma sekund po incydentach: pierwotne obliczenie (pasek) i w tym przebiegu; z cache: {b.genes.extractors_cached}, uruchomione: {b.genes.extractors_run}; geny: obserwowane / niedostępne / błędy</p>
          {ex.length ? ex.map((x) => (
            <div key={x.k} className="grid grid-cols-[minmax(0,110px)_minmax(0,1fr)_48px_48px_64px] sm:grid-cols-[110px_1fr_56px_56px_84px] items-center gap-2 py-0.5 text-xs" title={x.s?.original_unknown ? `${x.s.original_unknown} wpisów cache bez zapisanego czasu` : undefined}>
              <span className="truncate font-mono text-muted">{x.k}</span><span className="h-2.5 rounded-r-[3px] bg-fam-vis" style={{ width: `${Math.max(1, (100 * x.orig) / exMax)}%` }} /><span className="text-right font-mono" title="pierwotnie">{plNumber(x.orig, 2)} s</span><span className="text-right font-mono text-subtle" title="w tym przebiegu">{plNumber(x.run, 2)} s</span><span className="text-right font-mono text-2xs text-muted">{x.s ? `${x.s.observed}/${x.s.unavailable}/${x.s.failed}` : "—"}</span>
            </div>
          )) : <div className="text-xs text-subtle">Brak czasów ekstraktorów w metrykach (starszy przebieg). Uruchom analizę ponownie.</div>}
        </section>
        <section className="surface-card min-w-0 p-5">
          <h2 className="mb-1 text-sm font-semibold">Decyzje par</h2>
          <p className="mb-3 text-xs text-subtle">hipotezy w ocenionych parach</p>
          {Object.entries(b.scoring.hypotheses).sort((x, y) => y[1] - x[1]).map(([h, v]) => (
            <div key={h} className="grid grid-cols-[minmax(0,170px)_1fr_40px] items-center gap-2 py-0.5 text-xs">
              <span className="truncate text-muted">{HYPOTHESIS_LABEL[h] ?? h}</span><span className={cx("h-2.5 rounded-r-[3px]", h === "same_campaign" ? "bg-for" : h === "abstain" ? "hatch-abstain" : "bg-unknown")} style={{ width: `${Math.max(1, (100 * v) / Math.max(1, ...Object.values(b.scoring.hypotheses)))}%` }} /><span className="text-right font-mono">{v}</span>
            </div>
          ))}
        </section>
      </div>
      <section className="surface-card min-w-0 p-5">
        <h2 className="mb-1 text-sm font-semibold">Zbiory danych</h2>
        <p className="mb-3 text-xs text-subtle">organizatora (nask), materiał analityka (upload) i zewnętrzne (ext:*, public:*) są rozdzielone; „Tylko dane NASK” w górnym pasku wybiera przebieg bez zewnętrznych</p>
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
          {corp.map((c) => (
            <div key={c.corpus} className="rounded-md border border-line p-4">
              <div className="flex items-center gap-2"><span className="font-mono text-sm font-semibold">{c.corpus}</span>{(c.corpus === "extended" || c.corpus === "public" || c.corpus.startsWith("ext:") || c.corpus.startsWith("public:")) && <span className="rounded border border-line px-1 text-2xs">zewnętrzny</span>}</div>
              <div className="mt-2 grid grid-cols-3 gap-2"><Num v={c.incidents} l="incydenty" /><Num v={c.campaigns_in_latest_run} l="kampanie w przebiegu" /><Num v={c.in_latest_run} l="w przebiegu" /></div>
              <div className="mt-2 h-1.5 rounded-full bg-hover"><div className="h-1.5 rounded-full bg-accent" style={{ width: `${(100 * c.incidents) / corpMax}%` }} /></div>
              <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-2xs text-muted">{FAMILIES.map((fm) => <span key={fm} title={familyLabel(fm)}><FamChip family={fm} /> {formatCount(c.genes_by_family[fm] ?? 0)}</span>)}</div>
            </div>
          ))}
          {corp.length === 0 && <QueryState query={cs} what="zbiory danych" />}
        </div>
      </section>
      <p className="text-xs text-subtle">Rodziny: {FAMILIES.map((x) => <span key={x} className="mr-3"><span className="mr-1 inline-block size-2 rounded-sm" style={{ backgroundColor: fam[x] }} />{x} {familyLabel(x)}</span>)}</p>
      </section>
    </div>
  );
}
