/** Builds the graphology graphs drawn by SigmaView for each view mode. Pure apart from graphology. */
import Graph from "graphology";
import { familyColors } from "../../lib/evidence";
import { assignGroupColors, GROUP_PALETTE, GROUP_PALETTE_DARK } from "../../lib/graph";
import type { Theme } from "../../lib/theme";
import type { Explore, GEdge, Indicators } from "./api";
import { campaignLinks, edgeStyle, edgeWidth, hexA, sunflower, type PackResult } from "./layout";
import type { LabeledDisc } from "./SigmaView";

export const NEUTRAL = { light: "#8a9099", dark: "#6b7580" } as const;
export const MUTED_IND = { light: "#c3c8cf", dark: "#3a414a" } as const;

/** Same assignment as the dashboard (campaignColors): multi-member campaigns by id, 8 slots, then neutral. */
export function campaignPalette(data: Explore, theme: Theme): Record<string, string> {
  const multi = assignGroupColors(data.campaigns.filter((c) => c.size > 1).map((c) => c.id), {}, theme === "dark" ? GROUP_PALETTE_DARK : GROUP_PALETTE);
  return Object.fromEntries(data.campaigns.map((c) => [c.id, multi.get(c.id) ?? NEUTRAL[theme]]));
}

export function shortTitle(t: string | null | undefined, id: string, n = 34): string {
  const s = (t ?? "").replace(/\s+/g, " ").trim();
  if (!s) return id;
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

function styleAttrs(e: Pick<GEdge, "level" | "inferred">, color: string) {
  const st = edgeStyle(e);
  if (st === "solid") return { dashSize: 6, gapSize: 0, gapColor: color };
  if (st === "dashed") return { dashSize: 6, gapSize: 4, gapColor: "rgba(0,0,0,0)" };
  return { dashSize: 1.6, gapSize: 3, gapColor: "rgba(0,0,0,0)" };
}

export function edgeColor(family: string | null, theme: Theme): string {
  return familyColors(theme)[family ?? ""] ?? NEUTRAL[theme];
}

/** Bounding box of every packed disc (camera frame). */
export function packBBox(pack: PackResult): { x: [number, number]; y: [number, number] } | null {
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const d of [...pack.groups.values(), ...pack.campaigns.values()]) {
    x0 = Math.min(x0, d.x - d.r); x1 = Math.max(x1, d.x + d.r); y0 = Math.min(y0, d.y - d.r); y1 = Math.max(y1, d.y + d.r);
  }
  return Number.isFinite(x0) ? { x: [x0, x1], y: [y0, y1] } : null;
}

export function discsFor(pack: PackResult, data: Explore, colors: Record<string, string>): LabeledDisc[] {
  const camps = new Map(data.campaigns.map((c) => [c.id, c]));
  const groups = new Map(data.groups.map((g) => [g.id, g]));
  const out: LabeledDisc[] = [];
  for (const g of pack.groups.values()) out.push({ ...g, kind: "group", label: groups.get(g.id)?.label ?? g.id, color: "", size: groups.get(g.id)?.campaign_ids.length ?? 0 });
  for (const c of pack.campaigns.values()) {
    const cc = camps.get(c.id);
    if (!cc) continue;
    out.push({ ...c, kind: "campaign", label: shortTitle(cc.label, cc.id, 40), color: colors[c.id] ?? "#8a9099", size: cc.size });
  }
  return out;
}

export function buildIncidentGraph(data: Explore, pack: PackResult, theme: Theme, colors: Record<string, string>): Graph {
  const g = new Graph({ type: "undirected", multi: false });
  const n = data.nodes.length;
  const base = n > 2000 ? 2.2 : n > 300 ? 3.5 : 7;
  for (const node of data.nodes) {
    const p = pack.pos.get(node.id) ?? { x: 0, y: 0 };
    g.addNode(node.id, {
      x: p.x, y: p.y, size: base + Math.min(4, Math.sqrt(node.degree) * 0.8),
      color: (node.campaign_id && colors[node.campaign_id]) || NEUTRAL[theme],
      label: shortTitle(node.title, node.id), thumbnail: node.thumbnail, kind: "incident",
    });
  }
  for (const e of data.edges) {
    if (!g.hasNode(e.source) || !g.hasNode(e.target) || g.hasEdge(e.source, e.target)) continue;
    const solid = edgeColor(e.family, theme);
    const color = hexA(solid, e.level === "weak" ? 0.45 : 0.8);
    g.addEdgeWithKey(e.id, e.source, e.target, { size: edgeWidth(e.bits) * (n > 2000 ? 0.45 : n > 300 ? 0.7 : 1), color, solidColor: solid, ...styleAttrs(e, color) });
  }
  return g;
}

