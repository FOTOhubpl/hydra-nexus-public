/** Client for hydra/api_visual.py (pipeline visibility). */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "./client";
import { runCacheOptions } from "../lib/runSelection";
import type { Hypothesis } from "./task";
import type { VideoSegment } from "../components/VideoSegmentStrip";

const enc = encodeURIComponent;

export interface ExtractorStat {
  run_seconds?: number; original_seconds?: number; incidents_run?: number; incidents_cached?: number; original_unknown?: number;
  observed: number; unavailable: number; failed: number;
}

export interface SelfAssignSummary {
  mode: string; assignments: number; by_decision: Record<string, number>; agreement_with_clustering: number | null;
  abstain_rate: number | null; member_return_rate: number | null; singleton_new_rate: number | null;
  held_out_campaign_rejection_rate: number | null; held_out_n?: number | null; seconds?: number;
}

export interface SystemFlow {
  run_id: string | null;
  created_at?: string | null;
  revision_of?: string | null;
  timings_s?: Record<string, number>;
  boxes?: {
    input: { incidents: number; by_corpus: Record<string, number> };
    adapters: { artifacts: number; by_role: Record<string, number> };
    genes: { by_family: Record<string, number>; total: number; extractor_seconds: Record<string, number>; extractor_original_seconds?: Record<string, number>; extractors?: Record<string, ExtractorStat>; extractors_cached: number; extractors_run: number };
    enrichment: { records_by_source: Record<string, number>; genes_with_enrichment: number };
    rarity: { distinct_values_by_level: Record<string, number> };
    candidates: { by_index: Record<string, number>; candidate_pairs: number | null; all_pairs: number };
    scoring: { pairs_scored: number; hypotheses: Record<string, number>; bits_by_level: Record<string, number>; scorer?: string };
    clustering: { campaigns: number; multi_member: number; groups: number };
    assignment: { by_decision: Record<string, number>; conformal: boolean; self?: number; new_material?: number; summary?: SelfAssignSummary | null; hint_pl?: string | null };
    reviews: { reviews: number; undos: number; revisions: number; constraints_in_run: number; reviews_in_run?: number; hint_pl?: string | null };
    outputs: { llm_enabled: boolean; exports?: { total: number; by_kind: Record<string, number>; last_at: string | null }; hint_pl?: string | null };
  };
}

export interface DiffRow { kind: string; state: "same" | "partly" | "changed" | "one_side"; shared: string[]; only_a: string[]; only_b: string[] }
export interface PairDiff {
  a: string; b: string; same_campaign: boolean; campaign_id: string | null;
  text: DiffRow[]; infrastructure: DiffRow[]; visual: DiffRow[];
  image_matches: { kind: string; value_a: string | null; value_b: string | null; similarity: number | null; bits: number; explanation_pl: string; artifact_a: string | null; artifact_b: string | null }[];
  summary: { shared_values: number; only_a: number; only_b: number; invariant_kinds: [string, number][]; changed_kinds: string[]; campaign_core: { family: string; kind: string; value: string | null }[] };
  hypothesis: Hypothesis | null; total_bits: number | null;
  video_segments?: VideoSegment[];
  av_sync?: { incident_id: string; level: "low" | "medium" | "high"; quality: number; artifact_id: string | null; note: string | null }[];
}

export interface Provenance {
  a: string; b: string; hypothesis: Hypothesis; total_bits: number | null;
  links: { source: string; target: string; bits: number }[];
  items: { family: string; kind: string; value: string | null; bits: number; extractor: string; artifact_id: string | null; role: string; path: string | null; pointer_a: Record<string, unknown> | null; pointer_b: Record<string, unknown> | null; explanation_pl: string }[];
}

export interface Revisions {
  items: { run_id: string; revision_of: string; created_at: string; review_id: string | null; action: string | null; analyst: string | null; note: string | null; target: Record<string, unknown> | null; constraints: number; moved: { incident_id: string; from: string | null; to: string | null; to_successor_of?: string | null }[]; renamed?: Record<string, string>; before: Record<string, string[]>; after: Record<string, string[]> }[];
  current_run: string | null;
  constraints: { kind: "must" | "cannot" | "pin"; a: string; b: string; review_id: string }[];
}

