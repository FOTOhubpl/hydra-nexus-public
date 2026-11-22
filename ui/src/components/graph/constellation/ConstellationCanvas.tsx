/**
 * WebGL host of the constellation (sigma 4 + graphology) with two 2D canvases:
 * - underlay: group halos, campaign glows (one cached sprite per colour), soft hulls of expanded campaigns;
 * - overlay: the radial "why" ring of the focused campaign, loading shimmer rings, search pulses, hull labels.
 * The graph holds campaign nodes from the overview plus the incidents of the campaigns currently shown; incidents are
 * added / removed incrementally as the lazy loader delivers them. Far-zoom edge reduction (top-k per node), time
 * scrubbing, focus+context and selection all go through sigma reducers, so nothing is rebuilt while interacting.
 */
import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";
import Graph from "graphology";
import Sigma, { DEFAULT_STYLES } from "sigma";
import { layerDashed } from "sigma/rendering";
import { thumbUrl } from "../../../api/artifacts";
import type { CampaignDetail, GEdge, OvLink, Overview } from "../../../api/graph";
import { familyColors } from "../../../lib/evidence";
import type { Theme } from "../../../lib/theme";
import { hexA } from "../layout";
import { NEUTRAL } from "./colors";
import { bitsWidth, dateOf, edgeRanks, levelStyle, topKForZoom, type Constellation, type Disc, type Pt } from "./geometry";

export interface Shown { detail: CampaignDetail; pos: Map<string, Pt>; hull: Pt[] }
export type Hit = { kind: "campaign" | "incident" | "link" | "edge"; id: string; campaign: string | null; x: number; y: number };

export interface RingData { campaign: string; families: [string, number][]; genes: { label: string; bits: number; family: string | null }[] }

export interface CanvasView {
  hoverCampaign: string | null;
  focusCampaign: string | null;
  selected: { kind: "campaign" | "incident" | "edge"; id: string } | null;
  cursorDate: string | null;
  /** Evidence levels drawn (links and incident edges of other levels are hidden). */
  levels: ReadonlySet<string>;
  highlight: ReadonlySet<string>;
  key: string;
}

export interface CameraInfo { unitPx: number; ratio: number; width: number; height: number; visible: { id: string; x: number; y: number; rPx: number }[] }

export interface ConstellationHandle {
  zoomToDisc: (d: Disc, fill?: number) => void;
  focusPoint: (p: Pt, unitPx?: number) => void;
  fit: () => void;
  zoom: (f: number) => void;
  sigma: () => Sigma | null;
  toViewport: (p: Pt) => Pt | null;
  panToGraph: (p: Pt) => void;
}

interface Props {
  theme: Theme;
  overview: Overview;
  layout: Constellation;
  colors: Map<string, string>;
  shown: ReadonlyMap<string, Shown>;
  loading: ReadonlySet<string>;
  view: CanvasView;
  ring: RingData | null;
  reducedMotion: boolean;
  onHover?: (h: Hit | null) => void;
  onClick?: (h: Hit | null, mods: { shift: boolean; alt: boolean }) => void;
  onCamera?: (c: CameraInfo) => void;
  onFirstPaint?: () => void;
}

const TRANSPARENT = "rgba(0,0,0,0)";
const DIM = { light: { node: "#dde1e6", edge: "rgba(160,168,178,0.18)" }, dark: { node: "#232a32", edge: "rgba(80,90,100,0.25)" } };
const INCIDENT_SIZE = 0.28;

/** Dash pattern in screen pixels; the reducer converts it to graph units ("positions" sizing). */
function dashAttrs(style: "solid" | "dashed" | "dotted", color: string) {
  if (style === "solid") return { dashPx: 6, gapPx: 0, gapColor: color };
  if (style === "dashed") return { dashPx: 7, gapPx: 5, gapColor: TRANSPARENT };
  return { dashPx: 2, gapPx: 4, gapColor: TRANSPARENT };
}

function glowSprite(color: string, cache: Map<string, HTMLCanvasElement>): HTMLCanvasElement {
  const hit = cache.get(color);
  if (hit) return hit;
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const ctx = c.getContext("2d");
  if (ctx) {
    const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
    g.addColorStop(0, hexA(color, 0.55));
    g.addColorStop(0.35, hexA(color, 0.28));
    g.addColorStop(0.7, hexA(color, 0.08));
    g.addColorStop(1, hexA(color, 0));
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, 128, 128);
  }
  cache.set(color, c);
  return c;
}

