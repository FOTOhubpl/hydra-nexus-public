/**
 * "Poszukiwanie wariantów" (HUNT, INNOWACJE §3-§5, §17): hypothesis cards issued BEFORE the later material,
 * a replay time slider, hit and miss hypotheses, a counterexample and metrics against same-budget baselines.
 * Every number comes from GET /v1/hunt/replay and /v1/hunt/metrics; nothing is invented on the client.
 */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../../api/client";
import { Badge, type BadgeVariant } from "../../components/Badge";
import { Button } from "../../components/Button";
import { Panel } from "../../components/Panel";
import { Page, QueryState } from "./common";

type Status = "pending" | "confirmed" | "refuted" | "unverifiable";

interface Check {
  observation_id: string;
  ref: string;
  unit?: string;
  available_at: string;
  verdict: string;
  reason_pl?: string;
  matched_invariants?: string[];
  missing_groups?: string[];
  conflicts?: string[];
  decoration?: string[];
  truth?: string;
  mutation?: string;
}

interface Result {
  hypothesis_id: string;
  status: Status;
  reason_pl: string;
  first_confirming: Check | null;
  confirming: string[];
  counts: Record<string, number>;
  coverage_complete: boolean;
}

interface SigGene {
  kind: string;
  value: string;
  role: string;
  tier: string;
  stability: number;
  rarity: number;
  reliability: number;
  utility: number;
  background_df: number;
  background_n: number;
  background_basis: string;
}

interface Hypothesis {
  hypothesis_id: string;
  campaign_id: string;
  campaign_revision: string;
  issued_at: string;
  knowledge_cutoff: string;
  horizon_end: string;
  horizon_hours: number;
  statement_pl: string;
  invariants: string[];
  allowed_mutations: string[];
  required_evidence_groups: string[];
  sources: string[];
  query_budget: number;
  status: Status;
  corpus_manifest_sha256: string;
  signature_sha256: string;
  dataset_kind: string;
  mode: string;
  versions: Record<string, string>;
}

interface Card {
  hypothesis: Hypothesis;
  signature: { genes: SigGene[]; approval_basis: string; mutations_basis: string; members: string[] };
  evidence_before: { ref: string; available_at: string; basis?: string }[];
  missing_evidence?: string[];
  later_observation?: { ref: string; available_at: string; available_at_basis: string; event_time?: string } | null;
  checks?: Check[];
  next_actions?: NextStep[];
  result: Result;
}

interface NextStep {
  ref: string;
  before: Check;
  after: Check | null;
  executed: { action: string; label_pl: string; resolving_power: string; availability: string; cost: string; priority: number; revealed: string[] } | null;
  ranking: { action: string; label_pl: string; resolving_power: string; availability: string; cost: string; priority: number }[];
  decision_changed: boolean;
}

interface StrategyMetrics {
  issued: number;
  budget_k: number;
  confirmed: number;
  refuted: number;
  unverifiable: number;
  pending: number;
  precision_at_k: { lower: number; upper: number; point: number | null };
  confirmed_new_items: number;
  confirmed_per_100_queries: number | null;
  lead_time_h: { reference_source: string; n: number; median: number | null };
}

interface Report {
  available: boolean;
  note_pl?: string;
  generated_at?: string;
  REAL: {
    status?: string;
    reason?: string;
    source?: string;
    cutoffs?: string[];
    horizon_hours?: number;
    budget_k_per_cutoff?: number;
    time_basis?: string;
    campaign_notion?: string;
    metrics?: Record<string, StrategyMetrics>;
    cases?: Card[];
    counterexamples?: { hypothesis_id: string; template: string; observation: Check }[];
    ledger?: { ok: boolean; records: number };
  };
  SYNTHETIC: {
    note: string;
    hypotheses: StrategyMetrics;
    candidates: Record<string, { k: number; hits: number; precision_at_k: number | null; flagged: string[] }>;
    cards: Card[];
    ledger: { ok: boolean; records: number };
  };
  rules?: string[];
}

