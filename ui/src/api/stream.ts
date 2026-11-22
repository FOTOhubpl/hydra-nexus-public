/**
 * Live data stream client (DATAFLOW, contract: docs/ui/STREAM_API.md).
 *
 * One EventSource per tab on `GET /v1/stream` of the selected run. Deltas over the analysed base are applied to the
 * react-query caches in place (dashboard, campaign list, raw graph overview payloads) by the pure `patch*` functions
 * below; `reset` invalidates run-scoped queries. After repeated failures (or without EventSource) the client falls back
 * to polling. Patches are immutable and idempotent: a delta with `seq <= live.seq` of the cached payload is skipped.
 */
import { useEffect, useSyncExternalStore } from "react";
import { useQueryClient, type Query, type QueryClient } from "@tanstack/react-query";
import { apiUrl, queryKeys } from "./client";
import { normOverview, type Overview } from "./graph";
import type { Campaign, DashboardData, Page } from "./task";
import { isRunScopedKey, useSelectedRun, withRun } from "../lib/runSelection";
import { activity, deltaEntries, jobEntry, metaAnalysisEntry, watchEntry, watchMatchEntry } from "../lib/activity";
import { patchStatusSource, watchRtBus, type RtItem, type RtMatch, type RtSource, type WatchPollEvent } from "./watchRt";

// ------------------------------------------------------------------ wire types

/** Absolute counter: `[n, n_new]`. */
export type Count2 = [number, number];

export interface LiveNode {
  id: string;
  title?: string | null;
  campaign_id?: string | null;
  map_campaign?: string | null;
  decision?: string | null;
  observed_at?: string | null;
  corpus?: string | null;
  thumbnail?: string | null;
  assign_run?: string | null;
  created_at?: string | null;
  new?: boolean;
  x?: number;
  y?: number;
}

export interface LiveEdge {
  source: string;
  target: string;
  bits: number;
  p?: number;
  hypothesis?: string;
  level?: string | null;
  family?: string | null;
  new?: boolean;
}

/** `live` block of /v1/dashboard, /v1/campaigns and /v1/graph/overview. */
export interface LiveBlock {
  run_id: string | null;
  version: number;
  seq: number;
  status?: "ready" | "building" | "missing";
  base_finished_at?: string | null;
  new_incidents?: number;
  by_decision?: Record<string, number>;
  label_pl?: string;
  /** Only in /v1/graph/overview. */
  nodes?: LiveNode[];
  edges?: LiveEdge[];
}

export interface DeltaCampaign {
  id: string;
  label?: string | null;
  size: number;
  new?: number;
  group_id?: string | null;
  first_seen?: string | null;
  last_seen?: string | null;
  solo?: boolean;
  internal_edges?: number;
  max_internal_bits?: number | null;
  family?: string | null;
}

export interface DeltaLink {
  source: string;
  target: string;
  level: string;
  pairs: number;
  max_bits: number;
  sum_bits: number;
  family_bits?: Record<string, number>;
  new?: number;
}

export interface StreamDelta {
  run_id: string;
  v: number;
  seq: number;
  from_seq?: number;
  n?: number;
  kinds?: string[];
  kpis?: DashboardData["kpis"];
  counts?: Record<string, Record<string, Count2>>;
  campaigns?: DeltaCampaign[];
  groups?: { id: string; size: number; new?: number }[];
  links?: DeltaLink[];
  nodes?: LiveNode[];
  removed_nodes?: string[];
  edges?: LiveEdge[];
  top_genes?: { family: string; kind: string; value: string; df: number; new?: number; level?: string }[];
  exports?: { total: number; by_kind: Record<string, number> };
  /** An assign-batch run against this base finished (hydra/dataflow.py `_batch_finished`). */
  batch?: {
    run_id: string;
    base_run: string;
    summary: Record<string, number>;
    new_patterns: { id: string; size: number; members: string[] }[];
    items: { incident_id: string | null; decision: string | null; campaign_id: string | null; new_pattern_id: string | null; duplicate_of: string | null }[];
    truncated: boolean;
  };
}

export interface StreamHello {
  run_id: string | null;
  latest_run?: string | null;
  version: number;
  seq: number;
  status?: string;
  base_finished_at?: string | null;
  resumed?: boolean;
  replayed?: number;
  heartbeat_s?: number;
  coalesce_ms?: number;
  max_message_bytes?: number;
}

export interface StreamReset {
  reason: "new_run" | "rebuilt" | "gap" | "overflow" | "too_large" | string;
  run_id?: string | null;
  latest_run?: string | null;
}

export interface StreamJob {
  id: string;
  kind?: string;
  status: string;
  total?: number;
  done?: number;
  failed?: number;
  stage?: string | null;
  run_id?: string | null;
  progress?: unknown;
}

/** Raw `/v1/graph/overview` table. */
export interface RawTable {
  columns: string[];
  rows: unknown[][];
}

