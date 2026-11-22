/**
 * State of the open-set decision rule for one assignment, straight from Assignment.meta (hydra/assign.py
 * `explain()`): support of the best campaign vs the link threshold, bits missing to the threshold, margin #1 − #2
 * and whether the strong-evidence rule (operator evidence or two strong families) is met. Nothing is recomputed
 * here; fields absent on older runs show "—". The backend reason text is shown by the caller next to this.
 */
import type { AssignmentMeta, ConformalSet, PairScore, TopCandidate } from "../api/task";
import { pointerText } from "../lib/artifactEvidence";
import { cx } from "../lib/cx";
import { COVERAGE_LABEL, P_VALUE_LABEL, polishReason } from "../lib/displayPl";
import { familyLabel, formatBits, plNumber } from "../lib/evidence";
import { KindLabel } from "./evidence";

const DECIDED_BY: Record<string, string> = { threshold: "próg bitów", conformal: "zbiór konformalny" };

function bitsAbs(v: number | null | undefined): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "—" : `${plNumber(v, 1)} b`;
}

function RuleBadge({ ok, yes, no }: { ok: boolean | undefined; yes: string; no: string }) {
  if (ok === undefined) return <span className="text-subtle">—</span>;
  return (
    <span className={cx("inline-flex h-[20px] items-center gap-1 rounded-[3px] border px-1.5 text-xs font-semibold", ok ? "border-for bg-for-soft text-fg" : "border-against bg-against-soft text-fg")}>
      <span aria-hidden="true">{ok ? "✓" : "✕"}</span>{ok ? yes : no}
    </span>
  );
}

/** meta.retrieval_scope written by hydra/retrieval.py `query_scope` (U03 telemetry, no scoring effect). */
export interface RetrievalScope {
  mode?: string;
  truncated?: boolean;
  bounded_by?: string[] | string;
  holder_slots_not_reached?: number;
  postings_dropped?: Record<string, number>;
  postings_backstop_capped?: Record<string, number>;
  postings_stop?: number;
  [key: string]: unknown;
}

const MODE_PL: Record<string, string> = { all_pairs: "wszystkie pary (mały korpus, przegląd pełny)", indexed: "indeksy rzadkich cech" };
const BOUND_PL: Record<string, string> = {
  postings_over_max_df_dropped: "pominięte zbyt częste wartości",
  backstop_query_cap: "limit zapytań zapasowych",
  knn_top_k: "limit najbliższych sąsiadów (k-NN)",
  max_candidates: "limit liczby kandydatów",
};

function sumCounts(v: unknown): number {
  if (!v || typeof v !== "object") return 0;
  return Object.values(v as Record<string, unknown>).reduce<number>((a, x) => a + (typeof x === "number" && Number.isFinite(x) ? x : 0), 0);
}

export function asRetrievalScope(v: unknown): RetrievalScope | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as RetrievalScope) : null;
}

