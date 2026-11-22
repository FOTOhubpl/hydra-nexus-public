/**
 * Task endpoints (hydra/api_task.py, contracts v2.0.0): wire types, fetchers and react-query hooks.
 * Types mirror hydra/contracts.py; extra fields from the API are optional.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient, type Query } from "@tanstack/react-query";
import { apiFetch, ApiError, apiUrl, isApiError, isTerminalStatus, shouldRetry, type UploadEntry } from "./client";
import type { Job } from "./types";
import { runCacheOptions, runSelection, withRun } from "../lib/runSelection";
import { liveStatus, type LiveBlock } from "./stream";
import { postFormWithProgress, type UploadProgress } from "./upload";

// ------------------------------------------------------------------ contracts

export const FAMILIES = ["VIS", "TXT", "INF", "ID", "NAR", "META"] as const;
export type Family = (typeof FAMILIES)[number];
export type GeneStatus = "observed" | "unavailable" | "failed" | "not_applicable";
export type Provenance = "OBS" | "INF" | "HYP";
export type Hypothesis = "same_campaign" | "shared_template" | "shared_asset" | "independent" | "abstain";
export type AssignmentDecision = "assign" | "new_campaign" | "abstain";
export type ReviewAction =
  | "accept_link"
  | "reject_link"
  | "merge_campaigns"
  | "split_campaign"
  | "move_incident"
  | "undo";

export interface EvidencePointer {
  artifact_id?: string | null;
  bbox?: [number, number, number, number] | null;
  char_range?: [number, number] | null;
  timecode_s?: number | null;
  har_entry?: number | null;
  dom_path?: string | null;
  enrichment_key?: string | null;
  note?: string | null;
}

export interface Gene {
  id?: number;
  incident_id?: string;
  family: string;
  kind: string;
  value: string | null;
  vector_ref?: string | null;
  quality: number;
  status: GeneStatus;
  provenance: Provenance;
  evidence: EvidencePointer;
  extractor: string;
  dependency_group?: string | null;
}

export interface GeneMatch {
  family: string;
  kind: string;
  value_a?: string | null;
  value_b?: string | null;
  similarity?: number | null;
  doc_freq?: number | null;
  bits: number;
  provenance: Provenance;
  evidence_a?: EvidencePointer | null;
  evidence_b?: EvidencePointer | null;
  explanation_pl: string;
}

export interface PairScore {
  a: string;
  b: string;
  family_bits: Record<string, number>;
  missing_families: string[];
  unknown_families?: string[];
  contradictions: GeneMatch[];
  reasons: GeneMatch[];
  total_bits: number;
  probability: number;
  calibrated: boolean;
  hypothesis: Hypothesis;
  candidate_sources?: string[];
  model: string;
  /** CORE tier sums (operator/campaign, kit/template, shared asset); extra fields beyond the contract. */
  campaign_bits?: number;
  kit_bits?: number;
  asset_bits?: number;
}

export interface Campaign {
  id: string;
  label: string;
  members: string[];
  core_genome: GeneMatch[];
  scheme?: string | null;
  first_seen?: string | null;
  last_seen?: string | null;
  confidence: number;
  status: string;
  group_id?: string | null;
  group_label?: string | null;
  meta?: Record<string, unknown>;
  size?: number;
  /** DATAFLOW: members assigned since the analysis (counted in `size`). */
  new?: number;
}

export interface CampaignGroup {
  id: string;
  label: string;
  campaign_ids: string[];
  shared_genes: GeneMatch[];
  confidence: number;
  /** CR-ASTRA-9: heuristic evidence strength of the group in bits (absent on old runs). */
  meta?: { evidence_bits?: number | null; [key: string]: unknown } | null;
}

/** Analyst state of a pair: accepted / rejected link, or marked unsure (ADOPT #14, does not change campaigns). */
export type PairReviewState = "accepted" | "rejected" | "unsure" | null;

// CR-ASTRA-9: campaign support (meta.support). Heuristic evidence bits and indices, never a probability.

export interface CampaignBridge {
  a: string;
  b: string;
  bits: number | null;
  split_sizes: [number, number];
}

export interface CampaignMemberSupport {
  id: string;
  /** Sum of campaign-level bits to the other members. */
  bits_to_rest: number;
  /** Strongest direct link of the member (the weakest link that keeps it in). */
  anchor_bits: number | null;
  links: number;
  families: string[];
  /** Independent families behind the member's links. */
  n_families: number;
  /** Heuristic member index 0..1, not a probability. */
  index: number;
  articulation: boolean;
  analyst_link: boolean;
}

export interface FamilyStability {
  parts: number;
  largest_share: number;
  detached: string[];
}

export interface CampaignSupport {
  calibrated: false;
  kind: string;
  singleton: boolean;
  links: number;
  possible_links: number;
  /** links / possible links, 0..1. */
  density: number;
  median_link_bits: number | null;
  /** Bottleneck: weakest edge of the maximum spanning tree (bits). */
  weakest_link_bits: number | null;
  /** Heuristic index 0..1 (mean member index), not a probability. */
  cohesion: number | null;
  min_member_index: number | null;
  weakest_member: string | null;
  bridges: CampaignBridge[];
  articulation_points: string[];
  members: CampaignMemberSupport[];
  stability: { families: Record<string, FamilyStability>; stable: boolean; fragile_families: string[] } | null;
  /** Heuristic rank among multi-member campaigns by median link bits, 1 = strongest. */
  rank: number | null;
  rank_of: number;
}

/** meta.support of a campaign, or null on runs that predate CR-ASTRA-9. Lists default to empty. */
export function campaignSupport(c: { meta?: Record<string, unknown> | null } | null | undefined): CampaignSupport | null {
  const s = c?.meta?.support;
  if (!s || typeof s !== "object") return null;
  const v = s as Partial<CampaignSupport>;
  return {
    ...(v as CampaignSupport),
    bridges: Array.isArray(v.bridges) ? v.bridges : [],
    articulation_points: Array.isArray(v.articulation_points) ? v.articulation_points : [],
    members: Array.isArray(v.members) ? v.members : [],
    stability: v.stability && typeof v.stability === "object" && v.stability.families ? v.stability : null,
  };
}

export interface AssignmentCandidate {
  campaign_id: string;
  probability: number;
  best_pair?: PairScore | null;
  support: number;
}