export interface OverviewBounds {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** Raw `/v1/graph/overview` payload (only the parts the stream patches are typed). */
export interface RawOverview {
  run_id: string | null;
  bounds?: OverviewBounds | null;
  campaigns: RawTable;
  links: RawTable;
  groups?: RawTable;
  /** Level filter the links were built with (comma list or array). */
  levels?: string | string[] | null;
  min_bits?: number | null;
  /** Base box (before new material) used by the placement rule; `bounds` also covers the new solo discs. */
  base_bounds?: OverviewBounds | null;
  live?: LiveBlock;
  [key: string]: unknown;
}

export type CampaignPage = Page<Campaign> & { live?: LiveBlock };

export const LIVE_LABEL_PL = "nowe od analizy";
/** Server default of `levels` for /v1/graph/overview (hydra/api_graph.py). */
export const DEFAULT_OVERVIEW_LEVELS = ["kit", "campaign", "review"] as const;
export const LEVEL_RANK: Record<string, number> = { review: 5, campaign: 4, kit: 3, asset: 2, weak: 1 };

// ------------------------------------------------------------------ placement (same rule as the server)

/** 32-bit FNV-1a over the UTF-8 bytes of `text`. */
export function fnv1a32(text: string): number {
  let h = 2166136261;
  for (const byte of new TextEncoder().encode(text)) {
    h ^= byte;
    h = Math.imul(h, 16777619) >>> 0;
  }
  return h >>> 0;
}

/**
 * Position of a new node: on 0.8·r around its campaign disc, or on the ring R = max(|x0|,|x1|,|y0|,|y1|) + 4 of the
 * overview bounds when it has no disc (solo).
 */
export function placeNode(id: string, disc: { x: number; y: number; r: number } | null, bounds: OverviewBounds | null | undefined): { x: number; y: number } {
  const theta = (2 * Math.PI * (fnv1a32(id) % 3600)) / 3600;
  if (disc) return { x: disc.x + 0.8 * disc.r * Math.cos(theta), y: disc.y + 0.8 * disc.r * Math.sin(theta) };
  const b = bounds ?? { x0: 0, y0: 0, x1: 0, y1: 0 };
  const R = Math.max(Math.abs(b.x0), Math.abs(b.x1), Math.abs(b.y0), Math.abs(b.y1)) + 4;
  return { x: R * Math.cos(theta), y: R * Math.sin(theta) };
}

// ------------------------------------------------------------------ shared helpers

const round3 = (v: number) => Math.round(v * 1000) / 1000;

/** True when the cached payload already includes the delta (or a newer state). */
export function isApplied(live: LiveBlock | null | undefined, delta: Pick<StreamDelta, "seq">): boolean {
  return live != null && typeof live.seq === "number" && delta.seq <= live.seq;
}

/**
 * True when the cache is older than the first delta coalesced into this message (deltas between were missed, e.g.
 * the payload was fetched before the stream connected): patching would lose changes, the query must be refetched.
 */
export function isGap(live: LiveBlock | null | undefined, delta: Pick<StreamDelta, "v" | "n" | "seq">): boolean {
  if (!live || typeof live.version !== "number" || typeof delta.v !== "number") return false;
  if (isApplied(live, delta)) return false;
  return delta.v - Math.max(1, delta.n ?? 1) > live.version;
}

/** New `live` block after a delta: seq/version, by_decision from `counts.decision[k][1]`, new_incidents adjusted. */
export function bumpLive(prev: LiveBlock | null | undefined, delta: StreamDelta): LiveBlock {
  const byDecision = { ...(prev?.by_decision ?? {}) };
  let newIncidents = prev?.new_incidents ?? 0;
  for (const [k, v] of Object.entries(delta.counts?.decision ?? {})) {
    const n = Array.isArray(v) ? Number(v[1]) || 0 : 0;
    newIncidents += n - (byDecision[k] ?? 0);
    byDecision[k] = n;
  }
  return {
    ...(prev ?? {}),
    run_id: delta.run_id ?? prev?.run_id ?? null,
    version: typeof delta.v === "number" ? delta.v : (prev?.version ?? 0),
    seq: delta.seq,
    new_incidents: newIncidents,
    by_decision: byDecision,
    label_pl: prev?.label_pl ?? LIVE_LABEL_PL,
  };
}

/** The delta belongs to another run than the cached payload (or the payload has no run): leave it alone. */
function otherRun(run: string | null | undefined, delta: StreamDelta): boolean {
  if (!run) return true;
  return Boolean(delta.run_id) && run !== delta.run_id;
}

function skip(live: LiveBlock | null | undefined, run: string | null | undefined, delta: StreamDelta): boolean {
  return otherRun(run, delta) || isApplied(live, delta);
}

function nodesByCampaign(nodes: LiveNode[] | undefined): Map<string, LiveNode[]> {
  const out = new Map<string, LiveNode[]>();
  for (const n of nodes ?? []) {
    if (!n.campaign_id) continue;
    const list = out.get(n.campaign_id) ?? [];
    list.push(n);
    out.set(n.campaign_id, list);
  }
  return out;
}

export function dashboardRun(data: DashboardData): string | null {
  return data.live?.run_id ?? data.run?.id ?? null;
}

// ------------------------------------------------------------------ /v1/dashboard

type Preview = DashboardData["previews"][number];
type DashCampaign = DashboardData["campaigns"][number];

/** Apply a delta to a /v1/dashboard payload (kpis, previews, campaign sizes and members, live). */
export function patchDashboard(data: DashboardData, delta: StreamDelta): DashboardData {
  if (skip(data.live, dashboardRun(data), delta)) return data;
  const nodes = delta.nodes ?? [];
  const removed = new Set(delta.removed_nodes ?? []);
  const byId = new Map(nodes.map((n) => [n.id, n]));

  let previews = data.previews;
  if (nodes.length > 0 || removed.size > 0) {
    const seen = new Set<string>();
    previews = data.previews.map((p) => {
      const n = byId.get(p.id);
      if (n) {
        seen.add(p.id);
        return { ...p, title: n.title ?? p.title, observed_at: n.observed_at ?? p.observed_at, thumbnail: n.thumbnail ?? p.thumbnail,
          campaign_id: n.campaign_id ?? null, new: n.new ?? true, decision: n.decision ?? p.decision };
      }
      return removed.has(p.id) && p.new ? { ...p, new: false } : p;
    });
    for (const n of nodes) {
      if (seen.has(n.id)) continue;
      const row: Preview = { id: n.id, title: n.title ?? null, observed_at: n.observed_at ?? null, thumbnail: n.thumbnail ?? null, scheme: null,
        campaign_id: n.campaign_id ?? null, video: null, new: n.new ?? true, decision: n.decision ?? null };
      previews = [...previews, row];
    }
  }

  let campaigns = data.campaigns;
  const changed = new Map((delta.campaigns ?? []).map((c) => [c.id, c]));
  const joined = nodesByCampaign(nodes);
  if (changed.size > 0 || nodes.length > 0 || removed.size > 0) {
    campaigns = data.campaigns.map((c) => {
      const dc = changed.get(c.id);
      const add = joined.get(c.id) ?? [];
      // members that moved to another campaign, or are no longer new
      let members = c.members.filter((m) => { const n = byId.get(m.id); return !n || n.campaign_id === c.id; });
      if (removed.size > 0) members = members.map((m) => (removed.has(m.id) && m.new ? { ...m, new: false } : m));
      for (const n of add) {
        const i = members.findIndex((m) => m.id === n.id);
        const row = { id: n.id, thumbnail: n.thumbnail ?? (i >= 0 ? members[i]!.thumbnail : null), new: n.new ?? true };
        members = i >= 0 ? members.map((m, j) => (j === i ? { ...m, ...row } : m)) : [...members, row];
      }
      const membersChanged = members.length !== c.members.length || members.some((m, i) => m !== c.members[i]);
      if (!dc && !membersChanged) return c;
      const next: DashCampaign = { ...c, members: membersChanged ? members : c.members };
      if (dc) {
        next.size = dc.size;
        next.new = dc.new ?? 0;
        if (dc.label != null) next.label = dc.label;
        if (dc.group_id !== undefined) next.group_id = dc.group_id;
      }
      return next;
    });
  }

  return { ...data, kpis: delta.kpis ?? data.kpis, previews, campaigns, live: bumpLive(data.live, delta) };
}

/**
 * Campaigns of the delta that are not in the cached list and are not solo pseudo-campaigns: the patch cannot create
 * them (confidence, pivots are unknown), the query has to be refetched.
 */
export function unknownCampaigns(ids: Iterable<string>, delta: StreamDelta): string[] {
  const have = new Set(ids);
  return (delta.campaigns ?? []).filter((c) => !c.solo && !c.id.startsWith("solo:") && !have.has(c.id)).map((c) => c.id);
}

/** Update a job row of the dashboard from a `job` event; same object when the job is not listed or unchanged. */
export function patchDashboardJob(data: DashboardData, job: StreamJob): DashboardData {
  const i = data.jobs.findIndex((j) => j.id === job.id);
  if (i < 0) return data;
  const cur = data.jobs[i]!;
  const next = { ...cur, status: job.status ?? cur.status, total: job.total ?? cur.total, done: job.done ?? cur.done, failed: job.failed ?? cur.failed };
  if (next.status === cur.status && next.total === cur.total && next.done === cur.done && next.failed === cur.failed) return data;
  return { ...data, jobs: data.jobs.map((j, k) => (k === i ? next : j)) };
}

// ------------------------------------------------------------------ /v1/campaigns

/** Apply a delta to a /v1/campaigns page: current `size`, `new`, members joined since the analysis. */
export function patchCampaignPage(page: CampaignPage, delta: StreamDelta): CampaignPage {
  if (skip(page.live, page.live?.run_id ?? page.run_id, delta)) return page;
  const changed = new Map((delta.campaigns ?? []).map((c) => [c.id, c]));
  const joined = nodesByCampaign(delta.nodes);
  const items = changed.size === 0 && joined.size === 0 ? page.items : page.items.map((c) => {
    const dc = changed.get(c.id);
    const add = (joined.get(c.id) ?? []).map((n) => n.id).filter((id) => !c.members.includes(id));
    if (!dc && add.length === 0) return c;
    const next: Campaign = { ...c, members: add.length ? [...c.members, ...add] : c.members };
    if (dc) {
      next.size = dc.size;
      next.new = dc.new ?? 0;
      if (dc.label != null) next.label = dc.label;
      if (dc.group_id !== undefined) next.group_id = dc.group_id;
      if (dc.first_seen !== undefined) next.first_seen = dc.first_seen;
      if (dc.last_seen !== undefined) next.last_seen = dc.last_seen;
    }
    return next;
  });
  return { ...page, items, live: bumpLive(page.live, delta) };
}

// ------------------------------------------------------------------ /v1/graph/overview (raw tables)

export function isRawOverview(value: unknown): value is RawOverview {
  if (!value || typeof value !== "object") return false;
  const v = value as { campaigns?: { columns?: unknown; rows?: unknown }; links?: { columns?: unknown; rows?: unknown } };
  return Array.isArray(v.campaigns?.columns) && Array.isArray(v.campaigns?.rows) && Array.isArray(v.links?.columns) && Array.isArray(v.links?.rows);
}

/** Levels the overview links were built with; the server default when the payload does not say. */
export function overviewLevels(raw: Pick<RawOverview, "levels">): Set<string> {
  const lv = raw.levels;
  const list = Array.isArray(lv) ? lv : typeof lv === "string" ? lv.split(",") : [...DEFAULT_OVERVIEW_LEVELS];
  return new Set(list.map((x) => String(x).trim()).filter(Boolean));
}

/** Table with a trailing column `name` (filled with `fill`) when it is missing. */
function withColumn(t: RawTable, name: string, fill: unknown): RawTable {
  if (t.columns.includes(name)) return t;
  return { columns: [...t.columns, name], rows: t.rows.map((r) => [...r, fill]) };
}

function rowOf(columns: string[], values: Record<string, unknown>, base?: unknown[]): unknown[] {
  return columns.map((c, i) => (c in values ? values[c] : base ? base[i] : null));
}

const linkKey = (a: string, b: string) => (a < b ? `${a}\u0000${b}` : `${b}\u0000${a}`);

/** Link row for one (source, target) from its per-level entries under a level filter; null when no pairs are left. */
export function aggregateLink(entries: DeltaLink[], allowed: Set<string>): { pairs: number; max_bits: number; sum_bits: number; family: string | null; level: string; new: number } | null {
  let pairs = 0;
  let maxBits = 0;
  let sumBits = 0;
  let fresh = 0;
  let level: string | null = null;
  const fam = new Map<string, number>();
  for (const e of entries) {
    if (!allowed.has(e.level) || !(e.pairs > 0)) continue;
    pairs += e.pairs;
    maxBits = Math.max(maxBits, e.max_bits ?? 0);
    sumBits += e.sum_bits ?? 0;
    fresh += e.new ?? 0;
    for (const [f, v] of Object.entries(e.family_bits ?? {})) if (v > 0) fam.set(f, (fam.get(f) ?? 0) + v);
    if (level === null || (LEVEL_RANK[e.level] ?? 0) > (LEVEL_RANK[level] ?? 0)) level = e.level;
  }
  if (pairs === 0 || level === null) return null;
  let family: string | null = null;
  let best = -Infinity;
  for (const [f, v] of fam) if (v > best) { best = v; family = f; }
  return { pairs, max_bits: maxBits, sum_bits: round3(sumBits), family, level, new: fresh };
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/**
 * Apply a delta to a raw /v1/graph/overview payload: campaign rows (size, new, internal stats), new solo campaigns
 * appended, cross-campaign links recomputed for the payload's `levels` filter, `live.nodes` / `live.edges` upserted
 * with positions from `placeNode`.
 */
export function patchOverview(raw: RawOverview, delta: StreamDelta): RawOverview {
  if (skip(raw.live, raw.live?.run_id ?? raw.run_id, delta)) return raw;
  const nodes = delta.nodes ?? [];
  const nodeById = new Map(nodes.map((n) => [n.id, n]));

  // campaigns
  let camps = withColumn(raw.campaigns, "new", 0);
  const changed = delta.campaigns ?? [];
  if (changed.length > 0) {
    const cols = camps.columns;
    const idx = cols.indexOf("id");
    const pos = new Map(camps.rows.map((r, i) => [String(r[idx]), i]));
    const rows = [...camps.rows];
    for (const c of changed) {
      const vals: Record<string, unknown> = { size: c.size, new: c.new ?? 0 };
      if (c.label != null) vals.label = c.label;
      if (c.group_id !== undefined) vals.group_id = c.group_id;
      if (c.first_seen !== undefined) vals.first_seen = c.first_seen;
      if (c.last_seen !== undefined) vals.last_seen = c.last_seen;
      if (c.internal_edges !== undefined) vals.internal_edges = c.internal_edges;
      if (c.max_internal_bits !== undefined) vals.max_internal_bits = c.max_internal_bits == null ? null : round3(c.max_internal_bits);
      if (c.family !== undefined) vals.family = c.family;
      const i = pos.get(c.id);
      if (i !== undefined) {
        rows[i] = rowOf(cols, vals, rows[i]);
        continue;
      }
      // new pseudo-campaign (solo:<incident>) or a campaign the base does not know: a small disc on the outer ring
      const solo = c.solo ?? c.id.startsWith("solo:");
      const nodeId = c.id.startsWith("solo:") ? c.id.slice(5) : c.id;
      const n = nodeById.get(nodeId);
      const p = placeNode(nodeId, null, raw.base_bounds ?? raw.bounds);
      rows.push(rowOf(cols, {
        id: c.id, label: c.label ?? n?.title ?? c.id, confidence: null, group_id: c.group_id ?? null, size: c.size,
        first_seen: c.first_seen ?? n?.observed_at ?? null, last_seen: c.last_seen ?? n?.observed_at ?? null, scheme: null,
        x: p.x, y: p.y, r: 0.8, internal_edges: c.internal_edges ?? 0,
        max_internal_bits: c.max_internal_bits == null ? null : round3(c.max_internal_bits), family: c.family ?? null,
        thumbnail: n?.thumbnail ?? null, solo, new: c.new ?? 0,
      }));
      pos.set(c.id, rows.length - 1);
    }
    camps = { columns: cols, rows };
  }

  // groups (size / new when the table has those columns)
  let groups = raw.groups;
  if (groups && (delta.groups ?? []).length > 0) {
    const g = groups;
    const gi = g.columns.indexOf("id");
    const byId = new Map((delta.groups ?? []).map((x) => [x.id, x]));
    groups = { columns: g.columns, rows: g.rows.map((r) => {
      const d = byId.get(String(r[gi]));
      if (!d) return r;
      const vals: Record<string, unknown> = { size: d.size };
      if (g.columns.includes("new")) vals.new = d.new ?? 0;
      return rowOf(g.columns, vals, r);
    }) };
  }

  // links: per (source, target) of the delta, recompute the row from all its levels under the payload's filter
  let links = withColumn(raw.links, "new", 0);
  const minBits = num(raw.min_bits) ?? 0;
  if ((delta.links ?? []).length > 0 && minBits <= 0) {
    const allowed = overviewLevels(raw);
    const byKey = new Map<string, DeltaLink[]>();
    for (const l of delta.links ?? []) {
      const k = linkKey(l.source, l.target);
      byKey.set(k, [...(byKey.get(k) ?? []), l]);
    }
    const cols = links.columns;
    const si = cols.indexOf("source");
    const ti = cols.indexOf("target");
    const rows: unknown[][] = [];
    const done = new Set<string>();
    for (const r of links.rows) {
      const k = linkKey(String(r[si]), String(r[ti]));
      const entries = byKey.get(k);
      if (!entries) { rows.push(r); continue; }
      done.add(k);
      const agg = aggregateLink(entries, allowed);
      if (agg) rows.push(rowOf(cols, agg, r));
    }
    for (const [k, entries] of byKey) {
      if (done.has(k)) continue;
      const agg = aggregateLink(entries, allowed);
      if (agg) rows.push(rowOf(cols, { source: entries[0]!.source, target: entries[0]!.target, ...agg }));
    }
    links = { columns: cols, rows };
  }

  // live nodes / edges
  const live = bumpLive(raw.live, delta);
  const removed = new Set(delta.removed_nodes ?? []);
  let liveNodes = raw.live?.nodes ?? [];
  let liveEdges = raw.live?.edges ?? [];
  if (nodes.length > 0 || removed.size > 0) {
    const cols = camps.columns;
    const [ci, xi, yi, ri, soloI] = ["id", "x", "y", "r", "solo"].map((c) => cols.indexOf(c)) as [number, number, number, number, number];
    const discs = new Map<string, { x: number; y: number; r: number } | null>();
    for (const r of camps.rows) {
      const x = num(r[xi]); const y = num(r[yi]); const rr = num(r[ri]);
      const solo = soloI >= 0 && r[soloI] === true;
      discs.set(String(r[ci]), !solo && x !== null && y !== null && rr !== null ? { x, y, r: rr } : null);
    }
    const map = new Map(liveNodes.filter((n) => !removed.has(n.id)).map((n) => [n.id, n]));
    for (const n of nodes) {
      const mc = n.map_campaign ?? n.campaign_id ?? `solo:${n.id}`;
      const disc = mc.startsWith("solo:") ? null : (discs.get(mc) ?? null);
      const p = placeNode(n.id, disc, raw.base_bounds ?? raw.bounds);
      map.set(n.id, { ...(map.get(n.id) ?? {}), ...n, x: p.x, y: p.y });
    }
    liveNodes = [...map.values()];
  }
  if ((delta.edges ?? []).length > 0) {
    const map = new Map(liveEdges.map((e) => [linkKey(e.source, e.target), e]));
    for (const e of delta.edges ?? []) map.set(linkKey(e.source, e.target), e);
    liveEdges = [...map.values()];
  }
  live.nodes = liveNodes;
  live.edges = liveEdges;

  const out: RawOverview = { ...raw, campaigns: camps, links, live };
  if (groups) out.groups = groups;
  return out;
}

// ------------------------------------------------------------------ status store

export type LiveStatus = "live" | "connecting" | "polling" | "off";

let status: LiveStatus = "off";
const statusListeners = new Set<() => void>();

export const liveStatus = {
  get: (): LiveStatus => status,
  set(next: LiveStatus) {
    if (next === status) return;
    status = next;
    statusListeners.forEach((l) => l());
  },
  subscribe(l: () => void) {
    statusListeners.add(l);
    return () => { statusListeners.delete(l); };
  },
};

export function useLiveStatus(): LiveStatus {
  return useSyncExternalStore(liveStatus.subscribe, liveStatus.get, liveStatus.get);
}

// ------------------------------------------------------------------ latency

const PATCH_MS_KEEP = 200;
const patchMs: number[] = [];
let deltasApplied = 0;
let lastSeqSeen: number | null = null;

/** Last ≤ 200 durations (ms, performance.now) of applying one stream message to the caches. */
export function getPatchLatencies(): number[] {
  return [...patchMs];
}

function recordPatch(ms: number, seq: number) {
  patchMs.push(ms);
  if (patchMs.length > PATCH_MS_KEEP) patchMs.splice(0, patchMs.length - PATCH_MS_KEEP);
  deltasApplied += 1;
  lastSeqSeen = seq;
  stampDebug();
}

function debugEnabled(): boolean {
  if (import.meta.env.DEV) return true;
  try {
    return typeof localStorage !== "undefined" && localStorage.getItem("hydraLiveDebug") === "1";
  } catch {
    return false;
  }
}

function stampDebug() {
  if (typeof window === "undefined" || !debugEnabled()) return;
  (window as unknown as { __hydraLive?: unknown }).__hydraLive = { deltas: deltasApplied, lastSeq: lastSeqSeen, patchMs: [...patchMs] };
}

/** Test hook: clear latency samples and counters. */
export function resetPatchStats() {
  patchMs.length = 0;
  deltasApplied = 0;
  lastSeqSeen = null;
}

// ------------------------------------------------------------------ cache application

const isDashboardKey = (q: Query) => q.queryKey[0] === "task" && q.queryKey[1] === "dashboard";
const isCampaignsKey = (q: Query) => q.queryKey[0] === "task" && q.queryKey[1] === "campaigns" && q.queryKey.length === 2;

function isCampaignPage(v: unknown): v is CampaignPage {
  return Boolean(v && typeof v === "object" && Array.isArray((v as { items?: unknown }).items));
}

function isDashboard(v: unknown): v is DashboardData {
  return Boolean(v && typeof v === "object" && Array.isArray((v as { previews?: unknown }).previews) && Array.isArray((v as { campaigns?: unknown }).campaigns));
}

/** GRAPH-2 constellation cache: `["task", "graph2", "overview", run, source]` → `{source, overview}`. */
const isGraph2OverviewKey = (q: Query) => q.queryKey[0] === "task" && q.queryKey[1] === "graph2" && q.queryKey[2] === "overview";

function isGraph2Overview(v: unknown): v is { source: unknown; overview: Overview & { raw: RawOverview } } {
  const ov = v && typeof v === "object" ? (v as { overview?: Overview }).overview : undefined;
  return Boolean(ov && typeof ov === "object" && isRawOverview(ov.raw));
}

function refetch(client: QueryClient, q: Query) {
  void client.invalidateQueries({ predicate: (x) => x === q });
}

/**
 * Apply one delta to every cached payload it concerns. Queries are updated through `Query.setData` (not
 * `setQueryData(key)`), because run-scoped keys hash with the selected run and caches of other runs must not be hit.
 * Returns how many caches changed.
 */
export function applyDelta(client: QueryClient, delta: StreamDelta): number {
  let changed = 0;
  // a finished assign-batch run (`kinds: ["batch"]`, merge-batch4): batch views re-read the stored report
  if (delta.kinds?.includes("batch")) void client.invalidateQueries({ queryKey: ["task", "assign-batch"] });
  const update = <T,>(q: Query, data: T, next: T) => {
    if (next !== data) {
      q.setData(next, { manual: true });
      changed += 1;
    }
  };
  for (const q of client.getQueryCache().findAll({ predicate: (q) => q.state.data !== undefined })) {
    const data = q.state.data;
    if (isDashboardKey(q) && isDashboard(data)) {
      if (otherRun(dashboardRun(data), delta) || isApplied(data.live, delta)) continue;
      if (isGap(data.live, delta) || unknownCampaigns(data.campaigns.map((c) => c.id), delta).length) { refetch(client, q); continue; }
      update(q, data, patchDashboard(data, delta));
    } else if (isCampaignsKey(q) && isCampaignPage(data)) {
      if (otherRun(data.live?.run_id ?? data.run_id, delta) || isApplied(data.live, delta)) continue;
      if (isGap(data.live, delta) || unknownCampaigns(data.items.map((c) => c.id), delta).length) { refetch(client, q); continue; }
      update(q, data, patchCampaignPage(data, delta));
    } else if (isGraph2OverviewKey(q) && isGraph2Overview(data)) {
      // GRAPH-2 caches the normalised overview: patch the raw payload it keeps, then normalise again
      const raw = data.overview.raw;
      if (otherRun(raw.live?.run_id ?? raw.run_id, delta) || isApplied(raw.live, delta)) continue;
      if (isGap(raw.live, delta)) { refetch(client, q); continue; }
      const next = patchOverview(raw, delta);
      if (next !== raw) update(q, data, { ...data, overview: { ...normOverview(next, data.overview.source), raw: next } });
    } else if (isRawOverview(data)) {
      if (otherRun(data.live?.run_id ?? data.run_id, delta) || isApplied(data.live, delta)) continue;
      if (isGap(data.live, delta)) { refetch(client, q); continue; }
      update(q, data, patchOverview(data, delta));
    }
  }
  return changed;
}

/** Caches of `run` whose `live.version` is behind the stream (fetched before connect, no replay): refetch them. */
function refetchBehind(client: QueryClient, hello: StreamHello) {
  for (const q of client.getQueryCache().findAll({ predicate: (q) => q.state.data !== undefined })) {
    const data = q.state.data as { live?: LiveBlock; run_id?: string | null; run?: { id?: string } | null } | undefined;
    const live = data?.live;
    if (!live || typeof live.version !== "number") continue;
    const run = live.run_id ?? data?.run_id ?? data?.run?.id ?? null;
    if (run === hello.run_id && live.version < hello.version) refetch(client, q);
  }
}

// ------------------------------------------------------------------ connection

/** Reconnect delay: 1 s doubling to 30 s, plus up to 25 % jitter (capped at 30 s). */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(30_000, 1000 * 2 ** Math.max(0, attempt));
  return Math.min(30_000, Math.round(base * (1 + 0.25 * random())));
}

export const FAIL_LIMIT = 5;
export const FAIL_WINDOW_MS = 60_000;

/** EventSource-compatible constructor (tests inject a fake). */
export type EventSourceCtor = new (url: string) => Pick<EventSource, "addEventListener" | "close" | "readyState"> & {
  onerror: ((this: EventSource, ev: Event) => unknown) | null;
  onopen: ((this: EventSource, ev: Event) => unknown) | null;
};

export interface LiveStreamOptions {
  client: QueryClient;
  /** Selected run (null = latest); only used to decide whether a `new_run` reset moves the stream. */
  runId: string | null;
  EventSourceImpl?: EventSourceCtor | null;
  random?: () => number;
  now?: () => number;
}

export function streamUrl(lastEventId: number | null): string {
  const path = lastEventId == null ? "/v1/stream" : `/v1/stream?last_event_id=${lastEventId}`;
  return apiUrl(withRun(path));
}

type Source = InstanceType<EventSourceCtor>;

export class LiveStream {
  private readonly opts: LiveStreamOptions;
  private readonly ES: EventSourceCtor | null;
  private es: Source | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private failures: number[] = [];
  private lastSeq: number | null = null;
  private stopped = true;
  private missingJobRefetchAt = 0;

