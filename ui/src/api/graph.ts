/**
 * Graph data adapter for the constellation explorer (GRAPH-2).
 *
 * Two sources behind one interface:
 * - "lod": the level-of-detail endpoints of hydra/api_graph.py (overview of campaigns/groups with positions,
 *   campaign detail, incident neighbours), see docs/ui/GRAPH_API.md and CR-GRAPH2-1;
 * - "legacy": the single `/v1/graph/explore` payload, fetched once per run and sliced in memory into the same
 *   shapes (same numbers, slow first paint on large runs).
 * The source is picked by probing the overview endpoint: HTTP 404 -> legacy. Every number shown by the UI comes from
 * one of these payloads; the adapter only regroups and sums values that are already there (link aggregates).
 */
import { ApiError, apiFetch } from "./client";
import type { Explore, GCampaign, GEdge, GGroup, GNode, GStats, Level } from "../components/graph/api";

export type { GEdge, GNode, Level } from "../components/graph/api";

/** Pseudo campaign holding incidents without a campaign (legacy source; the LOD overview may send its own). */
export const UNASSIGNED = "__bez_kampanii__";

export interface OvCampaign {
  id: string;
  label: string;
  size: number;
  group_id: string | null;
  first_seen: string | null;
  last_seen: string | null;
  confidence: number | null;
  /** Precomputed position and radius (any unit) when the API sends them. */
  x: number | null;
  y: number | null;
  r: number | null;
  /** Sum of bits per gene family over the campaign's internal links (absent = not sent by the API). */
  family_bits: Record<string, number> | null;
  thumbnail: string | null;
  /** True for the "no campaign" bucket. */
  unassigned?: boolean;
  /** first_seen is the earliest observed / registration day of the member incidents (campaign row had none). */
  first_seen_from_incidents?: boolean;
  /** Dominant gene family of the internal links as sent by the API (LOD overview has this instead of family_bits). */
  family?: string | null;
  /** Pseudo campaign `solo:<incident>` for one incident without a campaign (LOD contract). */
  solo?: boolean;
}

export interface OvGroup { id: string; label: string; campaign_ids: string[]; x: number | null; y: number | null; r: number | null }

/** Inter-campaign link aggregated over incident pairs. */
export interface OvLink { id: string; source: string; target: string; level: Level; pairs: number; max_bits: number; family: string | null }

export interface Overview {
  source: "lod" | "legacy";
  run_id: string | null;
  corpus: string | null;
  synthetic: boolean;
  thresholds: { link_bits: number; kit_bits: number } | null;
  stats: GStats | null;
  campaigns: OvCampaign[];
  groups: OvGroup[];
  links: OvLink[];
  /** All campaigns carry x/y/r from the API (no client layout needed). */
  positioned: boolean;
  incidents_total: number;
  /** LOD only: the raw `/v1/graph/overview` payload, kept so the live stream can patch it (`patchOverview`) and
   *  normalise again (merge-batch4; the normalised campaigns alone cannot carry the stream's placement inputs). */
  raw?: Raw;
}

export interface DetailNode extends GNode { x?: number | null; y?: number | null }
export interface ExternalEdge extends GEdge { other_campaign: string | null }

export interface CampaignDetail {
  campaign_id: string;
  nodes: DetailNode[];
  edges: GEdge[];
  external: ExternalEdge[];
  /** The API cut the member or edge list. */
  truncated: boolean;
  /** External links the API left out (strongest kept), when it says so. */
  truncated_external?: number;
}

export interface Neighbours { incident: GNode | null; campaign_id: string | null; edges: GEdge[]; nodes: GNode[] }

export interface GraphSource {
  kind: "lod" | "legacy";
  overview(signal?: AbortSignal): Promise<Overview>;
  campaign(id: string, signal?: AbortSignal): Promise<CampaignDetail>;
  neighbours(incident: string, signal?: AbortSignal): Promise<Neighbours>;
}

const enc = encodeURIComponent;

/** Endpoint paths of the LOD contract (kept in one place so the contract can be followed with a one-line change). */
export const LOD_PATHS = {
  // asset links are aggregated too (the contract default is kit,campaign,review); weak ones stay out of the overview
  overview: "/v1/graph/overview?levels=campaign,kit,asset,review",
  campaign: (id: string) => `/v1/graph/campaign/${enc(id)}?top_reasons=3`,
  neighbours: (id: string) => `/v1/graph/neighbors/${enc(id)}?limit=200`,
};

