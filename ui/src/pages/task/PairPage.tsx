import { useState } from "react";
import { READ_ONLY_TIP, useCanWrite } from "../../api/auth";
import { Link, useParams } from "react-router";
import { artifactContentUrl } from "../../api/client";
import { useAssignments, useLatestRun, usePair, useReview, type Assignment, type PairSide } from "../../api/task";
import { DecisionRule } from "../../components/DecisionRule";
import { Button } from "../../components/Button";
import { DecisionBadge, FamChip, GeneMatchTable, HypothesisBadge, ProbBar, ReviewBadge, SectionNote, Waterfall } from "../../components/evidence";
import { AiAnalystPanel } from "../../components/AiAnalystPanel";
import { useProvenance, type RobustnessDetail } from "../../api/visual";
import { ProvenanceFlow } from "../../components/ProvenanceFlow";
import { EvidenceCompare } from "../../components/artifacts/EvidenceCompare";
import { RobustnessChart } from "../../components/RobustnessChart";
import { RobustnessBadge, type Robustness } from "../../components/RobustnessBadge";
import { ReviewEffectNote } from "../../components/ReviewEffectNote";
import { Panel } from "../../components/Panel";
import { bitsWord, familyLabel, formatBits, formatDatePl, formatProbability, REVIEW_LABEL, ROLE_LABEL } from "../../lib/evidence";
import { CampaignLink, IncidentLink, Page, QueryState } from "./common";
import { polishReason } from "../../lib/displayPl";
import { LabLink, topPair } from "../../components/LabLink";

function Side({ side, letter }: { side: PairSide | null; letter: string }) {
  if (!side) return <div className="text-sm text-subtle">Incydent {letter} nie istnieje w bazie.</div>;
  const main = side.visuals[0];
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex items-center gap-2 text-sm">
        <span className="flex size-5 items-center justify-center rounded-[3px] border border-line-strong font-mono text-xs">{letter}</span>
        <IncidentLink id={side.id} title={side.title} />
        <span className="truncate text-muted">{side.title}</span>
      </div>
      <div className="flex aspect-[4/3] items-center justify-center overflow-hidden rounded-md border border-line bg-raised">
        {main ? (
          main.mime?.startsWith("video/") ? (
            <video src={artifactContentUrl(main.artifact_id)} controls preload="metadata" className="h-full w-full bg-black object-contain" aria-label={`${letter}: ${ROLE_LABEL[main.role] ?? main.role}`} />
          ) : (
            <a href={artifactContentUrl(main.artifact_id)} target="_blank" rel="noreferrer" className="h-full w-full">
              <img src={artifactContentUrl(main.artifact_id)} alt={`${letter}: ${ROLE_LABEL[main.role] ?? main.role}`} className="h-full w-full object-contain" />
            </a>
          )
        ) : (
          <span className="text-xs text-subtle">brak obrazu (kreacja / zrzut niedostępne)</span>
        )}
      </div>
      {side.visuals.length > 1 && (
        <div className="flex gap-1.5 overflow-x-auto">
          {side.visuals.slice(1).map((v) => (
            <a key={v.artifact_id + v.path_in_incident} href={artifactContentUrl(v.artifact_id)} target="_blank" rel="noreferrer" title={`${ROLE_LABEL[v.role] ?? v.role}: ${v.path_in_incident}`} className="block size-14 shrink-0 overflow-hidden rounded-[3px] border border-line">
              {v.mime?.startsWith("video/") ? (
                <span className="flex h-full w-full items-center justify-center bg-fg text-xs text-canvas">▶ wideo</span>
              ) : (
                <img src={artifactContentUrl(v.artifact_id)} alt={v.path_in_incident} className="h-full w-full object-cover" loading="lazy" />
              )}
            </a>
          ))}
        </div>
      )}
      <dl className="grid grid-cols-[110px_1fr] gap-x-2 gap-y-1 text-xs">
        <dt className="text-subtle">Rola obrazu</dt><dd>{main ? ROLE_LABEL[main.role] ?? main.role : "—"}</dd>
        <dt className="text-subtle">Strona docelowa</dt><dd className="break-words">{side.landing_title ?? <span className="text-subtle">tytuł nieznany</span>}</dd>
        <dt className="text-subtle">Hosty</dt><dd className="font-mono break-all">{side.hosts.length ? side.hosts.join(", ") : <span className="font-sans text-subtle">brak</span>}</dd>
        <dt className="text-subtle">Data</dt><dd>{formatDatePl(side.observed_at)}</dd>
        <dt className="text-subtle">Kampania</dt><dd><CampaignLink id={side.campaign_id} /></dd>
      </dl>
    </div>
  );
}