/** Conformal prediction set stored with an assignment (hydra/conformal.py, split_conformal@1). */
export interface ConformalSet {
  method?: string;
  prediction_set?: string[];
  set_size?: number;
  p_values?: Record<string, number>;
  eps?: number;
  n_calibration?: number;
  usable?: boolean;
  decided_by?: string;
  empirical_coverage?: number | null;
  held_out_rejection_rate?: number | null;
  excluded_by_evidence_gate?: string[];
  note_pl?: string;
}

/** Assignment.meta written by hydra/assign.py `explain()`; every field optional (older runs lack them). */
export interface AssignmentMeta {
  top_candidates?: TopCandidate[];
  margin_bits?: number | null;
  missing_bits_to_link?: number | null;
  link_threshold?: number | null;
  link_rule_met?: boolean;
  conformal?: ConformalSet | null;
  [key: string]: unknown;
}

export interface Assignment {
  incident_id: string;
  decision: AssignmentDecision;
  campaign_id: string | null;
  group_id?: string | null;
  candidates: AssignmentCandidate[];
  reason_pl: string;
  model: string;
  created_at?: string;
  meta?: AssignmentMeta | null;
}

export interface Review {
  id: string;
  action: ReviewAction;
  target: Record<string, unknown>;
  analyst: string;
  note?: string | null;
  created_at: string;
  undoes?: string | null;
  undone?: boolean;
}

export interface EnrichmentRecord {
  key: string;
  source: string;
  query: string;
  fetched_at: string | null;
  status: GeneStatus;
  response_sha256?: string | null;
  cache_path?: string | null;
  summary: Record<string, unknown>;
  /** API-derived: summary built from genes citing this key (no stored record). */
  derived_from_genes?: boolean;
}

// ------------------------------------------------------------------ API shapes

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  run_id: string | null;
  /** DATAFLOW delta layer included in the payload (docs/ui/STREAM_API.md). */
  live?: LiveBlock;
}

export interface Run {
  id: string;
  kind: string;
  params: Record<string, unknown>;
  metrics: Record<string, unknown>;
  created_at: string | null;
  finished_at: string | null;
}

export interface LatestRun {
  run: Run | null;
  counts: { incidents: number; pairs?: number; campaigns?: number; assignments?: number; members?: number };
  contracts_version: string;
}

export interface AppStatus {
  offline: boolean;
  enrichment_mode: "live" | "replay";
  run_id: string | null;
  eval_run_id: string | null;
  audit: { ok: boolean; events: number; first_bad_seq: number | null };
  active_jobs: number;
  reviews: number;
  /** true when CORE applies reviews to the clustering (apply_review); otherwise reviews are annotations. */
  reviews_change_grouping?: boolean;
  contracts_version: string;
  /** Product version (hydra.__version__). */
  version?: string;
}

export interface IncidentSummary {
  id: string;
  source: string;
  source_ref: string;
  title: string | null;
  observed_at: string | null;
  meta: Record<string, unknown>;
  artifact_roles: Record<string, number>;
  artifact_count: number;
  observed_genes: Record<string, number>;
  thumbnail: string | null;
  campaign_id: string | null;
  assignment?: AssignmentDecision | null;
}

export interface IncidentArtifactRow {
  artifact_id: string;
  role: string;
  path_in_incident: string;
  meta: Record<string, unknown>;
  mime: string | null;
  size: number | null;
  status: string | null;
}

export interface FamilyGroup {
  family: string;
  status: "observed" | "unknown" | GeneStatus;
  counts: Partial<Record<GeneStatus, number>>;
  kinds: Record<string, number>;
  genes: Gene[];
  truncated: number;
}

export interface Neighbour {
  other: string;
  probability: number;
  total_bits: number;
  hypothesis: Hypothesis;
}

export interface IncidentDetail extends Omit<IncidentSummary, "assignment"> {
  artifacts: IncidentArtifactRow[];
  families: FamilyGroup[];
  enrichment: EnrichmentRecord[];
  run_id: string | null;
  assignment: Assignment | null;
  neighbours: Neighbour[];
}

export interface ValueShare {
  kind: string;
  value: string;
  provenance: Provenance;
  incidents: string[];
  share: number;
}

export interface CampaignEdge {
  a: string;
  b: string;
  probability: number;
  total_bits: number;
  hypothesis: Hypothesis;
  dominant_family: string | null;
  other_campaign?: string | null;
}

export interface CampaignCard extends Campaign {
  member_incidents: IncidentSummary[];
  infrastructure: Record<string, ValueShare[]>;
  persons: Record<string, ValueShare[]>;
  timeline: { incident_id: string; observed_at: string | null; title: string | null }[];
  internal_pairs: CampaignEdge[];
  cross_links: CampaignEdge[];
  group?: CampaignGroup | null;
  host_count?: number;
  run_id: string;
}

export interface TaskGraphNode {
  id: string;
  label: string;
  title: string | null;
  group: string | null;
  campaign_label: string | null;
  observed_at: string | null;
  size: number;
  degree: number;
  [key: string]: unknown;
}

export interface TaskGraphEdge {
  id: string;
  source: string;
  target: string;
  kind: string;
  weight: number;
  probability: number;
  total_bits: number;
  hypothesis: Hypothesis;
  calibrated: boolean;
  dominant_family: string | null;
  family_bits: Record<string, number>;
  review: PairReviewState;
  [key: string]: unknown;
}

export interface TaskGraph {
  run_id: string | null;
  nodes: TaskGraphNode[];
  edges: TaskGraphEdge[];
  stats: { nodes: number; edges: number; edges_total: number };
}

export interface PairSide extends IncidentSummary {
  visuals: IncidentArtifactRow[];
  landing_title: string | null;
  hosts: string[];
}

export interface PairDetail {
  run_id: string;
  score: PairScore;
  dominant_family: string | null;
  incident_a: PairSide | null;
  incident_b: PairSide | null;
  same_campaign_in_run: boolean;
  review: PairReviewState;
  reviews: Review[];
}

export interface PairRow {
  a: string;
  b: string;
  probability: number;
  total_bits: number;
  hypothesis: Hypothesis;
  calibrated: boolean;
  model: string;
  family_bits: Record<string, number>;
  missing_families: string[];
  dominant_family: string | null;
  review: PairReviewState;
}

