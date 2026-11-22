/**
 * BONUS-AUTO "nowe materiały powiązane z wcześniej wykrytymi kampaniami": Meta ads the assigner put into an EXISTING
 * campaign of its base run, and WATCH-RT matches (CERT Polska, URLhaus, OpenPhish, crt.sh) of campaign indicators.
 * Used on the campaign card (CampaignAutoLinks) and in the "Automatyczne wyszukiwanie" overview. Values come from
 * /v1/watch/rt/meta/auto and /v1/watch/rt/meta/campaigns/{id}/links.
 */
import { Link } from "react-router";
import { useCampaignLinks, type LinkedAd, type WatchLink } from "../../../api/autoSearch";
import { Badge } from "../../Badge";
import { Panel } from "../../Panel";

function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "short" });
}

const MODE_VARIANT = { live: "success", recorded: "info", synthetic: "warning" } as const;

export function LinkedAdsTable({ ads, showCampaign = true }: { ads: LinkedAd[]; showCampaign?: boolean }) {
  if (!ads.length) return <p className="text-xs text-muted" data-testid="linked-ads-empty">Brak reklam przypisanych do znanych kampanii.</p>;
  return (
    <div className="overflow-x-auto overscroll-x-contain">
      <table className="w-full text-xs" data-testid="linked-ads">
        <thead className="text-left text-subtle">
          <tr>
            <th className="py-1 pr-2 font-medium">Reklama</th>
            <th className="py-1 pr-2 font-medium">Strona reklamodawcy</th>
            {showCampaign && <th className="py-1 pr-2 font-medium">Kampania (przebieg bazowy)</th>}
            <th className="py-1 pr-2 font-medium">Pierwsza obserwacja</th>
            <th className="py-1 pr-2 font-medium">Dowód (bity)</th>
            <th className="py-1 pr-2 font-medium">Ocena wstępna</th>
            <th className="py-1 pr-2 font-medium">Incydent</th>
          </tr>
        </thead>
        <tbody>
          {ads.map((a) => (
            <tr key={`${a.mode}-${a.analysis_id}`} className="border-t border-line align-top" data-testid="linked-ad-row" data-ad-id={a.ad_id}>
              <td className="py-1 pr-2">
                <div className="flex flex-col gap-0.5">
                  <Link to={`/watch?view=meta${a.mode === "live" ? "" : `&mode=${a.mode}`}`} className="font-mono text-accent hover:underline">{a.ad_id}</Link>
                  <span><Badge variant={MODE_VARIANT[a.mode] ?? "neutral"}>{a.mode_label_pl}</Badge></span>
                  {a.library_link && <a href={a.library_link} target="_blank" rel="noreferrer noopener" className="text-2xs text-accent hover:underline">Ad Library ↗</a>}
                </div>
              </td>
              <td className="max-w-48 py-1 pr-2 text-fg">{a.page_name ?? "—"}{a.page_id && <div className="font-mono text-2xs text-muted">{a.page_id}</div>}</td>
              {showCampaign && (
                <td className="py-1 pr-2">
                  <Link to={`/campaigns/${encodeURIComponent(a.campaign_id)}`} className="font-mono text-accent hover:underline">{a.campaign_id}</Link>
                  {a.campaign_label && <div className="max-w-40 truncate text-fg">{a.campaign_label}</div>}
                  {a.base_run_id && <div className="font-mono text-2xs text-muted">{a.base_run_id}</div>}
                </td>
              )}
              <td className="py-1 pr-2 whitespace-nowrap text-fg">{when(a.first_observed_at)}</td>
              <td className="py-1 pr-2">
                <div className="font-mono text-fg">{a.total_bits != null ? `${a.total_bits.toFixed(1)} bit` : "—"}</div>
                <ul className="mt-0.5 flex flex-col gap-0.5" data-testid="linked-ad-evidence">
                  {a.evidence.slice(0, 4).map((e, i) => (
                    <li key={i} className="max-w-64 truncate text-2xs text-muted" title={e.value}>
                      <span className="font-mono">{e.kind}</span> {e.value} {e.bits != null && <span className="font-mono">({e.bits.toFixed(1)})</span>}
                    </li>
                  ))}
                </ul>
              </td>
              <td className="py-1 pr-2 whitespace-nowrap">{a.triage_priority_pl}<div className="text-2xs text-muted">{a.review_status === "unreviewed" ? "nieprzejrzane" : a.review_status === "confirmed_by_analyst" ? "potwierdzone" : "odrzucone"}</div></td>
              <td className="py-1 pr-2">{a.incident_id ? <Link to={`/incident/${encodeURIComponent(a.incident_id)}`} className="font-mono text-accent hover:underline" data-testid="linked-ad-incident">{a.incident_id}</Link> : "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function WatchLinksTable({ matches }: { matches: WatchLink[] }) {
  if (!matches.length) return <p className="text-xs text-muted" data-testid="watch-links-empty">Brak dopasowań wskaźników kampanii w źródłach publicznych.</p>;
  return (
    <div className="overflow-x-auto overscroll-x-contain">
      <table className="w-full text-xs" data-testid="watch-links">
        <thead className="text-left text-subtle">
          <tr>
            <th className="py-1 pr-2 font-medium">Źródło</th>
            <th className="py-1 pr-2 font-medium">Wskaźnik kampanii</th>
            <th className="py-1 pr-2 font-medium">Wpis w źródle</th>
            <th className="py-1 pr-2 font-medium">Kampania</th>
            <th className="py-1 pr-2 font-medium">Siła</th>
            <th className="py-1 pr-2 font-medium">Wykryto</th>
          </tr>
        </thead>
        <tbody>
          {matches.map((m) => (
            <tr key={m.match_id} className="border-t border-line align-top" data-testid="watch-link-row">
              <td className="py-1 pr-2 text-fg">{m.source_pl}{m.replay && <div><Badge variant="warning">ODTWORZENIE</Badge></div>}</td>
              <td className="py-1 pr-2"><span className="text-subtle">{m.indicator_kind}</span> <span className="font-mono text-fg">{m.indicator_display}</span></td>
              <td className="max-w-56 truncate py-1 pr-2 font-mono text-muted" title={m.item_defanged ?? undefined}>{m.item_defanged ?? "—"}</td>
              <td className="py-1 pr-2"><Link to={`/campaigns/${encodeURIComponent(m.campaign_id)}`} className="font-mono text-accent hover:underline">{m.campaign_id}</Link>{m.campaign_label && <div className="max-w-40 truncate">{m.campaign_label}</div>}</td>
              <td className="py-1 pr-2">{m.strength === "strong" ? "silne" : "słabe"}{m.bits != null && <span className="ml-1 font-mono text-muted">{m.bits.toFixed(1)} bit</span>}</td>
              <td className="py-1 pr-2 whitespace-nowrap">{when(m.matched_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Campaign card section: new Meta ads and WATCH-RT matches linked to this campaign after the base analysis. */
export function CampaignAutoLinks({ campaignId, runId }: { campaignId: string; runId: string | null | undefined }) {
  const q = useCampaignLinks(campaignId, runId);
  const d = q.data;
  return (
    <div className="flex flex-col gap-4" data-testid="campaign-auto-links">
      <Panel
        title={`Nowe reklamy z biblioteki Meta powiązane z tą kampanią (${d?.meta_ads.length ?? 0})`}
        actions={<Link to="/watch?view=auto" className="text-xs text-accent hover:underline">Automatyczne wyszukiwanie →</Link>}
      >
        <div data-testid="campaign-meta-links">
          {q.isError ? <p className="text-xs text-err">Nie udało się odczytać powiązanych reklam.</p> : d ? <LinkedAdsTable ads={d.meta_ads} showCampaign={false} /> : <p className="text-xs text-muted">Wczytywanie…</p>}
        </div>
      </Panel>
      <Panel title={`Wskaźniki kampanii w źródłach publicznych: CERT Polska, URLhaus, OpenPhish, crt.sh (${d?.watch_matches.length ?? 0})`}>
        <div data-testid="campaign-watch-links">
          {d ? <WatchLinksTable matches={d.watch_matches} /> : null}
          {d && <p className="mt-2 text-2xs text-muted">{d.note_pl}</p>}
        </div>
      </Panel>
    </div>
  );
}
