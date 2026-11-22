import { useSyncExternalStore } from "react";
import { hashKey, type Query, type QueryClient, type QueryKey } from "@tanstack/react-query";

/** Optional analyse run chosen in the top bar; null = latest. Read by the API client for run-scoped paths. */
let selected: string | null = null;
const listeners = new Set<() => void>();

export const runSelection = {
  get: () => selected,
  set(id: string | null) {
    selected = id;
    listeners.forEach((l) => l());
  },
  subscribe(l: () => void) {
    listeners.add(l);
    return () => listeners.delete(l);
  },
};

export function useSelectedRun(): string | null {
  return useSyncExternalStore(runSelection.subscribe, runSelection.get, runSelection.get);
}

const RUN_SCOPED = ["/v1/campaigns", "/v1/graph", "/v1/pairs", "/v1/incidents", "/v1/assignments", "/v1/dashboard",
  "/v1/simulation", "/v1/analytics/overview", "/v1/campaign-groups", "/v1/runs/latest", "/v1/system/flow", "/v1/stream",
  // CR-ASTRA-PIPE-304: panels whose contents are defined by the selected analysis.
  "/v1/review/queue", "/v1/genes/explain", "/v1/analytics/indicators", "/v1/export/neo4j", "/v1/export/dataset"];

/** Append `run_id` to run-scoped task paths when a run is selected. */
export function withRun(path: string): string {
  if (!selected || path.includes("run_id=") || path.includes("kind=eval")) return path;
  if (!RUN_SCOPED.some((p) => path.startsWith(p))) return path;
  return `${path}${path.includes("?") ? "&" : "?"}run_id=${encodeURIComponent(selected)}`;
}

/**
 * Second element of `["task", <name>, ...]` query keys whose endpoint is run-scoped (see RUN_SCOPED above).
 * `graphx` covers the graph explorer keys (/v1/graph/*); `diff`/`prov` are /v1/pairs/{a}/{b}/*; `lifecycle` is
 * /v1/campaigns/{id}/lifecycle; `similar` is /v1/incidents/{id}/similar; `review-queue`, `gene`, `indicators` are
 * /v1/review/queue, /v1/genes/explain, /v1/analytics/indicators.
 */
const RUN_SCOPED_KEYS = new Set(["run", "incidents", "incident", "similar", "campaigns", "campaign", "lifecycle", "groups", "graph",
  "graphx", "pair", "pairs", "diff", "prov", "assignments", "dashboard", "simulation", "analytics", "flow",
  "review-queue", "gene", "indicators"]);

export function isRunScopedKey(key: QueryKey): boolean {
  return key[0] === "task" && typeof key[1] === "string" && RUN_SCOPED_KEYS.has(key[1]);
}

/**
 * Query hash that makes the selected run part of the cache identity of run-scoped queries, so each run has its
 * own cache entries (switching back is served from cache, the other run's data never flashes). Observers pick
 * the new hash up on re-render; AppShell remounts the routed subtree on a run change for that.
 */
export function runAwareQueryKeyHash(key: QueryKey): string {
  const hash = hashKey(key);
  return isRunScopedKey(key) ? `${hash}#run=${selected ?? ""}` : hash;
}

/** Runs are immutable: an explicitly selected run never changes; "latest" (null) can move after a new analysis. */
export const RUN_STALE_MS = 10 * 60_000;
export const RUN_GC_MS = 30 * 60_000;
export const runStaleTime = () => (selected ? RUN_STALE_MS : 30_000);
export const runCacheOptions = { staleTime: runStaleTime, gcTime: RUN_GC_MS } as const;

/** Switch the selected run: abort in-flight requests of the old run (cache of both runs is kept). */
export function switchRun(client: QueryClient, id: string | null): void {
  if (id === selected) return;
  void client.cancelQueries({ predicate: (q: Query) => isRunScopedKey(q.queryKey) });
  runSelection.set(id);
}