/** Campaign bubbles (node size in graph units = the packed disc radius) and kit-only inter-campaign links. */
export function buildCampaignGraph(data: Explore, pack: PackResult, theme: Theme, colors: Record<string, string>): Graph {
  const g = new Graph({ type: "undirected", multi: false });
  for (const c of data.campaigns) {
    const d = pack.campaigns.get(c.id);
    if (!d) continue;
    g.addNode(c.id, { x: d.x, y: d.y, size: d.r, color: hexA(colors[c.id] ?? NEUTRAL[theme], c.size > 1 ? 0.85 : 0.5), label: `${shortTitle(c.label, c.id, 30)} (${c.size})`, kind: "campaign" });
  }
  for (const l of campaignLinks(data.nodes, data.edges)) {
    if (!g.hasNode(l.source) || !g.hasNode(l.target)) continue;
    const solid = edgeColor(l.family, theme);
    const color = hexA(solid, 0.75);
    g.addEdgeWithKey(l.id, l.source, l.target, { size: Math.min(0.35, 0.08 + 0.04 * l.pairs), color, solidColor: solid, dashSize: 0.8, gapSize: 0.5, gapColor: "rgba(0,0,0,0)", pairs: l.pairs, maxBits: l.maxBits, family: l.family });
  }
  return g;
}

/** Bipartite incidents <-> indicators; an indicator sits at the centroid of its incidents. */
export function buildIndicatorGraph(data: Explore, ind: Indicators, pack: PackResult, theme: Theme, colors: Record<string, string>): Graph {
  const g = new Graph({ type: "undirected", multi: false });
  const fam = familyColors(theme);
  const used = new Set(ind.indicators.flatMap((i) => i.incidents));
  const nodes = new Map(data.nodes.map((n) => [n.id, n]));
  for (const id of used) {
    const node = nodes.get(id);
    const p = pack.pos.get(id) ?? { x: 0, y: 0 };
    g.addNode(id, { x: p.x, y: p.y, size: used.size > 1500 ? 2.5 : 5, color: (node?.campaign_id && colors[node.campaign_id]) || NEUTRAL[theme], label: shortTitle(node?.title, id), thumbnail: node?.thumbnail ?? null, kind: "incident" });
  }
  const atCentroid = new Map<string, number>();
  for (const it of ind.indicators) {
    const pts = it.incidents.filter((i) => g.hasNode(i));
    if (pts.length < 2) continue;
    let x = 0;
    let y = 0;
    for (const i of pts) {
      x += g.getNodeAttribute(i, "x");
      y += g.getNodeAttribute(i, "y");
    }
    x /= pts.length;
    y /= pts.length;
    const k = `${Math.round(x)},${Math.round(y)}`;
    const slot = atCentroid.get(k) ?? 0;
    atCentroid.set(k, slot + 1);
    const off = sunflower(slot + 1).pts[slot]!;
    const color = it.over_correlating ? MUTED_IND[theme] : fam[it.family] ?? NEUTRAL[theme];
    g.addNode(`ind:${it.id}`, { x: x + off.x * 0.8 + 0.4, y: y + off.y * 0.8 + 0.4, size: 3 + Math.min(7, Math.sqrt(it.df) * 1.4), color, label: `${it.kind_pl}: ${it.value.slice(0, 26)}`, kind: "indicator" });
    for (const i of pts) {
      const ec = hexA(it.over_correlating ? MUTED_IND[theme] : fam[it.family] ?? NEUTRAL[theme], it.over_correlating ? 0.6 : 0.7);
      g.addEdgeWithKey(`ind:${it.id}|${i}`, `ind:${it.id}`, i, { size: 1.4, color: ec, solidColor: ec, dashSize: it.over_correlating ? 2 : 6, gapSize: it.over_correlating ? 3 : 0, gapColor: it.over_correlating ? "rgba(0,0,0,0)" : ec });
    }
  }
  return g;
}