export interface AnalyseCoreResult {
  kind?: string;
  incident_id?: string;
  run_id?: string;
  notes?: string[];
  urls_in_image?: string[];
  fetch?: Record<string, unknown>;
  metrics?: Record<string, unknown>;
  incident_ids?: string[];
  /** Dataset import: files that did not become an artifact (skipped / rejected), capped at 500 by CORE. */
  import_report?: DatasetImportReport;
  search?: {
    incidents?: { incident_id: string; score: number; terms_covered?: string[]; matches?: { kind: string; value: string; bits: number; doc_freq?: number | null }[] }[];
    campaigns?: { campaign_id: string; label?: string; score: number; incidents?: string[] }[];
    [k: string]: unknown;
  };
  [key: string]: unknown;
}

export interface DatasetImportReport {
  incidents: number;
  files_stored: number;
  skipped_files: number;
  rejected_archives: number;
  by_code: Record<string, number>;
  files: { incident_id: string | null; path: string; code: string; status_pl: string }[];
  truncated: boolean;
  summary_pl: string;
  notes_pl?: string[];
}

export interface AnalyseResult {
  job: Job & { kind?: string; error_code?: string | null; items_by_status?: Record<string, number> };
  assignment: Assignment | null;
  campaign: Campaign | null;
  result?: AnalyseCoreResult | null;
  live?: LiveProgress | null;
}

/** Live stages of an analyse job, derived by the API from rows written after the job started. */
export interface LiveProgress {
  incident_id: string | null;
  title?: string | null;
  thumbnail?: string | null;
  artifacts_by_role: Record<string, number>;
  genes_by_family: Record<string, Record<string, number>>;
  genes_total: number;
  assign_run_id: string | null;
  pairs_scored: number | null;
  top_pairs: { a: string; b: string; other: string; probability: number; total_bits: number; hypothesis: Hypothesis }[];
  campaign_members?: { id: string; thumbnail: string | null }[];
  campaign_id_for_card?: string | null;
  derived: boolean;
}

export interface SearchResult {
  q: string;
  run_id: string | null;
  incidents: { id: string; title: string | null }[];
  campaigns: { id: string; label: string }[];
  genes: { family: string; kind: string; value: string; incidents: string[]; doc_freq: number }[];
}

// ------------------------------------------------------------------ fetchers

function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== null && value !== undefined && value !== "") search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

const enc = encodeURIComponent;

export const taskApi = {
  status: (signal?: AbortSignal) => apiFetch<AppStatus>("/v1/status", { signal }),
  signingKey: (signal?: AbortSignal) => apiFetch<SigningKey>("/v1/exports/signing-key", { signal }),
  latestRun: (kind = "analyse", signal?: AbortSignal) => apiFetch<LatestRun>(`/v1/runs/latest${qs({ kind })}`, { signal }),
  incidents: (p: { q?: string; campaign_id?: string; limit?: number; offset?: number }, signal?: AbortSignal) =>
    apiFetch<Page<IncidentSummary>>(`/v1/incidents${qs(p)}`, { signal }),
  incident: (id: string, signal?: AbortSignal) => apiFetch<IncidentDetail>(`/v1/incidents/${enc(id)}`, { signal }),
  campaigns: (signal?: AbortSignal) => apiFetch<Page<Campaign>>("/v1/campaigns?limit=2000", { signal }),
  campaign: (id: string, signal?: AbortSignal) => apiFetch<CampaignCard>(`/v1/campaigns/${enc(id)}`, { signal }),
  graph: (p: { min_p?: number; min_bits?: number; links_only?: boolean; family?: string | null; campaign_id?: string | null }, signal?: AbortSignal) =>
    apiFetch<TaskGraph>(`/v1/graph${qs(p)}`, { signal }),
  pair: (a: string, b: string, signal?: AbortSignal) => apiFetch<PairDetail>(`/v1/pairs/${enc(a)}/${enc(b)}`, { signal }),
  pairs: (p: { min_p?: number; max_p?: number; order?: string; incident?: string; limit?: number; offset?: number }, signal?: AbortSignal) =>
    apiFetch<Page<PairRow>>(`/v1/pairs${qs(p)}`, { signal }),
  assignments: (p: { decision?: string; limit?: number; offset?: number }, signal?: AbortSignal) =>
    apiFetch<Page<Assignment>>(`/v1/assignments${qs(p)}`, { signal }),
  analyseResult: (jobId: string, signal?: AbortSignal) => apiFetch<AnalyseResult>(`/v1/analyse/${enc(jobId)}`, { signal }),
  search: (q: string, signal?: AbortSignal) => apiFetch<SearchResult>(`/v1/search${qs({ q })}`, { signal }),
  reviewQueue: (limit = 200, signal?: AbortSignal, order?: QueueOrder) => apiFetch<ReviewQueue>(`/v1/review/queue${qs({ limit, order })}`, { signal }),
  similar: (id: string, k = 10, signal?: AbortSignal) => apiFetch<SimilarCases>(`/v1/incidents/${enc(id)}/similar${qs({ k })}`, { signal }),
  reviews: (signal?: AbortSignal) => apiFetch<Page<Review>>("/v1/reviews", { signal }),
};

/** Download links are plain hrefs (no apiFetch), so they carry the selected run themselves (CR-ASTRA-PIPE-303). */
export function campaignExportUrl(id: string, format: "json" | "csv"): string {
  return apiUrl(withRun(`/v1/campaigns/${enc(id)}/export?format=${format}`));
}

/** Abuse-report ZIP (XARF JSON per domain + notes + manifest) and STIX 2.1 bundle of one campaign. */
export function campaignAbusePackageUrl(id: string): string {
  return apiUrl(withRun(`/v1/campaigns/${enc(id)}/abuse-package`));
}
export function campaignStixUrl(id: string): string {
  return apiUrl(withRun(`/v1/campaigns/${enc(id)}/stix`));
}

export type AnalyseInput =
  | { kind: "files"; entries: UploadEntry[] }
  | { kind: "screenshot"; entries: UploadEntry[] }
  | { kind: "url"; url: string }
  | { kind: "hint"; hint: string }
  | { kind: "path"; path: string };

export interface StartAnalyseOptions {
  /** Upload progress of multipart inputs (XMLHttpRequest, hydra IMPORT-PROGRESS). */
  onUploadProgress?: (p: UploadProgress) => void;
  signal?: AbortSignal;
}

