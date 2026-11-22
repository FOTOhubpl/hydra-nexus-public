/**
 * BONUS-AUTO client: the "Automatyczne wyszukiwanie" overview (`GET /v1/watch/rt/meta/auto?mode=`) and the
 * per-campaign "new material" links (`GET /v1/watch/rt/meta/campaigns/{id}/links?run=`). Every number comes from the
 * server (meta_* / watch_* tables); nothing is computed or invented here. Query keys live under ["meta-ads", ...] so
 * the existing Meta invalidations (stream `meta.analysis`, config save) refresh them too.
 */
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "./client";
import type { MetaMode, MetaStatus, QueueCounts } from "./metaAds";

const BASE = "/v1/watch/rt/meta";

export interface AutoCycle {
  mode: MetaMode;
  trigger: string;
  trigger_pl: string;
  started_at: string;
  finished_at: string;
  status: string;
  message_pl: string;
  requests: number;
  pages: number;
  records: number;
  ads_seen: number;
  new_ads: number;
  changed_ads: number;
  unchanged_ads: number;
  ended_ads: number;
  queued_analyses: number;
}

export interface LinkedAd {
  analysis_id: number;
  mode: MetaMode;
  mode_label_pl: string;
  ad_id: string;
  revision: number | null;
  current_revision: boolean;
  incident_id: string | null;
  base_run_id: string | null;
  campaign_id: string;
  campaign_label: string | null;
  page_id: string | null;
  page_name: string | null;
  first_observed_at: string | null;
  last_observed_at: string | null;
  analysed_at: string;
  triage_priority: string;
  triage_priority_pl: string;
  review_status: string;
  total_bits: number | null;
  probability: number | null;
  evidence: { kind: string; value: string; bits: number | null }[];
  reason_pl: string | null;
  library_link: string | null;
}

export interface WatchLink {
  match_id: number;
  source: string;
  source_pl: string;
  indicator_kind: string;
  indicator_display: string;
  campaign_id: string;
  campaign_label: string | null;
  run_id: string | null;
  strength: string;
  bits: number | null;
  status: string;
  replay: boolean;
  matched_at: string;
  item_first_seen: string | null;
  item_defanged: string | null;
}

export interface AutoSource {
  id: string;
  label_pl: string;
  enabled: boolean;
  state: string;
  interval_s: number;
  next_due: string | null;
  last_poll: string | null;
  last_ok: string | null;
  total_polls: number;
  total_items: number;
  last_error: string | null;
}

export interface AutoOverview {
  mode: MetaMode;
  mode_label_pl: string;
  rules: {
    keywords_sent: string[];
    page_ids_sent: string[];
    countries: string[];
    ad_active_status: string;
    languages: string[];
    date_window_days: number;
    overlap_days: number;
    date_from: string | null;
    date_to: string | null;
    platforms: string[];
    local_keywords_default: number;
    local_keywords: string[];
    celebrities: string[];
    page_name_patterns: string[];
    same_text_min_pages: number;
    alert_min_priority: string;
    expansion: { enabled?: boolean; max_depth?: number; max_pages?: number };
  };
  sources: AutoSource[];
  cycles: AutoCycle[];
  last_update: string | null;
  totals: {
    cycles_recorded: number;
    unique_ads: number;
    records_fetched: number;
    new_revisions: number;
    analyses_done: number;
    analyses_partial: number;
    assigned: number;
    new_patterns: number;
    abstained: number;
    alerts_open: number;
    analyst_confirmed: number;
    analyst_dismissed: number;
    linked_campaigns: number;
  };
  queue: QueueCounts;
  triage: Record<string, number>;
  linked_ads: LinkedAd[];
  watch_matches: { counts: { total: number; to_campaigns: number; new: number; new_to_campaigns: number }; latest: WatchLink[] };
  collector: MetaStatus["collector"];
  budget: MetaStatus["budget"];
  offline: boolean;
  token_configured: boolean;
  engine: { state: string; replay: boolean };
}

export interface CampaignLinks {
  campaign_id: string;
  run_id: string | null;
  meta_ads: LinkedAd[];
  watch_matches: WatchLink[];
  note_pl: string;
}

export const autoKeys = {
  overview: (mode: MetaMode) => ["meta-ads", "auto", mode] as const,
  campaign: (id: string, run: string | null | undefined) => ["meta-ads", "campaign-links", id, run ?? ""] as const,
};

export const autoApi = {
  overview: (mode: MetaMode, signal?: AbortSignal) => apiFetch<AutoOverview>(`${BASE}/auto?mode=${mode}`, { signal }),
  campaign: (id: string, run: string | null | undefined, signal?: AbortSignal) =>
    apiFetch<CampaignLinks>(`${BASE}/campaigns/${encodeURIComponent(id)}/links${run ? `?run=${encodeURIComponent(run)}` : ""}`, { signal }),
};

export function useAutoOverview(mode: MetaMode) {
  return useQuery({ queryKey: autoKeys.overview(mode), queryFn: ({ signal }) => autoApi.overview(mode, signal), refetchInterval: 5000 });
}

export function useCampaignLinks(id: string | undefined, run: string | null | undefined) {
  return useQuery({
    queryKey: autoKeys.campaign(id ?? "", run),
    queryFn: ({ signal }) => autoApi.campaign(id!, run, signal),
    enabled: !!id,
    refetchInterval: 15000,
  });
}

export const PRIORITY_PL: Record<string, string> = { unknown: "brak oceny", low: "niski", medium: "średni", high: "wysoki" };
