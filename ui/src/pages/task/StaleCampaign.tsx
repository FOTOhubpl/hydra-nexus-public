/**
 * B3 (audit f450a70): campaign ids are derived per analysis run, so a link copied before a review decision (which
 * triggers a re-run) points at an id that no longer exists. Instead of the generic "API endpoint not available"
 * state, look the old id up in earlier analyse runs (GET /v1/campaigns/{id}?run_id=…), find the current campaign
 * holding most of its members and redirect there; otherwise explain the id change and offer the incidents.
 */
import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, useLocation } from "react-router";
import { apiFetch, isApiError } from "../../api/client";
import type { Campaign, CampaignCard, Page } from "../../api/task";
import { EmptyState } from "../../components/EmptyState";
import { IncidentLink, Loading } from "./common";

export const STALE_CAMPAIGN_TITLE = "Ta kampania nie istnieje w bieżącym przebiegu";
export const STALE_CAMPAIGN_TEXT = "Ta kampania nie istnieje w bieżącym przebiegu (identyfikatory zmieniają się po ponownej analizie).";
const MAX_RUNS = 12;

export interface StaleCampaignLookup {
  /** Run in which the old id was found, with its members; null when no earlier run knows the id. */
  oldRun: string | null;
  oldLabel: string | null;
  members: string[];
  successor: { id: string; label: string; overlap: number } | null;
}

/** Current campaign sharing most members with the old one (ties: larger overlap share of the candidate). */
export function bestSuccessor(members: string[], campaigns: Pick<Campaign, "id" | "label" | "members">[]): StaleCampaignLookup["successor"] {
  const old = new Set(members);
  let best: StaleCampaignLookup["successor"] = null;
  let bestShare = 0;
  for (const c of campaigns) {
    const overlap = (c.members ?? []).filter((m) => old.has(m)).length;
    if (overlap === 0) continue;
    const share = overlap / Math.max(1, c.members.length);
    if (!best || overlap > best.overlap || (overlap === best.overlap && share > bestShare)) {
      best = { id: c.id, label: c.label, overlap };
      bestShare = share;
    }
  }
  return best;
}

export async function lookupStaleCampaign(id: string, signal?: AbortSignal): Promise<StaleCampaignLookup> {
  const enc = encodeURIComponent;
  const current = await apiFetch<Page<Campaign>>("/v1/campaigns?limit=2000", { signal });
  const runs = await apiFetch<{ items: { id: string }[] }>(`/v1/runs?kind=analyse&limit=${MAX_RUNS + 1}`, { signal });
  for (const r of runs.items) {
    if (r.id === current.run_id) continue;
    try {
      const card = await apiFetch<CampaignCard>(`/v1/campaigns/${enc(id)}?run_id=${enc(r.id)}`, { signal });
      const members = card.members ?? [];
      return { oldRun: r.id, oldLabel: card.label ?? null, members, successor: bestSuccessor(members, current.items) };
    } catch (err) {
      if (isApiError(err) && err.status === 404) continue;
      throw err;
    }
  }
  return { oldRun: null, oldLabel: null, members: [], successor: null };
}

export interface StaleRedirectState {
  staleFrom?: { id: string; oldRun: string; overlap: number; total: number };
}

/** Banner on the successor's card after a redirect from an old id. */
export function StaleRedirectNote() {
  const st = (useLocation().state ?? {}) as StaleRedirectState;
  const f = st.staleFrom;
  if (!f) return null;
  return (
    <div role="status" data-testid="stale-redirect-note" className="rounded-md border border-line bg-raised px-3 py-2 text-sm text-fg">
      Kampania <span className="font-mono">{f.id}</span> (przebieg <span className="font-mono">{f.oldRun}</span>) nie istnieje w bieżącym przebiegu,
      bo identyfikatory zmieniają się po ponownej analizie. Pokazujemy kampanię, która zawiera {f.overlap} z {f.total} jej incydentów.
    </div>
  );
}

export function StaleCampaign({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["stale-campaign", id], queryFn: ({ signal }) => lookupStaleCampaign(id, signal), retry: false, staleTime: 60_000 });
  if (q.isPending) return <Loading what="poprzednie przebiegi tej kampanii" />;
  const found = q.data;
  if (found?.successor && found.oldRun) {
    const state: StaleRedirectState = { staleFrom: { id, oldRun: found.oldRun, overlap: found.successor.overlap, total: found.members.length } };
    return <Navigate replace to={`/campaigns/${encodeURIComponent(found.successor.id)}`} state={state} />;
  }
  const members = found?.members ?? [];
  const searchTo = members.length ? `/search?q=${encodeURIComponent(members[0]!)}` : "/incidents";
  return (
    <EmptyState
      tone="neutral"
      testId="stale-campaign"
      title={STALE_CAMPAIGN_TITLE}
      description={
        <>
          <span className="block">{STALE_CAMPAIGN_TEXT}</span>
          {members.length > 0 && (
            <span className="mt-2 block">
              W przebiegu <span className="font-mono">{found?.oldRun}</span> należały do niej incydenty:{" "}
              {members.map((m, i) => <span key={m}>{i > 0 && ", "}<IncidentLink id={m} /></span>)}. Karta incydentu pokazuje jego bieżącą kampanię.
            </span>
          )}
        </>
      }
      detail={<span className="font-mono">{id}</span>}
      action={
        <span className="inline-flex flex-wrap gap-2">
          <Link to={searchTo} className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover">Szukaj po incydentach tej kampanii</Link>
          <Link to="/campaigns" className="inline-flex h-7 items-center rounded-[4px] border border-line-strong bg-panel px-3 text-sm hover:bg-hover">Przejdź do listy kampanii</Link>
        </span>
      }
    />
  );
}
