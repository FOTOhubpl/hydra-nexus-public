import { useState } from "react";
import { useLatestRun } from "../../api/task";
import { BarList, MiniLine } from "../../components/charts";
import { FamChip } from "../../components/evidence";
import { EmptyState } from "../../components/EmptyState";
import { Panel } from "../../components/Panel";
import { formatDatePl, plNumber } from "../../lib/evidence";
import { formatCount } from "../../lib/format";
import { cx } from "../../lib/cx";
import { Page, QueryState } from "./common";
import { Toc, MethodSection, type TocItem } from "../../components/methodology/Kit";
import { Assignment, Genome, Geometry, Grouping, Hypotheses, Limits, Overview, QueueKontra, Rarity, SmoothUnits } from "../../components/methodology/Sections";

const TOC: TocItem[] = [
  { id: "m-przeglad", n: 1, title: "Przegląd" }, { id: "m-genom", n: 2, title: "Genom incydentu" },
  { id: "m-rzadkosc", n: 3, title: "Rzadkość i bity" }, { id: "m-pary", n: 4, title: "Hipotezy par" },
  { id: "m-grupowanie", n: 5, title: "Grupowanie" }, { id: "m-przypisanie", n: 6, title: "Przypisanie" },
  { id: "m-operator", n: 7, title: "Jednostki operatora" }, { id: "m-geometria", n: 8, title: "Geometria" },
  { id: "m-kolejka", n: 9, title: "Kolejka i KONTRA" }, { id: "m-ewaluacja", n: 10, title: "Ewaluacja" },
  { id: "m-ograniczenia", n: 11, title: "Ograniczenia i słownik" },
];

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === "object" && v !== null && !Array.isArray(v);

/** EVAL metrics that are counts; every other numeric metric is a rate / score shown with 3 decimals. */
export const COUNT_METRICS = new Set(["false_merge_pairs", "hard_negative_violations", "n", "incidents", "campaigns", "pairs", "assignments"]);

/** Polish numbers everywhere: integers with pl-PL grouping, fractions with a decimal comma and 3 digits.
 * `rate` forces 3 digits for whole numbers too (an F1 of 1 reads "1,000" next to "0,946"). */
export function fmt(v: unknown, rate = false): string {
  if (typeof v === "number") return Number.isInteger(v) && !rate ? formatCount(v) : plNumber(v, 3);
  if (typeof v === "boolean") return v ? "tak" : "nie";
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.length > 6 ? `[${v.length} el.]` : JSON.stringify(v);
  return typeof v === "object" ? JSON.stringify(v) : String(v);
}

/** Flatten nested objects to dotted scalar keys (depth ≤ 3); lists and deeper objects stay as JSON. */
export function flatten(o: Obj, prefix = "", depth = 0): Obj {
  const out: Obj = {};
  for (const [k, v] of Object.entries(o)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (isObj(v) && depth < 2) Object.assign(out, flatten(v, key, depth + 1));
    else out[key] = v;
  }
  return out;
}