/**
 * Compact table of the LOD contract (`{columns, rows}`) to objects, read by column name so columns added at the end
 * never break the UI. Plain arrays of objects pass through.
 */
export function rowsOf(t: unknown): Record<string, unknown>[] {
  if (Array.isArray(t)) return t.filter((x) => x && typeof x === "object") as Record<string, unknown>[];
  if (!t || typeof t !== "object") return [];
  const { columns, rows } = t as { columns?: unknown; rows?: unknown };
  if (!Array.isArray(columns) || !Array.isArray(rows)) return [];
  return rows.filter(Array.isArray).map((r: unknown[]) => Object.fromEntries(columns.map((c, i) => [String(c), r[i] ?? null])));
}

// ----------------------------------------------------------------------------------------------- normalisers
type Raw = Record<string, unknown>;
const num = (v: unknown): number | null => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v: unknown): string | null => (typeof v === "string" && v ? v : null);
const arr = (v: unknown): Raw[] => rowsOf(v);
const LEVEL_SET = new Set<Level>(["campaign", "kit", "asset", "weak", "review"]);
const level = (v: unknown): Level => (typeof v === "string" && LEVEL_SET.has(v as Level) ? (v as Level) : "weak");

function famBits(v: unknown): Record<string, number> | null {
  if (!v || typeof v !== "object") return null;
  const out: Record<string, number> = {};
  for (const [k, x] of Object.entries(v as Raw)) if (typeof x === "number" && Number.isFinite(x)) out[k] = x;
  return out;
}

export function normCampaign(r: Raw): OvCampaign {
  const pos = (r.pos && typeof r.pos === "object" ? r.pos : r) as Raw;
  const id = String(r.id ?? r.campaign_id ?? "");
  return {
    id,
    label: str(r.label) ?? id,
    size: num(r.size) ?? num(r.n) ?? 0,
    group_id: str(r.group_id),
    first_seen: str(r.first_seen),
    last_seen: str(r.last_seen),
    confidence: num(r.confidence),
    x: num(pos.x),
    y: num(pos.y),
    r: num(pos.r) ?? num(r.radius),
    family_bits: famBits(r.family_bits),
    thumbnail: str(r.thumbnail),
    unassigned: Boolean(r.unassigned) || id === UNASSIGNED,
    family: str(r.family),
    solo: Boolean(r.solo) || id.startsWith("solo:"),
  };
}

export function normGroup(r: Raw): OvGroup {
  const id = String(r.id ?? "");
  return { id, label: str(r.label) ?? id, campaign_ids: Array.isArray(r.campaign_ids) ? r.campaign_ids.map(String) : [], x: num(r.x), y: num(r.y), r: num(r.r) };
}

export function normLink(r: Raw): OvLink {
  const s = String(r.source ?? r.a ?? "");
  const t = String(r.target ?? r.b ?? "");
  return { id: str(r.id) ?? `${s}|${t}`, source: s, target: t, level: level(r.level), pairs: num(r.pairs) ?? num(r.n) ?? 1, max_bits: num(r.max_bits) ?? num(r.bits) ?? 0, family: str(r.family) };
}