export async function startAnalyse(input: AnalyseInput, opts: StartAnalyseOptions = {}): Promise<{ job_id: string; mode?: string; skipped?: { path: string; reason: string }[] }> {
  let result: { job_id?: unknown };
  if (input.kind === "files" || input.kind === "screenshot") {
    if (input.entries.length === 0) throw new ApiError({ kind: "contract", message: "Brak plików", path: "/v1/analyse" });
    const form = new FormData();
    if (input.kind === "screenshot") form.append("kind", "screenshot");
    for (const { file, path } of input.entries) form.append("files", file, path);
    result = typeof XMLHttpRequest === "function"
      ? await postFormWithProgress("/v1/analyse", form, { onProgress: opts.onUploadProgress, signal: opts.signal, expect: [202] })
      : await apiFetch("/v1/analyse", { method: "POST", body: form, expect: [202], signal: opts.signal });
  } else {
    const json = input.kind === "url" ? { url: input.url } : input.kind === "hint" ? { hint: input.hint } : { path: input.path };
    result = await apiFetch("/v1/analyse", { method: "POST", json, expect: [202], signal: opts.signal });
  }
  if (!result || typeof result.job_id !== "string") {
    throw new ApiError({ kind: "contract", message: "Brak job_id w odpowiedzi", path: "/v1/analyse", body: result });
  }
  const r = result as { mode?: unknown; skipped?: unknown };
  return { job_id: result.job_id, mode: typeof r.mode === "string" ? r.mode : undefined, skipped: Array.isArray(r.skipped) ? (r.skipped as { path: string; reason: string }[]) : undefined };
}

export interface ReviewInput {
  action: ReviewAction;
  target?: Record<string, unknown>;
  note?: string | null;
  undoes?: string | null;
}

export function postReview(body: ReviewInput): Promise<{ review: Review; event_hash: string }> {
  return apiFetch("/v1/reviews", { method: "POST", json: body, expect: [201] });
}

// ------------------------------------------------------------------ review queue (ADOPT #14)

export type QueueKind = "bridge" | "near_threshold" | "abstain" | "conflict";

export interface QueueReason {
  family: string;
  kind: string;
  value: string | null;
  bits: number;
  explanation_pl: string;
}

export interface QueueCandidate {
  campaign_id: string;
  bits: number;
  supporting_members: number;
  top_reasons: QueueReason[];
}

/** One `meta.top_candidates[]` entry (hydra/assign.py `explain`); the first one also carries the best pair's caveats
 * and evidence pointers (CR-UI-FINAL-4, ADV-QUEUE): display only. */
export interface TopCandidate {
  campaign_id: string;
  support?: number | null;
  operator_bits?: number | null;
  campaign_bits?: number | null;
  kit_bits?: number | null;
  asset_bits?: number | null;
  campaign_by_family?: Record<string, number>;
  pair?: [string, string] | null;
  supporting_members?: number;
  hypothesis?: string;
  /** count of contradictions of the best pair */
  contradictions?: number;
  contradiction_list?: { kind: string; family: string; bits: number; explanation_pl: string }[];
  missing_families?: string[];
  evidence?: TopEvidence[];
}

export interface TopEvidence {
  family: string;
  kind: string;
  value: string | null;
  bits: number;
  evidence_a: EvidencePointer | null;
  evidence_b: EvidencePointer | null;
  /** true when a more precise pointer (frame, region, text span) of the same value replaced the stored one */
  refined: boolean;
}

export interface QueueEffect {
  decisions: number;
  incidents: number;
  campaigns: (string | null)[];
  effect: "none" | "split" | "merge" | "blocked" | "assign" | "assign_other";
  sides?: [number, number];
  sizes?: [number, number];
  assignments?: number;
}

export interface QueueItem {
  key: string;
  kinds: QueueKind[];
  a: string | null;
  b: string | null;
  incident_id: string | null;
  campaigns: string[];
  bits: number | null;
  distance_bits: number | null;
  impact: number;
  uncertainty: number;
  /** Expected value of an analyst decision (heuristic); the server orders items by it, descending. */
  ev: number;
  reason_pl: string;
  candidates?: QueueCandidate[];
  /** Expected number of changed decisions (counterfactual over the link graph, hydra/queue_gain.py); heuristic. */
  gain?: number;
  effort?: number;
  gain_per_effort?: number;
  /** Uncalibrated chance that the analyst confirms, from the bits. */
  p_confirm?: number;
  counterfactual?: { confirm: QueueEffect; reject: QueueEffect };
  /** Polish sentence: why the item has this rank. */
  why_first_pl?: string;
}

export interface UnsureTarget {
  a?: string;
  b?: string;
  incident_id?: string;
}

export interface UnsureMark {
  id: string;
  target: UnsureTarget;
  note: string | null;
  analyst: string;
  created_at: string;
}

export type QueueOrder = "gain" | "ev";

export interface ReviewQueue {
  run_id: string | null;
  order?: QueueOrder;
  method?: string;
  threshold_bits: number | null;
  method_pl: string | null;
  items: QueueItem[];
  skipped: { decided: number; unsure: number; decided_between_campaigns: number };
  unsure: UnsureMark[];
}

export type UnsureInput = { target: { a: string; b: string } | { incident_id: string }; note?: string };

export function postUnsure(body: UnsureInput): Promise<{ mark: UnsureMark; event_hash: string }> {
  return apiFetch("/v1/review/unsure", { method: "POST", json: body, expect: [200, 201] });
}

export function postUnsureUndo(id: string): Promise<{ undone: string; event_hash: string }> {
  return apiFetch("/v1/review/unsure/undo", { method: "POST", json: { id }, expect: [200, 201] });
}

// ------------------------------------------------------------------ similar cases (TheHive-style)

export interface SimilarValue {
  kind: string;
  family: string;
  value: string | null;
  df: number;
  bits: number;
  tier: string | null;
  explanation_pl: string;
}

export interface SimilarItem {
  incident_id: string;
  campaign_id: string | null;
  title: string | null;
  shared: number;
  total: number;
  shared_operator: number;
  total_operator: number;
  ratio: number;
  rare_bits: number;
  per_kind: Record<string, number>;
  duplicate: boolean;
  values: SimilarValue[];
  reasons_pl: string[];
}

export interface SimilarCases {
  incident_id: string;
  run_id: string | null;
  total_values: number;
  operator_values: number;
  excluded_common: number;
  method_pl: string | null;
  empty_reason_pl: string | null;
  items: SimilarItem[];
}

// ------------------------------------------------------------------ hooks

