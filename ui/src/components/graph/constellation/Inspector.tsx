/**
 * Side inspector of the constellation explorer (GRAPH-2): run overview with the visual legend, campaign, incident and
 * link panels. Every number comes from the overview / campaign detail payloads or from the existing task endpoints
 * (campaign summary, incident, pair); missing values are shown as "—" with a Polish note, never substituted.
 */
import { useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router";
import type { CampaignDetail, ExternalEdge, GEdge, GNode, Overview, OvCampaign, OvGroup } from "../../../api/graph";
import { thumbUrl } from "../../../api/artifacts";
import type { IncidentArtifactRow } from "../../../api/task";
import { useIncident, usePair } from "../../../api/task";
import { FAMILY_LABEL, formatBits, formatDatePl, plNumber } from "../../../lib/evidence";
import { EvidenceCompare, locatedReasons } from "../../artifacts/EvidenceCompare";
import { ArtifactGallery } from "../../artifacts/ArtifactGallery";
import { Button } from "../../Button";
import { FamChip, FamilyBars, SectionNote, Thumb, Waterfall } from "../../evidence";
import { LEVEL_LABEL, LEVELS, useCampaignSummary } from "../api";
import { shortTitle } from "../build";
import { LineKey, ReasonList, ReviewBadgeG, RobustVerdict, Section } from "../Panels";
import type { LoadStatus } from "./loader";

const enc = encodeURIComponent;
const LINK_CLS = "inline-flex h-6 items-center rounded-[4px] border border-line-strong bg-raised px-2 text-xs text-fg hover:bg-hover";
const ROW_CLS = "flex w-full items-center gap-2 rounded-[4px] px-1 py-1 text-left text-xs hover:bg-hover";
const BITS_NOTE = "Bity to wagi dowodów, nie prawdopodobieństwa: mówią, jak rzadkie są wspólne cechy, a nie jak pewne jest powiązanie.";

const pl = (n: number | null | undefined) => (n === null || n === undefined || !Number.isFinite(n) ? "—" : n.toLocaleString("pl-PL"));
const bitsLabel = (b: number | null | undefined) => `${plNumber(b, 1)} b`;

/** Panel title (h2 for screen readers) with the close button. */
function PanelHead({ kicker, title, onClose, children }: { kicker: string; title: ReactNode; onClose?: () => void; children?: ReactNode }) {
  return (
    <div className="border-b border-line px-4 py-3">
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="text-xs text-subtle">{kicker}</div>
          <h2 className="text-base font-semibold text-fg [overflow-wrap:anywhere]">{title}</h2>
        </div>
        {onClose && (
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Zamknij">
            <span aria-hidden="true">✕</span>
          </Button>
        )}
      </div>
      {children}
    </div>
  );
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="truncate text-2xs text-subtle">{label}</dt>
      <dd className="font-mono text-base text-fg tabular-nums">{value}</dd>
    </div>
  );
}

/** One link row: bits, family, other side, level. */
function EdgeRow({ e, label, extra, onPick }: { e: GEdge; label: string; extra?: ReactNode; onPick: (e: GEdge) => void }) {
  return (
    <li>
      <button type="button" className={ROW_CLS} onClick={() => onPick(e)}>
        <span className="w-14 shrink-0 text-right font-mono text-fg tabular-nums">{bitsLabel(e.bits)}</span>
        {e.family ? <FamChip family={e.family} /> : <span className="w-8 shrink-0 text-2xs text-subtle">brak</span>}
        <span className="min-w-0 flex-1 truncate text-muted">{label}</span>
        {extra}
        <span className="shrink-0 text-2xs text-subtle">{LEVEL_LABEL[e.level]}</span>
      </button>
    </li>
  );
}

const byBits = (x: GEdge, y: GEdge) => y.bits - x.bits;
const byDate = (x: GNode, y: GNode) => {
  if (x.observed_at === y.observed_at) return x.id.localeCompare(y.id);
  if (!x.observed_at) return 1;
  if (!y.observed_at) return -1;
  return x.observed_at.localeCompare(y.observed_at);
};

// ------------------------------------------------------------------------------------------------- overview
export interface OverviewPanelProps {
  overview: Overview;
  /** Number of campaigns whose detail is loaded (loader cache). */
  loaded: number;
  /** Number of campaign requests in flight. */
  loading: number;
  /** Loader budget in incidents (null = no budget). */
  budget: number | null;
  onFocusCampaign: (id: string) => void;
}

