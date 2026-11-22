/**
 * Client for the Meta Ad Library collector (`/v1/watch/rt/meta/*`, META-COLLECT): collector state and own budget,
 * monitoring configuration, layered contract diagnostics, collected ads with revisions and artefacts, the durable
 * analysis queue, analyses (correlation, fraud triage and manipulation signals kept separate) and reports.
 *
 * Every value shown comes from these endpoints; nothing is computed or invented client-side. Data of the three
 * modes (live / recorded / synthetic) is fetched separately and never mixed. Query keys live under ["meta-ads", ...].
 * The status is polled every 5 s, so the view re-reads the state after any stream interruption.
 */
import { useQuery } from "@tanstack/react-query";
import { apiFetch, apiUrl } from "./client";

// ------------------------------------------------------------------ wire types

export type MetaMode = "live" | "recorded" | "synthetic";
export const META_MODES: MetaMode[] = ["live", "recorded", "synthetic"];

export const MODE_BADGE: Record<MetaMode, string> = { live: "NA ŻYWO", recorded: "ODTWORZENIE", synthetic: "DANE SYNTETYCZNE" };
export const MODE_TAB: Record<MetaMode, string> = { live: "Na żywo", recorded: "Odtworzenie nagrania", synthetic: "Dane syntetyczne" };
export const MODE_BANNER: Record<MetaMode, string> = {
  live: "NA ŻYWO — rekordy pobrane z Meta Ad Library przez ten kolektor",
  recorded: "ODTWORZENIE — wyniki z zapisanego, zredagowanego nagrania odpowiedzi Meta, nie z bieżącego zapytania",
  synthetic: "DANE SYNTETYCZNE — wyniki z zestawu testowego, nie z Meta Ad Library",
};

export function parseMode(v: string | null | undefined): MetaMode {
  return v === "recorded" || v === "synthetic" ? v : "live";
}

export type CollectorState =
  | "disabled" | "idle" | "running" | "paused" | "deferred_budget" | "rate_limited" | "needs_config" | "offline" | "error" | "engine_stopped";

export interface MetaQuery {
  id?: string;
  kind: "keyword" | "page_ids";
  value: string;
  label?: string | null;
  enabled?: boolean;
  origin?: string;
  last_status?: string | null;
  last_run?: string | null;
}

export interface MetaConfig {
  countries: string[];
  queries: MetaQuery[];
  ad_active_status: "ALL" | "ACTIVE" | "INACTIVE";
  languages: string[];
  date_window_days: number;
  overlap_days: number;
  budget_per_h: number;
  max_active_queries: number;
  max_pages_per_query: number;
  interval_s: number;
  max_analyses_per_h: number;
  max_media_bytes: number;
  media_fetch: boolean;
  landing_fetch: boolean;
  expansion: { enabled: boolean; max_depth: number; max_pages: number };
  /** BONUS-AUTO rules sent to Meta: ad_delivery_date_min / _max floor and cap, publisher_platforms. */
  date_from?: string | null;
  date_to?: string | null;
  platforms?: string[];
  /** BONUS-AUTO local rules (triage only, never sent anywhere). */
  rules?: MetaRules;
  config_version: number;
}

export interface MetaRules {
  keywords: string[];
  celebrities: string[];
  page_name_patterns: string[];
  same_text_min_pages: number;
  alert_min_priority: "low" | "medium" | "high";
}

export const PLATFORMS: [string, string][] = [
  ["FACEBOOK", "Facebook"], ["INSTAGRAM", "Instagram"], ["MESSENGER", "Messenger"], ["AUDIENCE_NETWORK", "Audience Network"],
  ["WHATSAPP", "WhatsApp"], ["THREADS", "Threads"], ["OCULUS", "Oculus"],
];

export interface DiagLayer { id: string; label_pl: string; status: "pass" | "fail" | "skipped" | "not_run" | string; message_pl: string }

export interface Diagnostics {
  ok: boolean;
  at: string | null;
  graph_api_version: string | null;
  requests: number | null;
  layers: DiagLayer[];
  optional_fields: Record<string, "available" | "unavailable" | "unknown" | string>;
}

export interface MetaCounters {
  records_fetched: number;
  unique_ads: number;
  new_revisions: number;
  assets_acquired: number;
  analyses_done: number;
  analyses_partial: number;
  assigned: number;
  new_patterns: number;
  abstained: number;
  alerts_open: number;
  analyst_confirmed: number;
  analyst_dismissed: number;
}

export interface QueueCounts { queued: number; running: number; done: number; partial: number; failed: number; deferred: number }

