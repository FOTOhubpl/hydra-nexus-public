/**
 * Deterministic hierarchical layout for the relationship graph: group -> campaign -> incident.
 * Incidents sit on a sunflower inside their campaign disc; campaign discs are packed along a monotone
 * spiral inside their group disc; group and ungrouped campaign discs are packed the same way at the top.
 * Pure, O(n) amortised with a spatial grid, so 6-7k incidents lay out in a few milliseconds.
 */
import type { GCampaign, GEdge, GGroup, GNode } from "./api";

export interface Disc { id: string; x: number; y: number; r: number }
export interface PackResult {
  pos: Map<string, { x: number; y: number }>;
  campaigns: Map<string, Disc>;
  groups: Map<string, Disc>;
}

const GOLDEN = Math.PI * (3 - Math.sqrt(5));
export const SPACING = 1;

/** Sunflower offsets for n points with unit spacing; radius of the enclosing disc. */
export function sunflower(n: number, s = SPACING): { pts: { x: number; y: number }[]; r: number } {
  const pts = [];
  for (let i = 0; i < n; i++) {
    const rr = n === 1 ? 0 : s * Math.sqrt(i + 0.5) * 0.9;
    pts.push({ x: rr * Math.cos(i * GOLDEN), y: rr * Math.sin(i * GOLDEN) });
  }
  return { pts, r: n <= 1 ? s * 0.8 : s * Math.sqrt(n) * 0.9 + s * 0.8 };
}

/**
 * Packs discs (largest first) without overlap along an Archimedean spiral whose parameter never goes back,
 * with a uniform grid for overlap tests. Returns centres around the origin and the enclosing radius.
 */
export function spiralPack(items: { id: string; r: number }[], gap = 0.6): { at: Map<string, { x: number; y: number }>; r: number } {
  const sorted = [...items].sort((a, b) => b.r - a.r || a.id.localeCompare(b.id));
  const at = new Map<string, { x: number; y: number }>();
  if (sorted.length === 0) return { at, r: 0 };
  const cell = Math.max(1, sorted[sorted.length - 1]!.r * 2 + gap);
  const grid = new Map<string, { x: number; y: number; r: number }[]>();
  const key = (gx: number, gy: number) => `${gx},${gy}`;
  const placed: { x: number; y: number; r: number }[] = [];
  // every disc is registered in all grid cells its box covers, so a query only scans the candidate's own cells
  const fits = (x: number, y: number, r: number) => {
    const ext = r + gap;
    for (let i = Math.floor((x - ext) / cell); i <= Math.floor((x + ext) / cell); i++) {
      for (let j = Math.floor((y - ext) / cell); j <= Math.floor((y + ext) / cell); j++) {
        for (const p of grid.get(key(i, j)) ?? []) {
          if (Math.hypot(p.x - x, p.y - y) < p.r + r + gap) return false;
        }
      }
    }
    return true;
  };
  const add = (id: string, x: number, y: number, r: number) => {
    const p = { x, y, r };
    placed.push(p);
    at.set(id, { x, y });
    for (let i = Math.floor((x - r) / cell); i <= Math.floor((x + r) / cell); i++) {
      for (let j = Math.floor((y - r) / cell); j <= Math.floor((y + r) / cell); j++) {
        const k = key(i, j);
        const list = grid.get(k);
        if (list) list.push(p);
        else grid.set(k, [p]);
      }
    }
  };
  add(sorted[0]!.id, 0, 0, sorted[0]!.r);
  // Spiral walked by radius and angle separately: each full turn moves outward by the current disc's diameter,
  // so small discs pack densely instead of following arms spaced for the largest one.
  let rr = sorted[0]!.r + gap;
  let theta = 0;
  for (let i = 1; i < sorted.length; i++) {
    const it = sorted[i]!;
    rr = Math.max(0, rr - it.r * 1.5); // step back so small discs fill gaps
    for (let guard = 0; guard < 400000; guard++) {
      const x = rr * Math.cos(theta);
      const y = rr * Math.sin(theta);
      if (fits(x, y, it.r)) {
        add(it.id, x, y, it.r);
        break;
      }
      const step = Math.max(0.15, it.r * 0.5);
      const dTheta = step / Math.max(rr, it.r);
      theta += dTheta;
      rr += ((2 * it.r + gap) * dTheta) / (2 * Math.PI);
    }
  }
  // recentre on the bounding box so the enclosing disc is tight
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const p of placed) {
    x0 = Math.min(x0, p.x - p.r); x1 = Math.max(x1, p.x + p.r); y0 = Math.min(y0, p.y - p.r); y1 = Math.max(y1, p.y + p.r);
  }
  const cx = (x0 + x1) / 2;
  const cy = (y0 + y1) / 2;
  let r = 0;
  for (const p of placed) r = Math.max(r, Math.hypot(p.x - cx, p.y - cy) + p.r);
  for (const [id, p] of at) at.set(id, { x: p.x - cx, y: p.y - cy });
  return { at, r };
}

function dateKey(n: GNode): string {
  return n.observed_at ?? n.registered_at ?? "9999";
}