export function OverviewPanel({ overview, loaded, loading, budget, onFocusCampaign }: OverviewPanelProps) {
  const s = overview.stats;
  const top = useMemo(() => overview.campaigns.filter((c) => !c.unassigned).sort((a, b) => b.size - a.size || a.id.localeCompare(b.id)).slice(0, 12), [overview.campaigns]);
  const v = (f: (st: NonNullable<typeof s>) => number | null | undefined) => (s ? pl(f(s)) : "—");
  return (
    <div>
      <PanelHead kicker="Przegląd przebiegu" title={<span className="font-mono text-sm">{overview.run_id ?? "przebieg bez identyfikatora"}</span>}>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted">
          {overview.synthetic && <span className="inline-flex h-5 items-center rounded-[4px] border border-abstain bg-abstain-soft px-1.5 text-2xs font-semibold text-abstain-ink">dane SYNTETYCZNE</span>}
          {overview.corpus && <span>korpus: <span className="font-mono text-fg">{overview.corpus}</span></span>}
          <span>incydentów w przeglądzie: <span className="font-mono text-fg">{pl(overview.incidents_total)}</span></span>
        </div>
        {overview.source === "legacy" && (
          <p className="mt-2 text-xs text-subtle">
            Serwer nie ma jeszcze punktów końcowych poziomu szczegółowości (overview / kampania / sąsiedzi), więc pełny graf
            został pobrany jednorazowo i jest dzielony w przeglądarce. Liczby są te same, pierwsze wczytanie jest wolniejsze.
          </p>
        )}
      </PanelHead>

      <Section title="Statystyki przebiegu">
        <dl className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3">
          <Stat label="incydenty" value={v((x) => x.nodes)} />
          <Stat label="kampanie" value={v((x) => x.campaigns)} />
          <Stat label="kampanie wieloelementowe" value={v((x) => x.multi_campaigns)} />
          <Stat label="grupy kampanii" value={v((x) => x.groups)} />
          <Stat label="powiązania" value={v((x) => x.edges)} />
          <Stat label="składowe" value={v((x) => x.components)} />
        </dl>
        <h4 className="mt-3 text-2xs text-subtle">powiązania według poziomu dowodu</h4>
        <dl className="mt-1 space-y-0.5">
          {LEVELS.map((l) => (
            <div key={l} className="flex items-center gap-2 text-xs">
              <dt className="min-w-0 flex-1 truncate text-muted">{LEVEL_LABEL[l]}</dt>
              <dd className="font-mono text-fg tabular-nums">{s ? pl(s.levels[l] ?? 0) : "—"}</dd>
            </div>
          ))}
        </dl>
        <dl className="mt-3 space-y-0.5 text-xs">
          <div className="flex gap-2">
            <dt className="flex-1 text-muted">odporność (odporne / ocenione)</dt>
            <dd className="font-mono text-fg tabular-nums">{s ? (s.robustness_scored === 0 ? "nie oceniono" : `${pl(s.robust)} / ${pl(s.robustness_scored)}`) : "—"}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="flex-1 text-muted">decyzje analityka: musi łączyć / nie może łączyć</dt>
            <dd className="font-mono text-fg tabular-nums">{s ? `${pl(s.must_link)} / ${pl(s.cannot_link)}` : "—"}</dd>
          </div>
        </dl>
        {!s && <p className="mt-2 text-xs text-subtle">Serwer nie przesłał statystyk tego przebiegu; nie liczymy ich zastępczo w przeglądarce.</p>}
        {overview.thresholds && (
          <p className="mt-2 text-2xs text-subtle">
            progi przebiegu: kampania ≥ {plNumber(overview.thresholds.link_bits, 1)} b, zestaw / szablon ≥ {plNumber(overview.thresholds.kit_bits, 1)} b
          </p>
        )}
      </Section>

      <Section title="Jak czytać mapę">
        <ul className="space-y-1.5 text-xs text-muted">
          <li>
            <span className="text-fg">kolor linii</span> = dominująca rodzina genów powiązania:
            <span className="mt-1 flex flex-wrap gap-x-3 gap-y-1">
              {Object.keys(FAMILY_LABEL).map((f) => <FamChip key={f} family={f} withName />)}
            </span>
          </li>
          <li className="flex items-center gap-2">
            <span className="flex items-center gap-0.5"><LineKey style="solid" width={1} /><LineKey style="solid" width={2.5} /><LineKey style="solid" width={4} /></span>
            <span><span className="text-fg">grubość</span> = bity dowodu (skala logarytmiczna)</span>
          </li>
          <li className="flex items-center gap-2"><LineKey style="solid" />ciągła: {LEVEL_LABEL.campaign}</li>
          <li className="flex items-center gap-2"><LineKey style="dashed" />przerywana: {LEVEL_LABEL.kit}</li>
          <li className="flex items-center gap-2"><LineKey style="dotted" width={2.5} />kropkowana: {LEVEL_LABEL.asset} albo {LEVEL_LABEL.weak}</li>
          <li className="flex items-center gap-2">
            <svg aria-hidden="true" width="22" height="14" className="shrink-0"><circle cx="11" cy="7" r="6" fill="none" stroke="var(--color-muted)" strokeDasharray="2 2" /></svg>
            <span><span className="text-fg">przerywane nici i poświata</span> = grupa kampanii (łączy je zestaw / szablon)</span>
          </li>
          <li className="flex items-center gap-2">
            <svg aria-hidden="true" width="22" height="14" className="shrink-0"><circle cx="6" cy="7" r="3" fill="var(--color-accent)" opacity="0.5" /><circle cx="16" cy="7" r="5.5" fill="var(--color-accent)" opacity="0.5" /></svg>
            <span><span className="text-fg">blask kampanii</span>: wielkość = liczba incydentów, kolor = rodzina z największą sumą bitów powiązań wewnętrznych (szary: brak takich powiązań)</span>
          </li>
          <li className="flex items-center gap-2">
            <svg aria-hidden="true" width="22" height="14" className="shrink-0"><circle cx="11" cy="7" r="2" fill="var(--color-fg)" /><circle cx="11" cy="7" r="4.5" fill="none" stroke="var(--color-muted)" /><circle cx="11" cy="7" r="6.5" fill="none" stroke="var(--color-subtle)" /></svg>
            <span><span className="text-fg">pierścienie w kampanii</span> = wzrost w czasie (najstarszy incydent w środku)</span>
          </li>
        </ul>
        <p className="mt-2 text-xs text-subtle">{BITS_NOTE}</p>
      </Section>

      <Section title="Wczytywanie">
        <p className="text-xs text-muted" aria-live="polite">
          wczytane kampanie: <span className="font-mono text-fg">{pl(loaded)}</span>
          {budget !== null && <> (budżet {pl(budget)} incydentów)</>}, w toku: <span className="font-mono text-fg">{pl(loading)}</span>
        </p>
        <SectionNote>Incydenty kampanii są pobierane dopiero po jej rozwinięciu lub przybliżeniu.</SectionNote>
      </Section>

      <Section title="Największe kampanie">
        {top.length === 0 ? <p className="text-xs text-subtle">Przebieg nie zawiera kampanii.</p> : (
          <ol className="space-y-0.5" data-testid="top-campaigns">
            {top.map((c) => (
              <li key={c.id}>
                <button type="button" className={ROW_CLS} onClick={() => onFocusCampaign(c.id)}>
                  <span className="min-w-0 flex-1 truncate text-fg">{shortTitle(c.label, c.id, 44)}</span>
                  <span className="shrink-0 font-mono text-muted tabular-nums">{pl(c.size)}</span>
                  <span className="sr-only">incydentów</span>
                </button>
              </li>
            ))}
          </ol>
        )}
      </Section>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------- campaign
function CampaignSummaryBlock({ id }: { id: string }) {
  const q = useCampaignSummary(id);
  if (q.isPending) return <p className="text-xs text-subtle">wczytywanie podsumowania…</p>;
  if (q.isError || !q.data) return <p className="text-xs text-err">Brak podsumowania dla tej kampanii (serwer nie odpowiedział lub kampania nie ma dowodów).</p>;
  const d = q.data;
  const hasFam = Object.keys(d.family_bits).length > 0;
  return (
    <div className="text-sm text-fg">
      <p className="whitespace-pre-line">{d.text_pl}</p>
      {d.core_genes.length > 0 && (
        <ul className="mt-2 space-y-1">
          {d.core_genes.map((g, i) => (
            <li key={i} className="flex items-start gap-2 text-xs">
              <FamChip family={g.family} />
              <span className="min-w-0 flex-1 text-muted [overflow-wrap:anywhere]">{g.text}</span>
              <span className="shrink-0 font-mono text-fg tabular-nums">{formatBits(g.bits)}</span>
            </li>
          ))}
        </ul>
      )}
      {hasFam && (
        <div className="mt-2 flex items-center gap-2 text-2xs text-subtle">
          <FamilyBars bits={d.family_bits} /> bity per rodzina (VIS, TXT, INF, ID, NAR, META)
        </div>
      )}
      <p className="mt-1 text-2xs text-subtle">podsumowanie regułowe z dowodów kampanii</p>
    </div>
  );
}

export interface CampaignPanelProps {
  campaign: OvCampaign;
  group: OvGroup | null;
  detail: CampaignDetail | undefined;
  status: LoadStatus;
  error?: string;
  onRetry: () => void;
  onPickIncident: (id: string) => void;
  onPickEdge: (edge: GEdge) => void;
  onClose: () => void;
  /** Optional label lookup for other campaigns (external links); falls back to the id. */
  campaignLabel?: (id: string) => string | undefined;
}

const MEMBERS_SHOWN = 60;

export function CampaignPanel({ campaign, group, detail, status, error, onRetry, onPickIncident, onPickEdge, onClose, campaignLabel }: CampaignPanelProps) {
  const [allMembers, setAllMembers] = useState(false);
  const members = useMemo(() => (detail ? [...detail.nodes].sort(byDate) : []), [detail]);
  const titles = useMemo(() => new Map(members.map((n) => [n.id, n.title])), [members]);
  const strongest = useMemo(() => (detail ? [...detail.edges].sort(byBits).slice(0, 8) : []), [detail]);
  const external = useMemo(() => {
    const m = new Map<string, { n: number; max: number }>();
    for (const e of detail?.external ?? []) {
      const k = e.other_campaign ?? "";
      const cur = m.get(k) ?? { n: 0, max: 0 };
      m.set(k, { n: cur.n + 1, max: Math.max(cur.max, e.bits) });
    }
    return [...m].sort((a, b) => b[1].n - a[1].n || b[1].max - a[1].max).slice(0, 5);
  }, [detail]);
  const shown = allMembers ? members : members.slice(0, MEMBERS_SHOWN);

  return (
    <div data-testid="campaign-panel">
      <PanelHead kicker={campaign.unassigned ? "Incydenty bez kampanii" : "Kampania"} title={shortTitle(campaign.label, campaign.id, 80)} onClose={onClose}>
        <div className="font-mono text-2xs text-subtle [overflow-wrap:anywhere]">{campaign.id}</div>
        <dl className="mt-2 grid grid-cols-[110px_minmax(0,1fr)] gap-y-1 text-xs">
          <dt className="text-subtle">incydenty</dt><dd className="font-mono text-fg">{pl(campaign.size)}</dd>
          <dt className="text-subtle">pierwszy</dt><dd className="text-fg">{campaign.first_seen ? formatDatePl(campaign.first_seen) : "brak daty"}</dd>
          <dt className="text-subtle">ostatni</dt><dd className="text-fg">{campaign.last_seen ? formatDatePl(campaign.last_seen) : "brak daty"}</dd>
          <dt className="text-subtle">grupa</dt><dd className="min-w-0 truncate text-fg">{group ? shortTitle(group.label, group.id, 40) : "brak grupy"}</dd>
        </dl>
        {!campaign.unassigned && (
          <div className="mt-2">
            <Link className={LINK_CLS} to={`/campaigns/${enc(campaign.id)}`}>karta kampanii</Link>
          </div>
        )}
      </PanelHead>

      <Section title="Dlaczego ta kampania">
        {campaign.unassigned ? (
          <p className="text-xs text-muted">To są incydenty bez przypisanej kampanii: żaden zestaw dowodów nie przekroczył progu łączenia. Nie mają wspólnego podsumowania.</p>
        ) : (
          <CampaignSummaryBlock id={campaign.id} />
        )}
      </Section>

      {status === "loading" || status === "idle" ? (
        <Section title="Incydenty">
          <p className="sr-only" role="status">wczytywanie incydentów kampanii…</p>
          <ul aria-hidden="true" data-testid="members-skeleton" className="space-y-1.5">
            {Array.from({ length: Math.min(6, Math.max(1, campaign.size)) }, (_, i) => (
              <li key={i} className="flex items-center gap-2 motion-safe:animate-pulse">
                <span className="size-8 shrink-0 rounded-[4px] bg-hover" />
                <span className="h-3 flex-1 rounded-[2px] bg-hover" />
                <span className="h-3 w-12 rounded-[2px] bg-hover" />
              </li>
            ))}
          </ul>
          {status === "idle" && <p className="mt-2 text-2xs text-subtle">czeka w kolejce wczytywania</p>}
        </Section>
      ) : status === "error" ? (
        <Section title="Incydenty">
          <p className="text-xs text-err" role="alert">Nie udało się wczytać incydentów kampanii{error ? `: ${error}` : "."}</p>
          <Button size="sm" className="mt-2" onClick={onRetry}>Spróbuj ponownie</Button>
        </Section>
      ) : detail ? (
        <>
          <Section title={`Incydenty (${pl(members.length)})`}>
            {detail.truncated && (
              <p className="mb-2 text-xs text-abstain-ink">
                {detail.truncated_external
                  ? `Serwer przesłał najsilniejsze powiązania z innymi kampaniami; pominął ${pl(detail.truncated_external)} słabszych.`
                  : "Serwer przesłał niepełną listę incydentów lub powiązań tej kampanii."}
              </p>
            )}
            {members.length === 0 ? <p className="text-xs text-subtle">Serwer nie przesłał incydentów tej kampanii.</p> : (
              <ul className="space-y-0.5" data-testid="campaign-members">
                {shown.map((n) => (
                  <li key={n.id}>
                    <button type="button" className={ROW_CLS} onClick={() => onPickIncident(n.id)}>
                      <Thumb artifactId={n.thumbnail} alt="" size={32} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-fg">{shortTitle(n.title, n.id, 60)}</span>
                        <span className="block truncate font-mono text-2xs text-subtle">{n.id}</span>
                      </span>
                      <span className="shrink-0 text-2xs text-subtle">{n.observed_at ? formatDatePl(n.observed_at) : "brak daty"}</span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {members.length > shown.length && (
              <button type="button" onClick={() => setAllMembers(true)} className="mt-1 text-xs text-accent hover:underline">pokaż wszystkie ({pl(members.length)})</button>
            )}
          </Section>
          <Section title="Najsilniejsze powiązania wewnątrz">
            {strongest.length === 0 ? <p className="text-xs text-subtle">Brak powiązań wewnątrz tej kampanii w przesłanych danych.</p> : (
              <ul className="space-y-0.5">
                {strongest.map((e) => (
                  <EdgeRow key={e.id} e={e} onPick={onPickEdge} label={`${shortTitle(titles.get(e.source), e.source, 20)} ↔ ${shortTitle(titles.get(e.target), e.target, 20)}`} />
                ))}
              </ul>
            )}
            <p className="mt-1 text-2xs text-subtle">{BITS_NOTE}</p>
          </Section>
          <Section title={`Powiązania z innymi kampaniami (${pl(detail.external.length)})`}>
            {external.length === 0 ? <p className="text-xs text-subtle">Brak powiązań z innymi kampaniami w przesłanych danych.</p> : (
              <ul className="space-y-0.5 text-xs">
                {external.map(([other, v]) => (
                  <li key={other} className="flex items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-muted">{other ? shortTitle(campaignLabel?.(other), other, 40) : "nieznana kampania"}</span>
                    <span className="shrink-0 font-mono text-fg tabular-nums">{pl(v.n)}</span>
                    <span className="w-16 shrink-0 text-right font-mono text-2xs text-subtle">maks. {bitsLabel(v.max)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </>
      ) : null}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------- incident
export interface IncidentPanelProps {
  id: string;
  /** The incident as sent in the loaded campaign detail (shown while the full incident loads). */
  node: GNode | undefined;
  /** Links of this incident from the loaded campaign detail (internal and external). */
  edges: readonly (GEdge | ExternalEdge)[];
  onPickEdge: (edge: GEdge) => void;
  onPickIncident: (id: string) => void;
  onClose: () => void;
  /** Optional title lookup for the other incident of a link; falls back to the id. */
  titleOf?: (id: string) => string | null | undefined;
}

export function IncidentPanel({ id, node, edges, onPickEdge, onPickIncident, onClose, titleOf }: IncidentPanelProps) {
  const q = useIncident(id);
  const inc = q.data;
  const [sel, setSel] = useState<IncidentArtifactRow | null>(null);
  const top = useMemo(() => [...edges].sort(byBits).slice(0, 10), [edges]);
  const title = inc?.title ?? node?.title ?? null;
  const campaignId = inc?.campaign_id ?? node?.campaign_id ?? null;
  const observed = inc?.observed_at ?? node?.observed_at ?? null;
  return (
    <div>
      <PanelHead kicker="Incydent" title={title ?? "bez tytułu"} onClose={onClose}>
        <div className="font-mono text-2xs text-subtle [overflow-wrap:anywhere]">{id}</div>
        <dl className="mt-2 grid grid-cols-[110px_minmax(0,1fr)] gap-y-1 text-xs">
          <dt className="text-subtle">kampania</dt>
          <dd className="min-w-0 truncate">{campaignId ? <Link className="text-accent hover:underline" to={`/campaigns/${enc(campaignId)}`}>{campaignId}</Link> : "brak"}</dd>
          <dt className="text-subtle">obserwacja</dt><dd className="text-fg">{observed ? formatDatePl(observed) : "brak daty"}</dd>
          {inc && <><dt className="text-subtle">artefakty</dt><dd className="font-mono text-fg">{pl(inc.artifact_count)}</dd></>}
        </dl>
        <div className="mt-2">
          <Link className={LINK_CLS} to={`/incident/${enc(id)}`}>otwórz incydent</Link>
        </div>
      </PanelHead>

      <Section title="Materiały">
        {q.isPending ? (
          <p className="text-xs text-subtle">wczytywanie incydentu…</p>
        ) : q.isError || !inc ? (
          <p className="text-xs text-err">Nie udało się pobrać incydentu z serwera.</p>
        ) : inc.artifacts.length === 0 ? (
          <p className="text-xs text-subtle">Incydent nie ma zapisanych artefaktów (import nie przyniósł plików albo zostały pominięte); nie ma czego pokazać.</p>
        ) : (
          <>
            <ArtifactGallery artifacts={inc.artifacts} selected={sel?.artifact_id ?? null} onSelect={setSel} />
            {sel && (
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
                <span className="min-w-0 truncate font-mono">{sel.path_in_incident}</span>
                <Link className={LINK_CLS} to={`/incident/${enc(id)}?art=${enc(sel.artifact_id)}`}>pokaż w karcie incydentu</Link>
              </div>
            )}
          </>
        )}
      </Section>

      <Section title={`Powiązania (${pl(edges.length)})`}>
        {top.length === 0 ? (
          <p className="text-xs text-subtle">Wczytane dane kampanii nie zawierają powiązań tego incydentu.</p>
        ) : (
          <ul className="space-y-0.5">
            {top.map((e) => {
              const other = e.source === id ? e.target : e.source;
              const oc = "other_campaign" in e ? e.other_campaign : null;
              return (
                <EdgeRow
                  key={e.id}
                  e={e}
                  onPick={onPickEdge}
                  label={shortTitle(titleOf?.(other), other, 36)}
                  extra={oc ? <span className="shrink-0 text-2xs text-subtle" title={`inna kampania: ${oc}`}>inna kampania</span> : undefined}
                />
              );
            })}
          </ul>
        )}
        {edges.length > top.length && <p className="mt-1 text-2xs text-subtle">pokazano {top.length} najsilniejszych z {pl(edges.length)}</p>}
        {top.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {[...new Set(top.map((e) => (e.source === id ? e.target : e.source)))].slice(0, 5).map((o) => (
              <Button key={o} size="sm" variant="ghost" onClick={() => onPickIncident(o)} title={`przejdź do incydentu ${o}`}>
                {shortTitle(titleOf?.(o), o, 18)}
              </Button>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------- edge
export interface EdgePanelProps {
  edge: GEdge;
  a: GNode | undefined;
  b: GNode | undefined;
  onClose: () => void;
  onPickIncident: (id: string) => void;
}

export function EdgePanel({ edge, a, b, onClose, onPickIncident }: EdgePanelProps) {
  const pair = usePair(edge.source, edge.target);
  const s = pair.data?.score;
  const [showMaterials, setShowMaterials] = useState(false);
  const located = useMemo(() => (s ? locatedReasons([...s.reasons, ...s.contradictions]) : []), [s]);
  const sides: [string, GNode | undefined][] = [[edge.source, a], [edge.target, b]];
  return (
    <div>
      <PanelHead kicker="Powiązanie" title={`${LEVEL_LABEL[edge.level]}: ${bitsLabel(edge.bits)}`} onClose={onClose}>
        <div className="mt-2 grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)] gap-2">
          {sides.map(([id, n], i) => (
            <button key={id} type="button" className="min-w-0 rounded-md p-1 text-left hover:bg-hover" onClick={() => onPickIncident(id)}>
              {n?.thumbnail ? (
                <img src={thumbUrl(n.thumbnail, 320)} alt={`miniatura incydentu ${i === 0 ? "A" : "B"}`} loading="lazy" className="mb-1 aspect-video w-full rounded-md border border-line bg-raised object-cover" />
              ) : (
                <span className="mb-1 flex aspect-video w-full items-center justify-center rounded-md border border-line bg-raised text-2xs text-subtle">brak obrazu</span>
              )}
              <span className="block truncate font-mono text-2xs text-subtle">{i === 0 ? "A" : "B"}: {id}</span>
              <span className="line-clamp-2 block text-xs text-fg">{n?.title ?? "bez tytułu"}</span>
            </button>
          ))}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <span className="rounded-[4px] border border-line-strong px-1.5 py-0.5 text-fg">poziom: {LEVEL_LABEL[edge.level]}</span>
          {edge.family ? <FamChip family={edge.family} withName /> : <span className="text-subtle">brak dominującej rodziny</span>}
          {edge.inferred && <span className="text-subtle">wywnioskowane z wzbogacenia</span>}
          <ReviewBadgeG r={edge.review} />
        </div>
        <div className="mt-2"><RobustVerdict e={edge} /></div>
        <div className="mt-2 grid grid-cols-3 gap-2 text-2xs text-subtle">
          <div>kampania <span className="block font-mono text-xs text-fg">{bitsLabel(edge.campaign_bits)}</span></div>
          <div>zestaw <span className="block font-mono text-xs text-fg">{bitsLabel(edge.kit_bits)}</span></div>
          <div>zasób <span className="block font-mono text-xs text-fg">{bitsLabel(edge.asset_bits)}</span></div>
        </div>
      </PanelHead>

      <Section title="Kaskada bitów">
        <p className="mb-2 text-xs text-muted">
          suma: <span className="font-mono text-fg">{formatBits(s ? s.total_bits : edge.bits)}</span>. {BITS_NOTE}
        </p>
        {s ? (
          <Waterfall familyBits={s.family_bits} totalBits={s.total_bits} missing={s.missing_families} reasons={s.reasons} contradictions={s.contradictions} />
        ) : (
          <div className="text-xs">
            {pair.isError ? <p className="mb-2 text-err">Nie udało się pobrać dowodu pary; poniżej powody zapisane w grafie.</p> : <p className="mb-2 text-subtle">wczytywanie dowodu pary…</p>}
            <ReasonList reasons={edge.reasons} />
          </div>
        )}
      </Section>

      <Section title="Materiały">
        <div className="flex flex-wrap gap-2">
          <Link className={LINK_CLS} to={`/pair/${enc(edge.source)}/${enc(edge.target)}`}>porównaj materiały</Link>
          {located.length > 0 && (
            <Button size="sm" aria-expanded={showMaterials} onClick={() => setShowMaterials(!showMaterials)}>
              {showMaterials ? "ukryj materiały" : "pokaż materiały"}
            </Button>
          )}
        </div>
        {s && located.length === 0 && <p className="mt-2 text-xs text-subtle">Żaden zapisany powód nie wskazuje miejsca w artefaktach.</p>}
        {showMaterials && s && located.length > 0 && (
          <div className="mt-3"><EvidenceCompare a={edge.source} b={edge.target} reasons={[...s.reasons, ...s.contradictions]} /></div>
        )}
      </Section>
    </div>
  );
}