export interface MetaStatus {
  mode: MetaMode;
  mode_label_pl: string;
  offline: boolean;
  media_fetch_enabled: boolean;
  collector: {
    state: CollectorState | string;
    state_pl: string;
    message_pl: string | null;
    enabled: boolean;
    engine_state: string;
    next_due: string | null;
    last_poll: string | null;
    last_success: string | null;
    last_error_pl: string | null;
    interval_s: number;
  };
  token: { configured: boolean; source: "env" | "file" | null; last4: string | null };
  budget: { limit_per_h: number; used_60m: number; remaining: number; label_pl: string; deferred_until: string | null; last_usage: Record<string, unknown> | null };
  queue: QueueCounts;
  counters: MetaCounters;
  contract: { graph_api_version: string | null; verified_on: string | null; last_diagnostics: Diagnostics | null };
  config: MetaConfig;
  run?: Record<string, unknown> | null;
}

export interface AnalysisSummary {
  id: number;
  status: string;
  status_pl: string;
  decision: string | null;
  decision_pl: string | null;
  campaign_id: string | null;
  triage_priority: string;
  triage_priority_pl: string;
  review_status: string;
  coverage_pl: string;
}

export interface AdRow {
  ad_id: string;
  mode: MetaMode;
  mode_label_pl: string;
  page_id: string | null;
  page_name: string | null;
  first_observed_at: string;
  last_observed_at: string;
  seen_count: number;
  revisions: number;
  current_revision: { id: number; rev: number; content_sha256: string; observed_at: string; collection_status: string; collection_status_pl: string } | null;
  text_excerpt: string;
  link_hosts: string[];
  queries: string[];
  analysis: AnalysisSummary | null;
  alert: boolean;
}

export interface ArtifactRow {
  role: string;
  status: string;
  status_pl: string;
  sha256: string | null;
  mime: string | null;
  size: number | null;
  source: string;
  derived: boolean;
  fetched_at: string | null;
  retry_possible: boolean;
}

export interface Revision {
  id: number;
  rev: number;
  observed_at: string;
  content_sha256: string;
  content: Record<string, unknown>;
  collection_status: string;
  collection_status_pl: string;
  collection_reason_pl: string | null;
  artifacts: ArtifactRow[];
}

export interface AdDetail {
  ad: AdRow;
  source_link: string | null;
  revisions: Revision[];
  queries: { query_id: string; label: string; first_at: string; last_at: string; hits: number }[];
  analyses: AnalysisSummary[];
}

export interface OutboxRow {
  id: number;
  ad_id: string;
  revision_id: number;
  status: string;
  status_pl: string;
  attempts: number;
  last_error_pl: string | null;
  created_at: string;
  updated_at: string;
}

export type ReviewDecision = "confirmed_by_analyst" | "dismissed_by_analyst" | "unreviewed";

export interface Analysis {
  id: number;
  ad_id: string;
  mode: MetaMode;
  mode_label_pl: string;
  revision_id: number;
  incident_id: string | null;
  base_run_id: string | null;
  context_version: string | null;
  status: "done" | "partial" | "failed" | "running" | "queued" | string;
  status_pl: string;
  reason_pl: string | null;
  coverage_pl: string;
  report_id: string | null;
  correlation: {
    decision: string | null;
    decision_pl: string;
    campaign_id: string | null;
    group_id: string | null;
    reason_pl: string | null;
    candidates: { campaign_id: string; probability: number | null; support: number | null; total_bits: number | null }[];
    evidence: { kind: string; value: string; bits: number | null; level: string | null; family: string | null }[];
    /** KONTRA-style missing-observation tasks (P1): what to collect next; heuristic order, no measured benefit. */
    evidence_tasks?: { kind: string; adapter: string | null; task_pl: string }[];
    base_run_id: string | null;
    assign_run_id: string | null;
    note_pl: string;
  };
  triage: {
    review_status: ReviewDecision | string;
    triage_priority: "unknown" | "low" | "medium" | "high" | string;
    triage_priority_pl: string;
    reasons: { code: string; description_pl: string; artifact: string | null; source: string; rule_version: string }[];
    note_pl: string;
  };
  manipulation: {
    modality: "image" | "video" | "audio" | string;
    status: "unavailable" | "not_applicable" | "inconclusive" | "signal_detected" | "no_signal" | string;
    status_pl: string;
    method: string;
    version: string;
    scope: string;
    quality_and_coverage: string;
    evidence_refs: string[];
    limitations_pl: string;
  }[];
  review: { status: string; history: { decision: string; analyst: string; note: string | null; created_at: string }[] };
}

