/**
 * Pure geometry of the constellation view: campaign placement (API positions rescaled, or a deterministic group ->
 * campaign spiral pack), incident "growth rings" inside a campaign (oldest at the centre), convex hulls, top-k edge
 * ranks for far-zoom edge reduction, the growth timeline and keyboard navigation by direction.
 */
import type { GEdge, Level, OvCampaign, OvGroup } from "../../../api/graph";
import { spiralPack, sunflower } from "../layout";

export interface Pt { x: number; y: number }
export interface Disc extends Pt { id: string; r: number }

export interface Constellation {
  campaigns: Map<string, Disc>;
  groups: Map<string, Disc>;
  bbox: { x: [number, number]; y: [number, number] };
  /** Factor applied to API coordinates (null when the layout was computed in the browser). */
  scale: number | null;
}

/** Radius that holds `size` incidents on a unit-spaced sunflower. */
export const campaignRadius = (size: number) => sunflower(Math.max(1, size)).r;

function bboxOf(discs: Iterable<Disc>): Constellation["bbox"] {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const d of discs) {
    x0 = Math.min(x0, d.x - d.r); x1 = Math.max(x1, d.x + d.r); y0 = Math.min(y0, d.y - d.r); y1 = Math.max(y1, d.y + d.r);
  }
  return Number.isFinite(x0) ? { x: [x0, x1], y: [y0, y1] } : { x: [-1, 1], y: [-1, 1] };
}

function groupDiscs(groups: readonly OvGroup[], campaigns: Map<string, Disc>, pad: number): Map<string, Disc> {
  const out = new Map<string, Disc>();
  for (const g of groups) {
    const ms = g.campaign_ids.map((c) => campaigns.get(c)).filter((d): d is Disc => Boolean(d));
    if (ms.length === 0) continue;
    const cx = ms.reduce((s, d) => s + d.x, 0) / ms.length;
    const cy = ms.reduce((s, d) => s + d.y, 0) / ms.length;
    const r = Math.max(...ms.map((d) => Math.hypot(d.x - cx, d.y - cy) + d.r)) + pad;
    out.set(g.id, { id: g.id, x: cx, y: cy, r });
  }
  return out;
}

/**
 * Campaign discs in "incident units" (unit spacing between incidents). API positions are kept as given (only scaled,
 * so the median campaign is just large enough for its incidents); without positions the campaigns are packed by
 * group, largest first, like the classic explorer.
 */