  constructor(opts: LiveStreamOptions) {
    this.opts = opts;
    const fallback = typeof EventSource === "undefined" ? null : (EventSource as unknown as EventSourceCtor);
    this.ES = opts.EventSourceImpl === undefined ? fallback : opts.EventSourceImpl;
  }

  private now() {
    return (this.opts.now ?? Date.now)();
  }

  start() {
    this.stopped = false;
    if (!this.ES) {
      liveStatus.set("polling");
      return;
    }
    liveStatus.set("connecting");
    this.open();
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.es?.close();
    this.es = null;
    liveStatus.set("off");
  }

  private open() {
    if (this.stopped || !this.ES) return;
    this.es?.close();
    const es = new this.ES(streamUrl(this.lastSeq));
    this.es = es;
    const on = (name: string, fn: (data: unknown, ev: MessageEvent) => void) =>
      es.addEventListener(name, ((ev: MessageEvent) => {
        if (this.es !== es) return;
        let data: unknown;
        try {
          data = JSON.parse(String(ev.data));
        } catch {
          return;
        }
        fn(data, ev);
      }) as EventListener);
    on("hello", (d) => { activity.touch(); this.onHello(d as StreamHello); });
    on("delta", (d, ev) => { activity.push(deltaEntries(d as StreamDelta)); activity.touch(); this.onDelta(d as StreamDelta, ev); });
    on("reset", (d, ev) => {
      const r = d as StreamReset;
      activity.push([{ kind: "reset", title: r?.reason === "new_run" ? "Nowy przebieg analizy" : "Odświeżenie danych przebiegu", detail: r?.reason, tone: "info" }]);
      this.onReset(r, ev);
    });
    on("job", (d) => {
      const e = d && typeof (d as StreamJob).id === "string" ? jobEntry(d as StreamJob) : null;
      if (e) activity.push([e]); else activity.touch();
      this.onJob(d as StreamJob);
    });
    on("watch", (d) => {
      activity.push([watchEntry((d ?? {}) as Parameters<typeof watchEntry>[0])]);
      void this.opts.client.invalidateQueries({ queryKey: ["watch"] });
    });
    // realtime watch engine (WATCH-RT): published on watchRtBus for the /watch page; matches also go to the activity
    // feed. Query keys of that page are ["watch-rt", ...], so the classic invalidation above does not touch them.
    on("watch.poll", (d) => {
      activity.touch();
      const p = d as WatchPollEvent | null;
      if (p && typeof p.source === "string") watchRtBus.emit({ type: "poll", poll: p });
    });
    on("watch.item", (d) => {
      activity.touch();
      const item = (d as { item?: RtItem } | null)?.item;
      if (item && typeof item.id === "number") watchRtBus.emit({ type: "item", item });
    });
    on("watch.match", (d) => {
      const match = (d as { match?: RtMatch } | null)?.match;
      if (!match || typeof match.id !== "number") { activity.touch(); return; }
      activity.push([watchMatchEntry(match)]);
      watchRtBus.emit({ type: "match", match });
      void this.opts.client.invalidateQueries({ queryKey: ["watch-rt", "matches"] });
    });
    on("meta.analysis", (d) => {
      const e = d && typeof d === "object" ? metaAnalysisEntry(d as Parameters<typeof metaAnalysisEntry>[0]) : null;
      if (e) activity.push([e]); else activity.touch();
      void this.opts.client.invalidateQueries({ queryKey: ["meta-ads"] });
    });
    on("watch.source_state", (d) => {
      activity.touch();
      const source = (d as { source?: RtSource } | null)?.source;
      if (!source || typeof source.id !== "string") return;
      patchStatusSource(this.opts.client, source);
      watchRtBus.emit({ type: "source_state", source });
    });
    on("watch.engine", () => {
      // engine state change (start / pause / replay done): the status payload carries the full state
      activity.touch();
      void this.opts.client.invalidateQueries({ queryKey: ["watch-rt", "status"] });
    });
    es.onerror = () => {
      if (this.es !== es) return;
      this.fail();
    };
  }