/** "Zakres wyszukiwania": which retrieval mode proposed the candidates and whether the set is a bounded sample. */
export function RetrievalScopeView({ scope, className }: { scope: RetrievalScope; className?: string }) {
  const bounded = scope.bounded_by === undefined || scope.bounded_by === null ? [] : Array.isArray(scope.bounded_by) ? scope.bounded_by : [String(scope.bounded_by)];
  return (
    <div className={cx("space-y-1 text-xs", className)} data-testid="retrieval-scope">
      <div className="font-semibold text-fg">Zakres wyszukiwania</div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-0.5">
        {typeof scope.mode === "string" && <><dt className="text-muted">tryb</dt><dd className="text-fg">{MODE_PL[scope.mode] ?? scope.mode}</dd></>}
        {typeof scope.truncated === "boolean" && <><dt className="text-muted">wyszukiwanie obcięte</dt><dd className="text-fg">{scope.truncated ? "tak" : "nie"}</dd></>}
        {bounded.length > 0 && <><dt className="text-muted">ograniczenia</dt><dd className="text-fg">{bounded.map((b) => BOUND_PL[b] ?? b).join(", ")}</dd></>}
        {typeof scope.holder_slots_not_reached === "number" && <><dt className="text-muted">nieporównani posiadacze wspólnych wartości</dt><dd className="font-mono text-fg">{scope.holder_slots_not_reached}</dd></>}
        {scope.postings_dropped !== undefined && <><dt className="text-muted">pominięte listy wartości</dt><dd className="font-mono text-fg">{sumCounts(scope.postings_dropped)}</dd></>}
        {scope.postings_backstop_capped !== undefined && <><dt className="text-muted">listy obcięte limitem</dt><dd className="font-mono text-fg">{sumCounts(scope.postings_backstop_capped)}</dd></>}
        {typeof scope.postings_stop === "number" && <><dt className="text-muted">pominięte wartości szablonowe</dt><dd className="font-mono text-fg">{scope.postings_stop}</dd></>}
      </dl>
      {scope.truncated === true && (
        <p className="text-fg" data-testid="retrieval-scope-truncated">
          Wyszukiwanie kandydatów było ograniczone: część incydentów dzielących z tym materiałem częste wartości nie została z nim porównana. Nie oznacza to, że pominięto kampanię, ale lista kandydatów jest próbką, a nie pełnym przeglądem.
        </p>
      )}
    </div>
  );
}

type Caveats = Pick<PairScore, "contradictions" | "missing_families">;

/** Caveats of the best pair: the PairScore of the top candidate, else the copy on `meta.top_candidates[0]`
 * (CR-UI-FINAL-4; also present when the assignment has no `candidates`). */
export function caveatsOf(pair: PairScore | null | undefined, top: TopCandidate | undefined): Caveats | null {
  if (pair) return pair;
  if (!top || (top.contradiction_list === undefined && top.missing_families === undefined)) return null;
  return { contradictions: (top.contradiction_list ?? []).map((c) => ({ ...c, family: c.family as PairScore["contradictions"][number]["family"] })) as PairScore["contradictions"], missing_families: top.missing_families ?? [] };
}

