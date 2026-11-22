/**
 * HYDRA KONTRA K1 "Laboratorium hipotez": counterfactual shadow experiment for one incident.
 * Shows only what /v1/lab returns. Bits are heuristic evidence strength, never probabilities.
 */
import { Link, useParams } from "react-router";
import { Badge } from "../../components/Badge";
import { Button } from "../../components/Button";
import { ApiErrorState } from "../../components/ApiErrorState";
import { EmptyState } from "../../components/EmptyState";
import { Panel } from "../../components/Panel";
import { PropertyList } from "../../components/PropertyList";
import { FamChip, SectionNote } from "../../components/evidence";
import {
  isNoExperiment,
  labFileUrl,
  useCreateLabPackage,
  useLabExperiment,
  useRunLab,
  useSendLabPackage,
  type LabEvidence,
  type LabExperiment,
  type LabHypothesis,
  type LabScenario,
  type LabSendResult,
} from "../../api/lab";
import { cx } from "../../lib/cx";
import { formatDatePl, plNumber } from "../../lib/evidence";
import { CampaignLink, IncidentLink, Loading, Page } from "./common";
import { isWholeFile, WHOLE_FILE_TEXT } from "../../lib/displayPl";
import { RetrievalScopeView, type RetrievalScope } from "../../components/DecisionRule";

export const LAB_DECISION_LABEL: Record<string, string> = {
  assign: "Przypisano",
  new_campaign: "Nowy wzorzec",
  abstain: "Wstrzymano",
};

export const SCENARIO_CLASS_LABEL: Record<string, string> = {
  baseline: "bazowy",
  observation_uncertainty: "niepewność obserwacji",
  reuse_hypothesis: "hipoteza ponownego użycia",
  diagnostic_stress: "test diagnostyczny",
  competitor_check: "weryfikacja konkurenta",
};

const HYPOTHESIS_KIND_LABEL: Record<string, string> = {
  campaign: "kampania",
  new_pattern: "nowy wzorzec",
  shared_kit: "wspólny zestaw (kit)",
};

const COST_LABEL: Record<string, string> = { low: "niski", medium: "średni", high: "wysoki" };

const SEND_STATUS_LABEL: Record<string, string> = {
  acknowledged: "potwierdzono odbiór",
  duplicate: "duplikat (już odebrany)",
  unconfirmed: "niepotwierdzone",
  delivery_unknown: "niepotwierdzone",
  unavailable: "odbiornik niedostępny",
  rejected: "odrzucono",
};

export const NEXT_OBS_LABEL = "rekomendacja heurystyczna, bez wykazanej przewagi";
const ARTEFACT_LABEL: Record<string, string> = { har: "HAR", html: "strona HTML", image: "obraz" };

/** Fields added by the KONTRA fixes (ASTRA-DELTA-02/10, §7); optional so older stored experiments still render. */
type LabStatus = { status?: string; status_pl?: string; stale_reasons?: string[]; stale_reasons_pl?: string[] };
type LabUnavailable = { action: string; label_pl: string; source: string; requires: string; cost: string; why_pl: string };

/** A send counts as received only with a receipt of THIS package (ASTRA-DELTA-03); anything else is unconfirmed. */
export function isConfirmedAck(result: LabSendResult, packageId?: string): boolean {
  if (result.status !== "acknowledged" && result.status !== "duplicate") return false;
  const ack = result.ack;
  if (!ack || typeof ack.receipt_id !== "string" || ack.receipt_id.trim() === "") return false;
  return packageId === undefined || ack.package_id === packageId;
}

function bits(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined ? "—" : `${plNumber(value, digits)} bit`;
}

function decisionLabel(value: string | null | undefined): string {
  if (!value) return "—";
  return LAB_DECISION_LABEL[value] ?? value;
}