/** Short ring label: the gene description up to its quoted value, plus the start of the value. */
export function ringLabel(text: string, max = 40): string {
  const t = text.replace(/\s+/g, " ").trim();
  if (t.length <= max) return t;
  const q = t.indexOf("„");
  if (q > 0) {
    const head = t.slice(0, q).replace(/[:\s]+$/, "");
    const val = t.slice(q + 1).replace(/[”"].*$/, "");
    return `${head.slice(0, Math.max(12, max - 14))} „${val.slice(0, 10)}…”`;
  }
  return `${t.slice(0, max - 1)}…`;
}

function fitCanvas(c: HTMLCanvasElement): CanvasRenderingContext2D | null {
  const ctx = c.getContext("2d");
  if (!ctx) return null;
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth;
  const h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) {
    c.width = Math.round(w * dpr);
    c.height = Math.round(h * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return ctx;
}

export { campaignColors } from "./colors";

export const ConstellationCanvas = forwardRef<ConstellationHandle, Props>(function ConstellationCanvas(props, ref) {
  const { theme, overview, layout, colors } = props;
  const container = useRef<HTMLDivElement>(null);
  const under = useRef<HTMLCanvasElement>(null);
  const over = useRef<HTMLCanvasElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph | null>(null);
  const live = useRef(props);
  live.current = props;
  const hovered = useRef<string | null>(null);
  const unitAtRefresh = useRef(1);
  const unitNow = useRef(1);
  const sprites = useRef(new Map<string, HTMLCanvasElement>());
  const shownIn = useRef(new Map<string, Shown>());
  const ranks = useRef(new Map<string, number>());
  const drawRef = useRef<() => void>(() => {});
  const painted = useRef(false);
  const images = useRef(new Map<string, HTMLImageElement>());

  // ------------------------------------------------------------------ build the base graph (campaigns + links)
  useEffect(() => {
    const el = container.current;
    if (!el) return;
    const g = new Graph({ type: "undirected", multi: false, allowSelfLoops: false });
    const firstSeen = new Map(overview.campaigns.map((c) => [c.id, c.first_seen?.slice(0, 10) ?? null]));
    for (const c of overview.campaigns) {
      const d = layout.campaigns.get(c.id);
      if (!d) continue;
      const color = colors.get(c.id) ?? NEUTRAL[theme];
      g.addNode(`c:${c.id}`, {
        x: d.x, y: d.y, size: d.r, color: hexA(color, c.size > 1 ? 0.9 : 0.6), baseColor: color, kind: "campaign", cid: c.id,
        label: c.unassigned ? `bez kampanii (${c.size})` : `${c.label.length > 34 ? `${c.label.slice(0, 33)}…` : c.label} (${c.size})`,
        date: firstSeen.get(c.id) ?? null, zIndex: 1,
      });
    }
    const fam = familyColors(theme);
    const linkRanks = edgeRanks(overview.links.map((l) => ({ id: `L:${l.id}`, source: l.source, target: l.target, bits: l.max_bits })));
    for (const l of overview.links as OvLink[]) {
      const s = `c:${l.source}`;
      const t = `c:${l.target}`;
      if (!g.hasNode(s) || !g.hasNode(t) || g.hasEdge(s, t)) continue;
      const solid = (l.family && fam[l.family]) || NEUTRAL[theme];
      const color = hexA(solid, 0.55);
      g.addEdgeWithKey(`L:${l.id}`, s, t, { kind: "link", level: l.level, px: Math.min(4, 0.6 + bitsWidth(l.max_bits) * 0.6 + Math.log2(1 + l.pairs) * 0.3), color, solidColor: solid, rank: linkRanks.get(`L:${l.id}`) ?? 0, ...dashAttrs(levelStyle(l.level), color) });
    }
    graphRef.current = g;
    shownIn.current = new Map();
    ranks.current = new Map();
    painted.current = false;

    const dim = DIM[theme];
    const t0 = performance.now();
    const sigma = new Sigma(g, el, {
      primitives: {
        edges: {
          variables: {
            gapColor: { type: "color", default: TRANSPARENT },
            dashSize: { type: "number", default: 6 },
            gapSize: { type: "number", default: 0 },
          },
          layers: [
            layerDashed({
              dashSize: { attribute: "dashSize", default: 6, mode: "pixels" },
              gapSize: { attribute: "gapSize", default: 0, mode: "pixels" },
              gapColor: { attribute: "gapColor", default: TRANSPARENT },
            }),
          ],
        },
      },
      styles: {
        nodes: {
          ...DEFAULT_STYLES.nodes,
          labelColor: theme === "dark" ? "#e6e8eb" : "#0f1419",
          labelFont: '"Inter Variable", system-ui, sans-serif',
          labelSize: 12,
          cursor: "pointer",
          backdropColor: theme === "dark" ? "rgba(17,20,24,0.85)" : "rgba(255,255,255,0.85)",
          labelVisibility: { whenState: "isHovered", then: "visible", else: "auto" },
        },
        edges: {
          ...DEFAULT_STYLES.edges,
          color: { attribute: "color", defaultValue: "#8a9099" },
          gapColor: { attribute: "gapColor", defaultValue: TRANSPARENT },
          dashSize: { attribute: "dashSize", defaultValue: 6 },
          gapSize: { attribute: "gapSize", defaultValue: 0 },
          cursor: "pointer",
        },
      },
      settings: {
        allowInvalidContainer: true,
        enableEdgeEvents: true,
        renderLabels: true,
        itemSizesReference: "positions",
        zoomToSizeRatioFunction: (r: number) => r,
        labelRenderedSizeThreshold: 10,
        labelDensity: 0.35,
        labelGridCellSize: 140,
        stagePadding: 30,
        minCameraRatio: 0.004,
        maxCameraRatio: 4,
        edgePickingPadding: 3,
        hideEdgesOnMove: false,
      },
      nodeReducer: (key: string, _d: unknown, a: Record<string, unknown>) => {
        const v = live.current.view;
        const cid = a.cid as string;
        const date = a.date as string | null;
        if (v.cursorDate && date && date > v.cursorDate) return { visibility: "hidden" as const };
        const focus = hovered.current ? (g.getNodeAttribute(hovered.current, "cid") as string) : v.hoverCampaign ?? v.focusCampaign;
        const isSel = v.selected && ((v.selected.kind === "campaign" && a.kind === "campaign" && v.selected.id === cid) || (v.selected.kind === "incident" && key === `i:${v.selected.id}`));
        const out: Record<string, unknown> = {};
        const expanded = a.kind === "campaign" && shownIn.current.has(cid);
        if (expanded) {
          // expanded: the hull underlay draws the campaign; a tiny transparent anchor keeps its links attached
          out.color = TRANSPARENT;
          out.size = 0.01;
          out.label = null;
          out.zIndex = 0;
        } else {
          // collapsed campaign = bright core; the glow sprite drawn underneath spans the whole disc
          if (a.kind === "campaign") out.size = Math.max(0.7, Number(a.size) * 0.5);
          if (v.cursorDate && !date) out.color = hexA(String(a.baseColor ?? a.color), 0.25);
        }
        if (focus && cid !== focus) {
          // context stays legible but recedes: a faint tint of its own colour instead of a grey disc
          if (!expanded) out.color = a.kind === "campaign" ? hexA(String(a.baseColor ?? a.color), theme === "dark" ? 0.16 : 0.2) : dim.node;
          out.labelVisibility = "hidden";
        }
        if (v.highlight.has(cid) && a.kind === "campaign") out.highlighted = true;
        if (isSel) {
          out.highlighted = true;
          out.zIndex = 5;
          out.labelVisibility = "visible";
        }
        return out;
      },
      edgeReducer: (key: string, _d: unknown, a: Record<string, unknown>) => {
        const v = live.current.view;
        const [s, t] = g.extremities(key);
        const sd = g.getNodeAttribute(s, "date") as string | null;
        const td = g.getNodeAttribute(t, "date") as string | null;
        if (v.cursorDate && ((sd && sd > v.cursorDate) || (td && td > v.cursorDate))) return { visibility: "hidden" as const };
        const unit = unitAtRefresh.current;
        if (!v.levels.has(String(a.level))) return { visibility: "hidden" as const };
        const k = topKForZoom(unit);
        const rank = Number(a.rank ?? 0);
        const sc = g.getNodeAttribute(s, "cid") as string;
        const tc = g.getNodeAttribute(t, "cid") as string;
        const focus = hovered.current ? (g.getNodeAttribute(hovered.current, "cid") as string) : v.hoverCampaign ?? v.focusCampaign;
        const selEdge = v.selected?.kind === "edge" && key === `E:${v.selected.id}`;
        const touchesSel = v.selected?.kind === "incident" && (s === `i:${v.selected.id}` || t === `i:${v.selected.id}`);
        const px = Number(a.px ?? 1);
        // sizes and dashes are in graph units ("positions"); convert the wanted pixels at the last refresh scale
        const u = Math.max(0.05, unit);
        const size = Math.max(0.02, (px * (selEdge || touchesSel ? 1.8 : 1)) / u);
        const dash = { dashSize: Number(a.dashPx ?? 6) / u, gapSize: Number(a.gapPx ?? 0) / u };
        if (!selEdge && !touchesSel && rank >= k && !(focus && (sc === focus || tc === focus))) return { visibility: "hidden" as const };
        if (a.kind === "edge" && !(shownIn.current.has(sc) && shownIn.current.has(tc))) return { visibility: "hidden" as const };
        const dashed = Number(a.gapPx) > 0;
        if (focus && sc !== focus && tc !== focus && !selEdge) return { color: dim.edge, gapColor: dashed ? TRANSPARENT : dim.edge, size, zIndex: 0, ...dash };
        if (selEdge || touchesSel) {
          const solid = String(a.solidColor ?? a.color);
          return { color: solid, gapColor: dashed ? TRANSPARENT : solid, size, zIndex: 6, ...dash };
        }
        return { size, ...dash };
      },
    } as never);
    sigma.setCustomBBox(layout.bbox);
    sigmaRef.current = sigma;
    try {
      performance.mark("hydra:graph:sigma-ready", { detail: { ms: Math.round(performance.now() - t0), nodes: g.order, edges: g.size } });
    } catch {
      /* Performance API unavailable */
    }
    if (import.meta.env.DEV || new URLSearchParams(window.location.search).has("perf")) {
      (window as unknown as Record<string, unknown>).__hydraConstellation = { sigma, graph: g };
    }

    const hit = (key: string, x: number, y: number): Hit => {
      if (key.startsWith("c:")) return { kind: "campaign", id: key.slice(2), campaign: key.slice(2), x, y };
      if (key.startsWith("i:")) return { kind: "incident", id: key.slice(2), campaign: (g.getNodeAttribute(key, "cid") as string) ?? null, x, y };
      if (key.startsWith("L:")) return { kind: "link", id: key.slice(2), campaign: null, x, y };
      return { kind: "edge", id: key.slice(2), campaign: (g.getNodeAttribute(g.source(key), "cid") as string) ?? null, x, y };
    };
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        drawRef.current();
      });
    };
    sigma.on("afterRender", () => {
      const o = sigma.graphToViewport({ x: 0, y: 0 });
      const u = sigma.graphToViewport({ x: 1, y: 0 });
      unitNow.current = Math.hypot(u.x - o.x, u.y - o.y) || 1;
      if (Math.abs(Math.log(unitNow.current / unitAtRefresh.current)) > Math.log(1.3)) {
        unitAtRefresh.current = unitNow.current;
        requestAnimationFrame(() => {
          if (sigmaRef.current === sigma) sigma.refresh({ skipIndexation: true });
        });
      }
      schedule();
      if (!painted.current) {
        painted.current = true;
        live.current.onFirstPaint?.();
      }
    });
    sigma.on("enterNode", ({ node, event }) => {
      hovered.current = node;
      sigma.refresh({ skipIndexation: true });
      live.current.onHover?.(hit(node, event.x, event.y));
    });
    sigma.on("leaveNode", () => {
      hovered.current = null;
      sigma.refresh({ skipIndexation: true });
      live.current.onHover?.(null);
    });
    sigma.on("enterEdge", ({ edge, event }) => live.current.onHover?.(hit(edge, event.x, event.y)));
    sigma.on("leaveEdge", () => live.current.onHover?.(null));
    sigma.on("clickNode", ({ node, event }) => {
      const o = event.original as MouseEvent;
      live.current.onClick?.(hit(node, event.x, event.y), { shift: o.shiftKey, alt: o.altKey });
    });
    sigma.on("clickEdge", ({ edge, event }) => live.current.onClick?.(hit(edge, event.x, event.y), { shift: false, alt: false }));
    sigma.on("clickStage", () => live.current.onClick?.(null, { shift: false, alt: false }));
    // initial scale before the first render
    unitAtRefresh.current = 1;
    return () => {
      cancelAnimationFrame(frame);
      sigma.kill();
      sigmaRef.current = null;
      graphRef.current = null;
      // forget what was drawn: the next graph starts without incidents
      shownIn.current = new Map();
    };
  }, [overview, layout, colors, theme]);

  // ------------------------------------------------------------------ sync incidents of shown campaigns
  useEffect(() => {
    const g = graphRef.current;
    const sigma = sigmaRef.current;
    if (!g || !sigma) return;
    const want = props.shown;
    const fam = familyColors(theme);
    let changed = false;
    for (const [cid, s] of shownIn.current) {
      if (want.get(cid) === s) continue;
      for (const n of s.detail.nodes) if (g.hasNode(`i:${n.id}`)) g.dropNode(`i:${n.id}`);
      shownIn.current.delete(cid);
      changed = true;
    }
    for (const [cid, s] of want) {
      if (shownIn.current.has(cid)) continue;
      const color = colors.get(cid) ?? NEUTRAL[theme];
      for (const n of s.detail.nodes) {
        const p = s.pos.get(n.id);
        if (!p || g.hasNode(`i:${n.id}`)) continue;
        const d = dateOf(n);
        g.addNode(`i:${n.id}`, { x: p.x, y: p.y, size: INCIDENT_SIZE * (1 + Math.min(0.6, Math.sqrt(n.degree) * 0.08)), color, baseColor: color, kind: "incident", cid, date: d ? d.slice(0, 10) : null, label: (n.title ?? n.id).slice(0, 40), zIndex: 3 });
      }
      shownIn.current.set(cid, s);
      changed = true;
    }
    if (changed) {
      // incident edges between shown campaigns (internal + external whose other side is shown)
      const all: GEdge[] = [];
      for (const s of shownIn.current.values()) all.push(...s.detail.edges, ...s.detail.external);
      const stale: string[] = [];
      g.forEachEdge((key, a) => {
        if (a.kind === "edge") stale.push(key);
      });
      for (const key of stale) g.dropEdge(key);
      const seen = new Set<string>();
      const usable = all.filter((e) => {
        if (seen.has(e.id)) return false;
        seen.add(e.id);
        return g.hasNode(`i:${e.source}`) && g.hasNode(`i:${e.target}`);
      });
      ranks.current = edgeRanks(usable);
      for (const e of usable) {
        const s = `i:${e.source}`;
        const t = `i:${e.target}`;
        if (g.hasEdge(s, t)) continue;
        const solid = (e.family && fam[e.family]) || NEUTRAL[theme];
        const color = hexA(solid, e.level === "weak" ? 0.4 : 0.75);
        g.addEdgeWithKey(`E:${e.id}`, s, t, { kind: "edge", level: e.level, px: bitsWidth(e.bits), color, solidColor: solid, rank: ranks.current.get(e.id) ?? 0, ...dashAttrs(levelStyle(e.level), color) });
      }
      sigma.refresh();
    }
  }, [props.shown, colors, theme]);

  useEffect(() => {
    sigmaRef.current?.refresh({ skipIndexation: true });
  }, [props.view.key]);

  // ------------------------------------------------------------------ 2D layers
  drawRef.current = () => {
    const sigma = sigmaRef.current;
    const uc = under.current;
    const oc = over.current;
    if (!sigma || !uc || !oc) return;
    const p = live.current;
    const u = fitCanvas(uc);
    const o = fitCanvas(oc);
    if (!u || !o) return;
    const w = uc.clientWidth;
    const h = uc.clientHeight;
    const unit = unitNow.current;
    const v = p.view;
    const focus = hovered.current && graphRef.current?.hasNode(hovered.current) ? (graphRef.current.getNodeAttribute(hovered.current, "cid") as string) : v.hoverCampaign ?? v.focusCampaign;
    const dark = theme === "dark";
    const cams = new Map(p.overview.campaigns.map((c) => [c.id, c]));
    const vis = (d: Disc, pad = 0) => {
      const c = sigma.graphToViewport(d);
      const r = d.r * unit + pad;
      return c.x + r < 0 || c.y + r < 0 || c.x - r > w || c.y - r > h ? null : { ...c, r };
    };
    const hiddenByTime = (cid: string) => {
      const d = cams.get(cid)?.first_seen?.slice(0, 10);
      return Boolean(v.cursorDate && d && d > v.cursorDate);
    };
    // group halos: soft hull around member campaigns
    for (const grp of p.overview.groups) {
      const gd = p.layout.groups.get(grp.id);
      if (!gd || !vis(gd)) continue;
      const pts = grp.campaign_ids.map((c) => p.layout.campaigns.get(c)).filter((d): d is Disc => Boolean(d) && !hiddenByTime(d!.id));
      if (pts.length < 2) continue;
      const centre = sigma.graphToViewport(gd);
      const ring = vis(gd)!;
      const grad = u.createRadialGradient(centre.x, centre.y, 0, centre.x, centre.y, ring.r);
      const base = dark ? "#8fa3b8" : "#5f6874";
      grad.addColorStop(0, hexA(base, focus && !grp.campaign_ids.includes(focus) ? 0.01 : dark ? 0.07 : 0.05));
      grad.addColorStop(1, hexA(base, 0));
      u.fillStyle = grad;
      u.beginPath();
      u.arc(centre.x, centre.y, ring.r, 0, Math.PI * 2);
      u.fill();
      // threads from the group centre to each member: the group is what links these clusters
      u.strokeStyle = hexA(base, dark ? 0.22 : 0.18);
      u.lineWidth = 1;
      u.setLineDash([3, 4]);
      for (const d of pts) {
        const c = sigma.graphToViewport(d);
        u.beginPath();
        u.moveTo(centre.x, centre.y);
        u.quadraticCurveTo((centre.x + c.x) / 2 + (c.y - centre.y) * 0.15, (centre.y + c.y) / 2 - (c.x - centre.x) * 0.15, c.x, c.y);
        u.stroke();
      }
      u.setLineDash([]);
      if (ring.r > 80) {
        u.font = '500 11px "Inter Variable", system-ui, sans-serif';
        u.fillStyle = dark ? "#a3acb6" : "#3f4752";
        u.textAlign = "center";
        u.fillText(`grupa: ${grp.label}`.slice(0, 48), centre.x, Math.max(12, centre.y - ring.r + 14));
      }
    }
    // campaign glows (sprite per colour) and expanded hulls
    for (const c of p.overview.campaigns) {
      const d = p.layout.campaigns.get(c.id);
      if (!d || hiddenByTime(c.id)) continue;
      const s = vis(d, 40);
      if (!s) continue;
      const color = p.colors.get(c.id) ?? NEUTRAL[theme];
      const dimmed = focus && focus !== c.id;
      const shown = shownIn.current.get(c.id);
      if (shown) {
        const pts = shown.hull.map((q) => sigma.graphToViewport(q));
        u.globalAlpha = dimmed ? 0.25 : 1;
        u.lineJoin = "round";
        u.beginPath();
        if (pts.length >= 3) {
          pts.forEach((q, i) => (i ? u.lineTo(q.x, q.y) : u.moveTo(q.x, q.y)));
          u.closePath();
        } else u.arc(s.x, s.y, Math.max(6, d.r * unit), 0, Math.PI * 2);
        const pad = Math.max(10, unit * 0.9);
        u.lineWidth = pad * 2;
        u.strokeStyle = hexA(color, dark ? 0.14 : 0.1);
        u.fillStyle = hexA(color, dark ? 0.12 : 0.08);
        u.fill();
        u.stroke();
        u.lineWidth = 1.25;
        u.strokeStyle = hexA(color, 0.5);
        u.stroke();
        u.globalAlpha = 1;
        continue;
      }
      const r = Math.max(5, d.r * unit * 2.1);
      u.globalAlpha = dimmed ? 0.12 : v.cursorDate && !c.first_seen ? 0.3 : c.size > 1 ? 1 : 0.55;
      u.drawImage(glowSprite(color, sprites.current), s.x - r, s.y - r, 2 * r, 2 * r);
      u.globalAlpha = 1;
    }
    // overlay: hull labels of expanded campaigns
    o.textAlign = "center";
    for (const [cid, sh] of shownIn.current) {
      const d = p.layout.campaigns.get(cid);
      const c = cams.get(cid);
      if (!d || !c) continue;
      const s = vis(d);
      if (!s) continue;
      let top = Infinity;
      for (const q of sh.hull) top = Math.min(top, sigma.graphToViewport(q).y);
      if (!Number.isFinite(top)) top = s.y - s.r;
      const text = `${c.unassigned ? "bez kampanii" : c.label.slice(0, 40)} · ${c.size}`;
      o.font = '600 12px "Inter Variable", system-ui, sans-serif';
      const tw = o.measureText(text).width;
      const y = Math.max(16, top - Math.max(14, unit * 1.1) - 6);
      o.fillStyle = dark ? "rgba(17,20,24,0.88)" : "rgba(255,255,255,0.9)";
      o.fillRect(s.x - tw / 2 - 6, y - 12, tw + 12, 18);
      o.strokeStyle = hexA(p.colors.get(cid) ?? NEUTRAL[theme], 0.8);
      o.lineWidth = 1;
      o.strokeRect(s.x - tw / 2 - 6, y - 12, tw + 12, 18);
      o.fillStyle = dark ? "#e6e8eb" : "#0f1419";
      o.fillText(text, s.x, y + 1);
    }
    // artefact thumbnails on incidents at near zoom (real images from the artefact store, lazily decoded)
    if (unit >= 36) {
      const rPx = Math.min(30, unit * INCIDENT_SIZE * 1.05);
      let drawn = 0;
      for (const [cid, sh] of shownIn.current) {
        if (focus && focus !== cid) continue;
        for (const n of sh.detail.nodes) {
          if (!n.thumbnail || drawn >= 150) continue;
          const q = sh.pos.get(n.id);
          if (!q) continue;
          const nd = n.observed_at ?? n.registered_at;
          if (v.cursorDate && nd && nd.slice(0, 10) > v.cursorDate) continue;
          const c = sigma.graphToViewport(q);
          if (c.x < -rPx || c.y < -rPx || c.x > w + rPx || c.y > h + rPx) continue;
          let img = images.current.get(n.thumbnail);
          if (!img) {
            img = new Image();
            img.decoding = "async";
            img.onload = () => drawRef.current();
            img.src = thumbUrl(n.thumbnail, 160);
            images.current.set(n.thumbnail, img);
          }
          if (!img.complete || img.naturalWidth === 0) continue;
          drawn++;
          o.save();
          o.beginPath();
          o.arc(c.x, c.y, rPx, 0, Math.PI * 2);
          o.closePath();
          o.clip();
          const k = Math.max((2 * rPx) / img.naturalWidth, (2 * rPx) / img.naturalHeight);
          o.drawImage(img, c.x - (img.naturalWidth * k) / 2, c.y - (img.naturalHeight * k) / 2, img.naturalWidth * k, img.naturalHeight * k);
          o.restore();
          const sel = v.selected?.kind === "incident" && v.selected.id === n.id;
          o.strokeStyle = sel ? (dark ? "#ffffff" : "#0f1419") : p.colors.get(cid) ?? NEUTRAL[theme];
          o.lineWidth = sel ? 3 : 2;
          o.beginPath();
          o.arc(c.x, c.y, rPx, 0, Math.PI * 2);
          o.stroke();
        }
      }
    }
    const now = performance.now();
    // loading shimmer: rotating dashed ring
    for (const cid of p.loading) {
      const d = p.layout.campaigns.get(cid);
      const s = d && vis(d, 20);
      if (!s) continue;
      o.save();
      o.strokeStyle = dark ? "rgba(76,159,254,0.85)" : "rgba(11,92,173,0.75)";
      o.lineWidth = 2;
      o.setLineDash([6, 8]);
      o.lineDashOffset = p.reducedMotion ? 0 : -now / 40;
      o.beginPath();
      o.arc(s.x, s.y, Math.max(8, s.r) + 6, 0, Math.PI * 2);
      o.stroke();
      o.restore();
    }
    // search pulses
    for (const cid of v.highlight) {
      const d = p.layout.campaigns.get(cid);
      const s = d && vis(d, 30);
      if (!s) continue;
      const k = p.reducedMotion ? 0.5 : (now % 1400) / 1400;
      o.strokeStyle = dark ? `rgba(250,178,25,${0.9 - 0.7 * k})` : `rgba(208,59,59,${0.9 - 0.7 * k})`;
      o.lineWidth = 2;
      o.beginPath();
      o.arc(s.x, s.y, Math.max(8, s.r) + 4 + 14 * k, 0, Math.PI * 2);
      o.stroke();
    }
    // radial "why" ring: family share of internal link bits + top shared genes
    const ring = p.ring;
    const rd = ring && p.layout.campaigns.get(ring.campaign);
    const rs = rd && vis(rd, 60);
    if (ring && rd && rs && !hiddenByTime(ring.campaign)) {
      const fam = familyColors(theme);
      const R = Math.max(22, rd.r * unit * (shownIn.current.has(ring.campaign) ? 1.15 : 1)) + 10;
      const total = ring.families.reduce((s2, [, b]) => s2 + b, 0);
      o.lineCap = "butt";
      if (total > 0) {
        let a0 = -Math.PI / 2;
        for (const [f, b] of ring.families) {
          const a1 = a0 + (b / total) * Math.PI * 2;
          // a gap of 0.02 rad each side; slivers narrower than the gaps are drawn as a thin tick (a reversed arc
          // would otherwise sweep the whole circle)
          const gap = Math.min(0.02, (a1 - a0) / 4);
          o.strokeStyle = fam[f] ?? NEUTRAL[theme];
          o.lineWidth = 7;
          o.beginPath();
          o.arc(rs.x, rs.y, R, a0 + gap, Math.max(a0 + gap + 0.004, a1 - gap));
          o.stroke();
          a0 = a1;
        }
      } else {
        o.strokeStyle = dark ? "rgba(163,172,182,0.6)" : "rgba(63,71,82,0.5)";
        o.lineWidth = 1.5;
        o.setLineDash([4, 4]);
        o.beginPath();
        o.arc(rs.x, rs.y, R, 0, Math.PI * 2);
        o.stroke();
        o.setLineDash([]);
      }
      // gene labels on spokes around the ring
      const genes = ring.genes.slice(0, 6);
      o.font = '500 11px "Inter Variable", system-ui, sans-serif';
      genes.forEach((gn, i) => {
        const ang = -Math.PI / 2 + ((i + 0.5) / Math.max(genes.length, 1)) * Math.PI * 2;
        const x1 = rs.x + Math.cos(ang) * (R + 6);
        const y1 = rs.y + Math.sin(ang) * (R + 6);
        const x2 = rs.x + Math.cos(ang) * (R + 26);
        const y2 = rs.y + Math.sin(ang) * (R + 26);
        o.strokeStyle = (gn.family && fam[gn.family]) || NEUTRAL[theme];
        o.lineWidth = 1.5;
        o.beginPath();
        o.moveTo(x1, y1);
        o.lineTo(x2, y2);
        o.stroke();
        const text = `${ringLabel(gn.label)}: ${gn.bits.toFixed(1).replace(".", ",")} b`;
        const tw = o.measureText(text).width;
        const right = Math.cos(ang) >= 0;
        const tx = Math.min(w - tw - 6, Math.max(6, right ? x2 + 4 : x2 - tw - 4));
        const ty = Math.min(h - 6, Math.max(14, y2 + 4));
        o.fillStyle = dark ? "rgba(17,20,24,0.9)" : "rgba(255,255,255,0.92)";
        o.fillRect(tx - 4, ty - 12, tw + 8, 17);
        o.fillStyle = dark ? "#e6e8eb" : "#0f1419";
        o.textAlign = "left";
        o.fillText(text, tx, ty);
      });
      o.textAlign = "center";
    }
    const animate = !p.reducedMotion && (p.loading.size > 0 || v.highlight.size > 0);
    if (animate) requestAnimationFrame(() => drawRef.current());
  };

  useEffect(() => {
    drawRef.current();
  }, [props.loading, props.ring, props.view.key, props.reducedMotion]);

  // camera reports (debounced to animation frames) for the lazy loader and the minimap
  useEffect(() => {
    const sigma = sigmaRef.current;
    if (!sigma) return;
    let raf = 0;
    const report = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const el = container.current;
        if (!el) return;
        const w = el.clientWidth;
        const h = el.clientHeight;
        const unit = unitNow.current;
        const visible: CameraInfo["visible"] = [];
        for (const d of live.current.layout.campaigns.values()) {
          const c = sigma.graphToViewport(d);
          const r = d.r * unit;
          if (c.x + r < 0 || c.y + r < 0 || c.x - r > w || c.y - r > h) continue;
          visible.push({ id: d.id, x: c.x, y: c.y, rPx: r });
        }
        live.current.onCamera?.({ unitPx: unit, ratio: sigma.getCamera().getState().ratio, width: w, height: h, visible });
      });
    };
    sigma.getCamera().on("updated", report);
    sigma.on("resize", report);
    report();
    return () => {
      cancelAnimationFrame(raf);
      sigma.getCamera().off("updated", report);
    };
  }, [overview, layout, colors, theme]);

  useImperativeHandle(ref, () => ({
    sigma: () => sigmaRef.current,
    toViewport: (p: Pt) => sigmaRef.current?.graphToViewport(p) ?? null,
    fit: () => void sigmaRef.current?.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1, angle: 0 }, { duration: live.current.reducedMotion ? 0 : 400 }),
    zoom: (f: number) => {
      const c = sigmaRef.current?.getCamera();
      if (c) void c.animate({ ratio: c.getState().ratio * f }, { duration: live.current.reducedMotion ? 0 : 200 });
    },
    panToGraph: (p: Pt) => {
      const s = sigmaRef.current;
      if (!s) return;
      const f = s.viewportToFramedGraph(s.graphToViewport(p));
      void s.getCamera().animate({ x: f.x, y: f.y }, { duration: live.current.reducedMotion ? 0 : 250 });
    },
    focusPoint: (p: Pt, unitPx = 40) => {
      const s = sigmaRef.current;
      if (!s) return;
      const cam = s.getCamera();
      const ratio = cam.getState().ratio * (unitNow.current / unitPx);
      const f = s.viewportToFramedGraph(s.graphToViewport(p));
      void cam.animate({ x: f.x, y: f.y, ratio: Math.max(0.004, Math.min(4, ratio)) }, { duration: live.current.reducedMotion ? 0 : 500 });
    },
    zoomToDisc: (d: Disc, fill = 0.55) => {
      const s = sigmaRef.current;
      const el = container.current;
      if (!s || !el) return;
      const cam = s.getCamera();
      const target = (fill * Math.min(el.clientWidth, el.clientHeight)) / Math.max(1e-6, 2 * d.r);
      const ratio = cam.getState().ratio * (unitNow.current / target);
      const f = s.viewportToFramedGraph(s.graphToViewport(d));
      void cam.animate({ x: f.x, y: f.y, ratio: Math.max(0.004, Math.min(4, ratio)) }, { duration: live.current.reducedMotion ? 0 : 500 });
    },
  }), []);

  return (
    <div className="absolute inset-0">
      <canvas ref={under} className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true" />
      <div ref={container} className="absolute inset-0" />
      <canvas ref={over} className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true" />
    </div>
  );
});