export function packLayout(nodes: GNode[], campaigns: GCampaign[], groups: GGroup[]): PackResult {
  const byCampaign = new Map<string, GNode[]>();
  for (const n of nodes) {
    const c = n.campaign_id ?? `solo:${n.id}`;
    const list = byCampaign.get(c);
    if (list) list.push(n);
    else byCampaign.set(c, [n]);
  }
  const local = new Map<string, { pts: { id: string; x: number; y: number }[]; r: number }>();
  for (const [cid, list] of byCampaign) {
    list.sort((a, b) => dateKey(a).localeCompare(dateKey(b)) || a.id.localeCompare(b.id));
    const f = sunflower(list.length);
    local.set(cid, { pts: list.map((n, i) => ({ id: n.id, ...f.pts[i]! })), r: f.r });
  }
  const groupOf = new Map<string, string>();
  for (const g of groups) for (const c of g.campaign_ids ?? []) if (byCampaign.has(c)) groupOf.set(c, g.id);
  for (const c of campaigns) if (c.group_id && byCampaign.has(c.id) && !groupOf.has(c.id)) groupOf.set(c.id, c.group_id);
  // pack campaigns inside each group
  const inGroup = new Map<string, string[]>();
  for (const [c, g] of groupOf) inGroup.set(g, [...(inGroup.get(g) ?? []), c]);
  const groupLocal = new Map<string, { at: Map<string, { x: number; y: number }>; r: number }>();
  for (const [g, cids] of inGroup) {
    const packed = spiralPack(cids.map((c) => ({ id: c, r: local.get(c)!.r })), 1.2);
    groupLocal.set(g, { at: packed.at, r: packed.r + 1.5 });
  }
  // top level: groups + ungrouped campaigns
  const top = [
    ...[...groupLocal].map(([g, v]) => ({ id: `g:${g}`, r: v.r })),
    ...[...byCampaign.keys()].filter((c) => !groupOf.has(c)).map((c) => ({ id: `c:${c}`, r: local.get(c)!.r })),
  ];
  const packed = spiralPack(top, 2);
  const pos = new Map<string, { x: number; y: number }>();
  const cDiscs = new Map<string, Disc>();
  const gDiscs = new Map<string, Disc>();
  const placeCampaign = (cid: string, cx: number, cy: number) => {
    const l = local.get(cid)!;
    cDiscs.set(cid, { id: cid, x: cx, y: cy, r: l.r });
    for (const p of l.pts) pos.set(p.id, { x: cx + p.x, y: cy + p.y });
  };
  for (const [id, c] of packed.at) {
    if (id.startsWith("g:")) {
      const g = id.slice(2);
      const gl = groupLocal.get(g)!;
      gDiscs.set(g, { id: g, x: c.x, y: c.y, r: gl.r });
      for (const [cid, p] of gl.at) placeCampaign(cid, c.x + p.x, c.y + p.y);
    } else {
      placeCampaign(id.slice(2), c.x, c.y);
    }
  }
  return { pos, campaigns: cDiscs, groups: gDiscs };
}

/** Campaign-level aggregation: inter-campaign links built only from kit / group-level pairs. */
export interface CampaignLink { id: string; source: string; target: string; pairs: number; maxBits: number; family: string | null }
export function campaignLinks(nodes: GNode[], edges: GEdge[]): CampaignLink[] {
  const camp = new Map(nodes.map((n) => [n.id, n.campaign_id]));
  const agg = new Map<string, { s: string; t: string; pairs: number; maxBits: number; fam: Map<string, number> }>();
  for (const e of edges) {
    if (e.level !== "kit") continue;
    const a = camp.get(e.source);
    const b = camp.get(e.target);
    if (!a || !b || a === b) continue;
    const [s, t] = a < b ? [a, b] : [b, a];
    const k = `${s}|${t}`;
    const cur = agg.get(k) ?? { s, t, pairs: 0, maxBits: 0, fam: new Map() };
    cur.pairs += 1;
    cur.maxBits = Math.max(cur.maxBits, e.bits);
    if (e.family) cur.fam.set(e.family, (cur.fam.get(e.family) ?? 0) + e.bits);
    agg.set(k, cur);
  }
  return [...agg].map(([id, v]) => ({
    id, source: v.s, target: v.t, pairs: v.pairs, maxBits: v.maxBits,
    family: [...v.fam].sort((x, y) => y[1] - x[1])[0]?.[0] ?? null,
  }));
}

/** Edge line style: solid = campaign-level, dashed = kit/template (group-only), dotted = enrichment-inferred. */
export type EdgeStyle = "solid" | "dashed" | "dotted";
export function edgeStyle(e: Pick<GEdge, "level" | "inferred">): EdgeStyle {
  if (e.inferred) return "dotted";
  return e.level === "campaign" || e.level === "review" ? "solid" : "dashed";
}

/** Width in px from bits (log scale, so 100 b does not dwarf 10 b). */
export function edgeWidth(bits: number): number {
  return 1.2 + 2.4 * Math.log2(1 + Math.max(0, bits) / 8);
}

export function hexA(hex: string, alpha: number): string {
  if (!hex.startsWith("#")) return hex;
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h.slice(0, 6), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

/** Point in polygon (ray casting) for the lasso. */
export function inPolygon(x: number, y: number, poly: { x: number; y: number }[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const a = poly[i]!;
    const b = poly[j]!;
    if (a.y > y !== b.y > y && x < ((b.x - a.x) * (y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

/** Number of connected components (union-find) among `ids` over `edges`. */
export function components(ids: Iterable<string>, edges: { source: string; target: string }[]): number {
  const parent = new Map<string, string>();
  for (const id of ids) parent.set(id, id);
  const find = (x: string): string => {
    let r = x;
    while (parent.get(r) !== r) r = parent.get(r)!;
    let c = x;
    while (parent.get(c) !== r) {
      const n = parent.get(c)!;
      parent.set(c, r);
      c = n;
    }
    return r;
  };
  for (const e of edges) {
    if (!parent.has(e.source) || !parent.has(e.target)) continue;
    const a = find(e.source);
    const b = find(e.target);
    if (a !== b) parent.set(a, b);
  }
  let n = 0;
  for (const [k, v] of parent) if (k === v) n++;
  return n;
}

export function nodeDate(n: GNode): string | null {
  return n.observed_at ?? n.registered_at;
}