export interface SchemeShared { hosts?: string[]; phones?: string[]; media_sha256?: string[] }
export interface SchemeEvidence { kind: string; value: string; bits: number | null; level: string | null; family: string | null }

/** P1 "nowa reklama, ten sam schemat": versions of one ad, same-scheme ads and look-alikes the system did not merge. */
export interface AdScheme {
  mode: MetaMode;
  mode_label_pl: string;
  ad_id: string;
  decision: string | null;
  campaign_id: string | null;
  versions: {
    from_rev: number;
    to_rev: number;
    observed_at: string;
    text_changed: boolean;
    domain_change: { removed: string[]; added: string[] } | null;
    media_changed: boolean;
    kept: { phones: string[] };
  }[];
  same_scheme: { ad_id: string; decision: string | null; campaign_id: string | null; shared: SchemeShared; analysis_id: number | null; evidence: SchemeEvidence[] }[];
  lookalike_not_merged: { ad_id: string; decision: string | null; campaign_id: string | null; shared: SchemeShared; why_not_merged_pl: string; analysis_id: number | null }[];
  note_pl: string;
}

export type AdView = "all" | "unprocessed" | "new_revisions" | "alerts";

// ------------------------------------------------------------------ labels (Polish, UI-only)

export const VIEW_LABEL: Record<AdView, string> = {
  all: "Wszystkie",
  unprocessed: "Nieprzetworzone",
  new_revisions: "Nowe rewizje",
  alerts: "Alerty",
};

export const COUNTER_LABEL: [keyof MetaCounters, string, string][] = [
  ["records_fetched", "Rekordy pobrane", "Wszystkie rekordy odczytane ze stron odpowiedzi (z powtórzeniami)."],
  ["unique_ads", "Unikalne reklamy", "Różne identyfikatory reklam w tym trybie."],
  ["new_revisions", "Nowe rewizje", "Istotne zmiany treści, linku lub materiału tej samej reklamy."],
  ["assets_acquired", "Materiały pozyskane", "Pliki obrazów/wideo/stron rzeczywiście zapisane w magazynie (CAS)."],
  ["analyses_done", "Analizy zakończone", "Analizy z kompletem dostępnych etapów."],
  ["analyses_partial", "Analizy częściowe", "Analizy, w których część etapów była niedostępna (np. brak materiału lub modelu)."],
  ["assigned", "Przypisania do kampanii", "Decyzja korelacji „assign” względem bazowego przebiegu."],
  ["new_patterns", "Nowe wzorce", "Decyzja „new_campaign”: brak znanej kampanii o wystarczającym poparciu."],
  ["abstained", "Abstencje", "Decyzja „abstain”: dane niewystarczające do rozstrzygnięcia."],
  ["alerts_open", "Alerty do sprawdzenia", "Nieprzejrzane wyniki z przesłankami lub przypisaniem; to nie jest potwierdzenie oszustwa."],
  ["analyst_confirmed", "Potwierdzenia analityka", "Podejrzenia potwierdzone decyzją analityka."],
];

export const DIAG_STATUS_LABEL: Record<string, string> = { pass: "zaliczone", fail: "niezaliczone", skipped: "pominięte", not_run: "nie wykonano" };

// ------------------------------------------------------------------ requests

const BASE = "/v1/watch/rt/meta";

export function adsPath(mode: MetaMode, view: AdView, q: string, before?: string | null, limit = 50): string {
  const p = new URLSearchParams({ mode, view, limit: String(limit) });
  if (q) p.set("q", q);
  if (before) p.set("before", before);
  return `${BASE}/ads?${p.toString()}`;
}

/** Download URL of a report (browser link; the API path goes through the same /api prefix as every request). */
export function analysisReportUrl(id: number, format: "json" | "md"): string {
  return apiUrl(`${BASE}/analyses/${id}/report?format=${format}`);
}

export function periodReportUrl(mode: MetaMode, format: "json" | "md"): string {
  return apiUrl(`${BASE}/report?mode=${mode}&format=${format}`);
}

export const metaKeys = {
  all: ["meta-ads"] as const,
  status: (mode: MetaMode) => ["meta-ads", "status", mode] as const,
  config: ["meta-ads", "config"] as const,
  ads: (mode: MetaMode, view: AdView, q: string) => ["meta-ads", "ads", mode, view, q] as const,
  ad: (mode: MetaMode, id: string) => ["meta-ads", "ad", mode, id] as const,
  scheme: (mode: MetaMode, id: string) => ["meta-ads", "scheme", mode, id] as const,
  analysis: (id: number) => ["meta-ads", "analysis", id] as const,
  queue: (mode: MetaMode) => ["meta-ads", "queue", mode] as const,
};

