/**
 * Batch import of new material (hydra/api_batch.py, GAP-AUDIT B3): every item is decided alone against the
 * known campaigns, then the new patterns of the batch are compared with each other.
 */
import { useMutation, useQuery, type Query } from "@tanstack/react-query";
import { apiFetch, ApiError, isApiError, isTerminalStatus, type UploadEntry } from "./client";
import type { Job } from "./types";

export interface BatchRow {
  item: string;
  incident_id?: string | null;
  decision: string;
  campaign_id?: string | null;
  group_id?: string | null;
  new_pattern_id?: string | null;
  duplicate_of?: string | null;
  duplicate_of_item?: string | null;
  assign_run?: string | null;
}

export interface BatchLink {
  a: string;
  b: string;
  hypothesis: string;
  total_bits: number;
  support: number;
  reasons: { family: string; kind: string; bits: number; explanation_pl: string }[];
}

export interface NewPattern {
  id: string;
  members: string[];
  items: string[];
  size: number;
  label?: string | null;
  batch_group_id?: string | null;
  known_group_ids?: string[];
  links: BatchLink[];
  reason_pl: string;
}

export interface BatchReport {
  batch_run: string;
  base_run?: string | null;
  job_id?: string | null;
  created_at?: string;
  decisions?: Record<string, number>;
  summary?: {
    items: number;
    new_campaign_items: number;
    new_patterns: number;
    multi_member_new_patterns: number;
    items_in_multi_member_patterns: number;
    duplicates: number;
    pairs_scored: number;
  };
  new_patterns?: NewPattern[];
  duplicates?: { item: string; duplicate_of: string; duplicate_of_item?: string | null; reason_pl: string }[];
  items?: BatchRow[];
  note_pl?: string;
}

export interface BatchResult {
  job: Job | null;
  report: BatchReport | null;
}

export type BatchInput = { kind: "path"; path: string } | { kind: "files"; entries: UploadEntry[] };

export async function startBatch(input: BatchInput): Promise<{ job_id: string; items?: number }> {
  let result: { job_id?: unknown; items?: unknown };
  if (input.kind === "files") {
    if (input.entries.length === 0) throw new ApiError({ kind: "contract", message: "Brak plików", path: "/v1/assign-batch" });
    const form = new FormData();
    for (const { file, path } of input.entries) form.append("files", file, path);
    result = await apiFetch("/v1/assign-batch", { method: "POST", body: form, expect: [202] });
  } else {
    result = await apiFetch("/v1/assign-batch", { method: "POST", json: { path: input.path }, expect: [202] });
  }
  if (!result || typeof result.job_id !== "string") {
    throw new ApiError({ kind: "contract", message: "Brak job_id w odpowiedzi", path: "/v1/assign-batch", body: result });
  }
  return { job_id: result.job_id, items: typeof result.items === "number" ? result.items : undefined };
}

export function batchResult(key: string, signal?: AbortSignal): Promise<BatchResult> {
  return apiFetch<BatchResult>(`/v1/assign-batch/${encodeURIComponent(key)}`, { signal });
}

function interval(query: Query<BatchResult, Error>): number | false {
  const data = query.state.data;
  if (data?.job && isTerminalStatus(data.job.status)) return false;
  if (data && !data.job && data.report) return false;
  if (isApiError(query.state.error) && query.state.error.isClientError) return false;
  return 1500;
}

export function useBatchResult(key: string | null) {
  return useQuery<BatchResult, Error>({
    queryKey: ["task", "assign-batch", key ?? ""],
    queryFn: ({ signal }) => batchResult(key as string, signal),
    enabled: Boolean(key),
    refetchInterval: interval,
    retry: false,
  });
}

export function useStartBatch() {
  return useMutation({ mutationFn: startBatch });
}