/** Table for {rowKey: {metric...}} (EVAL metric blocks). */
function DictTable({ data, rowHeader = "", columns }: { data: Obj; rowHeader?: string; columns?: string[] }) {
  const rows = Object.entries(data).filter(([, v]) => isObj(v)).map(([k, v]) => [k, flatten(v as Obj)] as const);
  const scalars = Object.entries(data).filter(([, v]) => !isObj(v));
  const cols = columns ?? Array.from(new Set(rows.flatMap(([, r]) => Object.keys(r))));
  // a column holding any fractional number is a rate column: whole values get the same 3 decimals
  const rateCol = new Set(cols.filter((c) => !COUNT_METRICS.has(c.split(".").pop() ?? c) && rows.some(([, r]) => typeof r[c] === "number" && !Number.isInteger(r[c]))));
  return (
    <div className="overflow-x-auto">
      {scalars.length > 0 && (
        <div className="flex flex-wrap gap-x-4 px-3 py-2 text-xs text-muted">
          {scalars.map(([k, v]) => <span key={k}>{k}: <span className="font-mono text-fg">{fmt(v)}</span></span>)}
        </div>
      )}
      {rows.length > 0 && (
        <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
          <thead className="bg-raised text-2xs tracking-wider text-subtle">
            <tr>
              <th className="px-2 py-1.5 text-left">{rowHeader}</th>
              {cols.map((c) => <th key={c} className="px-2 py-1.5 text-right whitespace-nowrap">{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {rows.map(([k, r]) => (
              <tr key={k} className="border-t border-line">
                <td className="px-2 py-1 font-mono whitespace-nowrap">{k}</td>
                {cols.map((c) => <td key={c} className="px-2 py-1 text-right font-mono">{fmt(r[c], rateCol.has(c))}</td>)}
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
    </div>
  );
}

/** ladder: {method: {corpus: row}} → one row per method × corpus. */
function ladderRows(ladder: Obj): Obj {
  const out: Obj = {};
  for (const [name, byCorpus] of Object.entries(ladder)) {
    if (!isObj(byCorpus)) continue;
    for (const [corpus, row] of Object.entries(byCorpus)) if (isObj(row)) out[`${name} · ${corpus}`] = row;
  }
  return out;
}

const LADDER_COLS = [
  "campaign_pairwise.f1", "campaign_pairwise.precision", "campaign_pairwise.recall", "campaign_pairwise.fp",
  "campaign_bcubed.f1", "group_pairwise.f1", "loo_accuracy", "open_set_rejection", "threshold",
];

function systemSummary(system: Obj): Obj {
  const out: Obj = {};
  for (const [corpus, v] of Object.entries(system)) {
    if (!isObj(v)) continue;
    const g = (v.grouping ?? {}) as Obj;
    const loo = ((v.loo as Obj | undefined)?.summary ?? {}) as Obj;
    const os = ((v.open_set as Obj | undefined)?.summary ?? {}) as Obj;
    const hn = (v.hard_negatives ?? {}) as Obj;
    out[corpus] = { grouping: g, loo, open_set: os, hard_negatives: { pairs: hn.pairs, violations_campaign: hn.violations_campaign, violations_group: hn.violations_group } };
  }
  return out;
}

const SECTIONS: { key: string; title: string; note: string; render?: (v: unknown) => Obj | null; cols?: string[] }[] = [
  { key: "headline", title: "Najważniejsze wyniki", note: "per korpus: F1 grupowania, trafność przypisania (LOO), odrzucanie spoza zbioru, twarde negatywy" },
  { key: "system", title: "System: grupowanie, przypisanie, zbiór otwarty", note: "LOO = każdy incydent po kolei jako nowy materiał", render: (v) => (isObj(v) ? systemSummary(v) : null) },
  { key: "ladder", title: "Drabina metod (baseline → fuzja)", note: "każdy szczebel dodaje jeden element metody; ten sam podział", render: (v) => (isObj(v) ? ladderRows(v) : null), cols: LADDER_COLS },
  { key: "ablation", title: "Ablacja rodzin genów", note: "spadek po usunięciu rodziny = użyteczność cech (kryterium 30%)" },
  { key: "robustness", title: "Odporność: duplikaty, braki danych, formaty", note: "warianty z każdego incydentu próbki" },
  { key: "coverage", title: "Pokrycie rodzin genów", note: "udział incydentów z zaobserwowaną rodziną" },
  { key: "runtime", title: "Czas działania", note: "zmierzony w przebiegu ewaluacji" },
  { key: "crashes", title: "Błędy w przebiegu", note: "każdy wyjątek jest liczony, nie ukrywany" },
];

const SKIP = new Set(["meta", "sample_build", "system_alt", "synthetic", "core_sweep"]);

const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const get = (o: unknown, path: string): unknown => path.split(".").reduce<unknown>((x, k) => (isObj(x) ? x[k] : undefined), o);

function Charts({ m }: { m: Obj }) {
  const headline = (m.headline ?? {}) as Obj;
  const corpora = Object.keys(headline).filter((k) => isObj(headline[k]) && !k.includes(":"));
  const ladder = (m.ladder ?? {}) as Obj;
  const ladderCorpora = Array.from(new Set(Object.values(ladder).flatMap((v) => (isObj(v) ? Object.keys(v) : [])))).slice(0, 2);
  const ablation = (m.ablation ?? {}) as Obj;
  const abl = Object.entries(ablation).filter(([k, v]) => k.startsWith("-") && isObj(v));
  const rob = (m.robustness ?? {}) as Obj;
  const robRows = Object.entries(rob).filter(([, v]) => isObj(v));
  const curves = (m.threshold_curves ?? {}) as Obj;
  const thresholds = (m.thresholds ?? {}) as Obj;
  const COLS: [string, string][] = [["accuracy", "trafność"], ["agreement_with_undegraded", "zgodność z oryginałem"], ["mean_gene_stability", "stabilność genów"], ["linked_to_parent_rate", "łączy z rodzicem"]];
  const cell = (v: number | null) => v === null ? "bg-hover text-subtle" : v >= 0.95 ? "bg-for/35 font-semibold text-fg" : v >= 0.8 ? "bg-for/20 text-fg" : v >= 0.5 ? "bg-for/10 text-fg" : "bg-against-soft text-fg"; // text-fg on light tints: AA in both themes
  const tile = "surface-card p-5";
  return (
    <div className="space-y-4">
      {corpora.length > 0 && (
        <div className="grid gap-4 md:grid-cols-2">
          {corpora.map((c) => {
            const h = headline[c] as Obj;
            const items: [string, string, unknown][] = [["F1 par kampanii", "campaign_pairwise_f1", h.campaign_pairwise_f1], ["B³ F1", "campaign_bcubed_f1", h.campaign_bcubed_f1], ["przypisanie (LOO)", "loo_accuracy", h.loo_accuracy], ["odrzucanie spoza zbioru", "open_set_rejection", h.open_set_rejection], ["fałszywe scalenia", "false_merge_pairs", h.false_merge_pairs], ["twarde negatywy (naruszenia)", "hard_negative_violations", h.hard_negative_violations]];
            return (
              <section key={c} className={tile}>
                <h3 className="mb-3 text-sm font-semibold">Korpus <span className="font-mono">{c}</span></h3>
                <div className="grid grid-cols-3 gap-3">
                  {items.map(([label, key, v]) => <div key={label}><div className="text-xs text-subtle">{label}</div><div className="font-mono text-xl">{num(v) === null ? "—" : fmt(v, !COUNT_METRICS.has(key))}</div></div>)}
                </div>
              </section>
            );
          })}
        </div>
      )}
      <div className="grid gap-4 xl:grid-cols-2">
        {Object.keys(ladder).length > 0 && (
          <section className={tile}>
            <h3 className="mb-1 text-sm font-semibold">Drabina metod: F1 par kampanii</h3>
            <p className="mb-3 text-xs text-subtle">każdy szczebel dodaje element metody; ten sam podział danych</p>
            <BarList series={ladderCorpora.map((c, i) => ({ label: c, color: i === 0 ? "var(--color-for)" : "var(--color-fam-nar)" }))} max={1}
              rows={Object.entries(ladder).map(([name, v]) => ({ label: name, values: ladderCorpora.map((c) => num(get(v, `${c}.campaign_pairwise.f1`))) }))} />
          </section>
        )}
        {abl.length > 0 && (
          <section className={tile}>
            <h3 className="mb-1 text-sm font-semibold">Ablacja rodzin genów</h3>
            <p className="mb-3 text-xs text-subtle">spadek po usunięciu rodziny (korpus {String(ablation.corpus ?? "—")}); dwa wskaźniki, dwa wykresy</p>
            <div className="grid gap-4 md:grid-cols-2">
              <div><div className="mb-1 text-xs text-muted">Δ F1 kampanii</div><BarList series={[{ label: "Δ F1", color: "var(--color-for)" }]} rows={abl.map(([k, v]) => ({ label: k, values: [num((v as Obj).delta_campaign_f1)] }))} /></div>
              <div><div className="mb-1 text-xs text-muted">Δ trafności przypisania (LOO)</div><BarList series={[{ label: "Δ LOO", color: "var(--color-fam-vis)" }]} rows={abl.map(([k, v]) => ({ label: k, values: [num((v as Obj).delta_loo_accuracy)] }))} /></div>
            </div>
            <div className="mt-2 flex flex-wrap gap-2 text-xs text-subtle">{abl.map(([k]) => <FamChip key={k} family={k.slice(1)} withName />)}</div>
          </section>
        )}
      </div>
      {robRows.length > 0 && (
        <section className={tile}>
          <h3 className="mb-1 text-sm font-semibold">Odporność: duplikaty, braki danych, formaty</h3>
          <p className="mb-3 text-xs text-subtle">warianty zbudowane z każdego incydentu próbki; kolor = wartość (≥ 0,95 ciemny), — = nie mierzono</p>
          <div className="overflow-x-auto">
            <table className="w-full border-separate border-spacing-[2px] text-xs">
              <thead><tr><th className="text-left font-normal text-subtle">wariant</th><th className="font-normal text-subtle">n</th>{COLS.map(([, l]) => <th key={l} className="font-normal text-subtle">{l}</th>)}</tr></thead>
              <tbody>
                {robRows.map(([k, v]) => {
                  const o = v as Obj;
                  return (
                    <tr key={k}>
                      <td className="pr-3 font-mono">{k}</td>
                      <td className="text-center font-mono">{fmt(o.n)}</td>
                      {COLS.map(([key]) => { const x = num(o[key]); return <td key={key} className={cx("h-7 rounded-[3px] text-center font-mono", cell(x))}>{x === null ? "—" : plNumber(x, 3)}</td>; })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
      {Object.keys(curves).length > 0 && (
        <section className={tile}>
          <h3 className="mb-1 text-sm font-semibold">Wrażliwość na próg</h3>
          <p className="mb-3 text-xs text-subtle">F1 kampanii względem progu, osobno dla każdej metody (różne skale progów); przerywana linia = próg wybrany na zbiorze dev</p>
          <div className="grid grid-cols-[repeat(auto-fill,minmax(270px,1fr))] gap-4">
            {Object.entries(curves).map(([name, pts]) => (
              <MiniLine key={name} title={name} chosen={num(get(thresholds, `${name}.threshold`))}
                points={(Array.isArray(pts) ? pts : []).map((p) => ({ x: Number((p as Obj).threshold), y: Number((p as Obj).campaign_f1) })).filter((p) => Number.isFinite(p.x) && Number.isFinite(p.y))} />
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

export function MethodologyPage() {
  const q = useLatestRun("eval");
  const [tables, setTables] = useState(false);
  const run = q.data?.run;
  const metrics = (run?.metrics ?? {}) as Obj;
  const backend = String((metrics.meta as Obj | undefined)?.backend ?? (metrics.headline as Obj | undefined)?.backend ?? run?.params?.backend ?? "");
  const synthetic = Boolean(run?.params?.synthetic || metrics.synthetic);
  const stub = /stub/i.test(backend);
  const known = new Set(SECTIONS.map((s) => s.key));
  const rest = Object.fromEntries(Object.entries(metrics).filter(([k]) => !known.has(k) && !SKIP.has(k)));

  return (
    <Page title="Metodyka i wyniki" sub={run ? <>przebieg ewaluacji <span className="font-mono">{run.id}</span> · {formatDatePl(run.finished_at ?? run.created_at)}{backend && <> · backend: <span className="font-mono">{backend}</span></>}</> : undefined}>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-4 lg:grid-cols-[minmax(0,1fr)_220px]">
        <div className="sticky top-0 z-20 order-1 min-w-0 lg:static lg:order-2"><Toc items={TOC} /></div>
        <div className="order-2 flex min-w-0 flex-col gap-4 lg:order-1">
          <Overview />
          <Genome />
          <Rarity />
          <Hypotheses />
          <Grouping />
          <Assignment />
          <SmoothUnits />
          <Geometry />
          <QueueKontra />
          <MethodSection id="m-ewaluacja" n={10} title="Ewaluacja" lead={<>
            Progi są stałe i zamrożone na czas pomiaru (link 10, kit 6; ustalone podczas analizy próbki), a każda liczba poniżej pochodzi z ostatniego opublikowanego przebiegu ewaluacji (<span className="font-mono">GET /v1/runs/latest?kind=eval</span>). Uwaga: próbka to 10 incydentów NASK, przy domyślnych progach wyniki są in-sample; korpusy z dopiskiem „syn” to syntetyczne warianty tych 10 incydentów; etykiety to hipoteza zespołu, nie prawda organizatora.
          </>} tech={<>
            <p>Protokół: <span className="font-mono">docs/METHODOLOGY_EVAL.md</span>, <span className="font-mono">eval/run.py</span>. Grupowanie: pary i B³ P/R/F1 na poziomie kampanii i grupy. Przypisanie: leave-one-out (usuń incydent, przebuduj, przypisz z powrotem). Zbiór otwarty: odłóż wszystkich członków kampanii, każdy musi wrócić jako nowa kampania. Twarde negatywy analityków, ablacja rodzin, odporność (duplikaty, braki HAR / strony / kreacji, formaty). Podział <span className="font-mono">selective_risk</span> jest rozłączny względem kampanii; w LOO na <span className="font-mono">sample+syn</span> rodzic i rodzeństwo wariantu zostają w zbiorze (METHODOLOGY_EVAL §4e). <span className="font-mono">syn-dev</span> służy tylko do wyboru progów baseline'ów.</p>
          </>}>
          <div className="flex flex-col gap-3">
      {!q.data ? <QueryState query={q} what="wyniki ewaluacji" endpoint="/v1/runs/latest?kind=eval" /> : !run ? (
        <EmptyState title="Brak przebiegu ewaluacji" description={<>Wyniki pojawią się, gdy EVAL opublikuje metryki (<span className="font-mono">just eval-publish</span>, tabela runs, kind = eval). Nie pokazujemy liczb, których nie zmierzono.</>} />
      ) : (
        <>
          {synthetic && <div className="rounded-md border border-abstain bg-abstain-soft px-3 py-2 text-sm text-abstain-ink">Przebieg oznaczony jako syntetyczny (dane testowe interfejsu), nie wynik na danych NASK.</div>}
          {!stub && /core/i.test(backend) && <div className="rounded-md border border-ok/40 bg-panel px-3 py-2 text-sm text-fg">Metryki policzone na rdzeniu CORE (<span className="font-mono">{backend}</span>), na danych opisanych w parametrach przebiegu ewaluacji.</div>}
          {stub && <div className="rounded-md border border-abstain bg-abstain-soft px-3 py-2 text-sm text-abstain-ink">Backend ewaluacji to scorer zastępczy EVAL („stub”), nie rdzeń CORE: liczby opisują harness, nie system.</div>}
          <Charts m={metrics} />
          <button type="button" onClick={() => setTables(!tables)} className="text-sm text-accent hover:underline">{tables ? "Ukryj tabele danych" : "Pokaż tabele danych"}</button>
          {tables && SECTIONS.map((s) => {
            const raw = metrics[s.key];
            if (raw === undefined) return null;
            const data = s.render ? s.render(raw) : isObj(raw) ? (raw as Obj) : null;
            return (
              <Panel key={s.key} title={s.title} actions={<span className="text-xs text-subtle">{s.note}</span>} flush>
                {data ? <DictTable data={data} columns={s.cols} /> : <div className="px-3 py-2 font-mono text-sm">{fmt(raw)}</div>}
              </Panel>
            );
          })}
          {tables && Object.keys(rest).length > 0 && <Panel title="Pozostałe metryki" flush><DictTable data={rest} /></Panel>}
        </>
      )}
          </div>
          </MethodSection>
          <Limits />
        </div>
      </div>
    </Page>
  );
}