export function normEdge(r: Raw): GEdge {
  const source = String(r.source ?? r.a ?? "");
  const target = String(r.target ?? r.b ?? "");
  // LOD edge rows: robust = [robust, critical[], link]; reasons = [[family, kind, bits, value, doc_freq, text]]
  const robArr = Array.isArray(r.robust) ? (r.robust as unknown[]) : null;
  const rob = robArr ? ({ robust: robArr[0], critical: robArr[1], link: robArr[2] } as Raw) : r.robustness && typeof r.robustness === "object" ? (r.robustness as Raw) : null;
  const reasons = Array.isArray(r.reasons)
    ? (r.reasons as unknown[]).map((x) => (Array.isArray(x) ? ({ family: x[0], kind: x[1], bits: x[2], value: x[3], doc_freq: x[4], text: x[5] } as Raw) : (x as Raw))).filter((x) => x && typeof x === "object")
    : [];
  return {
    id: str(r.id) ?? `${source}|${target}`,
    source,
    target,
    bits: num(r.bits) ?? num(r.total_bits) ?? 0,
    p: num(r.p) ?? num(r.probability) ?? 0,
    hypothesis: str(r.hypothesis) ?? "",
    level: level(r.level),
    family: str(r.family),
    family_bits: famBits(r.family_bits) ?? {},
    campaign_bits: num(r.campaign_bits),
    kit_bits: num(r.kit_bits),
    asset_bits: num(r.asset_bits),
    inferred: Boolean(r.inferred),
    robustness: rob ? { robust: Boolean(rob.robust), critical: Array.isArray(rob.critical) ? rob.critical.map(String) : [], link: str(rob.link) } : null,
    review: (["must_link", "cannot_link", "unsure"] as const).find((x) => x === r.review) ?? null,
    reasons: reasons.map((x) => ({ family: str(x.family), kind: str(x.kind), bits: num(x.bits) ?? 0, value: str(x.value), doc_freq: num(x.doc_freq), text: str(x.text) ?? str(x.kind) ?? "" })),
  };
}

export function normNode(r: Raw): DetailNode {
  const id = String(r.id ?? "");
  return {
    id,
    title: str(r.title),
    campaign_id: str(r.campaign_id),
    group_id: str(r.group_id),
    observed_at: str(r.observed_at),
    registered_at: str(r.registered_at),
    corpus: str(r.corpus) ?? "ext",
    thumbnail: str(r.thumbnail),
    degree: num(r.degree) ?? 0,
    x: num(r.x),
    y: num(r.y),
  };
}

export function normOverview(r: Raw, source: Overview["source"]): Overview {
  const campaigns = arr(r.campaigns).map(normCampaign).filter((c) => c.id);
  const groups = arr(r.groups).map(normGroup).filter((g) => g.id);
  // LOD groups carry no member list: members are the campaigns pointing at the group
  for (const g of groups) if (g.campaign_ids.length === 0) g.campaign_ids = campaigns.filter((c) => c.group_id === g.id).map((c) => c.id);
  const stats = (r.stats && typeof r.stats === "object" ? r.stats : null) as GStats | null;
  const th = r.thresholds && typeof r.thresholds === "object" ? (r.thresholds as Raw) : null;
  return {
    source,
    run_id: str(r.run_id),
    corpus: str(r.corpus),
    synthetic: Boolean(r.synthetic),
    thresholds: th ? { link_bits: num(th.link_bits) ?? 10, kit_bits: num(th.kit_bits) ?? 6 } : null,
    stats,
    campaigns,
    groups,
    links: arr(r.links ?? r.campaign_links).map(normLink).filter((l) => l.source && l.target && l.source !== l.target),
    positioned: campaigns.length > 0 && campaigns.every((c) => c.x !== null && c.y !== null),
    incidents_total: num(r.incidents_total) ?? stats?.nodes ?? campaigns.reduce((s, c) => s + c.size, 0),
  };
}

export function normDetail(r: Raw, id: string): CampaignDetail {
  // members without a campaign belong to the requested pseudo campaign (`solo:<incident>`)
  const nodes = arr(r.nodes).map(normNode).filter((n) => n.id).map((n) => (n.campaign_id ? n : { ...n, campaign_id: id }));
  const inside = new Set(nodes.map((n) => n.id));
  const far = new Map(arr(r.external_nodes).map(normNode).map((n) => [n.id, n.campaign_id ?? `solo:${n.id}`]));
  const tr = r.truncated;
  const truncExt = tr && typeof tr === "object" ? num((tr as Raw).external) ?? 0 : 0;
  return {
    campaign_id: str(r.campaign_id) ?? id,
    nodes,
    edges: arr(r.edges).map(normEdge),
    external: arr(r.external ?? r.external_edges).map((e) => {
      const x = normEdge(e);
      const other = inside.has(x.source) ? x.target : x.source;
      return { ...x, other_campaign: str(e.other_campaign) ?? str(e.other_campaign_id) ?? far.get(other) ?? null };
    }),
    truncated: tr === true || truncExt > 0,
    ...(truncExt > 0 ? { truncated_external: truncExt } : {}),
  };
}