export function layoutConstellation(campaigns: readonly OvCampaign[], groups: readonly OvGroup[], positioned: boolean): Constellation {
  const out = new Map<string, Disc>();
  if (positioned && campaigns.length > 0) {
    const ratios = campaigns.filter((c) => (c.r ?? 0) > 0).map((c) => campaignRadius(c.size) / c.r!).sort((a, b) => a - b);
    let s = ratios.length ? ratios[Math.floor(ratios.length / 2)]! : 1;
    if (!ratios.length) {
      // no radii: scale so the median nearest-neighbour distance fits two median campaigns
      const sizes = campaigns.map((c) => campaignRadius(c.size)).sort((a, b) => a - b);
      const med = sizes[Math.floor(sizes.length / 2)] ?? 1;
      const pts = campaigns.map((c) => ({ x: c.x!, y: c.y! }));
      const nn = pts.slice(0, 400).map((p, i) => Math.min(...pts.filter((_, j) => j !== i).map((q) => Math.hypot(p.x - q.x, p.y - q.y)))).filter(Number.isFinite).sort((a, b) => a - b);
      const d = nn[Math.floor(nn.length / 2)] ?? 1;
      s = d > 0 ? (2.4 * med) / d : 1;
    }
    for (const c of campaigns) out.set(c.id, { id: c.id, x: c.x! * s, y: c.y! * s, r: c.r !== null && c.r > 0 ? c.r * s : campaignRadius(c.size) });
    // API group discs when sent (scaled like the campaigns), else the smallest disc around the members
    const gs = groupDiscs(groups, out, 1.5);
    for (const g of groups) if (g.x !== null && g.y !== null && g.r !== null && gs.has(g.id)) gs.set(g.id, { id: g.id, x: g.x * s, y: g.y * s, r: Math.max(g.r * s, gs.get(g.id)!.r) });
    return { campaigns: out, groups: gs, bbox: bboxOf([...out.values(), ...gs.values()]), scale: s };
  }
  const known = new Set(campaigns.map((c) => c.id));
  const groupOf = new Map<string, string>();
  for (const g of groups) for (const c of g.campaign_ids) if (known.has(c) && !groupOf.has(c)) groupOf.set(c, g.id);
  for (const c of campaigns) if (c.group_id && !groupOf.has(c.id) && groups.some((g) => g.id === c.group_id)) groupOf.set(c.id, c.group_id);
  const radius = new Map(campaigns.map((c) => [c.id, campaignRadius(c.size)]));
  const inGroup = new Map<string, string[]>();
  for (const [c, g] of groupOf) inGroup.set(g, [...(inGroup.get(g) ?? []), c]);
  const local = new Map<string, { at: Map<string, Pt>; r: number }>();
  for (const [g, cids] of inGroup) {
    const p = spiralPack(cids.map((c) => ({ id: c, r: radius.get(c)! })), 1.6);
    local.set(g, { at: p.at, r: p.r + 2 });
  }
  const top = [
    ...[...local].map(([g, v]) => ({ id: `g:${g}`, r: v.r })),
    ...campaigns.filter((c) => !groupOf.has(c.id)).map((c) => ({ id: `c:${c.id}`, r: radius.get(c.id)! })),
  ];
  const packed = spiralPack(top, 2.5);
  const gDiscs = new Map<string, Disc>();
  for (const [key, p] of packed.at) {
    if (key.startsWith("g:")) {
      const g = key.slice(2);
      const l = local.get(g)!;
      gDiscs.set(g, { id: g, x: p.x, y: p.y, r: l.r });
      for (const [c, q] of l.at) out.set(c, { id: c, x: p.x + q.x, y: p.y + q.y, r: radius.get(c)! });
    } else {
      const c = key.slice(2);
      out.set(c, { id: c, x: p.x, y: p.y, r: radius.get(c)! });
    }
  }
  return { campaigns: out, groups: gDiscs, bbox: bboxOf([...out.values(), ...gDiscs.values()]), scale: null };
}

export interface Dated { id: string; observed_at?: string | null; registered_at?: string | null; x?: number | null; y?: number | null }

export function dateOf(n: Dated): string | null {
  return n.observed_at ?? n.registered_at ?? null;
}

/**
 * Incident positions inside a campaign disc: API positions when every node has one (fitted into the disc), else
 * growth rings: a sunflower ordered by date, so the oldest incident is the core and later ones grow outward.
 */
export function incidentPositions(nodes: readonly Dated[], disc: Disc, scale: number | null = null): Map<string, Pt> {
  const out = new Map<string, Pt>();
  if (nodes.length === 0) return out;
  const all = nodes.every((n) => typeof n.x === "number" && typeof n.y === "number");
  if (all && scale !== null) {
    // absolute API layout coordinates, same frame as the campaign discs
    for (const n of nodes) out.set(n.id, { x: n.x! * scale, y: n.y! * scale });
    return out;
  }
  if (nodes.length > 1 && all) {
    const cx = nodes.reduce((s, n) => s + n.x!, 0) / nodes.length;
    const cy = nodes.reduce((s, n) => s + n.y!, 0) / nodes.length;
    const far = Math.max(1e-9, ...nodes.map((n) => Math.hypot(n.x! - cx, n.y! - cy)));
    const k = (disc.r * 0.85) / far;
    for (const n of nodes) out.set(n.id, { x: disc.x + (n.x! - cx) * k, y: disc.y + (n.y! - cy) * k });
    return out;
  }
  const sorted = [...nodes].sort((a, b) => (dateOf(a) ?? "9999").localeCompare(dateOf(b) ?? "9999") || a.id.localeCompare(b.id));
  const f = sunflower(sorted.length);
  // fit the sunflower to the disc actually drawn (API radius may differ from the computed one)
  const k = f.r > 0 ? Math.min(1, disc.r / f.r) : 1;
  sorted.forEach((n, i) => out.set(n.id, { x: disc.x + f.pts[i]!.x * k, y: disc.y + f.pts[i]!.y * k }));
  return out;
}