interface LiveList {
  items: { hypothesis_id: string; campaign_id: string; issued_at: string; status: Status; statement_pl: string }[];
  total: number;
}

const STATUS_PL: Record<Status, string> = { pending: "oczekuje", confirmed: "potwierdzona", refuted: "obalona", unverifiable: "nieweryfikowalna" };
const STATUS_VARIANT: Record<Status, BadgeVariant> = { pending: "info", confirmed: "success", refuted: "danger", unverifiable: "warning" };
const STRATEGY_PL: Record<string, string> = { hunt: "HUNT (odcisk + aktywność generatora)", volume: "baseline: wolumen", brand: "baseline: marka", brand_baseline: "baseline: marka" };

const ms = (s: string) => new Date(s).getTime();
const fmt = (s?: string | null) => (s ? new Date(s).toISOString().replace("T", " ").slice(0, 16) + " UTC" : "-");

function statusAt(card: Card, t: number): Status {
  const h = card.hypothesis;
  const first = card.result.first_confirming;
  if (first && ms(first.available_at) <= t) return "confirmed";
  if (t < ms(h.horizon_end)) return "pending";
  return card.result.status;
}

function StatusBadge({ status }: { status: Status }) {
  return <Badge variant={STATUS_VARIANT[status]} dot>{STATUS_PL[status]}</Badge>;
}