// ------------------------------------------------------------------------------- legacy: slice /explore in memory
/** Everything the legacy source derives from one /explore payload (pure; unit-tested). */
export interface LegacyIndex {
  overview: Overview;
  members: Map<string, GNode[]>;
  internal: Map<string, GEdge[]>;
  external: Map<string, ExternalEdge[]>;
  nodeById: Map<string, GNode>;
  edgesOf: Map<string, GEdge[]>;
}

export function indexExplore(x: Explore): LegacyIndex {
  const nodeById = new Map(x.nodes.map((n) => [n.id, n]));
  const camp = (id: string) => nodeById.get(id)?.campaign_id ?? UNASSIGNED;
  const members = new Map<string, GNode[]>();
  for (const n of x.nodes) {
    const c = n.campaign_id ?? UNASSIGNED;
    const list = members.get(c);
    if (list) list.push(n);
    else members.set(c, [n]);
  }
  const internal = new Map<string, GEdge[]>();
  const external = new Map<string, ExternalEdge[]>();
  const edgesOf = new Map<string, GEdge[]>();
  const fam = new Map<string, Record<string, number>>();
  const agg = new Map<string, { s: string; t: string; pairs: number; max: number; lv: Map<Level, number>; fam: Map<string, number> }>();
  const push = <T,>(m: Map<string, T[]>, k: string, v: T) => {
    const l = m.get(k);
    if (l) l.push(v);
    else m.set(k, [v]);
  };
  for (const e of x.edges) {
    push(edgesOf, e.source, e);
    push(edgesOf, e.target, e);
    const a = camp(e.source);
    const b = camp(e.target);
    if (a === b) {
      push(internal, a, e);
      const f = fam.get(a) ?? {};
      for (const [k, v] of Object.entries(e.family_bits)) if (v > 0) f[k] = (f[k] ?? 0) + v;
      fam.set(a, f);
      continue;
    }
    push(external, a, { ...e, other_campaign: b });
    push(external, b, { ...e, other_campaign: a });
    if (a === UNASSIGNED || b === UNASSIGNED) continue;
    const [s, t] = a < b ? [a, b] : [b, a];
    const k = `${s}|${t}`;
    const cur = agg.get(k) ?? { s, t, pairs: 0, max: 0, lv: new Map(), fam: new Map() };
    cur.pairs += 1;
    cur.max = Math.max(cur.max, e.bits);
    cur.lv.set(e.level, (cur.lv.get(e.level) ?? 0) + 1);
    if (e.family) cur.fam.set(e.family, (cur.fam.get(e.family) ?? 0) + e.bits);
    agg.set(k, cur);
  }
  const rank: Level[] = ["review", "campaign", "kit", "asset", "weak"];
  const links: OvLink[] = [...agg].map(([id, v]) => ({
    id, source: v.s, target: v.t, pairs: v.pairs, max_bits: v.max,
    level: rank.find((l) => v.lv.has(l)) ?? "weak",
    family: [...v.fam].sort((p, q) => q[1] - p[1])[0]?.[0] ?? null,
  }));
  const thumbOf = (c: string) => members.get(c)?.find((n) => n.thumbnail)?.thumbnail ?? null;
  const earliest = (c: string) => {
    let best: string | null = null;
    for (const n of members.get(c) ?? []) {
      const d = (n.observed_at ?? n.registered_at)?.slice(0, 10) ?? null;
      if (d && /^\d{4}-\d{2}-\d{2}$/.test(d) && (!best || d < best)) best = d;
    }
    return best;
  };
  const campaigns: OvCampaign[] = x.campaigns.map((c: GCampaign) => {
    const fromInc = c.first_seen ? null : earliest(c.id);
    return {
      id: c.id, label: c.label, size: members.get(c.id)?.length ?? c.size, group_id: c.group_id, first_seen: c.first_seen ?? fromInc, last_seen: c.last_seen,
      confidence: c.confidence, x: null, y: null, r: null, family_bits: fam.get(c.id) ?? {}, thumbnail: thumbOf(c.id),
      ...(fromInc ? { first_seen_from_incidents: true } : {}),
    };
  });
  const none = members.get(UNASSIGNED);
  if (none?.length) {
    campaigns.push({ id: UNASSIGNED, label: "bez kampanii", size: none.length, group_id: null, first_seen: null, last_seen: null, confidence: null, x: null, y: null, r: null, family_bits: fam.get(UNASSIGNED) ?? {}, thumbnail: null, unassigned: true });
  }
  const groups: OvGroup[] = x.groups.map((g: GGroup) => ({ id: g.id, label: g.label ?? g.id, campaign_ids: g.campaign_ids ?? [], x: null, y: null, r: null }));
  return {
    overview: {
      source: "legacy", run_id: x.run_id, corpus: x.corpus ?? null, synthetic: Boolean(x.synthetic), thresholds: x.thresholds ?? null,
      stats: x.stats, campaigns, groups, links, positioned: false, incidents_total: x.nodes.length,
    },
    members, internal, external, nodeById, edgesOf,
  };
}