export interface Lifecycle {
  campaign_id: string;
  incidents: { id: string; observed_at: string | null; title: string | null }[];
  dates: { incident_id: string; kind: string; date: string; subject: string | null }[];
  asset_reuse: { value: string; incidents: string[]; kind: string }[];
  first_seen: string | null; last_seen: string | null;
  robustness: { a: string; b: string; hypothesis: Hypothesis; robustness: RobustnessDetail; support: number | null; dominant_family: string | null }[];
  link_threshold: number | null;
  /** Run the body describes and the per-host domain lifecycle of hydra/lifecycle.py (same route, CR-ASTRA-PIPE-401). */
  run_id?: string;
  label?: string | null;
  domains?: { host: string; domain: string | null; incidents: string[]; events: { type: string; date: string }[]; lead_days: Record<string, number | null> }[] | null;
  campaign_first_events?: { type: string; date: string; host: string }[] | null;
  lead_times?: Record<string, unknown> | null;
}

export interface RobustnessDetail {
  link?: string; robust_link?: boolean; critical_families?: string[];
  families?: Record<string, { support_without?: number; total_bits_without?: number; still_linked?: boolean; kit_bits_without?: number }>;
}

export interface GeneExplain {
  kind: string; value: string; family: string; extractor: string; provenance: string; dependency_group: string | null;
  level: string; tier: string | null; df: number; incidents_in_run: number; holders: string[];
  df_histogram: { df: number; values: number }[]; caps: Record<string, number>;
  used_in_pairs: { a: string; b: string; bits: number; explanation_pl: string }[];
}

export interface CorpusStats { latest_run: string | null; groups: number; items: { corpus: string; incidents: number; genes_by_family: Record<string, number>; campaigns_in_latest_run: number; multi_member: number; in_latest_run: number }[] }

const opts = { retry: false, staleTime: 15_000 } as const;
/** Run-scoped reads (/v1/system/flow, /v1/pairs/*, /v1/campaigns/*): immutable per selected run. */
const runOpts = { retry: false, ...runCacheOptions } as const;
export const useSystemFlow = () => useQuery({ ...runOpts, queryKey: ["task", "flow"], queryFn: ({ signal }) => apiFetch<SystemFlow>("/v1/system/flow", { signal }) });
export const usePairDiff = (a?: string, b?: string) => useQuery({ ...runOpts, queryKey: ["task", "diff", a, b], enabled: Boolean(a && b), queryFn: ({ signal }) => apiFetch<PairDiff>(`/v1/pairs/${enc(a!)}/${enc(b!)}/diff`, { signal }) });
export const useProvenance = (a?: string, b?: string) => useQuery({ ...runOpts, queryKey: ["task", "prov", a, b], enabled: Boolean(a && b), queryFn: ({ signal }) => apiFetch<Provenance>(`/v1/pairs/${enc(a!)}/${enc(b!)}/provenance`, { signal }) });
export const useRevisions = (campaign?: string) => useQuery({ ...opts, queryKey: ["task", "revisions", campaign ?? ""], queryFn: ({ signal }) => apiFetch<Revisions>(`/v1/revisions${campaign ? `?campaign_id=${enc(campaign)}` : ""}`, { signal }) });
export const useLifecycle = (id?: string) => useQuery({ ...runOpts, queryKey: ["task", "lifecycle", id], enabled: Boolean(id), queryFn: ({ signal }) => apiFetch<Lifecycle>(`/v1/campaigns/${enc(id!)}/lifecycle`, { signal }) });
export const useGeneExplain = (kind?: string, value?: string) => useQuery({ ...opts, queryKey: ["task", "gene", kind, value], enabled: Boolean(kind && value), queryFn: ({ signal }) => apiFetch<GeneExplain>(`/v1/genes/explain?kind=${enc(kind!)}&value=${enc(value!)}`, { signal }) });
export const useCorpusStats = () => useQuery({ ...opts, queryKey: ["task", "corpus-stats"], queryFn: ({ signal }) => apiFetch<CorpusStats>("/v1/corpora/stats", { signal }) });

export function useUndo() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (reviewId: string) => apiFetch<{ effect?: string; applied?: unknown }>("/v1/reviews", { method: "POST", json: { action: "undo", undoes: reviewId }, expect: [201] }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["task"] }),
  });
}
