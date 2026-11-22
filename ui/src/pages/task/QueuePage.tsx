import { useEffect, useState } from "react";
import { READ_ONLY_TIP, useCanWrite } from "../../api/auth";
import { Link, useNavigate } from "react-router";
import {
  useIncident,
  useMarkUnsure,
  usePair,
  useReview,
  useReviewQueue,
  useUndoUnsure,
  type QueueCandidate,
  type QueueItem,
  type QueueKind,
  type QueueOrder,
  type UnsureInput,
  type UnsureMark,
} from "../../api/task";
import { FamChip, GeneValue, HypothesisBadge, KindLabel, ReviewBadge, SectionNote, Thumb, Waterfall } from "../../components/evidence";
import { Panel } from "../../components/Panel";
import { ReviewEffectNote } from "../../components/ReviewEffectNote";
import { cx } from "../../lib/cx";
import { formatBits, formatDatePl, plNumber } from "../../lib/evidence";
import { CampaignLink, IncidentLink, NoRun, Page, pairPath, QueryState } from "./common";
import { polishReason } from "../../lib/displayPl";

export const QUEUE_KIND_LABEL: Record<QueueKind, string> = {
  bridge: "most",
  near_threshold: "blisko progu",
  abstain: "wstrzymane",
  conflict: "konflikt",
};

const KIND_TITLE: Record<QueueKind, string> = {
  bridge: "powiązanie, którego usunięcie dzieli kampanię",
  near_threshold: "suma bitów blisko progu powiązania",
  abstain: "rdzeń wstrzymał decyzję przypisania",
  conflict: "sprzeczne dowody lub sprzeczna decyzja",
};

const NOTE = "kolejka analityka";

function KindBadge({ kind }: { kind: QueueKind }) {
  return (
    <span
      title={KIND_TITLE[kind]}
      className={cx(
        "inline-flex h-[18px] items-center rounded-[3px] border px-1 text-2xs font-medium whitespace-nowrap",
        kind === "conflict" ? "border-against bg-against-soft text-fg" : kind === "abstain" ? "hatch-abstain border-abstain text-abstain-ink" : "border-abstain bg-abstain-soft text-abstain-ink",
      )}
    >
      <span className={kind === "abstain" ? "rounded-sm bg-abstain-soft px-0.5" : undefined}>{QUEUE_KIND_LABEL[kind] ?? kind}</span>
    </span>
  );
}

/** "12,3 bit (próg 10)". */
export function bitsVsThreshold(bits: number | null, threshold: number | null): string {
  const b = `${bits === null ? "—" : plNumber(bits, 1)} bit`;
  return threshold === null ? b : `${b} (próg ${plNumber(threshold, Number.isInteger(threshold) ? 0 : 1)})`;
}

function isPair(it: QueueItem): it is QueueItem & { a: string; b: string } {
  return Boolean(it.a && it.b);
}

function itemTitle(it: QueueItem): string {
  return isPair(it) ? `${it.a} ↔ ${it.b}` : it.incident_id ?? it.key;
}

function unsureTarget(it: QueueItem): UnsureInput["target"] | null {
  if (isPair(it)) return { a: it.a, b: it.b };
  if (it.incident_id) return { incident_id: it.incident_id };
  return null;
}

function markLabel(m: UnsureMark): string {
  if (m.target.a && m.target.b) return `${m.target.a} ↔ ${m.target.b}`;
  return m.target.incident_id ?? m.id;
}

