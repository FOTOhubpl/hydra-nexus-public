import { CampaignAutoLinks } from "../../components/watch/meta/AutoLinks";
import { Link, useParams } from "react-router";
import { campaignAbusePackageUrl, campaignExportUrl, campaignStixUrl, campaignSupport, useCampaign, type CampaignCard, type CampaignEdge, type CampaignSupport, type ValueShare } from "../../api/task";
import { FamChip, GeneMatchTable, GeneValue, HypothesisBadge, Probability, Prov, SectionNote, Thumb } from "../../components/evidence";
import { AiAnalystPanel } from "../../components/AiAnalystPanel";
import { useLifecycle } from "../../api/visual";
import { LifecycleGantt } from "../../components/LifecycleGantt";
import { RevisionsPanel } from "../../components/RevisionsPanel";
import { RobustnessChart } from "../../components/RobustnessChart";
import { Panel } from "../../components/Panel";
import { StatTile } from "../../components/StatTile";
import { formatBits, formatDatePl, plNumber } from "../../lib/evidence";
import { schemesPl } from "../../lib/labelsPl";
import { CampaignLink, IncidentLink, Page, pairPath, QueryState } from "./common";
import { ConsistencyPanel } from "./ConsistencyPanel";
import { LineagePanel } from "./LineagePanel";
import { StaleCampaign, StaleRedirectNote } from "./StaleCampaign";
import { isApiError } from "../../api/client";
import { BITS_TITLE, formatDensity, SUPPORT_DISCLAIMER, WEAKEST_TITLE, WeakestFirst } from "./support";

const INFRA_LABEL: Record<string, string> = {
  domain: "Domeny",
  host: "Hosty",
  url: "Adresy URL",
  url_skeleton: "Szkielety URL",
  redirect_chain: "Łańcuchy przekierowań",
  ip: "Adresy IP",
  asn: "ASN / hosting",
  ns: "Serwery NS",
  cert_san: "Certyfikaty (SAN)",
  tracker_id: "Identyfikatory śledzące / piksele",
  tds_param: "Parametry TDS",
  favicon_mmh3: "Favicon (mmh3)",
};