export const metaApi = {
  status: (mode: MetaMode, signal?: AbortSignal) => apiFetch<MetaStatus>(`${BASE}/status?mode=${mode}`, { signal }),
  config: (signal?: AbortSignal) => apiFetch<MetaConfig>(`${BASE}/config`, { signal }),
  saveConfig: (body: Partial<MetaConfig>) => apiFetch<MetaConfig>(`${BASE}/config`, { method: "PUT", json: body }),
  diagnose: () => apiFetch<Diagnostics>(`${BASE}/diagnose`, { method: "POST" }),
  collector: (action: "start" | "pause" | "resume" | "poll_now") =>
    apiFetch<MetaStatus>(`${BASE}/collector`, { method: "POST", json: { action } }),
  ads: (mode: MetaMode, view: AdView, q: string, before?: string | null, signal?: AbortSignal) =>
    apiFetch<{ mode: MetaMode; ads: AdRow[]; next_before: string | null }>(adsPath(mode, view, q, before), { signal }),
  ad: (mode: MetaMode, id: string, signal?: AbortSignal) =>
    apiFetch<AdDetail>(`${BASE}/ads/${encodeURIComponent(id)}?mode=${mode}`, { signal }),
  scheme: (mode: MetaMode, id: string, signal?: AbortSignal) =>
    apiFetch<AdScheme>(`${BASE}/ads/${encodeURIComponent(id)}/scheme?mode=${mode}`, { signal }),
  analyse: (mode: MetaMode, id: string) =>
    apiFetch<{ job: OutboxRow; created: boolean }>(`${BASE}/ads/${encodeURIComponent(id)}/analyse?mode=${mode}`, { method: "POST" }),
  attach: (mode: MetaMode, id: string, file: File) => {
    const body = new FormData();
    body.append("file", file);
    return apiFetch<{ revision_id: number; job: OutboxRow }>(`${BASE}/ads/${encodeURIComponent(id)}/attach?mode=${mode}`, { method: "POST", body });
  },
  queue: (mode: MetaMode, signal?: AbortSignal) => apiFetch<{ items: OutboxRow[] }>(`${BASE}/queue?mode=${mode}`, { signal }),
  analysis: (id: number, signal?: AbortSignal) => apiFetch<Analysis>(`${BASE}/analyses/${id}`, { signal }),
  review: (id: number, decision: ReviewDecision, note?: string) =>
    apiFetch<Analysis>(`${BASE}/analyses/${id}/review`, { method: "POST", json: { decision, ...(note ? { note } : {}) } }),
};

// ------------------------------------------------------------------ hooks

export function useMetaStatus(mode: MetaMode) {
  return useQuery({ queryKey: metaKeys.status(mode), queryFn: ({ signal }) => metaApi.status(mode, signal), refetchInterval: 5000 });
}

export function useMetaConfig() {
  return useQuery({ queryKey: metaKeys.config, queryFn: ({ signal }) => metaApi.config(signal) });
}

export function useMetaAds(mode: MetaMode, view: AdView, q: string) {
  return useQuery({ queryKey: metaKeys.ads(mode, view, q), queryFn: ({ signal }) => metaApi.ads(mode, view, q, null, signal), refetchInterval: 5000 });
}

export function useMetaAd(mode: MetaMode, id: string | null) {
  return useQuery({
    queryKey: metaKeys.ad(mode, id ?? ""),
    queryFn: ({ signal }) => metaApi.ad(mode, id!, signal),
    enabled: !!id,
    refetchInterval: 5000,
  });
}

export function useMetaScheme(mode: MetaMode, id: string | null) {
  return useQuery({ queryKey: metaKeys.scheme(mode, id ?? ""), queryFn: ({ signal }) => metaApi.scheme(mode, id!, signal), enabled: !!id, refetchInterval: 10_000 });
}

/** Hosts are shown as text, never as links: "." becomes "[.]" (same convention as the rest of the watch pages). */
export function defang(host: string): string {
  return host.replace(/(?<!\[)\.(?!\])/g, "[.]");
}

export function useMetaAnalysis(id: number | null) {
  return useQuery({
    queryKey: metaKeys.analysis(id ?? 0),
    queryFn: ({ signal }) => metaApi.analysis(id!, signal),
    enabled: id != null,
    refetchInterval: (q) => (q.state.data && (q.state.data.status === "queued" || q.state.data.status === "running") ? 3000 : false),
  });
}

export function useMetaQueue(mode: MetaMode) {
  return useQuery({ queryKey: metaKeys.queue(mode), queryFn: ({ signal }) => metaApi.queue(mode, signal), refetchInterval: 5000 });
}