export function MetricsTable({ rows }: { rows: [string, StrategyMetrics][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-left text-xs">
        <thead className="text-subtle">
          <tr>
            <th className="py-1 pr-2">strategia</th><th className="pr-2">K</th><th className="pr-2">potw.</th><th className="pr-2">obal.</th>
            <th className="pr-2">nierozstrz.</th><th className="pr-2">precision@K (dolna-górna)</th><th className="pr-2">nowe potw. / 100 zapytań</th><th title="Czas dostępności listy CERT jest założony (+5 min), nie zmierzony.">mediana wyprzedzenia wobec listy CERT w odtworzeniu kontrfaktycznym (założony czas dostępności +5 min) [h]</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([name, m]) => (
            <tr key={name} className="border-t border-line">
              <td className="py-1 pr-2">{STRATEGY_PL[name] ?? name}</td>
              <td className="pr-2 font-mono">{m.budget_k}</td>
              <td className="pr-2 font-mono">{m.confirmed}</td>
              <td className="pr-2 font-mono">{m.refuted}</td>
              <td className="pr-2 font-mono">{m.pending + m.unverifiable}</td>
              <td className="pr-2 font-mono">{m.precision_at_k.point ?? `${m.precision_at_k.lower} - ${m.precision_at_k.upper}`}</td>
              <td className="pr-2 font-mono">{m.confirmed_per_100_queries ?? "-"}</td>
              <td className="font-mono">{m.lead_time_h.median ?? "-"} <span className="text-subtle">(n={m.lead_time_h.n})</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function HypothesisCard({ card, t }: { card: Card; t: number }) {
  const h = card.hypothesis;
  const st = statusAt(card, t);
  const issued = ms(h.issued_at) <= t;
  const later = card.later_observation;
  const laterVisible = later && ms(later.available_at) <= t;
  const inv = card.signature.genes.filter((g) => h.invariants.includes(`${g.kind}=${g.value}`));
  const deco = card.signature.genes.filter((g) => g.role === "decoration").slice(0, 4);
  return (
    <article className="rounded-[6px] border border-line bg-raised p-3 text-sm" aria-label={`Hipoteza ${h.hypothesis_id}`}>
      <header className="mb-2 flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs">{h.hypothesis_id}</span>
        <Badge variant="neutral">{h.dataset_kind}</Badge>
        <Badge variant="neutral">{h.mode}</Badge>
        {issued ? <StatusBadge status={st} /> : <Badge variant="neutral">jeszcze nie wydana</Badge>}
        <span className="ml-auto text-xs text-subtle">wydana {fmt(h.issued_at)} · horyzont do {fmt(h.horizon_end)}</span>
      </header>
      <p className="mb-2">{h.statement_pl}</p>
      <div className="grid gap-2 md:grid-cols-2">
        <section>
          <h4 className="text-xs font-semibold text-muted">Niezmienniki (użyteczność = stabilność × rzadkość × wiarygodność; heurystyka, nie prawdopodobieństwo)</h4>
          <ul className="text-xs">
            {inv.map((g) => (
              <li key={g.kind + g.value} className="font-mono">
                {g.kind}={g.value} <span className="text-subtle">· {g.stability} × {g.rarity} × {g.reliability} = {g.utility} · tło {g.background_df}/{g.background_n} ({g.background_basis})</span>
              </li>
            ))}
          </ul>
          <h4 className="mt-1 text-xs font-semibold text-muted">Dekoracja (może się zmieniać, nie potwierdza)</h4>
          <div className="text-xs font-mono">{deco.map((g) => `${g.kind}=${g.value}`).join(", ") || "-"}</div>
          <div className="text-xs text-subtle">Dozwolone mutacje: {h.allowed_mutations.join(", ")} ({card.signature.mutations_basis})</div>
        </section>
        <section>
          <h4 className="text-xs font-semibold text-muted">Dowody przed prognozą (dostępne ≤ {fmt(h.knowledge_cutoff)})</h4>
          <ul className="text-xs font-mono">
            {card.evidence_before.slice(0, 6).map((e) => <li key={e.ref}>{e.ref} <span className="text-subtle">{fmt(e.available_at)}</span></li>)}
          </ul>
          <h4 className="mt-1 text-xs font-semibold text-muted">Brakujący dowód</h4>
          <div className="text-xs">{(card.missing_evidence ?? h.required_evidence_groups).length && st !== "confirmed" ? `grupy: ${h.required_evidence_groups.join(", ")} w nowym materiale` : "brak"}</div>
          <h4 className="mt-1 text-xs font-semibold text-muted">Późniejsza obserwacja</h4>
          <div className="text-xs">
            {laterVisible && later ? (
              <span><span className="font-mono">{later.ref}</span> · dostępna {fmt(later.available_at)} <span className="text-subtle">({later.available_at_basis})</span></span>
            ) : issued ? "jeszcze nie odtworzona w replay" : "-"}
          </div>
        </section>
      </div>
      <footer className="mt-2 border-t border-line pt-2 text-xs">
        <div><b>Wynik:</b> {issued && st === card.result.status ? card.result.reason_pl : issued ? "w toku odtwarzania" : "-"}</div>
        <div className="text-subtle">
          Niepewność: ranking to priorytet heurystyczny; rodzina nazw to hipoteza wzorca, nie dowód jednego operatora; brak wyniku w źródle nie jest dowodem przeciw.
        </div>
        <div className="font-mono text-subtle">
          wersje: {Object.entries(h.versions).map(([k, v]) => `${k}=${v}`).join(" · ")} · manifest {h.corpus_manifest_sha256.slice(0, 12)} · rewizja {h.campaign_revision}
        </div>
      </footer>
    </article>
  );
}

function NextActionView({ step }: { step: NextStep }) {
  return (
    <div className="rounded-[6px] border border-line p-2 text-xs">
      <div className="font-mono">{step.ref}</div>
      <div>przed: <b>{step.before.verdict}</b> → czynność: <b>{step.executed?.label_pl ?? "brak"}</b> → po: <b>{step.after?.verdict ?? "-"}</b> {step.decision_changed && <Badge variant="info">decyzja zmieniona</Badge>}</div>
      <div className="overflow-x-auto overscroll-x-contain"><table className="mt-1 w-full text-left">
        <thead className="text-subtle"><tr><th>czynność</th><th>siła</th><th>dostępność</th><th>koszt</th><th>priorytet</th></tr></thead>
        <tbody>
          {step.ranking.map((r) => (
            <tr key={r.action} className="border-t border-line"><td>{r.label_pl}</td><td>{r.resolving_power}</td><td>{r.availability}</td><td>{r.cost}</td><td className="font-mono">{r.priority}</td></tr>
          ))}
        </tbody>
      </table></div>
      <div className="text-subtle">Reguła: priorytet = siła rozstrzygnięcia × dostępność / koszt (kategorie, bez udawanego VOI).</div>
    </div>
  );
}

export function HuntPage() {
  const client = useQueryClient();
  const report = useQuery({ queryKey: ["hunt", "replay"], queryFn: ({ signal }) => apiFetch<Report>("/v1/hunt/replay", { signal }) });
  const live = useQuery({ queryKey: ["hunt", "live"], queryFn: ({ signal }) => apiFetch<LiveList>("/v1/hypotheses", { signal }) });
  const rebuild = useMutation({
    mutationFn: () => apiFetch<{ available: boolean }>("/v1/hunt/replay", { method: "POST" }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["hunt"] }),
  });
  const r = report.data;
  const cases = useMemo(() => r?.REAL?.cases ?? [], [r]);
  const [lo, hi] = useMemo(() => {
    if (!cases.length) return [0, 0];
    return [Math.min(...cases.map((c) => ms(c.hypothesis.issued_at))), Math.max(...cases.map((c) => ms(c.hypothesis.horizon_end)))];
  }, [cases]);
  const [pos, setPos] = useState<number | null>(null);
  // default: the last moment at which every visible hypothesis is already resolved (shows hits AND misses)
  const settled = useMemo(() => {
    const done = cases.filter((c) => c.result.status === "refuted" || c.result.status === "unverifiable").map((c) => ms(c.hypothesis.horizon_end));
    return done.length ? Math.max(...done) : hi;
  }, [cases, hi]);
  const t = pos ?? settled;
  const visible = cases.filter((c) => ms(c.hypothesis.issued_at) <= t && ms(c.hypothesis.issued_at) > t - 24 * 3600 * 1000 * 2);
  const hits = visible.filter((c) => c.result.status === "confirmed").slice(0, 2);
  const misses = visible.filter((c) => c.result.status !== "confirmed")
    .sort((a, b) => Number(a.result.status === "pending") - Number(b.result.status === "pending")).slice(0, 2);

  return (
    <Page
      title="Poszukiwanie wariantów"
      sub="hipoteza zapisana przed odsłonięciem materiału → celowy dowód → weryfikacja; REAL, SYNTHETIC i TEMPORAL REPLAY osobno"
      actions={<Button onClick={() => rebuild.mutate()} disabled={rebuild.isPending}>{rebuild.isPending ? "Przeliczanie…" : "Przelicz replay (offline)"}</Button>}
    >
      {!r ? <QueryState query={report} what="raport replay" endpoint="/v1/hunt/replay" /> : !r.available ? (
        <Panel title="Brak raportu"><div className="text-sm text-subtle">{r.note_pl}</div></Panel>
      ) : (
        <>
          <Panel title="TEMPORAL REPLAY · REAL · lista ostrzeżeń CERT Polska (nagranie strumienia)">
            {r.REAL.status === "unavailable" ? <div className="text-sm text-subtle">{r.REAL.reason}</div> : (
              <div className="space-y-2 text-sm">
                <div className="text-xs text-subtle">
                  Punkty odcięcia: {r.REAL.cutoffs?.length} · horyzont {r.REAL.horizon_hours} h · budżet K = {r.REAL.budget_k_per_cutoff} hipotez na punkt · {r.REAL.time_basis}. {r.REAL.campaign_notion}.
                  Źródło odniesienia wyprzedzenia: {r.REAL.source}. Rejestr: {r.REAL.ledger?.ok ? "łańcuch hashy poprawny" : "BŁĄD łańcucha"} ({r.REAL.ledger?.records} wpisów).
                </div>
                {r.REAL.metrics && <MetricsTable rows={Object.entries(r.REAL.metrics)} />}
              </div>
            )}
          </Panel>

          {cases.length > 0 && (
            <Panel title="Odtwarzanie w czasie: hipotezy trafione i nietrafione">
              <label className="flex items-center gap-3 text-xs">
                <span>czas odtwarzania</span>
                <input type="range" min={lo} max={hi} step={3600 * 1000} value={t} onChange={(e) => setPos(Number(e.target.value))} className="flex-1" aria-label="Czas odtwarzania" />
                <span className="font-mono">{fmt(new Date(t).toISOString())}</span>
              </label>
              <div className="mt-2 grid gap-3 xl:grid-cols-2">
                {[...hits, ...misses].map((c) => <HypothesisCard key={c.hypothesis.hypothesis_id} card={c} t={t} />)}
                {hits.length + misses.length === 0 && <div className="text-xs text-subtle">Brak hipotez wydanych w ostatnich 48 h czasu replay.</div>}
              </div>
            </Panel>
          )}

          <Panel title="Kontrprzykład: ta sama marka, inny generator / operator - nie powiązano">
            <ul className="space-y-1 text-xs">
              {(r.REAL.counterexamples ?? []).slice(0, 3).map((c) => (
                <li key={c.hypothesis_id + c.observation.ref}>
                  <span className="font-mono">{c.observation.ref}</span> vs hipoteza <span className="font-mono">{c.template}</span>: {c.observation.reason_pl}
                </li>
              ))}
              {r.SYNTHETIC.cards[0]?.checks?.filter((c) => c.verdict === "conflict").slice(0, 2).map((c) => (
                <li key={c.ref}><Badge variant="neutral">SYNTHETIC</Badge> <span className="font-mono">{c.ref}</span> ({c.mutation}): {c.reason_pl}</li>
              ))}
            </ul>
          </Panel>

          <Panel title="SYNTHETIC · test mechanizmu (nie jest wynikiem skuteczności)">
            <div className="space-y-2 text-sm">
              <div className="text-xs text-subtle">{r.SYNTHETIC.note}</div>
              <MetricsTable rows={[["hipotezy", r.SYNTHETIC.hypotheses]]} />
              <div className="text-xs">
                Kandydaci przy tym samym budżecie K: {Object.entries(r.SYNTHETIC.candidates).map(([k, v]) => `${STRATEGY_PL[k] ?? k}: ${v.hits}/${v.k} (${v.flagged.join(", ")})`).join(" · ")}
              </div>
              <h3 className="text-xs font-semibold text-muted">Następny dowód: wykonana czynność i zmiana decyzji</h3>
              <div className="grid gap-2 xl:grid-cols-2">
                {r.SYNTHETIC.cards.flatMap((c) => c.next_actions ?? []).map((s) => <NextActionView key={s.ref} step={s} />)}
              </div>
              <div className="grid gap-3 xl:grid-cols-2">
                {r.SYNTHETIC.cards.map((c) => <HypothesisCard key={c.hypothesis.hypothesis_id} card={c} t={Number.MAX_SAFE_INTEGER} />)}
              </div>
            </div>
          </Panel>
        </>
      )}

      <Panel title="LIVE · hipotezy na lokalnym magazynie">
        {!live.data ? <QueryState query={live} what="hipotezy" endpoint="/v1/hypotheses" /> : live.data.total === 0 ? (
          <div className="text-xs text-subtle">Brak hipotez LIVE. Wydaj je przez POST /v1/hypotheses dla zatwierdzonej kampanii.</div>
        ) : (
          <ul className="space-y-1 text-xs">
            {live.data.items.map((h) => (
              <li key={h.hypothesis_id} className="flex flex-wrap items-center gap-2">
                <span className="font-mono">{h.hypothesis_id}</span><StatusBadge status={h.status} /><span>{h.campaign_id}</span><span className="text-subtle">{fmt(h.issued_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </Page>
  );
}