function ShareTable({ rows }: { rows: ValueShare[] }) {
  return (
    <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
      <tbody>
        {rows.slice(0, 30).map((r) => (
          <tr key={`${r.kind}|${r.value}`} className="border-t border-line first:border-t-0">
            <td className="py-1 pr-2"><Prov value={r.provenance} /></td>
            <td className="max-w-[320px] truncate py-1 pr-2 text-fg"><GeneValue value={r.value} max={70} /></td>
            <td className="w-28 py-1 pr-2">
              <span className="flex items-center gap-1.5">
                <span className="h-1.5 w-14 overflow-hidden rounded-full bg-hover">
                  <span className="block h-full bg-muted" style={{ width: `${r.share * 100}%` }} />
                </span>
                <span className="font-mono text-muted">{Math.round(r.share * 100)}%</span>
              </span>
            </td>
            <td className="py-1 text-right text-subtle" title={r.incidents.join(", ")}>{r.incidents.length} inc.</td>
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

function EdgeList({ edges, external = false, idPrefix }: { edges: CampaignEdge[]; external?: boolean; idPrefix?: string }) {
  if (edges.length === 0) return <div className="text-xs text-subtle">Brak.</div>;
  return (
    <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
      <thead className="text-2xs tracking-wider text-subtle uppercase">
        <tr>
          <th className="py-1 text-left">Para</th>
          {external && <th className="py-1 text-left">Inna kampania</th>}
          <th className="py-1 text-left">Rodzina</th>
          <th className="py-1 text-left">Hipoteza</th>
          <th className="py-1 text-right">Bity</th>
          <th className="py-1 text-right" title="wynik nieskalibrowany (nie prawdopodobieństwo)">wynik nieskalibrowany</th>
        </tr>
      </thead>
      <tbody>
        {edges.slice(0, 40).map((e, i) => (
          <tr key={`${e.a}|${e.b}`} id={idPrefix ? `${idPrefix}-${i}` : undefined} className="scroll-mt-20 border-t border-line">
            <td className="py-1"><Link className="font-mono text-accent hover:underline" to={pairPath(e.a, e.b)}>{e.a} ↔ {e.b}</Link> <Link className="text-subtle hover:underline" to={`/diff/${encodeURIComponent(e.a)}/${encodeURIComponent(e.b)}`}>co się zmieniło</Link></td>
            {external && <td className="py-1"><CampaignLink id={e.other_campaign} /></td>}
            <td className="py-1">{e.dominant_family ? <FamChip family={e.dominant_family} /> : <span className="text-subtle">—</span>}</td>
            <td className="py-1"><HypothesisBadge value={e.hypothesis} /></td>
            <td className="py-1 text-right font-mono">{formatBits(e.total_bits)}</td>
            <td className="py-1 text-right"><Probability p={e.probability} /></td>
          </tr>
        ))}
      </tbody>
    </table></div>
  );
}

const FLAG = "inline-flex h-[18px] items-center rounded-[3px] border px-1 text-2xs font-medium";

/** CR-ASTRA-9 support of the campaign (meta.support): weakest member and bridges first, members, stability. */
function SupportPanels({ c, s }: { c: CampaignCard; s: CampaignSupport | null }) {
  if (!s) {
    return <SectionNote>Ten przebieg nie zawiera wsparcia kampanii (meta.support); wskaźniki bitowe są niedostępne.</SectionNote>;
  }
  if (s.singleton) {
    return <Panel title="Wsparcie członków"><WeakestFirst s={s} /></Panel>;
  }
  const bridgeEnds = new Set(s.bridges.flatMap((b) => [b.a, b.b]));
  const members = [...s.members].sort((x, y) => x.index - y.index);
  const fams = Object.entries(s.stability?.families ?? {}).sort(([a], [b]) => a.localeCompare(b));
  const fragile = new Set(s.stability?.fragile_families ?? []);
  return (
    <div className="space-y-3">
      <div className="grid gap-3 xl:grid-cols-[1.6fr_1fr]">
        <Panel title={`Wsparcie członków (${members.length})`} actions={<span className="text-xs text-subtle">najsłabsi pierwsi · indeks to heurystyka 0–1</span>} flush>
          <div className="space-y-2 p-3">
            <WeakestFirst s={s} />
            <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs" data-testid="member-support">
              <thead className="text-2xs tracking-wider text-subtle uppercase">
                <tr>
                  <th className="py-1 text-left">Incydent</th>
                  <th className="py-1 text-right" title="suma bitów (poziom kampanii) do pozostałych członków">Bity do reszty</th>
                  <th className="py-1 text-right" title="najsilniejsze bezpośrednie powiązanie członka: najsłabsze ogniwo, które go utrzymuje">Najsilniejsze ogniwo</th>
                  <th className="py-1 pl-2 text-left" title="niezależne rodziny genów za powiązaniami członka">Rodziny</th>
                  <th className="py-1 text-right">Indeks</th>
                  <th className="py-1 pl-2 text-left">Uwagi</th>
                </tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.id} className="border-t border-line">
                    <td className="py-1"><IncidentLink id={m.id} /></td>
                    <td className="py-1 text-right font-mono">{formatBits(m.bits_to_rest)}</td>
                    <td className="py-1 text-right font-mono">{formatBits(m.anchor_bits)}</td>
                    <td className="py-1 pl-2"><span className="inline-flex flex-wrap items-center gap-1">{m.families.map((f) => <FamChip key={f} family={f} />)}<span className="text-subtle">({m.n_families})</span></span></td>
                    <td className="py-1 text-right font-mono">{plNumber(m.index, 2)}</td>
                    <td className="py-1 pl-2">
                      <span className="inline-flex flex-wrap gap-1">
                        {m.articulation && <span className={`${FLAG} border-abstain bg-abstain-soft text-abstain-ink`} title="usunięcie tego członka dzieli kampanię">punkt artykulacji</span>}
                        {bridgeEnds.has(m.id) && <span className={`${FLAG} border-abstain bg-abstain-soft text-abstain-ink`} title="koniec powiązania-mostu">most</span>}
                        {m.analyst_link && <span className={`${FLAG} border-for bg-for-soft text-fg`} title="powiązanie potwierdzone przez analityka">analityk</span>}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          </div>
        </Panel>
        <div className="flex min-w-0 flex-col gap-3">
          <Panel title={`Mosty (${s.bridges.length})`} actions={<span className="text-xs text-subtle">powiązania, których usunięcie dzieli kampanię</span>}>
            {s.bridges.length === 0 ? <div className="text-xs text-subtle">Brak mostów: usunięcie jednego powiązania nie dzieli kampanii.</div> : (
              <ul className="space-y-1 text-xs">
                {s.bridges.map((b) => (
                  <li key={`${b.a}|${b.b}`}>
                    <Link className="font-mono text-accent hover:underline" to={pairPath(b.a, b.b)}>{b.a} ↔ {b.b}</Link>
                    <span className="text-muted"> · {formatBits(b.bits)} · po usunięciu części {b.split_sizes[0]} i {b.split_sizes[1]}</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          <Panel title="Stabilność bez jednej rodziny" actions={<span className="text-xs text-subtle">{s.stability ? (s.stability.stable ? "stabilna" : `krucha bez: ${s.stability.fragile_families.join(", ")}`) : ""}</span>}>
            {fams.length === 0 ? <div className="text-xs text-subtle">Brak testu stabilności w tym przebiegu.</div> : (
              <div className="overflow-x-auto overscroll-x-contain"><table className="w-full text-xs">
                <thead className="text-2xs tracking-wider text-subtle uppercase">
                  <tr>
                    <th className="py-1 text-left">Bez rodziny</th>
                    <th className="py-1 text-right">Części</th>
                    <th className="py-1 text-right" title="udział członków w największej części po usunięciu rodziny">Największa część</th>
                    <th className="py-1 pl-2 text-left">Odłączeni</th>
                  </tr>
                </thead>
                <tbody>
                  {fams.map(([f, st]) => (
                    <tr key={f} className="border-t border-line">
                      <td className="py-1"><span className="inline-flex items-center gap-1"><FamChip family={f} />{fragile.has(f) && <span className={`${FLAG} border-against bg-against-soft text-fg`}>krucha</span>}</span></td>
                      <td className="py-1 text-right font-mono">{st.parts}</td>
                      <td className="py-1 text-right font-mono">{plNumber(st.largest_share, 2)}</td>
                      <td className="py-1 pl-2">{st.detached.length === 0 ? <span className="text-subtle">—</span> : st.detached.map((id, i) => <span key={id}>{i > 0 && ", "}<IncidentLink id={id} /></span>)}</td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </Panel>
        </div>
      </div>
      <SectionNote>{SUPPORT_DISCLAIMER} Kampania: {c.members.length} incydentów, {formatDensity(s)}.</SectionNote>
    </div>
  );
}

/** The campaign route answered "campaign not found in this run": an id from an earlier run (B3). */
export function isStaleCampaignError(error: unknown): boolean {
  return isApiError(error) && error.isResourceNotFound && /campaign not/i.test(error.message);
}

export function CampaignCardPage() {
  const { id } = useParams();
  const q = useCampaign(id);
  const lc = useLifecycle(id);
  if (!q.data && id && isStaleCampaignError(q.error)) return <div className="p-4"><StaleCampaign id={id} /></div>;
  if (!q.data) return <div className="p-4"><QueryState query={q} what="kartę kampanii" endpoint={`/v1/campaigns/${id}`} /></div>;
  const c = q.data;
  const infraKinds = Object.keys(c.infrastructure).sort((a, b) => Object.keys(INFRA_LABEL).indexOf(a) - Object.keys(INFRA_LABEL).indexOf(b));
  const domains = c.host_count ?? (c.infrastructure.domain?.length ?? 0) + (c.infrastructure.host?.length ?? 0);
  const sup = campaignSupport(c);
  const personRows = Object.values(c.persons).flat();

  return (
    <Page
      title={<span className="inline-flex items-center gap-2"><span className="font-mono">{c.id}</span> · {c.label} <Prov value="HYP" /></span>}
      sub={<>przebieg <span className="font-mono">{c.run_id}</span> · status {c.status}{c.group_id && <> · grupa podobnych kampanii: <span className="font-mono">{c.group_id}</span> {c.group?.label && c.group.label !== c.group_id ? `„${c.group.label}”` : ""} {c.group && c.group.campaign_ids.length > 1 && <>(razem z {c.group.campaign_ids.filter((x) => x !== c.id).map((x, i) => <span key={x}>{i > 0 && ", "}<CampaignLink id={x} /></span>)})</>}</>}</>}
      actions={
        <>
          <a className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover" href={campaignExportUrl(c.id, "json")} download>↓ Eksport JSON</a>
          <a className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover" href={campaignExportUrl(c.id, "csv")} download>↓ Eksport CSV (geny)</a>
          <a className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover" href={campaignAbusePackageUrl(c.id)} download title="Pakiet zgłoszeń nadużyć: XARF JSON dla każdej domeny, notatki i manifest">↓ Pakiet abuse (XARF)</a>
          <a className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover" href={campaignStixUrl(c.id)} download={`stix-${c.id}.json`}>↓ STIX 2.1</a>
          <Link className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover" to={`/graph?campaign=${encodeURIComponent(c.id)}`}>⋈ Pokaż w grafie</Link>
        </>
      }
    >
      <StaleRedirectNote />
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8">
        <StatTile label="Incydentów" value={c.members.length} />
        <StatTile label="Domen / hostów" value={domains} />
        <StatTile label="Dowody (mediana)" value={sup && sup.median_link_bits !== null ? <span title={BITS_TITLE}>{formatBits(sup.median_link_bits)}</span> : null} hint={sup?.rank ? `ranking heurystyczny #${sup.rank} z ${sup.rank_of}` : `${c.internal_pairs.length} par wewnętrznych`} />
        <StatTile label="Najsłabsze ogniwo" value={sup && sup.weakest_link_bits !== null ? <span title={WEAKEST_TITLE}>{formatBits(sup.weakest_link_bits)}</span> : null} hint={sup && sup.bridges.length > 0 ? `mosty: ${sup.bridges.length}` : "bity, nie prawdopodobieństwo"} />
        <StatTile label="Gęstość" value={sup && !sup.singleton ? plNumber(sup.density, 2) : null} hint={sup && !sup.singleton ? formatDensity(sup) : undefined} />
        <StatTile label="Indeks spójności (heurystyka)" value={sup?.cohesion !== null && sup?.cohesion !== undefined ? plNumber(sup.cohesion, 2) : null} hint="średni indeks członków 0–1, nie prawdopodobieństwo" />
        <StatTile label="Schemat" value={c.scheme ? <span title={`kod: ${c.scheme}`}>{schemesPl(c.scheme)}</span> : null} />
        <StatTile label="Okres" value={c.first_seen ? formatDatePl(c.first_seen).split(",")[0] : null} hint={c.last_seen ? `do ${formatDatePl(c.last_seen)}` : "brak dat w materiale"} />
      </div>

      <SupportPanels c={c} s={sup} />
      {c.members.length > 1 && <ConsistencyPanel campaignId={c.id} runId={c.run_id} />}

      <AiAnalystPanel kind="campaign" a={c.id} />
      <div className="grid gap-3 xl:grid-cols-[1.5fr_1fr]">
        <Panel title="Cykl życia kampanii" actions={<span className="text-xs text-subtle">materiały, rejestracje domen, certyfikaty, ponowne użycie plików</span>}>
          {lc.data ? <LifecycleGantt lc={lc.data} /> : <div className="text-xs text-subtle">Ładowanie…</div>}
        </Panel>
        <Panel title="Odporność powiązań" actions={<span className="text-xs text-subtle">najsilniejsza para kampanii bez jednej rodziny</span>}>
          {lc.data?.robustness.length ? (() => { const best = [...lc.data.robustness].sort((x, y) => (y.support ?? 0) - (x.support ?? 0))[0]!; return (
            <div className="space-y-2">
              <div className="text-xs text-muted">para <Link className="font-mono text-accent hover:underline" to={pairPath(best.a, best.b)}>{best.a} ↔ {best.b}</Link> · odporne pary: {lc.data.robustness.filter((r) => r.robustness.robust_link).length}/{lc.data.robustness.length}</div>
              <RobustnessChart r={best.robustness} support={best.support} threshold={lc.data.link_threshold} />
            </div>); })() : <div className="text-xs text-subtle">Brak testu odporności dla par tej kampanii.</div>}
        </Panel>
      </div>
      {c.members.length > 1 && <LineagePanel campaignId={c.id} runId={c.run_id} />}
      <CampaignAutoLinks campaignId={c.id} runId={c.run_id} />
      <Panel title="Historia rewizji" actions={<span className="text-xs text-subtle">recenzje analityka, które zmieniły grupowanie</span>}><RevisionsPanel campaignId={c.id} /></Panel>
      <div className="grid gap-3 xl:grid-cols-[1.6fr_1fr]">
        <Panel title="Genom rdzenia" actions={<span className="text-xs text-subtle">geny wspólne dla większości członków · klucze do pivotu i zgłoszeń</span>} flush>
          <div className="p-3">
            <GeneMatchTable rows={c.core_genome} idPrefix="ev-core_genome" limit={200} empty="Przebieg nie wyznaczył genomu rdzenia." />
          </div>
        </Panel>
        <Panel title="Oś czasu" actions={<span className="text-xs text-subtle">najwcześniejsza data z materiału</span>}>
          <ol className="space-y-1.5">
            {c.timeline.map((t) => (
              <li key={t.incident_id} className="flex items-baseline gap-3 text-sm">
                <span className="w-40 shrink-0 font-mono text-xs text-muted">{t.observed_at ? formatDatePl(t.observed_at) : "data nieznana"}</span>
                <span className="shrink-0 whitespace-nowrap"><IncidentLink id={t.incident_id} /></span>
                <span className="truncate text-muted">{t.title}</span>
              </li>
            ))}
          </ol>
        </Panel>
      </div>

      <Panel title={`Członkowie (${c.member_incidents.length})`}>
        <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] gap-3">
          {c.member_incidents.map((m) => (
            <Link key={m.id} to={`/incident/${encodeURIComponent(m.id)}`} className="group flex flex-col gap-1.5 rounded-md border border-line p-2 hover:border-accent">
              <Thumb artifactId={m.thumbnail} alt={m.title ?? m.id} size={160} className="!h-28 !w-full" />
              <span className="font-mono text-xs text-accent">{m.id}</span>
              <span className="line-clamp-2 text-xs text-fg">{m.title ?? <span className="text-subtle">bez tytułu</span>}</span>
              <span className="flex flex-wrap gap-1.5 text-2xs text-subtle">
                {Object.entries(m.observed_genes).map(([f, n]) => <span key={f}>{f} {n}</span>)}
              </span>
            </Link>
          ))}
        </div>
      </Panel>

      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title="Infrastruktura" actions={<span className="text-xs text-subtle">domeny, hosty, IP, trackery z genów INF członków</span>}>
          {infraKinds.length === 0 ? (
            <div className="text-xs text-subtle">Brak zaobserwowanych genów infrastruktury u członków.</div>
          ) : (
            <div className="space-y-3">
              {infraKinds.map((k) => (
                <div key={k}>
                  <div className="mb-1 text-xs font-semibold text-muted">{INFRA_LABEL[k] ?? k} <span className="font-normal text-subtle">({c.infrastructure[k]!.length})</span></div>
                  <ShareTable rows={c.infrastructure[k]!} />
                </div>
              ))}
            </div>
          )}
        </Panel>
        <Panel title="Osoby i wizerunki" actions={<span className="text-xs text-subtle">geny rodziny ID</span>}>
          {personRows.length === 0 ? (
            <div className="text-xs text-subtle">Brak zaobserwowanych genów tożsamości (nieznane, nie dowód przeciw).</div>
          ) : (
            <ShareTable rows={personRows} />
          )}
        </Panel>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <Panel title={`Powiązania wewnątrz (${c.internal_pairs.length})`}><EdgeList edges={c.internal_pairs} idPrefix="ev-internal_pairs" /></Panel>
        <Panel title={`Powiązania z innymi kampaniami (${c.cross_links.length})`}>
          <EdgeList edges={c.cross_links} external idPrefix="ev-cross_links" />
          <div className="mt-2"><SectionNote>Pary z jednym końcem poza kampanią: wspólny szablon lub zasób, kandydaci do scalenia, mosty.</SectionNote></div>
        </Panel>
      </div>
      <SectionNote>Bity to wagi dowodu (log₂ ilorazu wiarygodności), nie prawdopodobieństwa. {SUPPORT_DISCLAIMER}</SectionNote>
    </Page>
  );
}