export const taskKeys = {
  all: ["task"] as const,
  status: ["task", "status"] as const,
  latestRun: (kind: string) => ["task", "run", kind] as const,
  incidents: (p: object) => ["task", "incidents", p] as const,
  incident: (id: string) => ["task", "incident", id] as const,
  campaigns: ["task", "campaigns"] as const,
  campaign: (id: string) => ["task", "campaign", id] as const,
  graph: (p: object) => ["task", "graph", p] as const,
  pair: (a: string, b: string) => ["task", "pair", a, b] as const,
  pairs: (p: object) => ["task", "pairs", p] as const,
  assignments: (p: object) => ["task", "assignments", p] as const,
  analyse: (id: string) => ["task", "analyse", id] as const,
  search: (q: string) => ["task", "search", q] as const,
  reviewQueue: ["task", "review-queue"] as const,
  similar: (id: string) => ["task", "similar", id] as const,
};

const base = { retry: shouldRetry } as const;
/** Read endpoints of one analyse run: immutable once the run is selected (lib/runSelection.ts). */
const runBase = { ...base, ...runCacheOptions } as const;

/** Ed25519 key that signs every dataset export (SIGNED_MANIFEST.json + .sig); verify: `hydra verify-export`. */
export interface SigningKey {
  algorithm: "Ed25519";
  fingerprint: string | null;
  public_key: string | null;
}

export function useSigningKey() {
  return useQuery({ queryKey: ["task", "signing-key"] as const, queryFn: ({ signal }) => taskApi.signingKey(signal), staleTime: Infinity, retry: false });
}

export function useAppStatus(intervalMs = 5000) {
  return useQuery({ queryKey: taskKeys.status, queryFn: ({ signal }) => taskApi.status(signal), refetchInterval: intervalMs, retry: false });
}

export function useLatestRun(kind = "analyse") {
  return useQuery({ ...base, queryKey: taskKeys.latestRun(kind), queryFn: ({ signal }) => taskApi.latestRun(kind, signal) });
}

export function useIncidents(p: { q?: string; campaign_id?: string; limit?: number; offset?: number }) {
  return useQuery({ ...runBase, queryKey: taskKeys.incidents(p), queryFn: ({ signal }) => taskApi.incidents(p, signal), placeholderData: keepPreviousData });
}

export function useIncident(id: string | undefined) {
  return useQuery({ ...runBase, queryKey: taskKeys.incident(id ?? ""), queryFn: ({ signal }) => taskApi.incident(id as string, signal), enabled: Boolean(id) });
}

export function useCampaigns() {
  return useQuery({ ...runBase, queryKey: taskKeys.campaigns, queryFn: ({ signal }) => taskApi.campaigns(signal) });
}

export function useCampaignGroups() {
  return useQuery({ ...runBase, queryKey: ["task", "groups"], queryFn: ({ signal }) => apiFetch<{ run_id: string | null; items: CampaignGroup[] }>("/v1/campaign-groups", { signal }) });
}

export function useCampaign(id: string | undefined) {
  return useQuery({ ...runBase, queryKey: taskKeys.campaign(id ?? ""), queryFn: ({ signal }) => taskApi.campaign(id as string, signal), enabled: Boolean(id) });
}

export function useTaskGraph(p: { min_p?: number; min_bits?: number; links_only?: boolean; family?: string | null; campaign_id?: string | null }) {
  return useQuery({ ...runBase, queryKey: taskKeys.graph(p), queryFn: ({ signal }) => taskApi.graph(p, signal), placeholderData: keepPreviousData });
}

export function usePair(a: string | undefined, b: string | undefined) {
  return useQuery({ ...runBase, queryKey: taskKeys.pair(a ?? "", b ?? ""), queryFn: ({ signal }) => taskApi.pair(a as string, b as string, signal), enabled: Boolean(a && b) });
}

export function usePairs(p: { min_p?: number; max_p?: number; order?: string; incident?: string; limit?: number; offset?: number }) {
  return useQuery({ ...runBase, queryKey: taskKeys.pairs(p), queryFn: ({ signal }) => taskApi.pairs(p, signal), placeholderData: keepPreviousData });
}

export function useAssignments(p: { decision?: string; limit?: number; offset?: number }) {
  return useQuery({ ...base, queryKey: taskKeys.assignments(p), queryFn: ({ signal }) => taskApi.assignments(p, signal) });
}

export function useSearch(q: string) {
  return useQuery({ ...base, queryKey: taskKeys.search(q), queryFn: ({ signal }) => taskApi.search(q, signal), enabled: q.trim().length >= 2 });
}

export function useReviewQueue(limit = 200, order?: QueueOrder) {
  return useQuery({ ...base, queryKey: [...taskKeys.reviewQueue, limit, order ?? "default"], queryFn: ({ signal }) => taskApi.reviewQueue(limit, signal, order) });
}

export function useSimilarCases(id: string | undefined, k = 10) {
  return useQuery({ ...runBase, queryKey: [...taskKeys.similar(id ?? ""), k], queryFn: ({ signal }) => taskApi.similar(id as string, k, signal), enabled: Boolean(id) });
}

/** After an unsure mark/undo: the queue, pairs, pair details, graph and campaigns are stale. */
function invalidateReviewed(client: ReturnType<typeof useQueryClient>) {
  const keys: readonly (readonly string[])[] = [taskKeys.reviewQueue, ["task", "pairs"], ["task", "pair"], ["task", "graph"], taskKeys.campaigns, ["task", "campaign"]];
  for (const queryKey of keys) void client.invalidateQueries({ queryKey });
}

export function useMarkUnsure() {
  const client = useQueryClient();
  return useMutation({ mutationFn: postUnsure, onSuccess: () => invalidateReviewed(client) });
}

export function useUndoUnsure() {
  const client = useQueryClient();
  return useMutation({ mutationFn: postUnsureUndo, onSuccess: () => invalidateReviewed(client) });
}

function analyseInterval(query: Query<AnalyseResult, Error>): number | false {
  const data = query.state.data;
  if (data && isTerminalStatus(data.job.status)) return false;
  const error = query.state.error;
  if (isApiError(error) && error.isClientError) return false;
  return 1000;
}

export function useAnalyseResult(jobId: string | null) {
  return useQuery<AnalyseResult, Error>({
    queryKey: taskKeys.analyse(jobId ?? ""),
    queryFn: ({ signal }) => taskApi.analyseResult(jobId as string, signal),
    enabled: Boolean(jobId),
    refetchInterval: analyseInterval,
    retry: false,
  });
}

export function useStartAnalyse() {
  return useMutation({ mutationFn: ({ input, opts }: { input: AnalyseInput; opts?: StartAnalyseOptions }) => startAnalyse(input, opts) });
}

export function useReview() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: postReview,
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: taskKeys.all });
    },
  });
}

// ------------------------------------------------------------------ watchlist / scheduler / export