  private fail() {
    this.es?.close();
    this.es = null;
    if (this.stopped) return;
    const t = this.now();
    this.failures = [...this.failures.filter((x) => t - x < FAIL_WINDOW_MS), t];
    const prev = liveStatus.get();
    if (this.failures.length >= FAIL_LIMIT || prev === "polling") liveStatus.set("polling");
    else liveStatus.set("connecting");
    // leaving "live": data may have changed while disconnected and the dashboard poll has to restart
    if (prev === "live") void this.opts.client.invalidateQueries({ queryKey: ["task", "dashboard"] });
    const delay = backoffDelay(this.attempt, this.opts.random);
    this.attempt += 1;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => { this.timer = null; this.open(); }, delay);
  }

  private onHello(h: StreamHello) {
    this.attempt = 0;
    this.failures = [];
    if (typeof h.seq === "number") this.lastSeq = Math.max(this.lastSeq ?? 0, h.seq);
    liveStatus.set("live");
    if (!h.resumed) refetchBehind(this.opts.client, h);
  }

  private onDelta(d: StreamDelta, ev: MessageEvent) {
    if (typeof d?.seq !== "number") return;
    const t0 = performance.now();
    applyDelta(this.opts.client, d);
    recordPatch(performance.now() - t0, d.seq);
    this.lastSeq = Math.max(this.lastSeq ?? 0, d.seq, Number(ev.lastEventId) || 0);
    // flow and analytics are cheap to rebuild server-side; refetch instead of patching
    void this.opts.client.invalidateQueries({ queryKey: ["task", "flow"] });
    void this.opts.client.invalidateQueries({ queryKey: ["task", "analytics"] });
  }