function evidenceText(e: LabEvidence, kind?: string | null): string {
  const parts: string[] = [];
  const whole = isWholeFile({ ...e, bbox: e.bbox?.length === 4 ? (e.bbox as [number, number, number, number]) : null, char_range: null }, kind);
  if (whole) parts.push(WHOLE_FILE_TEXT);
  if (e.bbox && !whole) parts.push(`ramka ${e.bbox.map((v) => Math.round(v)).join(",")}`);
  if (e.char_range) parts.push(`znaki ${e.char_range[0]}–${e.char_range[1]}`);
  if (e.timecode_s !== null && e.timecode_s !== undefined && !whole) parts.push(`t=${plNumber(e.timecode_s, 1)} s`);
  if (e.har_entry !== null && e.har_entry !== undefined) parts.push(`HAR #${e.har_entry}`);
  if (e.dom_path) parts.push(`DOM ${e.dom_path}`);
  if (e.note) parts.push(e.note);
  return parts.join(" · ");
}

function EvidenceRef({ incident, evidence, kind }: { incident: string; evidence: LabEvidence | null | undefined; kind?: string | null }) {
  if (!evidence) return null;
  const text = evidenceText(evidence, kind);
  return (
    <span className="text-2xs text-subtle">
      {evidence.artifact_id && (
        <Link
          to={`/incident/${encodeURIComponent(incident)}?art=${encodeURIComponent(evidence.artifact_id)}`}
          className="font-mono text-accent hover:underline"
          title={evidence.artifact_id}
        >
          artefakt {evidence.artifact_id.slice(0, 10)}…
        </Link>
      )}
      {text && <span>{evidence.artifact_id ? " · " : ""}{text}</span>}
    </span>
  );
}

