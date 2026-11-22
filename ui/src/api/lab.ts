/**
 * HYDRA KONTRA K1 "Laboratorium hipotez" (shadow mode): wire types, fetchers and react-query hooks.
 * Mirrors the hydra.kontra.v1 experiment JSON. Bits are heuristic evidence, never probabilities.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, apiFetch, API_BASE, apiUrl, isApiError, shouldRetry } from "./client";

export type LabDecision = "assign" | "new_campaign" | "abstain";

export interface LabEvidence {
  artifact_id?: string | null;
  bbox?: number[] | null;
  char_range?: number[] | null;
  timecode_s?: number | null;
  har_entry?: number | null;
  dom_path?: string | null;
  note?: string | null;
}

export interface LabReason {
  family: string;
  kind: string;
  bits: number;
  explanation_pl: string;
  value?: string | null;
  group?: string | null;
  evidence?: LabEvidence | null;
}

export interface LabHypothesis {
  id: string;
  kind: "campaign" | "new_pattern" | "shared_kit" | string;
  label_pl: string;
  campaign_id: string | null;
  group_id: string | null;
  rank: number;
  leading: boolean;
  support_bits: number | null;
  campaign_bits?: number | null;
  kit_bits?: number | null;
  operator_bits?: number | null;
  supporting_members?: number | null;
  pair_hypothesis?: string | null;
  families: Record<string, number>;
  gap_to_link_bits: number | null;
  note_pl: string;
  reasons: LabReason[];
}

export interface LabGroup {
  group_id: string;
  unit: "dependency_group" | "family" | "artefact" | string;
  family: string | null;
  label_pl: string;
  n_genes: number;
  kinds: string[];
  relevance_bits: number;
}

export type ScenarioClass = "baseline" | "observation_uncertainty" | "reuse_hypothesis" | "diagnostic_stress" | "competitor_check";

export interface LabScenario {
  scenario_id: string;
  type: string;
  class: ScenarioClass | string;
  label_pl: string;
  removed: { groups: string[]; families: string[]; artefacts: string[]; campaigns: string[]; n_genes: number };
  decision: LabDecision | string;
  campaign_id: string | null;
  group_id: string | null;
  winner: string;
  support_bits: number;
  link_threshold: number;
  margin_bits: number;
  decision_changed: boolean;
  campaign_changed: boolean;
  runtime_ms: number;
  reason_pl: string;
}

export interface LabFlipSet {
  found: boolean;
  groups: string[];
  labels_pl: string[];
  decision: string | null;
  campaign_id: string | null;
  searched: number;
  budget: number;
  note_pl: string;
}

export interface LabObservation {
  action: string;
  label_pl: string;
  source: string;
  families: string[];
  separates: boolean;
  observed_in_query: boolean;
  cost: "low" | "medium" | "high" | string;
  priority: number;
  why_pl: string;
}

export interface LabExperiment {
  schema_version: string;
  experiment_id: string;
  incident_id: string;
  mode: string;
  created_at: string;
  context: {
    base_run: string | null;
    fingerprint: string;
    corpus_signature: string;
    product_commit: string | null;
    scorer?: Record<string, unknown> | null;
  };
  baseline: {
    decision: LabDecision | string;
    campaign_id: string | null;
    group_id: string | null;
    reason_pl: string;
    support_bits: number;
    link_threshold: number;
    margin_bits: number;
    winner: string;
    link_rule_met: boolean | null;
    stored: { decision: string; campaign_id: string | null } | null;
    matches_stored: boolean | null;
  };
  hypotheses: LabHypothesis[];
  groups: LabGroup[];
  scenarios: LabScenario[];
  flip_set: LabFlipSet | null;
  next_observations: {
    heuristic: boolean;
    rule: string;
    note_pl: string;
    pair: string[];
    ranking: LabObservation[];
  } | null;
  retrieval_scope: Record<string, unknown> | null;
  candidates: Record<string, unknown> | null;
  limits: {
    max_groups?: number;
    max_scenarios?: number;
    groups_total?: number;
    groups_tested?: number;
    scenarios_run?: number;
    scenarios_skipped?: number;
    scenario_space_exhausted?: boolean;
    retrieval_complete?: null;
  } | null;
  stats: { recomputations: number; runtime_ms_total: number; scenario_stability_fraction: number | null } | null;
}

export interface LabPackage {
  package_id: string;
  sha256: string;
  created_at: string;
  experiment_id: string;
  files: { json?: string; markdown?: string; html?: string };
}

export interface LabAck {
  receipt_id: string;
  package_id: string;
  received_at: string;
  duplicate: boolean;
  synthetic_case_id: string;
  note: string;
}

export interface LabSendResult {
  status: "acknowledged" | "duplicate" | "unavailable" | "rejected" | string;
  receiver_url: string;
  ack: LabAck | null;
  reason: string | null;
}

const enc = encodeURIComponent;

export const labKeys = { experiment: (incident: string) => ["lab", incident] as const };

/** `?missing=null`: "no experiment yet" is a normal state, answered 200 `null` (no 404 in the console); the
 * hook still sees it as the 404-style "no experiment" error so the empty state renders. */
export async function getLab(incident: string, signal?: AbortSignal): Promise<LabExperiment> {
  const path = `/v1/lab/${enc(incident)}?missing=null`;
  const exp = await apiFetch<LabExperiment | null>(path, { signal });
  if (exp === null) throw new ApiError({ kind: "http", status: 404, message: "no experiment yet", path });
  return exp;
}

export function runLab(incident: string): Promise<LabExperiment> {
  return apiFetch<LabExperiment>(`/v1/lab/${enc(incident)}/run`, { method: "POST", json: {} });
}

export function createLabPackage(incident: string): Promise<LabPackage> {
  return apiFetch<LabPackage>(`/v1/lab/${enc(incident)}/package`, { method: "POST", json: {} });
}

export function sendLabPackage(packageId: string): Promise<LabSendResult> {
  return apiFetch<LabSendResult>(`/v1/lab/packages/${enc(packageId)}/send`, { method: "POST", json: {} });
}

/** Package file links come back as API paths (`/v1/lab/packages/...`); route them through the same `/api` base. */
export function labFileUrl(path: string): string {
  if (/^https?:\/\//.test(path) || path.startsWith(`${API_BASE}/`)) return path;
  return apiUrl(path);
}

export function isNoExperiment(error: unknown): boolean {
  return isApiError(error) && error.status === 404;
}

export function useLabExperiment(incident: string | undefined) {
  return useQuery<LabExperiment, Error>({
    queryKey: labKeys.experiment(incident ?? ""),
    queryFn: ({ signal }) => getLab(incident as string, signal),
    enabled: Boolean(incident),
    retry: (count, error) => !isNoExperiment(error) && shouldRetry(count, error),
  });
}

export function useRunLab(incident: string | undefined) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => runLab(incident as string),
    onSuccess: (data) => client.setQueryData(labKeys.experiment(incident ?? ""), data),
  });
}

export function useCreateLabPackage(incident: string | undefined) {
  return useMutation({ mutationFn: () => createLabPackage(incident as string) });
}

export function useSendLabPackage() {
  return useMutation({ mutationFn: (packageId: string) => sendLabPackage(packageId) });
}