/** Convex hull (Andrew's monotone chain), counter-clockwise, no repeated end point. */
export function convexHull(points: readonly Pt[]): Pt[] {
  const p = [...points].sort((a, b) => a.x - b.x || a.y - b.y);
  if (p.length <= 2) return p;
  const cross = (o: Pt, a: Pt, b: Pt) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
  const lower: Pt[] = [];
  for (const q of p) {
    while (lower.length >= 2 && cross(lower[lower.length - 2]!, lower[lower.length - 1]!, q) <= 0) lower.pop();
    lower.push(q);
  }
  const upper: Pt[] = [];
  for (let i = p.length - 1; i >= 0; i--) {
    const q = p[i]!;
    while (upper.length >= 2 && cross(upper[upper.length - 2]!, upper[upper.length - 1]!, q) <= 0) upper.pop();
    upper.push(q);
  }
  lower.pop();
  upper.pop();
  return [...lower, ...upper];
}

/**
 * Rank of every edge for far-zoom reduction: an edge's rank is its best position (0 = strongest) in the bit-sorted
 * edge lists of its two endpoints. Showing rank < k keeps each node's k strongest links, so no node loses its
 * strongest evidence while the hairball disappears.
 */
export function edgeRanks(edges: readonly Pick<GEdge, "id" | "source" | "target" | "bits">[]): Map<string, number> {
  const by = new Map<string, { id: string; bits: number }[]>();
  for (const e of edges) {
    for (const n of [e.source, e.target]) {
      const l = by.get(n);
      if (l) l.push(e);
      else by.set(n, [e]);
    }
  }
  const rank = new Map<string, number>();
  for (const list of by.values()) {
    list.sort((a, b) => b.bits - a.bits || a.id.localeCompare(b.id));
    list.forEach((e, i) => rank.set(e.id, Math.min(rank.get(e.id) ?? Infinity, i)));
  }
  return rank;
}

/** k strongest links per node by on-screen scale (px per incident spacing); Infinity = all. */
export function topKForZoom(unitPx: number): number {
  if (unitPx < 6) return 1;
  if (unitPx < 14) return 2;
  if (unitPx < 30) return 4;
  return Infinity;
}

export type LineStyle = "solid" | "dashed" | "dotted";
/** Edge style = evidence level: campaign (and analyst must-link) solid, kit / template dashed, shared asset dotted. */
export function levelStyle(l: Level): LineStyle {
  if (l === "campaign" || l === "review") return "solid";
  if (l === "kit") return "dashed";
  return "dotted";
}

/** Width in px from bits, log scale (100 b does not dwarf 10 b). Bits are evidence weights, not probabilities. */
export function bitsWidth(bits: number): number {
  return 0.8 + 1.9 * Math.log2(1 + Math.max(0, bits) / 8);
}

export interface Growth { dates: string[]; counts: number[]; undated: number }
/** Unique first-seen days of the campaigns (sorted) with how many campaigns start on each; undated counted apart. */
export function growthTimeline(campaigns: readonly Pick<OvCampaign, "first_seen" | "unassigned">[]): Growth {
  const m = new Map<string, number>();
  let undated = 0;
  for (const c of campaigns) {
    if (c.unassigned) continue;
    const d = c.first_seen?.slice(0, 10);
    if (d && /^\d{4}-\d{2}-\d{2}$/.test(d)) m.set(d, (m.get(d) ?? 0) + 1);
    else undated++;
  }
  const dates = [...m.keys()].sort();
  return { dates, counts: dates.map((d) => m.get(d)!), undated };
}

/** Nearest candidate in a screen direction (cone of +-60 degrees), for arrow-key navigation between campaigns. */
export function nearestInDirection(from: Pt, cands: readonly (Pt & { id: string })[], dir: "left" | "right" | "up" | "down"): string | null {
  const v = { left: { x: -1, y: 0 }, right: { x: 1, y: 0 }, up: { x: 0, y: -1 }, down: { x: 0, y: 1 } }[dir];
  let best: string | null = null;
  let bestScore = Infinity;
  for (const c of cands) {
    const dx = c.x - from.x;
    const dy = c.y - from.y;
    const d = Math.hypot(dx, dy);
    if (d < 1e-9) continue;
    const cos = (dx * v.x + dy * v.y) / d;
    if (cos < 0.5) continue;
    const score = d * (2 - cos);
    if (score < bestScore) {
      bestScore = score;
      best = c.id;
    }
  }
  return best;
}