function abortable<T>(p: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return p;
  if (signal.aborted) return Promise.reject(new DOMException("Aborted", "AbortError"));
  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(new DOMException("Aborted", "AbortError"));
    signal.addEventListener("abort", onAbort, { once: true });
    p.then((v) => { signal.removeEventListener("abort", onAbort); resolve(v); }, (e) => { signal.removeEventListener("abort", onAbort); reject(e); });
  });
}

export function legacySource(fetchExplore: () => Promise<Explore> = () => apiFetch<Explore>("/v1/graph/explore")): GraphSource {
  let idx: Promise<LegacyIndex> | null = null;
  // one shared download; a caller's abort only detaches that caller, never the shared fetch
  const get = () => (idx ??= fetchExplore().then(indexExplore).catch((e) => { idx = null; throw e; }));
  return {
    kind: "legacy",
    overview: (signal) => abortable(get().then((i) => i.overview), signal),
    campaign: (id, signal) => abortable(get().then((i) => ({ campaign_id: id, nodes: i.members.get(id) ?? [], edges: i.internal.get(id) ?? [], external: i.external.get(id) ?? [], truncated: false })), signal),
    neighbours: (id, signal) => abortable(get().then((i) => {
      const edges = i.edgesOf.get(id) ?? [];
      const ids = new Set(edges.flatMap((e) => [e.source, e.target]));
      const incident = i.nodeById.get(id) ?? null;
      return { incident, campaign_id: incident?.campaign_id ?? null, edges, nodes: [...ids].map((n) => i.nodeById.get(n)).filter((n): n is GNode => Boolean(n)) };
    }), signal),
  };
}

export function lodSource(fetcher: <T>(path: string, signal?: AbortSignal) => Promise<T> = (p, s) => apiFetch(p, { signal: s })): GraphSource {
  return {
    kind: "lod",
    overview: async (signal) => {
      const raw = await fetcher<Raw>(LOD_PATHS.overview, signal);
      return { ...normOverview(raw, "lod"), raw };
    },
    campaign: async (id, signal) => normDetail(await fetcher<Raw>(LOD_PATHS.campaign(id), signal), id),
    neighbours: async (id, signal) => {
      const r = await fetcher<Raw>(LOD_PATHS.neighbours(id), signal);
      const inc = r.incident && typeof r.incident === "object" ? normNode(r.incident as Raw) : null;
      // an incident without a campaign sits in its pseudo campaign on the LOD map
      const cid = str(r.campaign_id) ?? inc?.campaign_id ?? (inc ? `solo:${inc.id}` : null);
      return { incident: inc, campaign_id: cid, edges: arr(r.edges).map(normEdge), nodes: arr(r.nodes).map(normNode) };
    },
  };
}

/**
 * Picks the source: the LOD overview when it answers, otherwise (HTTP 404/405: endpoint not deployed) the legacy
 * explore payload. Other errors propagate, so a broken server is reported instead of silently downgraded.
 * Returns the overview from the chosen source too, so the probe is not wasted.
 */
export async function resolveSource(signal?: AbortSignal, forced?: "lod" | "legacy"): Promise<{ source: GraphSource; overview: Overview }> {
  if (forced !== "legacy") {
    const lod = lodSource();
    try {
      return { source: lod, overview: await lod.overview(signal) };
    } catch (e) {
      if (forced === "lod" || !(e instanceof ApiError) || (e.status !== 404 && e.status !== 405)) throw e;
    }
  }
  const legacy = legacySource();
  return { source: legacy, overview: await legacy.overview(signal) };
}