function HypothesisCard({ h, incident, threshold }: { h: LabHypothesis; incident: string; threshold: number }) {
  const reasons = [...h.reasons].sort((x, y) => Math.abs(y.bits) - Math.abs(x.bits)).slice(0, 6);
  const families = Object.entries(h.families).sort((x, y) => Math.abs(y[1]) - Math.abs(x[1]));
  return (
    <section
      data-testid={`lab-hypothesis-${h.id}`}
      data-leading={h.leading || undefined}
      className={cx("flex min-w-0 flex-col gap-2 rounded-lg border bg-panel p-3", h.leading ? "border-accent ring-1 ring-accent" : "border-line")}
    >
      <header className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-subtle">#{h.rank}</span>
        <span className="min-w-0 truncate text-sm font-semibold text-fg" title={h.label_pl}>{h.label_pl}</span>
        <Badge variant="neutral">{HYPOTHESIS_KIND_LABEL[h.kind] ?? h.kind}</Badge>
        {h.leading && <Badge variant="info" dot>prowadząca</Badge>}
      </header>
      {(h.campaign_id || h.group_id) && (
        <div className="flex flex-wrap gap-x-3 text-xs text-muted">
          {h.campaign_id && <span>kampania <CampaignLink id={h.campaign_id} /></span>}
          {h.group_id && <span>grupa <span className="font-mono">{h.group_id}</span></span>}
        </div>
      )}
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 text-xs">
        <dt className="text-subtle">Wsparcie</dt>
        <dd className="font-mono">{bits(h.support_bits)} <span className="text-subtle">/ próg {bits(threshold)}</span></dd>
        <dt className="text-subtle">Do progu</dt>
        <dd className="font-mono">{h.gap_to_link_bits === null ? "—" : h.gap_to_link_bits <= 0 ? "próg osiągnięty" : `brakuje ${bits(h.gap_to_link_bits)}`}</dd>
        {h.campaign_bits !== null && h.campaign_bits !== undefined && (<><dt className="text-subtle">Poziom kampanii</dt><dd className="font-mono">{bits(h.campaign_bits)}</dd></>)}
        {h.kit_bits !== null && h.kit_bits !== undefined && (<><dt className="text-subtle">Poziom zestawu</dt><dd className="font-mono">{bits(h.kit_bits)}</dd></>)}
        {h.operator_bits !== null && h.operator_bits !== undefined && (<><dt className="text-subtle">Operator</dt><dd className="font-mono">{bits(h.operator_bits)}</dd></>)}
        {h.supporting_members !== null && h.supporting_members !== undefined && (<><dt className="text-subtle">Wspierający członkowie</dt><dd className="font-mono">{h.supporting_members}</dd></>)}
        {h.pair_hypothesis && (<><dt className="text-subtle">Hipoteza pary</dt><dd className="font-mono">{h.pair_hypothesis}</dd></>)}
      </dl>
      {families.length > 0 && (
        <div className="flex flex-wrap gap-1.5 text-xs">
          {families.map(([f, b]) => (
            <span key={f} className="inline-flex items-center gap-1 rounded-[3px] border border-line px-1">
              <FamChip family={f} />
              <span className="font-mono">{plNumber(b, 1)}</span>
            </span>
          ))}
        </div>
      )}
      {h.note_pl && <div className="text-xs text-muted">{h.note_pl}</div>}
      {reasons.length > 0 ? (
        <ul className="space-y-1 border-t border-line pt-2 text-xs">
          {reasons.map((r, i) => (
            <li key={i} className="flex min-w-0 flex-col gap-0.5">
              <div className="flex min-w-0 items-start gap-1.5">
                <FamChip family={r.family} />
                <span className="min-w-0 flex-1 text-fg">{r.explanation_pl}</span>
                <span className="font-mono whitespace-nowrap">{bits(r.bits)}</span>
              </div>
              <EvidenceRef incident={incident} evidence={r.evidence} kind={r.kind} />
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-xs text-subtle">Brak powodów dla tej hipotezy.</div>
      )}
    </section>
  );
}

function ChangeBadge({ s }: { s: LabScenario }) {
  if (s.decision_changed) return <Badge variant="warning" dot>zmiana decyzji</Badge>;
  if (s.campaign_changed) return <Badge variant="warning" dot>zmiana kampanii</Badge>;
  return <span className="text-subtle">—</span>;
}

function ScenarioTable({ exp }: { exp: LabExperiment }) {
  const stability = exp.stats?.scenario_stability_fraction;
  return (
    <Panel title={`Scenariusze kontrfaktyczne (${exp.scenarios.length})`} flush actions={<span className="text-xs text-subtle">przeliczenie bez wskazanych obserwacji</span>}>
      <div className="overflow-x-auto" data-testid="lab-scenarios">
        <table className="w-full text-xs">
          <thead className="bg-raised text-2xs tracking-wider text-subtle uppercase">
            <tr>
              <th className="px-2 py-1.5 text-left">Scenariusz</th>
              <th className="px-2 py-1.5 text-left">Klasa</th>
              <th className="px-2 py-1.5 text-left">Decyzja</th>
              <th className="px-2 py-1.5 text-right">Wsparcie vs próg</th>
              <th className="px-2 py-1.5 text-right">Margines</th>
              <th className="px-2 py-1.5 text-left">Zmiana</th>
              <th className="px-2 py-1.5 text-right">Czas ms</th>
            </tr>
          </thead>
          <tbody>
            {exp.scenarios.map((s) => (
              <tr key={s.scenario_id} data-testid={`lab-scenario-${s.scenario_id}`} className={cx("border-t border-line", (s.decision_changed || s.campaign_changed) && "bg-warn/5")}>
                <td className="max-w-[360px] px-2 py-1" title={s.reason_pl}>
                  <div className="text-fg">{s.label_pl}</div>
                  {s.removed.n_genes > 0 && <div className="text-2xs text-subtle">pominięte geny: {s.removed.n_genes}</div>}
                </td>
                <td className="px-2 py-1 whitespace-nowrap text-muted">{SCENARIO_CLASS_LABEL[s.class] ?? s.class}</td>
                <td className="px-2 py-1 whitespace-nowrap">
                  {decisionLabel(s.decision)}
                  {s.campaign_id && <> · <CampaignLink id={s.campaign_id} /></>}
                </td>
                <td className="px-2 py-1 text-right font-mono whitespace-nowrap">{plNumber(s.support_bits, 1)} / {plNumber(s.link_threshold, 1)} bit</td>
                <td className="px-2 py-1 text-right font-mono whitespace-nowrap">{bits(s.margin_bits)}</td>
                <td className="px-2 py-1"><ChangeBadge s={s} /></td>
                <td className="px-2 py-1 text-right font-mono">{plNumber(s.runtime_ms, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="border-t border-line px-3 py-2 text-xs text-muted">
        Klasy nie są sumowane w jedną liczbę zaufania. scenario_stability_fraction to statystyka sprawdzonego katalogu, nie prawdopodobieństwo.
        {stability !== null && stability !== undefined && <> Wartość: <span className="font-mono">{plNumber(stability, 2)}</span>.</>}
      </div>
    </Panel>
  );
}

function FlipSetPanel({ exp }: { exp: LabExperiment }) {
  const f = exp.flip_set;
  return (
    <Panel title="Minimalny zestaw zmieniający decyzję">
      <div data-testid="lab-flip-set" className="space-y-2 text-xs">
        {!f ? (
          <div className="text-subtle">Brak danych o zestawie zmieniającym decyzję.</div>
        ) : f.found ? (
          <>
            <ul className="list-disc space-y-0.5 pl-4">
              {(f.labels_pl.length ? f.labels_pl : f.groups).map((l, i) => <li key={i}>{l}</li>)}
            </ul>
            <div>
              Decyzja po usunięciu: <span className="font-semibold">{decisionLabel(f.decision)}</span>
              {f.campaign_id && <> · <CampaignLink id={f.campaign_id} /></>}
            </div>
          </>
        ) : (
          <div className="text-fg">W budżecie nie znaleziono zestawu zmieniającego decyzję</div>
        )}
        {f && (
          <>
            <div className="font-mono text-subtle">sprawdzono {f.searched} / budżet {f.budget}</div>
            {f.note_pl && <div className="text-muted">{f.note_pl}</div>}
          </>
        )}
      </div>
    </Panel>
  );
}

function RetrievalScopePanel({ exp }: { exp: LabExperiment }) {
  const scope = exp.retrieval_scope;
  return (
    <Panel title="Zakres wyszukiwania kandydatów">
      <div data-testid="lab-retrieval-scope" className="space-y-2 text-xs">
        {scope ? (
          <>
            <RetrievalScopeView scope={scope as RetrievalScope} />
            <details>
              <summary className="cursor-pointer text-muted">surowe pola zakresu</summary>
              <PropertyList data={scope} order={["truncated", "bounded_by"]} />
            </details>
          </>
        ) : (
          <div className="text-subtle">Brak danych o zakresie wyszukiwania</div>
        )}
        <div className="font-mono text-muted">retrieval_complete: brak certyfikacji</div>
      </div>
    </Panel>
  );
}

function NextObservationsPanel({ exp }: { exp: LabExperiment }) {
  const n = exp.next_observations;
  const unavailable = ((n as { unavailable?: LabUnavailable[] } | null)?.unavailable ?? []);
  return (
    <Panel title="Co by rozstrzygnęło" actions={<span className="text-xs text-subtle">{NEXT_OBS_LABEL}</span>}>
      <div data-testid="lab-next-observations" className="space-y-2 text-xs">
        {!n || n.ranking.length === 0 ? (
          <div className="text-subtle">Brak propozycji kolejnych obserwacji.</div>
        ) : (
          <>
            <div className="text-muted">
              Rozdziela hipotezy: <span className="font-mono">{n.pair.join(" ↔ ")}</span>
            </div>
            <ol className="space-y-1.5">
              {n.ranking.map((r, i) => (
                <li key={`${r.action}-${i}`} className="rounded-md border border-line p-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="font-mono text-subtle">{i + 1}.</span>
                    <span className="font-semibold text-fg">{r.label_pl}</span>
                    {r.families.map((f) => <FamChip key={f} family={f} />)}
                    <Badge variant="neutral">koszt: {COST_LABEL[r.cost] ?? r.cost}</Badge>
                    {r.observed_in_query ? <Badge variant="neutral">już zaobserwowane</Badge> : <Badge variant="info">jeszcze nie zaobserwowane</Badge>}
                    {r.separates && <Badge variant="success">rozdziela hipotezy</Badge>}
                  </div>
                  <div className="mt-1 text-muted">{r.why_pl}</div>
                  <div className="mt-0.5 text-2xs text-subtle">źródło: {r.source}</div>
                </li>
              ))}
            </ol>
          </>
        )}
        {unavailable.length > 0 && (
          <div data-testid="lab-next-unavailable" className="space-y-0.5 text-muted">
            <div className="text-subtle">Niewykonalne (brak wymaganego artefaktu, nie rankingowane):</div>
            <ul className="list-disc pl-4">
              {unavailable.map((u) => (
                <li key={u.action}>{u.label_pl}: brak {ARTEFACT_LABEL[u.requires] ?? u.requires}</li>
              ))}
            </ul>
          </div>
        )}
        {n?.note_pl && <SectionNote>{n.note_pl}</SectionNote>}
      </div>
    </Panel>
  );
}

function AckView({ result, packageId }: { result: LabSendResult; packageId?: string }) {
  const ok = isConfirmedAck(result, packageId);
  const claimed = result.status === "acknowledged" || result.status === "duplicate";
  const label = ok ? SEND_STATUS_LABEL[result.status] : claimed ? SEND_STATUS_LABEL.unconfirmed : (SEND_STATUS_LABEL[result.status] ?? result.status);
  return (
    <div data-testid="lab-ack" data-confirmed={ok || undefined} className="space-y-1 rounded-md border border-line p-2 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={ok ? "success" : result.status === "rejected" ? "danger" : "warning"} dot>{label}</Badge>
        <span className="font-mono text-subtle">{result.receiver_url}</span>
      </div>
      {ok && result.ack && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
          <dt className="text-subtle">Potwierdzenie</dt><dd className="font-mono break-all">{result.ack.receipt_id}</dd>
          <dt className="text-subtle">Numer sprawy (syntetyczny)</dt><dd className="font-mono break-all">{result.ack.synthetic_case_id}</dd>
          <dt className="text-subtle">Odebrano</dt><dd>{formatDatePl(result.ack.received_at)}</dd>
          {result.ack.duplicate && (<><dt className="text-subtle">Status</dt><dd><Badge variant="warning">duplikat</Badge></dd></>)}
          {result.ack.note && (<><dt className="text-subtle">Uwaga</dt><dd>{result.ack.note}</dd></>)}
        </dl>
      )}
      {result.reason && <div className="text-muted">{result.reason}</div>}
      {!ok && (claimed || result.status === "unconfirmed" || result.status === "delivery_unknown") && (
        <div className="text-muted">Brak poprawnego pokwitowania tego pakietu: odbiór niepotwierdzony.</div>
      )}
      {result.status === "unavailable" && (
        <div className="text-muted">
          Odbiornik nie działa. Uruchom go lokalnie: <span className="font-mono">uv run python scripts/kontra_receiver.py</span>
        </div>
      )}
    </div>
  );
}

function ExportPanel({ incident, stale }: { incident: string; stale: boolean }) {
  const pkg = useCreateLabPackage(incident);
  const send = useSendLabPackage();
  const p = pkg.data;
  const files: [string, string | undefined][] = p ? [["JSON", p.files.json], ["Markdown", p.files.markdown], ["HTML", p.files.html]] : [];
  return (
    <Panel title="Pakiet dowodowy" actions={<span className="text-xs text-subtle">lokalny odbiornik demonstracyjny (loopback), numer sprawy syntetyczny</span>}>
      <div data-testid="lab-export" className="space-y-2 text-xs">
        <div className="flex flex-wrap items-center gap-2">
          <Button data-testid="lab-package" onClick={() => { send.reset(); pkg.mutate(); }} disabled={pkg.isPending || stale}>
            {pkg.isPending ? "Tworzenie pakietu…" : "Utwórz pakiet dowodowy"}
          </Button>
          {p && (
            <Button data-testid="lab-send" variant="primary" onClick={() => send.mutate(p.package_id)} disabled={send.isPending}>
              {send.isPending ? "Wysyłanie…" : "Wyślij do lokalnego odbiornika"}
            </Button>
          )}
        </div>
        {pkg.isError && <ApiErrorState compact error={pkg.error} resource="pakiet dowodowy" />}
        {p && (
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
            <dt className="text-subtle">Pakiet</dt><dd className="font-mono break-all">{p.package_id}</dd>
            <dt className="text-subtle">sha256</dt><dd className="font-mono break-all">{p.sha256}</dd>
            <dt className="text-subtle">Utworzono</dt><dd>{formatDatePl(p.created_at)}</dd>
            <dt className="text-subtle">Pliki</dt>
            <dd className="flex flex-wrap gap-3">
              {files.filter(([, url]) => Boolean(url)).map(([name, url]) => (
                <a key={name} href={labFileUrl(url as string)} target="_blank" rel="noreferrer" download className="text-accent hover:underline">{name}</a>
              ))}
            </dd>
          </dl>
        )}
        {send.isError && <ApiErrorState compact error={send.error} resource="wysyłka do odbiornika" />}
        {stale && <div className="text-warn">Kontekst nieaktualny: uruchom przeliczenie ponownie przed utworzeniem pakietu.</div>}
        {send.data && <AckView result={send.data} packageId={p?.package_id} />}
      </div>
    </Panel>
  );
}

function LimitsLine({ exp }: { exp: LabExperiment }) {
  const l = exp.limits ?? {};
  const s = exp.stats;
  return (
    <div data-testid="lab-limits" className="flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-muted">
      {s && <span>przeliczenia: {s.recomputations}</span>}
      {s && <span>łączny czas: {plNumber(s.runtime_ms_total, 0)} ms</span>}
      {l.scenarios_run !== undefined && <span>scenariusze: {l.scenarios_run} uruchomione / {l.scenarios_skipped ?? 0} pominięte</span>}
      {l.groups_tested !== undefined && <span>grupy: {l.groups_tested} / {l.groups_total ?? "—"}</span>}
      {l.scenario_space_exhausted !== undefined && <span>{l.scenario_space_exhausted ? "katalog scenariuszy wyczerpany" : "katalog scenariuszy niewyczerpany (limit)"}</span>}
    </div>
  );
}

function StaleBanner({ exp }: { exp: LabExperiment }) {
  const st = exp as LabExperiment & LabStatus;
  if (st.status !== "stale_context") return null;
  const reasons = st.stale_reasons_pl?.length ? st.stale_reasons_pl : st.stale_reasons ?? [];
  return (
    <div role="alert" data-testid="lab-stale" className="rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">
      Kontekst nieaktualny: od zamrożenia zmieniły się {reasons.length ? reasons.join(", ") : "dane wejściowe"}. Wyniki
      dotyczą zamrożonego stanu, nie bieżącej analizy; uruchom przeliczenie ponownie.
    </div>
  );
}

function Baseline({ exp }: { exp: LabExperiment }) {
  const b = exp.baseline;
  const c = exp.context;
  return (
    <Panel title="Wynik analizy">
      <div className="space-y-2 text-sm" data-testid="lab-baseline">
        <StaleBanner exp={exp} />
        <div className="flex flex-wrap items-center gap-3">
          <span className="text-base font-semibold text-fg">{decisionLabel(b.decision)}</span>
          {b.campaign_id && <span className="text-xs">kampania <CampaignLink id={b.campaign_id} /></span>}
          <span className="font-mono text-xs">wsparcie {bits(b.support_bits)} vs próg {bits(b.link_threshold)}</span>
          <span className="font-mono text-xs">margines {bits(b.margin_bits)}</span>
          {b.link_rule_met !== null && <Badge variant={b.link_rule_met ? "success" : "neutral"}>{b.link_rule_met ? "reguła powiązania spełniona" : "reguła powiązania niespełniona"}</Badge>}
        </div>
        {b.reason_pl && <div className="text-xs text-muted">{b.reason_pl}</div>}
        {b.matches_stored === false && (
          <div role="alert" className="rounded-md border border-warn/40 bg-warn/10 p-2 text-xs text-warn">
            Przeliczenie bazowe różni się od zapisanej decyzji produktu
            {b.stored && <> (zapisano: {decisionLabel(b.stored.decision)}{b.stored.campaign_id && <> · {b.stored.campaign_id}</>})</>}.
          </div>
        )}
        <div className="flex flex-wrap gap-x-3 font-mono text-2xs text-subtle">
          <span title={c.fingerprint}>odcisk {c.fingerprint.slice(0, 16)}</span>
          <span>przebieg {c.base_run ?? "—"}</span>
          <span>commit {c.product_commit ? c.product_commit.slice(0, 10) : "—"}</span>
          <span>eksperyment {exp.experiment_id}</span>
          <span>{formatDatePl(exp.created_at)}</span>
        </div>
      </div>
    </Panel>
  );
}

function Experiment({ exp }: { exp: LabExperiment }) {
  return (
    <div className="flex flex-col gap-3">
      <Baseline exp={exp} />
      <div className="flex flex-col gap-2">
        <h3 className="text-xs font-semibold tracking-wider text-muted uppercase">Hipotezy ({exp.hypotheses.length})</h3>
        <div data-testid="lab-hypotheses" className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {exp.hypotheses.map((h) => <HypothesisCard key={h.id} h={h} incident={exp.incident_id} threshold={exp.baseline.link_threshold} />)}
        </div>
      </div>
      <ScenarioTable exp={exp} />
      <div className="grid gap-3 xl:grid-cols-2">
        <FlipSetPanel exp={exp} />
        <RetrievalScopePanel exp={exp} />
      </div>
      <NextObservationsPanel exp={exp} />
      <ExportPanel incident={exp.incident_id} stale={(exp as LabExperiment & LabStatus).status === "stale_context"} />
      <LimitsLine exp={exp} />
    </div>
  );
}

export function LabPage() {
  const { incident = "" } = useParams();
  const q = useLabExperiment(incident || undefined);
  const run = useRunLab(incident || undefined);
  const runButton = (
    <Button data-testid="lab-run" variant="primary" onClick={() => run.mutate()} disabled={run.isPending || !incident}>
      {run.isPending ? "Przeliczanie scenariuszy…" : "Uruchom przeliczenie"}
    </Button>
  );

  let body;
  if (run.isPending) {
    body = <div role="status" aria-busy="true" data-testid="lab-busy"><Loading what="Przeliczanie scenariuszy (może potrwać do minuty)" /></div>;
  } else if (q.data) {
    body = <Experiment exp={q.data} />;
  } else if (q.isPending) {
    body = <Loading what="eksperyment" />;
  } else if (isNoExperiment(q.error)) {
    body = (
      <EmptyState
        testId="lab-empty"
        title="Brak eksperymentu dla tego incydentu"
        description="Laboratorium nie ma jeszcze zapisanego przeliczenia. Uruchom je, aby zobaczyć hipotezy i scenariusze kontrfaktyczne."
        action={runButton}
      />
    );
  } else {
    body = <ApiErrorState error={q.error} resource="eksperyment laboratorium" endpoint={`/v1/lab/${incident}`} action={runButton} />;
  }

  return (
    <Page
      title={<span>Laboratorium hipotez — <span className="font-mono">{incident}</span></span>}
      sub={
        <span className="inline-flex flex-wrap items-center gap-2">
          <Badge variant="warning" dot>Symulacja (shadow) — nie zmienia decyzji produktu</Badge>
          <IncidentLink id={incident} />
        </span>
      }
      actions={q.data || run.isPending ? runButton : undefined}
    >
      {run.isError && <ApiErrorState compact error={run.error} resource="przeliczenie scenariuszy" endpoint={`/v1/lab/${incident}/run`} />}
      {body}
    </Page>
  );
}
