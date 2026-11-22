/** Graph explorer API (/v1/graph/*, hydra/api_graph.py): types and query hooks. */
import { useQuery } from "@tanstack/react-query";
import { apiFetch, apiUrl } from "../../api/client";
import { authHeaders, notifyAuthStatus } from "../../api/auth";
import { runSelection } from "../../lib/runSelection";

export type Level = "campaign" | "kit" | "asset" | "weak" | "review";
export const LEVELS: readonly Level[] = ["campaign", "kit", "asset", "weak", "review"];
export const LEVEL_LABEL: Record<Level, string> = {
  campaign: "kampania",
  kit: "zestaw / szablon",
  asset: "wspólny zasób",
  weak: "słabe (do oceny)",
  review: "decyzja analityka",
};

export interface GReason { family: string | null; kind: string | null; bits: number; value: string | null; doc_freq: number | null; text: string }

export interface GNode {
  id: string;
  title: string | null;
  campaign_id: string | null;
  group_id: string | null;
  observed_at: string | null;
  registered_at: string | null;
  corpus: string;
  thumbnail: string | null;
  degree: number;
}

export interface GEdge {
  id: string;
  source: string;
  target: string;
  bits: number;
  p: number;
  hypothesis: string;
  level: Level;
  family: string | null;
  family_bits: Record<string, number>;
  campaign_bits: number | null;
  kit_bits: number | null;
  asset_bits: number | null;
  inferred: boolean;
  robustness: { robust: boolean; critical: string[]; link: string | null } | null;
  review: "must_link" | "cannot_link" | "unsure" | null;
  reasons: GReason[];
}

export interface GCampaign {
  id: string;
  label: string;
  confidence: number;
  group_id: string | null;
  first_seen: string | null;
  last_seen: string | null;
  scheme: string | null;
  size: number;
}

export interface GGroup { id: string; label?: string; campaign_ids: string[] }

export interface GStats {
  nodes: number;
  edges: number;
  components: number;
  campaigns: number;
  multi_campaigns: number;
  groups: number;
  levels: Partial<Record<Level, number>>;
  largest_campaign: { id: string; label: string; size: number } | null;
  robustness_scored: number;
  robust: number;
  must_link: number;
  cannot_link: number;
  inferred: number;
}

export interface Explore {
  run_id: string | null;
  corpus?: string;
  synthetic?: boolean;
  thresholds?: { link_bits: number; kit_bits: number };
  nodes: GNode[];
  edges: GEdge[];
  campaigns: GCampaign[];
  groups: GGroup[];
  stats: GStats | null;
}

export interface PathStep { from: string; to: string; edge_id: string; bits: number; level: Level; level_pl: string; family: string | null; review: string | null; genes: GReason[] }
export interface EvidencePath {
  run_id: string;
  a: string;
  b: string;
  found: boolean;
  hops?: number;
  nodes: { id: string; title: string | null; campaign_id: string | null }[];
  steps: PathStep[];
  weakest_bits?: number;
  summary_pl?: string;
  reason_pl?: string;
}

export interface Indicator {
  id: string;
  family: string;
  kind: string;
  kind_pl: string;
  value: string;
  df: number;
  tier: string | null;
  over_correlating: boolean;
  reason_pl: string | null;
  incidents: string[];
}
export interface Indicators { run_id: string | null; incidents_total: number; indicators: Indicator[] }

export interface CampaignSummary { campaign_id: string; status: string; text_pl: string; core_genes: { family: string; kind: string; bits: number; text: string }[]; family_bits: Record<string, number> }

const enc = encodeURIComponent;

export function useExplore() {
  return useQuery({ queryKey: ["task", "graphx", "explore"], queryFn: ({ signal }) => apiFetch<Explore>("/v1/graph/explore", { signal }), staleTime: 30_000 });
}

export function useGraphIndicators(enabled: boolean) {
  return useQuery({ queryKey: ["task", "graphx", "indicators"], queryFn: ({ signal }) => apiFetch<Indicators>("/v1/graph/indicators?limit=600", { signal }), enabled, staleTime: 60_000 });
}

export function useEvidencePath(a: string | null, b: string | null) {
  return useQuery({
    queryKey: ["task", "graphx", "path", a, b],
    queryFn: ({ signal }) => apiFetch<EvidencePath>(`/v1/graph/path?a=${enc(a ?? "")}&b=${enc(b ?? "")}`, { signal }),
    enabled: Boolean(a && b && a !== b),
    retry: false,
  });
}

export function useCampaignSummary(id: string | null) {
  return useQuery({
    queryKey: ["task", "graphx", "summary", id],
    queryFn: ({ signal }) => apiFetch<CampaignSummary>(`/v1/graph/campaigns/${enc(id ?? "")}/summary`, { signal }),
    enabled: Boolean(id),
    retry: false,
  });
}

/** Downloads the subgraph of `ids` (JSON or Neo4j Cypher) through the browser. */
export async function downloadSubgraph(ids: string[], format: "json" | "cypher"): Promise<void> {
  const res = await fetch(apiUrl("/v1/graph/export"), { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", ...authHeaders("POST") }, body: JSON.stringify({ ids, format, run_id: runSelection.get() ?? undefined }) });
  notifyAuthStatus("/v1/graph/export", res.status);
  if (!res.ok) throw new Error(`Eksport nie powiódł się (HTTP ${res.status})`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = format === "json" ? "hydra-subgraph.json" : "hydra-subgraph.cypher";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