export interface WatchItem {
  kind: "keyword" | "domain";
  value: string;
}

/** CORE scheduler status (hydra/scheduler.py); shape owned by CORE, rendered generically. */
export interface SchedulerStatus {
  available: boolean;
  jobs?: Record<string, Record<string, unknown>>;
  meta_token_configured?: boolean;
  live_enrichment?: boolean;
  inbox_pending?: number;
  running?: boolean;
  [key: string]: unknown;
}

export function useWatchlist() {
  return useQuery({ ...base, queryKey: ["task", "watchlist"], queryFn: ({ signal }) => apiFetch<{ items: WatchItem[] }>("/v1/watchlist", { signal }) });
}

export function useSchedulerStatus() {
  return useQuery({ ...base, queryKey: ["task", "scheduler"], queryFn: ({ signal }) => apiFetch<SchedulerStatus>("/v1/scheduler/status", { signal }), refetchInterval: 10_000 });
}

export function useWatchlistChange() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { add?: WatchItem[]; remove?: WatchItem[] }) => apiFetch<{ items: WatchItem[] }>("/v1/watchlist", { method: "POST", json: body }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["task", "watchlist"] }),
  });
}

/** "Uruchom teraz": POST /v1/scheduler/run starts every job once in the background. */
export function useSchedulerRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<{ available: boolean; started: boolean; note?: string | null }>("/v1/scheduler/run", { method: "POST" }),
    onSuccess: () => void client.invalidateQueries({ queryKey: ["task", "scheduler"] }),
  });
}

/** Whole-run Neo4j export, the same graph as `hydra export-neo4j`: `cypher` = self-contained script, `zip` = CSV +
 *  LOAD CSV folder, `nodes_csv` / `edges_csv` = neo4j-admin import files. */
export function neo4jExportUrl(format: "cypher" | "zip" | "nodes_csv" | "edges_csv", minP = 0, runId?: string | null): string {
  return apiUrl(withRun(`/v1/export/neo4j?format=${format}&min_p=${minP}${runId ? `&run_id=${enc(runId)}` : ""}`));
}

/** Enriched dataset of a run (ZIP of the `hydra export-dataset` folder, with the assignments table). */
export function datasetExportUrl(runId?: string | null): string {
  return apiUrl(withRun(`/v1/export/dataset${runId ? `?run_id=${enc(runId)}` : ""}`));
}

// ------------------------------------------------------------------ dashboard + simulation

export interface DashboardData {
  run: { id: string; created_at: string | null; finished_at: string | null; scorer?: string; timings_s?: Record<string, number>; hypotheses?: Record<string, number>; candidate_pairs?: number; alerts: unknown[] } | null;
  kpis: {
    incidents: number;
    artifacts: number;
    artifacts_by_role: Record<string, number>;
    genes: number;
    genes_by_family: Record<string, Record<string, number>>;
    campaigns: number;
    multi_member_campaigns: number;
    groups: number;
    assignments: number;
    enriched_incidents: number;
  };
  previews: { id: string; title: string | null; observed_at: string | null; thumbnail: string | null; scheme: string | null; campaign_id: string | null; video: string | null; source?: string | null; new?: boolean; decision?: string | null }[];
  campaigns: { id: string; label: string; size: number; new?: number; confidence: number; group_id: string | null; members: { id: string; thumbnail: string | null; new?: boolean }[]; pivots: GeneMatch[] }[];
  groups: CampaignGroup[];
  coverage: { incident_id: string; families: Record<string, Record<string, number>> }[];
  jobs: { id: string; kind: string; status: string; total: number; done: number; failed: number; error_code: string | null; params: Record<string, unknown>; created_at: string; finished_at: string | null }[];
  /** DATAFLOW delta layer included in the payload (docs/ui/STREAM_API.md). */
  live?: LiveBlock;
}

/**
 * Poll the job list only while it can change: "latest" run (null selection) or jobs still running. Off while the live
 * stream is connected (api/stream.ts patches the cache and the job rows).
 */
export function dashboardInterval(query: Query<DashboardData, Error>): number | false {
  if (liveStatus.get() === "live") return false;
  const jobs = query.state.data?.jobs ?? [];
  return !runSelection.get() || jobs.some((j) => !isTerminalStatus(j.status)) ? 15_000 : false;
}

export function useDashboard() {
  return useQuery({ ...runBase, queryKey: ["task", "dashboard"], queryFn: ({ signal }) => apiFetch<DashboardData>("/v1/dashboard", { signal }), refetchInterval: dashboardInterval });
}

export interface SimPoint { a: string; b: string; p: number; bits: number; support: number | null; hypothesis: Hypothesis }

export interface SimulationData {
  run_id: string | null;
  created_at?: string | null;
  stages: {
    import: { incidents: number; artifacts_by_role: Record<string, number>; job_items_by_status: Record<string, number>; redacted_genes: number; redacted_incidents: number; incidents_list: { id: string; title: string | null; files: number; thumbnail: string | null }[]; timings_s?: Record<string, number> };
    genes: { incident_id: string | null; title: string | null; by_family: Record<string, Record<string, number>>; by_extractor: Record<string, number>; extract: { genes?: number; seconds?: number; cached?: boolean; run_id?: string; measured_seconds?: number | null; measured_run_id?: string | null; vectors?: number } | null; creative: string | null; creative_meta: Record<string, unknown>; boxes: { family: string; kind: string; value: string | null; bbox: [number, number, number, number] }[] };
    rarity: { incidents: number; distinct_values: number; histogram: ({ df: number } & Record<string, number>)[]; rare_shared: { family: string; kind: string; value: string; df: number; level: string }[]; boilerplate: { family: string; kind: string; value: string; df: number; level: string }[] };
    candidates: { incident_id: string | null; pairs: { other: string; probability: number; total_bits: number; hypothesis: Hypothesis; indexes: string[]; sources: string[]; n_sources: number }[]; by_index: Record<string, number>; candidate_pairs_total: number | null; all_pairs: number };
    evidence: { presets: { a: string; b: string; why: string }[] };
    decision: { hypotheses: Record<string, number>; probability_bins: number[]; band: [number, number]; points: SimPoint[]; thresholds: Record<string, number> | null };
    grouping: { edges: SimPoint[]; campaigns: { id: string; label: string; members: string[] }[]; groups: CampaignGroup[]; truth: Record<string, string> | null; truth_source: string | null };
    result: { campaigns: number; multi_member: number; groups: number; run_metrics: Record<string, unknown>; eval_run_id: string | null; eval_headline: Record<string, unknown> | null; eval_backend: string | null };
  };
}