/** Open-set assignments of the two incidents (leave-one-out in the run), with the decision rule state. */
function AssignmentRules({ a, b }: { a: string; b: string }) {
  const q = useAssignments({ limit: 2000 });
  const rows = (q.data?.items ?? []).filter((x: Assignment) => x.incident_id === a || x.incident_id === b);
  if (q.isPending) return <div className="text-xs text-subtle">Ładowanie przypisań…</div>;
  if (!rows.length) return <div className="text-xs text-subtle">{q.isError ? "Przypisania niedostępne." : "Brak przypisań tych incydentów w bieżącym przebiegu."}</div>;
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {rows.map((x) => {
        const bestPair = x.meta?.top_candidates?.[0]?.pair;
        const viaThis = bestPair && [a, b].every((id) => bestPair.includes(id));
        return (
          <div key={x.incident_id} className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="font-mono">{x.incident_id}</span>
              <DecisionBadge value={x.decision} />
              {x.campaign_id && <CampaignLink id={x.campaign_id} />}
              {bestPair && <span className="text-xs text-subtle">najlepsza para: <span className="font-mono">{bestPair[0]} ↔ {bestPair[1]}</span>{viaThis ? " (ta para)" : ""}</span>}
            </div>
            <div className="text-xs text-muted [overflow-wrap:anywhere]">{polishReason(x.reason_pl)}</div>
            <DecisionRule meta={x.meta} bestPair={topPair(x)} />
            <LabLink incidentId={x.incident_id} />
          </div>
        );
      })}
    </div>
  );
}