function CandidateColumn({ c, label }: { c: QueueCandidate | undefined; label: string }) {
  return (
    <div className="min-w-0 rounded-md border border-line p-2 text-xs">
      <div className="mb-1 text-2xs font-semibold tracking-wider text-subtle uppercase">{label}</div>
      {!c ? <div className="text-subtle">Brak kandydata.</div> : (
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2"><CampaignLink id={c.campaign_id} /><span className="font-mono">{formatBits(c.bits)}</span></div>
          <div className="text-muted">wspierający członkowie: {c.supporting_members}</div>
          {c.top_reasons.length === 0 ? <div className="text-subtle">Brak wspólnych genów.</div> : (
            <ul className="space-y-1">
              {c.top_reasons.map((r, i) => (
                <li key={i} className="flex min-w-0 items-center gap-1.5" title={r.explanation_pl}>
                  <FamChip family={r.family} /><KindLabel kind={r.kind} className="text-muted" />
                  <span className="min-w-0 flex-1 truncate"><GeneValue value={r.value} max={40} /></span>
                  <span className="font-mono">{formatBits(r.bits)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

/** Assignment abstain: campaign A evidence | the incident | campaign B evidence. */
function AbstainCard({ item }: { item: QueueItem }) {
  const inc = useIncident(item.incident_id ?? undefined);
  const cands = item.candidates ?? [];
  return (
    <div className="grid gap-2 md:grid-cols-3" data-testid="abstain-card">
      <CandidateColumn c={cands[0]} label="Kampania A: dowody" />
      <div className="min-w-0 rounded-md border border-line-strong p-2 text-xs">
        <div className="mb-1 text-2xs font-semibold tracking-wider text-subtle uppercase">Incydent</div>
        {item.incident_id && <IncidentLink id={item.incident_id} />}
        {inc.data && (
          <div className="mt-1.5 space-y-1">
            <Thumb artifactId={inc.data.thumbnail} alt={inc.data.title ?? inc.data.id} size={120} />
            <div className="text-muted">{inc.data.title ?? "bez tytułu"}</div>
          </div>
        )}
        {item.incident_id && <Link className="mt-1.5 inline-block text-accent hover:underline" to={`/incident/${encodeURIComponent(item.incident_id)}`}>strona incydentu (Enter) →</Link>}
      </div>
      <CandidateColumn c={cands[1]} label="Kampania B: dowody" />
    </div>
  );
}

function PairEvidence({ a, b }: { a: string; b: string }) {
  const pair = usePair(a, b);
  const s = pair.data?.score;
  if (!s) return <QueryState query={pair} what="parę" />;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-3">
        <HypothesisBadge value={s.hypothesis} />
        <ReviewBadge value={pair.data?.review} />
        <span className="text-sm text-muted">suma {formatBits(s.total_bits)}</span>
      </div>
      <Waterfall familyBits={s.family_bits} totalBits={s.total_bits} missing={Array.from(new Set([...(s.unknown_families ?? []), ...s.missing_families]))} reasons={s.reasons} contradictions={s.contradictions} />
    </div>
  );
}

type Last = { kind: "review" | "unsure"; id: string } | null;

const ORDER_LABEL: Record<QueueOrder, string> = { gain: "zysk informacji / wysiłek", ev: "wartość decyzji (niepewność × wpływ)" };

/** "Dlaczego to pierwsze": counterfactual effect of confirm/reject and the rank reason from the server. */
function WhyFirst({ item, first }: { item: QueueItem; first: boolean }) {
  if (!item.why_first_pl) return null;
  return (
    <div className="rounded-md border border-line bg-raised p-2 text-xs" data-testid="why-first">
      <div className="mb-0.5 font-semibold text-fg">{first ? "Dlaczego to pierwsze" : "Dlaczego ta pozycja"}</div>
      <p className="text-muted">{item.why_first_pl}</p>
    </div>
  );
}

/**
 * Analyst queue (ADOPT #14): bridges, near-threshold links, assignment abstains and conflicts, ordered by the server
 * by the heuristic value of a decision. Keys: J/K next/previous, A accept, R reject, N unsure, U undo last,
 * Enter full evidence. Accept/reject are reviews (POST /v1/reviews); unsure marks (POST /v1/review/unsure) only take
 * the item off the queue and do not change campaigns. Every action is an audit event.
 */
export function QueuePage() {
  const [order, setOrder] = useState<QueueOrder | undefined>(undefined);
  const q = useReviewQueue(200, order);
  const review = useReview();
  const unsure = useMarkUnsure();
  const undoUnsure = useUndoUnsure();
  const navigate = useNavigate();
  const [tab, setTab] = useState<"queue" | "unsure">("queue");
  const [idx, setIdx] = useState(0);
  const [last, setLast] = useState<Last>(null);
  const items = q.data?.items ?? [];
  const cur = items[Math.min(idx, Math.max(0, items.length - 1))];
  const canWrite = useCanWrite();
  const busy = review.isPending || unsure.isPending || undoUnsure.isPending || !canWrite;

  const decide = (action: "accept_link" | "reject_link") => {
    if (!cur || !isPair(cur) || busy) return;
    review.mutate({ action, target: { a: cur.a, b: cur.b }, note: NOTE }, { onSuccess: (res) => setLast({ kind: "review", id: res.review.id }) });
  };
  const markUnsure = () => {
    const target = cur ? unsureTarget(cur) : null;
    if (!target || busy) return;
    unsure.mutate({ target, note: NOTE }, { onSuccess: (res) => setLast({ kind: "unsure", id: res.mark.id }) });
  };
  const undoLast = () => {
    if (!last || busy) return;
    if (last.kind === "review") review.mutate({ action: "undo", undoes: last.id }, { onSuccess: () => setLast(null) });
    else undoUnsure.mutate(last.id, { onSuccess: () => setLast(null) });
  };
  const open = () => {
    if (!cur) return;
    if (isPair(cur)) navigate(pairPath(cur.a, cur.b));
    else if (cur.incident_id) navigate(`/incident/${encodeURIComponent(cur.incident_id)}`);
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
      if (e.metaKey || e.ctrlKey || e.altKey || tab !== "queue") return;
      const k = e.key.toLowerCase();
      if (k === "j") setIdx((i) => Math.min(i + 1, items.length - 1));
      else if (k === "k") setIdx((i) => Math.max(i - 1, 0));
      else if (k === "a") decide("accept_link");
      else if (k === "r") decide("reject_link");
      else if (k === "n") markUnsure();
      else if (k === "u") undoLast();
      else if (e.key === "Enter") open();
      else if (k === "o") { setOrder((o) => ((o ?? q.data?.order ?? "gain") === "gain" ? "ev" : "gain")); setIdx(0); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  if (!q.data) return <div className="p-4"><QueryState query={q} what="kolejkę" endpoint="/v1/review/queue" /></div>;
  if (q.data.run_id === null) return <div className="p-4"><NoRun what="Kolejka analityka" /></div>;
  const d = q.data;
  const err = [review, unsure, undoUnsure].find((m) => m.isError)?.error;

  return (
    <Page
      title="Kolejka analityka"
      sub={<>{items.length} do decyzji · pominięte: rozstrzygnięte {d.skipped.decided}, niepewne {d.skipped.unsure}, decyzje między kampaniami {d.skipped.decided_between_campaigns} · klawisze <kbd className="font-mono">J/K</kbd> następna/poprzednia, <kbd className="font-mono">A</kbd> potwierdź, <kbd className="font-mono">R</kbd> odrzuć, <kbd className="font-mono">N</kbd> niepewne, <kbd className="font-mono">U</kbd> cofnij, <kbd className="font-mono">Enter</kbd> pełny dowód, <kbd className="font-mono">O</kbd> zmień kolejność</>}
    >
      <div role="tablist" aria-label="Widok kolejki" className="flex gap-1">
        <button type="button" role="tab" aria-selected={tab === "queue"} onClick={() => setTab("queue")} className={cx("h-8 rounded-[4px] border px-3 text-sm", tab === "queue" ? "border-line-strong bg-active font-semibold" : "border-line hover:bg-hover")}>Do decyzji ({items.length})</button>
        <button type="button" role="tab" aria-selected={tab === "unsure"} onClick={() => setTab("unsure")} className={cx("h-8 rounded-[4px] border px-3 text-sm", tab === "unsure" ? "border-line-strong bg-active font-semibold" : "border-line hover:bg-hover")}>Niepewne (N) ({d.unsure.length})</button>
      </div>
      <div className="flex flex-wrap items-center gap-1 text-xs" role="group" aria-label="Kolejność kolejki">
        <span className="text-subtle">kolejność:</span>
        {(["gain", "ev"] as const).map((o) => (
          <button key={o} type="button" aria-pressed={(d.order ?? "ev") === o} onClick={() => { setOrder(o); setIdx(0); }} className={cx("h-7 rounded-[4px] border px-2", (d.order ?? "ev") === o ? "border-line-strong bg-active font-semibold" : "border-line hover:bg-hover")}>{ORDER_LABEL[o]}</button>
        ))}
      </div>
      <SectionNote>{d.method_pl}{d.threshold_bits !== null && <> Próg powiązania: {plNumber(d.threshold_bits, Number.isInteger(d.threshold_bits) ? 0 : 1)} bit.</>} Wartość decyzji to heurystyka kolejności, nie prawdopodobieństwo.</SectionNote>
      {err !== undefined && <div className="text-xs text-err">{err instanceof Error ? err.message : "Błąd zapisu"}</div>}

      {tab === "unsure" ? (
        <Panel title={`Niepewne (${d.unsure.length})`} actions={<span className="text-xs text-subtle">oznaczenie zdejmuje pozycję z kolejki, nie zmienia kampanii</span>}>
          {d.unsure.length === 0 ? <div className="text-sm text-subtle">Brak aktywnych oznaczeń „niepewne”.</div> : (
            <ul className="space-y-1.5" data-testid="unsure-list">
              {d.unsure.map((m) => (
                <li key={m.id} className="flex flex-wrap items-center gap-2 border-t border-line pt-1.5 text-xs first:border-t-0 first:pt-0">
                  {m.target.a && m.target.b ? <Link className="font-mono text-accent hover:underline" to={pairPath(m.target.a, m.target.b)}>{markLabel(m)}</Link> : m.target.incident_id ? <IncidentLink id={m.target.incident_id} /> : <span className="font-mono">{markLabel(m)}</span>}
                  <span className="text-muted">{m.analyst} · {formatDatePl(m.created_at)}{m.note ? ` · „${m.note}”` : ""}</span>
                  <button type="button" disabled={busy} title={!canWrite ? READ_ONLY_TIP : undefined} onClick={() => undoUnsure.mutate(m.id)} className="ml-auto h-7 rounded-[4px] border border-line-strong px-2 hover:bg-hover">↶ Cofnij oznaczenie</button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      ) : items.length === 0 ? (
        <Panel title="Kolejka"><div className="text-sm text-subtle">Brak pozycji do decyzji w tym przebiegu.</div></Panel>
      ) : (
        <div className="grid min-h-0 gap-3 xl:grid-cols-[420px_1fr]">
          <Panel title="Pozycje" actions={<span className="text-xs text-subtle">kolejność: {ORDER_LABEL[d.order ?? "ev"]}</span>} flush>
            <ul className="max-h-[70vh] overflow-auto" data-testid="queue-list">
              {items.map((it, i) => (
                <li key={it.key}>
                  <button type="button" onClick={() => setIdx(i)} className={cx("flex w-full flex-col gap-1 border-b border-line px-3 py-2 text-left text-xs", it === cur ? "bg-active" : "hover:bg-hover")}>
                    <span className="flex w-full items-center gap-2">
                      <span className="w-6 text-subtle">{i + 1}</span>
                      <span className="min-w-0 flex-1 truncate font-mono">{itemTitle(it)}</span>
                      <span className="font-mono text-muted">{bitsVsThreshold(it.bits, d.threshold_bits)}</span>
                    </span>
                    <span className="flex flex-wrap items-center gap-1 pl-8">
                      {it.kinds.map((k) => <KindBadge key={k} kind={k} />)}
                      {it.gain_per_effort !== undefined && <span className="ml-auto text-subtle" title="oczekiwana liczba zmienionych decyzji na jednostkę wysiłku (heurystyka)">zysk {plNumber(it.gain_per_effort, 2)}</span>}
                      <span className={cx("text-subtle", it.gain_per_effort === undefined && "ml-auto")} title="wartość decyzji (heurystyka)">wartość {plNumber(it.ev, 2)}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </Panel>
          {cur && (
            <Panel
              title={<span className="normal-case">{itemTitle(cur)}</span>}
              actions={isPair(cur) ? <Link className="text-xs text-accent hover:underline" to={pairPath(cur.a, cur.b)}>pełny dowód (Enter) →</Link> : undefined}
            >
              <div className="space-y-3">
                <div className="flex flex-wrap items-center gap-2">
                  {cur.kinds.map((k) => <KindBadge key={k} kind={k} />)}
                  <span className="font-mono text-sm">{bitsVsThreshold(cur.bits, d.threshold_bits)}</span>
                  {cur.distance_bits !== null && <span className="text-xs text-muted">odległość od progu {plNumber(cur.distance_bits, 1)} bit</span>}
                  <span className="ml-auto text-xs text-muted">wartość decyzji (heurystyka): <span className="font-mono text-fg">{plNumber(cur.ev, 2)}</span></span>
                </div>
                <div className="text-sm text-fg">{polishReason(cur.reason_pl)}</div>
                <WhyFirst item={cur} first={cur === items[0]} />
                {cur.campaigns.length > 0 && (
                  <div className="flex flex-wrap items-center gap-2 text-xs"><span className="text-subtle">kampanie:</span>{cur.campaigns.map((c) => <CampaignLink key={c} id={c} />)}</div>
                )}
                {isPair(cur) ? <PairEvidence a={cur.a} b={cur.b} /> : <AbstainCard item={cur} />}
                <div className="flex flex-wrap gap-2">
                  <button type="button" disabled={busy || !isPair(cur)} title={!canWrite ? READ_ONLY_TIP : isPair(cur) ? undefined : "pozycja bez pary: potwierdzenie/odrzucenie niedostępne"} onClick={() => decide("accept_link")} className="h-9 flex-1 rounded-[4px] border border-for bg-for-soft text-sm font-semibold disabled:opacity-50">✓ Potwierdź (A)</button>
                  <button type="button" disabled={busy || !isPair(cur)} title={!canWrite ? READ_ONLY_TIP : isPair(cur) ? undefined : "pozycja bez pary: potwierdzenie/odrzucenie niedostępne"} onClick={() => decide("reject_link")} className="h-9 flex-1 rounded-[4px] border border-against bg-against-soft text-sm font-semibold disabled:opacity-50">✕ Odrzuć (R)</button>
                  <button type="button" disabled={busy || !unsureTarget(cur)} title={!canWrite ? READ_ONLY_TIP : undefined} onClick={markUnsure} className="h-9 flex-1 rounded-[4px] border border-abstain bg-abstain-soft text-sm font-semibold text-abstain-ink disabled:opacity-50">? Niepewne (N)</button>
                  {last && <button type="button" disabled={busy} onClick={undoLast} className="h-9 rounded-[4px] border border-line-strong px-3 text-sm">↶ Cofnij (U)</button>}
                </div>
                <p className="text-xs text-muted">„Niepewne” zdejmuje pozycję z kolejki i trafia do dziennika audytu, ale nie zmienia kampanii.</p>
                <ReviewEffectNote />
              </div>
            </Panel>
          )}
        </div>
      )}
    </Page>
  );
}