export function useSimulation(incident?: string | null) {
  return useQuery({ ...runBase, queryKey: ["task", "simulation", incident ?? ""], queryFn: ({ signal }) => apiFetch<SimulationData>(`/v1/simulation${incident ? `?incident=${encodeURIComponent(incident)}` : ""}`, { signal }) });
}

// ------------------------------------------------------------------ AI analyst (optional layer)

export interface AiCitation { n: number; section: string; index: number; label?: string }
export interface AiResult { status: "ok" | "unavailable" | "error"; reason_pl?: string; text_pl?: string; citations?: AiCitation[]; model?: string; generated_at?: string }

export function useAiBrief(kind: "campaign" | "pair", a: string | undefined, b?: string) {
  const path = kind === "campaign" ? `/v1/campaigns/${enc(a ?? "")}/brief` : `/v1/pairs/${enc(a ?? "")}/${enc(b ?? "")}/explain`;
  return useQuery({ queryKey: ["task", "ai", kind, a, b], queryFn: ({ signal }) => apiFetch<AiResult>(path, { signal }), enabled: Boolean(a && (kind === "campaign" || b)), retry: false, staleTime: 10 * 60_000 });
}

// ------------------------------------------------------------------ analytics (redesign)

export interface AnalyticsOverview {
  run_id: string | null;
  sankey?: { unit: "bits"; links: { family: string; campaign_id: string; bits: number }[] };
  tiers?: { unit: "bits"; keys: string[]; links: ({ a: string; b: string; hypothesis: Hypothesis; total_bits: number; campaign_id: string | null; robustness?: { robust_link?: boolean; critical_families?: string[] } | null } & Record<string, unknown>)[] };
  timeline?: { id: string; title: string | null; observed_at: string | null; campaign_id: string | null }[];
  infrastructure?: Record<string, { value: string; incidents: string[]; df: number }[]>;
  clusters?: { id: string; label: string; members: string[]; size: number; group_id: string | null }[];
  groups?: CampaignGroup[];
}

export interface IndicatorGraph {
  run_id: string | null;
  incidents_total: number;
  kinds: string[];
  indicators: { id: string; family: string; kind: string; value: string; df: number; incidents: string[] }[];
  incidents: { id: string; campaign_id: string | null }[];
}

export function useAnalytics() {
  return useQuery({ ...runBase, queryKey: ["task", "analytics"], queryFn: ({ signal }) => apiFetch<AnalyticsOverview>("/v1/analytics/overview", { signal }) });
}

export function useIndicators(enabled: boolean) {
  return useQuery({ ...base, queryKey: ["task", "indicators"], queryFn: ({ signal }) => apiFetch<IndicatorGraph>("/v1/analytics/indicators", { signal }), enabled, staleTime: 30_000 });
}

/** `incidents_on_disk`: incident folders (null for a per-source public spec without its own folder);
 *  `incidents_in_db`: stored incidents the spec selects (public corpora only). */
export interface DemoCorpus { id: string; label: string; spec?: string; path: string; incidents_on_disk: number | null; incidents_in_db?: number }
export function useDemoCorpora() {
  return useQuery({ ...base, queryKey: ["task", "demo-corpora"], queryFn: ({ signal }) => apiFetch<{ items: DemoCorpus[] }>("/v1/demo/corpora", { signal }) });
}
export function startDemo(corpus: string, fresh = false) {
  return apiFetch<{ job_id: string; corpus: string }>("/v1/demo/run", { method: "POST", json: { corpus, fresh }, expect: [202] });
}
/** hydra.progress.run_progress (CORE): stage + counters of a running corpus job. */
export interface CoreProgress {
  stage?: string;
  stage_index?: number;
  stages?: string[];
  incidents_total?: number;
  incidents_done?: number;
  genes_by_family?: Record<string, number>;
  genes?: number;
  pairs_total?: number;
  pairs_scored?: number;
  campaigns?: number;
  groups?: number;
  status?: string;
  error?: string | null;
  run_id?: string | null;
  corpus?: string | null;
  started_at?: string;
  updated_at?: string;
}

export interface DemoProgress {
  job: AnalyseResult["job"];
  core_progress: CoreProgress | null;
  run_id: string | null;
  new_incidents: string[];
  genes_written_by_family: Record<string, number>;
  edges: { a: string; b: string; p: number; bits: number; hypothesis: Hypothesis; dominant_family?: string | null }[];
  campaigns: { id: string; members: string[] }[];
  groups?: CampaignGroup[];
  incidents: { id: string; title: string | null; source: string; thumbnail: string | null }[];
}
export function useDemoProgress(jobId: string | null) {
  return useQuery<DemoProgress, Error>({
    queryKey: ["task", "demo-progress", jobId ?? ""],
    queryFn: ({ signal }) => apiFetch<DemoProgress>(`/v1/demo/progress/${enc(jobId ?? "")}`, { signal }),
    enabled: Boolean(jobId),
    refetchInterval: (q) => (q.state.data && isTerminalStatus(q.state.data.job.status) ? false : 1000),
    retry: false,
  });
}
export interface CorpusRuns { items: { corpus: string; latest_run: string; runs: { id: string; created_at: string | null; finished_at?: string | null }[] }[] }
export function useCorpora() {
  return useQuery({ ...base, queryKey: ["task", "corpora"], queryFn: ({ signal }) => apiFetch<CorpusRuns>("/v1/corpora", { signal }) });
}

// ------------------------------------------------------------------ live analysis explain-data