  private onReset(r: StreamReset, ev: MessageEvent) {
    const seq = Number(ev.lastEventId);
    if (Number.isFinite(seq) && seq > 0) this.lastSeq = seq;
    void this.opts.client.invalidateQueries({ predicate: (q) => isRunScopedKey(q.queryKey) });
    if (r?.reason === "new_run" && this.opts.runId == null) {
      // following "latest": the stream moves to the new run (resolved by the server at connect)
      this.lastSeq = null;
      this.open();
    }
  }

  private onJob(j: StreamJob) {
    if (!j || typeof j.id !== "string") return;
    const client = this.opts.client;
    void client.invalidateQueries({ queryKey: queryKeys.job(j.id) });
    let listed = false;
    for (const q of client.getQueryCache().findAll({ predicate: (q) => isDashboardKey(q) && isDashboard(q.state.data) })) {
      const data = q.state.data as DashboardData;
      if (data.jobs.some((x) => x.id === j.id)) {
        listed = true;
        const next = patchDashboardJob(data, j);
        if (next !== data) q.setData(next, { manual: true });
      }
    }
    // a job the dashboard does not list yet (just started): refetch the dashboard, at most every 5 s
    const t = this.now();
    if (!listed && t - this.missingJobRefetchAt > 5000) {
      this.missingJobRefetchAt = t;
      void client.invalidateQueries({ queryKey: ["task", "dashboard"] });
    }
  }
}

/** Mount once (AppShell): one stream per tab for the selected run, reopened on a run switch. */
export function useLiveStream(): LiveStatus {
  const client = useQueryClient();
  const run = useSelectedRun();
  useEffect(() => {
    const stream = new LiveStream({ client, runId: run });
    stream.start();
    return () => stream.stop();
  }, [client, run]);
  return useLiveStatus();
}