export function PairPage() {
  const { a, b } = useParams();
  const q = usePair(a, b);
  const review = useReview();
  const canWrite = useCanWrite();
  const roTip = canWrite ? undefined : READ_ONLY_TIP;
  const prov = useProvenance(a, b);
  const latest = useLatestRun();
  const linkThreshold = Number(((latest.data?.run?.params?.thresholds as Record<string, unknown> | undefined) ?? {}).link ?? NaN);
  const [note, setNote] = useState("");
  if (!q.data) return <div className="p-4"><QueryState query={q} what="dowód pary" endpoint={`/v1/pairs/${a}/${b}`} /></div>;
  const { score, incident_a, incident_b, reviews } = q.data;
  const unknown = Array.from(new Set([...(score.unknown_families ?? []), ...score.missing_families]));
  const target = { a: score.a, b: score.b };
  const active = [...reviews].reverse().find((r) => !r.undone && (r.action === "accept_link" || r.action === "reject_link"));
  const decide = (action: "accept_link" | "reject_link") =>
    review.mutate({ action, target, note: note.trim() || null }, { onSuccess: () => setNote("") });

  return (
    <Page
      title="Dowód pary"
      sub={
        <span className="inline-flex flex-wrap items-center gap-2">
          <span className="font-mono">{score.a} ↔ {score.b}</span>
          <HypothesisBadge value={score.hypothesis} />
          <ReviewBadge value={q.data.review} />
          <RobustnessBadge r={(score as unknown as { robustness?: Robustness }).robustness} />
          <Link to={`/diff/${encodeURIComponent(score.a)}/${encodeURIComponent(score.b)}`} className="inline-flex h-7 items-center rounded-md border border-line-strong px-2.5 text-xs hover:bg-hover">Co się zmieniło</Link>
          <Link to={`/lab/${encodeURIComponent(score.a)}`} data-testid="pair-lab-link-a" title={`Laboratorium hipotez dla ${score.a}`} className="inline-flex h-7 items-center rounded-md border border-line-strong px-2.5 text-xs hover:bg-hover">Laboratorium hipotez: {score.a}</Link>
          <Link to={`/lab/${encodeURIComponent(score.b)}`} data-testid="pair-lab-link-b" title={`Laboratorium hipotez dla ${score.b}`} className="inline-flex h-7 items-center rounded-md border border-line-strong px-2.5 text-xs hover:bg-hover">Laboratorium hipotez: {score.b}</Link>
          {q.data.same_campaign_in_run && <span className="text-xs text-subtle">obie w tej samej kampanii</span>}
        </span>
      }
    >
      <div className="grid gap-3 xl:grid-cols-[1.05fr_1fr]">
        <Panel title="Materiały">
          <div className="grid grid-cols-2 gap-4">
            <Side side={incident_a} letter="A" />
            <Side side={incident_b} letter="B" />
          </div>
        </Panel>
        <div className="flex min-w-0 flex-col gap-3">
          <Panel title="Rachunek bitów" actions={<span className="text-xs text-subtle">kliknij rodzinę, aby rozwinąć geny</span>}>
            <Waterfall familyBits={score.family_bits} totalBits={score.total_bits} missing={unknown} reasons={score.reasons} contradictions={score.contradictions} />
          </Panel>
          {(score.campaign_bits !== undefined || score.kit_bits !== undefined || score.asset_bits !== undefined) && (
            <Panel title="Poziomy dowodu" actions={<span className="text-xs text-subtle">rozróżnia kampanię od wspólnego szablonu i zasobu</span>}>
              <div className="grid grid-cols-3 gap-3 text-sm">
                {([
                  ["campaign_bits", "Kampania / operator", "geny wskazujące tego samego operatora: identyfikatory, trackery, rzadka infrastruktura"],
                  ["kit_bits", "Szablon / kit", "wspólny zestaw stron, skryptów lub układ; sam w sobie nie dowodzi tej samej kampanii"],
                  ["asset_bits", "Wspólny zasób", "ten sam obraz, wideo lub plik; może być kradziony z innego źródła"],
                ] as const).map(([k, label, help]) => (
                  <div key={k} className="rounded-md border border-line p-2" title={help}>
                    <div className="text-xs text-subtle">{label}</div>
                    <div className="font-mono text-lg">{formatBits(score[k] as number | undefined)}</div>
                  </div>
                ))}
              </div>
              <div className="mt-2 text-xs text-subtle">Suma oceny ({formatBits(score.total_bits)}) to wynik scorera; bity rodzin są ograniczone limitem na rodzinę, stąd różnica w rachunku.</div>
            </Panel>
          )}
          <Panel title="Decyzja">
            <div className="flex items-baseline gap-3">
              <span className="font-mono text-3xl font-semibold text-fg">{formatProbability(score.probability)}</span>
              <span className="text-sm text-muted">{score.calibrated ? "P(ta sama kampania)" : "wynik nieskalibrowany (nie prawdopodobieństwo)"}</span>
              <span className="ml-auto text-sm text-muted">suma {formatBits(score.total_bits)} · „{bitsWord(score.total_bits)}”</span>
            </div>
            <div className="mt-3"><ProbBar p={score.probability} calibrated={score.calibrated} /></div>
            <div className="mt-3 grid grid-cols-[130px_1fr] gap-y-1 text-xs">
              <span className="text-subtle">Hipoteza</span><span><HypothesisBadge value={score.hypothesis} /></span>
              <span className="text-subtle">Model</span><span className="font-mono">{score.model}</span>
              <span className="text-subtle">Źródła kandydatów</span>
              <span className="font-mono" title={score.candidate_sources?.join("\n")}>
                {score.candidate_sources?.length ? `${score.candidate_sources.slice(0, 5).join(", ")}${score.candidate_sources.length > 5 ? ` … (+${score.candidate_sources.length - 5} indeksów)` : ""}` : "—"}
              </span>
              <span className="text-subtle">Nieznane rodziny</span>
              <span className="flex flex-wrap gap-2">
                {unknown.length ? unknown.map((f) => <span key={f} title="brak obserwacji: nie liczy się przeciw" className="inline-flex items-center gap-1 text-subtle"><FamChip family={f} /> {familyLabel(f)}</span>) : "—"}
              </span>
            </div>
            <div className="mt-4 border-t border-line pt-3">
              <div className="mb-1 text-xs font-semibold text-muted">Decyzja analityka (zapis w dzienniku audytu)</div>
              <div className="mb-2"><ReviewEffectNote /></div>
              <div className="flex flex-wrap items-center gap-2">
                <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="uzasadnienie (opcjonalnie)" aria-label="Uzasadnienie" className="h-7 min-w-0 flex-1 rounded-[4px] border border-line bg-raised px-2 text-sm focus:border-accent focus:outline-none" />
                <Button className="border-for bg-for-soft" onClick={() => decide("accept_link")} disabled={review.isPending || !canWrite} title={roTip}>✓ Potwierdź powiązanie</Button>
                <Button className="border-against bg-against-soft" onClick={() => decide("reject_link")} disabled={review.isPending || !canWrite} title={roTip}>✕ Odrzuć</Button>
                {active && <Button variant="ghost" onClick={() => review.mutate({ action: "undo", undoes: active.id })} disabled={review.isPending || !canWrite} title={roTip}>↶ Cofnij</Button>}
              </div>
              {review.isError && <div className="mt-1 text-xs text-err">{review.error instanceof Error ? review.error.message : "Błąd zapisu"}</div>}
              {reviews.length > 0 && (
                <ul className="mt-2 space-y-0.5 text-xs text-muted">
                  {reviews.map((r) => (
                    <li key={r.id} className={r.undone ? "line-through" : undefined}>
                      {formatDatePl(r.created_at)} · {r.analyst}: {REVIEW_LABEL[r.action] ?? r.action}{r.note ? ` („${r.note}”)` : ""}{r.undone ? " · cofnięte" : ""}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </Panel>
        </div>
      </div>
      <Panel title="Reguła decyzji przypisania" actions={<span className="text-xs text-subtle">przypisanie open-set obu incydentów: wsparcie vs próg, brakujące bity, przewaga, zbiór konformalny</span>}>
        <AssignmentRules a={score.a} b={score.b} />
      </Panel>
      <Panel title="Dopasowane miejsca w materiałach" actions={<span className="text-xs text-subtle">wybierz powód: obie strony z zaznaczonym fragmentem (ramka, tekst, żądanie HAR, chwila nagrania)</span>}>
        <EvidenceCompare a={score.a} b={score.b} reasons={[...score.reasons, ...score.contradictions]} />
      </Panel>
      <div className="grid gap-3 xl:grid-cols-[1.4fr_1fr]">
        <Panel title="Skąd pochodzi dowód" actions={<span className="text-xs text-subtle">artefakt → ekstraktor → gen → rodzina → hipoteza, szerokość = bity</span>}>
          {prov.data && Array.isArray(prov.data.links) ? <ProvenanceFlow p={prov.data} /> : <div className="text-xs text-subtle">{prov.isError ? "Brak zapisanych powodów dla tej pary." : "Ładowanie…"}</div>}
        </Panel>
        <Panel title="Odporność: bez jednej rodziny" actions={<span className="text-xs text-subtle">test pomijania rodzin (CORE)</span>}>
          <RobustnessChart r={(score as unknown as { robustness?: RobustnessDetail }).robustness} support={(score as unknown as { support?: number }).support ?? null} threshold={Number.isFinite(linkThreshold) ? linkThreshold : null} />
        </Panel>
      </div>
      <AiAnalystPanel kind="pair" a={score.a} b={score.b} />
      <Panel title={`Sprzeczności (${score.contradictions.length})`}>
        <GeneMatchTable rows={score.contradictions} idPrefix="ev-contradictions" limit={200} empty="Brak sprzeczności między incydentami." />
      </Panel>
      <Panel title={`Wszystkie powody (${score.reasons.length})`} actions={<span className="text-xs text-subtle">posortowane wg |bitów|</span>}>
        <GeneMatchTable rows={score.reasons} idPrefix="ev-reasons" limit={200} empty="Scorer nie zapisał powodów dla tej pary." />
      </Panel>
      <SectionNote>
        Bity to wagi dowodu (log₂ ilorazu wiarygodności) liczone łącznie per rodzina genów. Brak obserwacji w rodzinie oznacza „nieznane” i daje 0 bitów,
        nigdy dowód przeciw. <Link to="/methodology" className="text-accent hover:underline">Metodyka →</Link>
      </SectionNote>
    </Page>
  );
}