export interface ExplainData {
  job_id: string;
  status: string;
  incident_id: string | null;
  ready: boolean;
  title?: string | null;
  source?: string | null;
  thumbnail?: string | null;
  timing_source?: string;
  stages?: { key: string; label: string; seconds: number | null; state: "done" | "running" | "waiting" | "skipped" }[];
  per_family_seconds?: Record<string, number | null>;
  per_extractor?: { extractor: string; genes: number; seconds: number | null }[];
  total_seconds?: number | null;
  artifacts?: { artifact_id: string; sha_short: string; role: string; path: string; mime: string | null; size: number | null; duplicate_of: string[]; redactions: number; previewable: boolean }[];
  skipped?: { path: string; reason: string }[];
  genes_feed?: { family: string; kind: string; value: string; provenance: Provenance; df_other: number; level: string; tier: string | null; bits: number | null; why: string }[];
  genes_total?: number;
  rare_genes?: number;
  enrichment?: { kind: string; value: string | null; subject: string | null; source: string; fetched_at: string | null; origin: string }[];
  candidates?: { by_index: Record<string, number>; pairs: { a: string; b: string; other: string; bits: number; p: number; hypothesis: Hypothesis; indexes: string[]; dominant_family: string | null }[]; funnel: { incidents: number; candidates: number; scored: number; linked: number } };
  why?: {
    top: { campaign_id: string; probability: number; support: number | null; total_bits: number | null; family_bits: Record<string, number>; campaign_by_family: Record<string, number>; campaign_bits: number | null; kit_bits: number | null; asset_bits: number | null; pair: [string, string] | null }[];
    margin: number | null;
    link_threshold: number | null;
    best_pair: [string, string] | null;
    counterfactual: string | null;
    rationale_pl: string[];
    robustness: { robust_link?: boolean; critical_families?: string[] } | null;
    conformal: ConformalSet | null;
  };
  decision?: { decision: AssignmentDecision | null; campaign_id: string | null; group_id: string | null; reason_pl: string | null; probability: number | null };
  metrics?: { coverage: Record<string, Record<string, number>>; genes_total: number; rare_genes: number; enrichment_facts: number; candidates_scored: number; total_seconds: number | null; cache: boolean | null };
  profile?: { families: string[]; new_item: Record<string, number>; campaign_core: Record<string, number>; campaign_id: string | null };
  graph?: { node: string; edges: { other: string; bits: number; hypothesis: Hypothesis; family: string | null }[] };
  timeline?: { id: string; observed_at: string | null; is_new: boolean }[];
}

export function useExplainData(jobId: string | null, done: boolean) {
  return useQuery<ExplainData, Error>({
    queryKey: ["task", "explain-data", jobId ?? ""],
    queryFn: ({ signal }) => apiFetch<ExplainData>(`/v1/analyse/${enc(jobId ?? "")}/explain-data`, { signal }),
    enabled: Boolean(jobId),
    // keep polling until the job is done AND the panels are ready (the last rows land with the job)
    refetchInterval: (q) => {
      const err = q.state.error;
      if (isApiError(err) && err.isClientError) return false;
      return done && q.state.data?.ready && q.state.data.status === "done" ? false : 1000;
    },
    retry: false,
  });
}

// ------------------------------------------------------------------ variant lineage (hydra/api_lineage.py, G01)

export type LineageDirection = "observation_order" | "mixed" | "import_order" | "undirected";
export interface LineageItem {
  kind: string; family: string; category: string; category_pl: string;
  bits?: number | null; values?: string[]; before?: string[]; after?: string[]; side?: "parent" | "child"; explanation_pl?: string | null;
}
export interface LineageNode { incident_id: string; observed_at: string | null; order: number; root: boolean; parent: string | null; depth: number }
export interface LineageEdge {
  parent: string; child: string; bits: number; hypothesis: Hypothesis | null; total_bits: number | null;
  kept: LineageItem[]; changed: LineageItem[]; added: LineageItem[]; removed: LineageItem[]; unknown: LineageItem[];
  counts: Record<"kept" | "changed" | "added" | "removed" | "unknown", number>;
  summary_pl: string;
}
/** ADV-CAMP: spec M7 tree by mutation distance (hydra/lineage_mutation.py), present when HYDRA_ADV_LINEAGE=1. */
export interface Mutation {
  kind: string; family: string; category: string; category_pl: string; type: string; type_pl: string;
  cost: number; distance: number; before?: string[]; after?: string[]; hamming_bits?: number;
}
export interface MutationEdge {
  parent: string; child: string; cost: number; mutation_distance: number; direction_penalty: number;
  support_bits: number; against_observation_order: boolean; mutations: Mutation[]; mutation_count: number; summary_pl: string;
}
export interface MutationTree {
  method: string; root: string | null; spanning: boolean; candidates: string; total_cost: number;
  nodes: LineageNode[]; edges: MutationEdge[]; roots?: string[]; note_pl?: string; agreement_with_link_tree?: number;
}
export interface CampaignLineage {
  campaign_id: string; run_id: string; direction: LineageDirection; direction_pl: string;
  nodes: LineageNode[]; edges: LineageEdge[]; roots: string[]; note_pl: string; mutation_tree?: MutationTree;
}

// ------------------------------------------------------------------ consistency gate report (hydra/api_consistency.py)

export interface ConsistencyMember {
  id: string; supporting_pairs: number; scored_pairs: number; possible_pairs: number; median_bits: number | null;
  median_ok: boolean; status: "ok" | "strong_single" | "single_pair" | "no_direct_link" | "analyst_link" | "pair_campaign";
  contradictions: { with: string; explanation_pl: string[] }[];
  best_pair?: { with: string; bits: number; reasons_pl: string[] }; reason_pl?: string;
}
export interface CampaignConsistency {
  campaign_id: string; run_id: string; method: string; applicable: boolean; verdict: string; verdict_pl?: string;
  size: number; gate_enabled: boolean; link_threshold_bits: number; low_threshold_bits: number; strong_bits: number;
  cohesion?: number | null; weakest_member?: string | null; weakest_link_bits?: number | null; median_scored_bits?: number | null;
  median_ok?: boolean; scored_pairs?: number; possible_pairs?: number; links?: number;
  single_pair_members?: string[]; strong_single_members?: string[]; failing_members?: string[];
  contradiction_pairs?: string[][]; gate_parts?: string[][]; members: ConsistencyMember[];
  gate_applied?: { from_size: number; parts: number };
}

export function useCampaignConsistency(id: string | undefined, runId?: string | null) {
  return useQuery({
    ...base,
    queryKey: ["task", "consistency", id ?? "", runId ?? ""],
    queryFn: ({ signal }) => apiFetch<CampaignConsistency>(`/v1/campaigns/${enc(id ?? "")}/consistency${runId ? `?run_id=${enc(runId)}` : ""}`, { signal }),
    enabled: Boolean(id),
    retry: false,
  });
}

export function useCampaignLineage(id: string | undefined, runId?: string | null) {
  return useQuery({
    ...base,
    queryKey: ["task", "lineage", id ?? "", runId ?? ""],
    queryFn: ({ signal }) => apiFetch<CampaignLineage>(`/v1/campaigns/${enc(id ?? "")}/lineage${runId ? `?run_id=${enc(runId)}` : ""}`, { signal }),
    enabled: Boolean(id),
  });
}