/** Evidence pointers of the best pair's strongest reasons (frame / region level where the incident has one). */
function EvidencePointers({ top }: { top: TopCandidate | undefined }) {
  const ev = (top?.evidence ?? []).filter((e) => e.evidence_a || e.evidence_b);
  if (!ev.length) return null;
  return (
    <div className="space-y-0.5 border-t border-line pt-2" data-testid="evidence-pointers">
      <div className="font-semibold text-fg">Gdzie leży dowód (najmocniejsze powody)</div>
      <ul className="space-y-0.5">
        {ev.map((e, i) => (
          <li key={`${e.kind}-${i}`} className="text-muted">
            <span className="text-fg">{familyLabel(e.family)}</span> · <KindLabel kind={e.kind} /> <span className="font-mono">({formatBits(e.bits)})</span>
            : <span className="font-mono">{top?.pair?.[0] ?? "A"}</span> {pointerText(e.evidence_a, e.kind) || "brak wskazania"} · <span className="font-mono">{top?.pair?.[1] ?? "B"}</span> {pointerText(e.evidence_b, e.kind) || "brak wskazania"}
            {e.refined && <span className="text-subtle"> (dokładniejsze wskazanie tej samej wartości)</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Contradictions and missing families of the best pair (PairScore of the top assignment candidate). */
function PairCaveats({ pair }: { pair: Caveats }) {
  const contra = pair.contradictions ?? [];
  const missing = pair.missing_families ?? [];
  if (!contra.length && !missing.length) return null;
  return (
    <div className="space-y-1 border-t border-line pt-2" data-testid="assignment-caveats">
      {contra.length > 0 && (
        <div>
          <div className="font-semibold text-fg">Sprzeczności najlepszej pary ({contra.length})</div>
          <ul className="space-y-0.5">{contra.map((m, i) => <li key={`${m.kind}-${i}`} className="text-muted"><span className="text-fg">{familyLabel(m.family)}</span>: {polishReason(m.explanation_pl)} <span className="font-mono">({formatBits(m.bits)})</span></li>)}</ul>
        </div>
      )}
      {missing.length > 0 && (
        <div><span className="text-muted">brakujące rodziny cech (nieznane, nie kara): </span><span className="text-fg">{missing.map(familyLabel).join(", ")}</span></div>
      )}
    </div>
  );
}

export function DecisionRule({ meta, bestPair, className }: { meta: AssignmentMeta | null | undefined; bestPair?: PairScore | null; className?: string }) {
  if (!meta) return null;
  const caveats = caveatsOf(bestPair, meta.top_candidates?.[0]);
  const scope = asRetrievalScope(meta.retrieval_scope);
  const noFeatures = meta.no_features === true;
  if (meta.link_threshold === undefined && meta.link_rule_met === undefined) {
    if (!noFeatures && !scope && !caveats) return null;
    return (
      <section className={cx("space-y-2 rounded-md border border-line bg-raised p-3 text-xs", className)} aria-label="Reguła decyzji" data-testid="decision-rule">
        {noFeatures && <p className="font-semibold text-fg" data-testid="no-features">brak cech — decyzja wstrzymana</p>}
        {caveats && <PairCaveats pair={caveats} />}
        {scope && <RetrievalScopeView scope={scope} className="border-t border-line pt-2" />}
      </section>
    );
  }
  const best = meta.top_candidates?.[0];
  const support = best?.support ?? null;
  const th = meta.link_threshold ?? null;
  const reached = support !== null && th !== null ? support >= th : undefined;
  const rule = meta.link_rule_met;
  const both = reached === undefined || rule === undefined ? undefined : reached && rule;
  const fams = Object.entries(best?.campaign_by_family ?? {}).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const scale = Math.max(1, th ?? 0, support ?? 0);
  return (
    <section className={cx("rounded-md border border-line bg-raised p-3 text-xs", className)} aria-label="Reguła decyzji" data-testid="decision-rule">
      {noFeatures && <p className="mb-2 font-semibold text-fg" data-testid="no-features">brak cech — decyzja wstrzymana</p>}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <h4 className="text-sm font-semibold text-fg">Reguła decyzji</h4>
        <RuleBadge ok={both} yes="przesłanki łączenia spełnione" no="przesłanki łączenia niespełnione" />
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5">
        <dt className="text-muted">1. Wsparcie vs próg</dt>
        <dd className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-fg">{bitsAbs(support)}</span>
            <span className="text-subtle">wobec progu</span>
            <span className="font-mono text-fg">{bitsAbs(th)}</span>
            <RuleBadge ok={reached} yes="próg osiągnięty" no="poniżej progu" />
          </div>
          {support !== null && th !== null && (
            <div className="relative h-2 max-w-xs rounded-full bg-hover" aria-hidden="true">
              <div className={cx("absolute inset-y-0 left-0 rounded-full", reached ? "bg-for" : "bg-against")} style={{ width: `${(100 * Math.max(0, support)) / scale}%` }} />
              <div className="absolute -top-1 -bottom-1 w-[2px] bg-fg" style={{ left: `${(100 * th) / scale}%` }} />
            </div>
          )}
          {best?.campaign_id && <div className="text-subtle">najlepsza kampania <span className="font-mono">{best.campaign_id}</span></div>}
        </dd>
        <dt className="text-muted">Brakuje do progu</dt>
        <dd className="font-mono text-fg">{meta.missing_bits_to_link === undefined || meta.missing_bits_to_link === null ? "—" : meta.missing_bits_to_link > 0 ? bitsAbs(meta.missing_bits_to_link) : "0 b (próg osiągnięty)"}</dd>
        <dt className="text-muted">2. Silny dowód</dt>
        <dd className="space-y-1">
          <RuleBadge ok={rule} yes="spełniony" no="niespełniony" />
          <div className="text-subtle">wymagany dowód operatora (np. identyfikator, tracker, identyczny plik) albo silny dowód kampanii w co najmniej dwóch rodzinach</div>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5">
            <span>operator: <span className="font-mono text-fg">{bitsAbs(best?.operator_bits)}</span></span>
            {fams.map(([f, v]) => <span key={f} title={f}><span>{familyLabel(f)}</span> <span className="font-mono text-fg">{bitsAbs(v)}</span></span>)}
          </div>
        </dd>
        <dt className="text-muted">Przewaga #1 nad #2</dt>
        <dd className="font-mono text-fg">{meta.margin_bits === undefined || meta.margin_bits === null ? "—" : formatBits(meta.margin_bits)}</dd>
      </dl>
      {reached && rule === false && (
        <p className="mt-2 text-fg">Wsparcie osiąga próg, ale brakuje silnego dowodu (przesłanka 2), więc reguła nie pozwala przypisać materiału do kampanii.</p>
      )}
      {caveats && <div className="mt-3"><PairCaveats pair={caveats} /></div>}
      <div className="mt-3"><EvidencePointers top={best} /></div>
      {meta.conformal && <ConformalList c={meta.conformal} className="mt-3 border-t border-line pt-2" />}
      {scope && <RetrievalScopeView scope={scope} className="mt-3 border-t border-line pt-2" />}
    </section>
  );
}

/** Conformal prediction set as a readable list (not raw JSON). */
export function ConformalList({ c, className }: { c: ConformalSet; className?: string }) {
  const set = c.prediction_set ?? [];
  const p = c.p_values ?? {};
  const others = Object.entries(p).filter(([k]) => !set.includes(k)).sort((a, b) => b[1] - a[1]);
  const excluded = c.excluded_by_evidence_gate ?? [];
  return (
    <div className={cx("space-y-1 text-xs", className)} data-testid="conformal-set">
      <div className="font-semibold text-fg">Zbiór predykcji konformalnej</div>
      <div className="text-muted">
        {c.method ? <span className="font-mono">{c.method}</span> : null}
        {c.eps !== undefined && <> · ε = {plNumber(c.eps, 2)}</>}
        {c.n_calibration !== undefined && <> · kalibracja n = {c.n_calibration}</>}
        {c.decided_by && <> · decyduje: {DECIDED_BY[c.decided_by] ?? c.decided_by}</>}
        {c.usable !== undefined && <> · {c.usable ? "zbiór używany do decyzji" : "zbiór tylko raportowany"}</>}
      </div>
      {set.length ? (
        <ul className="space-y-0.5" aria-label="Kampanie w zbiorze predykcji">
          {set.map((id) => <li key={id} className="flex gap-2"><span className="font-mono text-fg">{id}</span>{p[id] !== undefined && <span className="text-muted">{P_VALUE_LABEL} = {plNumber(p[id], 2)}</span>}</li>)}
        </ul>
      ) : <div className="text-muted">Zbiór pusty: żadna kampania nie przechodzi testu (nowy materiał).</div>}
      {others.length > 0 && (
        <details>
          <summary className="cursor-pointer text-muted">poza zbiorem: {others.length}</summary>
          <ul className="mt-1 space-y-0.5">{others.map(([id, v]) => <li key={id} className="flex gap-2"><span className="font-mono">{id}</span><span className="text-muted">{P_VALUE_LABEL} = {plNumber(v, 2)}</span></li>)}</ul>
        </details>
      )}
      {c.empirical_coverage !== undefined && c.empirical_coverage !== null && <div className="text-muted" data-testid="conformal-coverage">{COVERAGE_LABEL}: <span className="font-mono text-fg">{plNumber(c.empirical_coverage, 2)}</span></div>}
      {excluded.length > 0 && <div className="text-muted">wykluczone bramką dowodów: <span className="font-mono">{excluded.join(", ")}</span></div>}
      {c.note_pl && <p className="text-subtle">{c.note_pl}</p>}
    </div>
  );
}
