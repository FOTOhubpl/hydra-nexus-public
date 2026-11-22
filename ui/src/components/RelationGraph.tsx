/**
 * Relationship graph (sigma.js 4, WebGL) with an overlay synced on every render:
 * campaign hulls, dashed group outlines and circular incident thumbnails (when zoomed in and the graph is small).
 * Edge width = bits, colour = dominant gene family, dashed = kit / group-only link, faded = below the threshold.
 * Hover lights the neighbourhood; clicks go to the caller (usually the inspector drawer).
 */
import { useEffect, useMemo, useRef, useState } from "react";
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import Sigma from "sigma";
import { DEFAULT_STYLES } from "sigma";
import { layerDashed } from "sigma/rendering";
import { artifactContentUrl } from "../api/client";
import { cx } from "../lib/cx";
import { familyColors, familyLabel } from "../lib/evidence";
import { useTheme } from "../lib/theme";
import { hasWebGL2 } from "./GraphCanvas";
import { EmptyState } from "./EmptyState";
import { convexHull } from "../lib/geometry";

export interface RGNode {
  id: string;
  label: string;
  group: string | null; // campaign id
  thumbnail?: string | null;
  kind?: "incident" | "indicator";
  family?: string | null; // indicator family
  title?: string | null;
}

export interface RGEdge {
  id: string;
  source: string;
  target: string;
  bits: number;
  family: string | null;
  dashed?: boolean;
  faded?: boolean;
  title?: string;
}

export interface RelationGraphProps {
  nodes: RGNode[];
  edges: RGEdge[];
  campaignColors: Record<string, string>;
  groups?: { id: string; campaign_ids: string[] }[];
  onNodeClick?: (id: string) => void;
  onEdgeClick?: (e: RGEdge) => void;
  focusId?: string | null;
  compact?: boolean;
  className?: string;
}

const NEUTRAL = { light: "#8a9099", dark: "#6b7580" };
const TRANSPARENT = "rgba(0,0,0,0)";

function hexA(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h.slice(0, 6), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

export { convexHull };

interface Overlay { pos: Record<string, { x: number; y: number; size: number }>; ratio: number; w: number; h: number }

export function RelationGraph({ nodes, edges, campaignColors, groups = [], onNodeClick, onEdgeClick, focusId, compact = false, className }: RelationGraphProps) {
  const theme = useTheme();
  const fam = familyColors(theme);
  const webgl = useMemo(hasWebGL2, []);
  const container = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const hovered = useRef<string | null>(null);
  const handlers = useRef({ onNodeClick, onEdgeClick });
  handlers.current = { onNodeClick, onEdgeClick };
  const [overlay, setOverlay] = useState<Overlay>({ pos: {}, ratio: 1, w: 0, h: 0 });
  const [query, setQuery] = useState("");

  const graph = useMemo(() => {
    const g = new Graph({ type: "undirected", multi: false });
    const groupIds = Array.from(new Set(nodes.map((n) => n.group ?? `solo:${n.id}`))).sort();
    const anchor = new Map(groupIds.map((gid, i) => {
      const a = (2 * Math.PI * i) / Math.max(1, groupIds.length);
      return [gid, { x: 100 * Math.cos(a), y: 100 * Math.sin(a) }] as const;
    }));
    const maxBits = Math.max(1, ...edges.map((e) => Math.abs(e.bits)));
    nodes.forEach((n, i) => {
      // live updates (SSE patches, re-import) can deliver the same node twice: keep the first, never throw
      if (g.hasNode(n.id)) return;
      const an = anchor.get(n.group ?? `solo:${n.id}`)!;
      const a = i * 2.399963;
      const isInd = n.kind === "indicator";
      g.addNode(n.id, {
        x: an.x + 12 * Math.cos(a), y: an.y + 12 * Math.sin(a),
        size: isInd ? 5 : compact ? 9 : 16,
        label: n.label,
        color: isInd ? (fam[n.family ?? ""] ?? NEUTRAL[theme]) : (n.group && campaignColors[n.group]) || NEUTRAL[theme],
        raw: n,
      });
    });
    for (const e of edges) {
      if (!g.hasNode(e.source) || !g.hasNode(e.target) || g.hasEdge(e.source, e.target) || g.hasEdge(e.id)) continue;
      const base = fam[e.family ?? ""] ?? NEUTRAL[theme];
      const color = e.faded ? hexA(base, theme === "dark" ? 0.25 : 0.2) : base;
      g.addEdgeWithKey(e.id, e.source, e.target, {
        size: 0.8 + 5 * (Math.max(0, e.bits) / maxBits),
        color, gapColor: e.dashed ? TRANSPARENT : color, dashed: Boolean(e.dashed), raw: e,
        weight: Math.max(0.1, e.bits / maxBits),
      });
    }
    if (g.order > 1) {
      forceAtlas2.assign(g, { iterations: Math.min(300, 40 + 4000 / Math.max(1, g.order)), settings: { ...forceAtlas2.inferSettings(g), gravity: 0.6, scalingRatio: 12, barnesHutOptimize: g.order > 300 }, getEdgeWeight: "weight" });
    }
    return g;
  }, [nodes, edges, campaignColors, theme, fam, compact]);

  useEffect(() => {
    if (!webgl || !container.current || graph.order === 0) return;
    const label = theme === "dark" ? "#e6e8eb" : "#0f1419";
    const options = {
      primitives: { edges: { variables: { gapColor: { type: "color", default: TRANSPARENT } }, layers: [layerDashed({ dashSize: { value: 6, mode: "pixels" }, gapSize: { value: 4, mode: "pixels" }, gapColor: { attribute: "gapColor", default: TRANSPARENT } })] } },
      styles: {
        nodes: { ...DEFAULT_STYLES.nodes, labelColor: label, labelFont: '"Inter Variable", system-ui, sans-serif', labelSize: 12, cursor: "pointer", backdropColor: theme === "dark" ? "#161a1f" : "#ffffff", labelVisibility: { whenState: "isHovered", then: "visible", else: "auto" } },
        edges: { ...DEFAULT_STYLES.edges, color: { attribute: "color", defaultValue: NEUTRAL[theme] }, gapColor: { attribute: "gapColor", defaultValue: TRANSPARENT }, cursor: "pointer" },
      },
      settings: { enableEdgeEvents: true, renderLabels: true, itemSizesReference: "screen", zoomToSizeRatioFunction: (r: number) => Math.sqrt(r), labelRenderedSizeThreshold: compact ? 100 : 6, stagePadding: compact ? 24 : 48, minCameraRatio: 0.05, maxCameraRatio: 10, edgePickingPadding: 4 },
      nodeReducer: (key: string) => {
        const h = hovered.current;
        if (!h) return {};
        if (key === h) return { highlighted: true, zIndex: 3, labelVisibility: "visible" };
        return graph.areNeighbors(key, h) ? { zIndex: 2, labelVisibility: "visible" } : { color: theme === "dark" ? "#242a31" : "#e3e6ea", labelVisibility: "hidden", zIndex: 0 };
      },
      edgeReducer: (key: string) => {
        const h = hovered.current;
        if (!h) return {};
        const [s, t] = graph.extremities(key);
        if (s === h || t === h) return { zIndex: 2 };
        const dim = theme === "dark" ? "#1d2228" : "#eef0f3";
        return { color: dim, gapColor: graph.getEdgeAttribute(key, "dashed") ? TRANSPARENT : dim, zIndex: 0 };
      },
    };
    const sigma = new Sigma(graph, container.current, options as never);
    sigmaRef.current = sigma;
    let raf = 0;
    const sync = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const pos: Overlay["pos"] = {};
        graph.forEachNode((n) => {
          const d = sigma.getNodeDisplayData(n);
          if (!d) return;
          const v = sigma.graphToViewport({ x: graph.getNodeAttribute(n, "x"), y: graph.getNodeAttribute(n, "y") });
          pos[n] = { x: v.x, y: v.y, size: d.size };
        });
        const el = container.current;
        setOverlay({ pos, ratio: sigma.getCamera().getState().ratio, w: el?.clientWidth ?? 0, h: el?.clientHeight ?? 0 });
      });
    };
    sigma.on("afterRender", sync);
    sigma.on("enterNode", ({ node }) => { hovered.current = node; sigma.refresh({ skipIndexation: true }); });
    sigma.on("leaveNode", () => { hovered.current = null; sigma.refresh({ skipIndexation: true }); });
    sigma.on("clickNode", ({ node }) => handlers.current.onNodeClick?.(node));
    sigma.on("clickEdge", ({ edge }) => handlers.current.onEdgeClick?.(graph.getEdgeAttribute(edge, "raw") as RGEdge));
    sync();
    return () => { cancelAnimationFrame(raf); sigma.kill(); sigmaRef.current = null; };
  }, [graph, webgl, theme, compact]);

  const focus = (id: string) => {
    const s = sigmaRef.current;
    const d = s?.getNodeDisplayData(id);
    if (!s || !d) return;
    void s.getCamera().animate({ x: d.x, y: d.y, ratio: 0.4 }, { duration: 400 });
    hovered.current = id;
    s.refresh({ skipIndexation: true });
  };
  useEffect(() => { if (focusId) focus(focusId); }, [focusId]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!webgl) return <EmptyState tone="error" title="WebGL2 niedostępny" description="Graf wymaga WebGL2 (akceleracja sprzętowa w przeglądarce)." className={className} />;
  if (nodes.length === 0) return <EmptyState title="Graf jest pusty" description="Przebieg nie zawiera incydentów w tym filtrze." className={className} />;

  // overlay geometry
  const byCampaign = new Map<string, { x: number; y: number }[]>();
  for (const n of nodes) {
    const p = overlay.pos[n.id];
    if (!p || !n.group || n.kind === "indicator") continue;
    byCampaign.set(n.group, [...(byCampaign.get(n.group) ?? []), p]);
  }
  const showThumbs = !compact ? nodes.length <= 150 && overlay.ratio <= 1.6 : nodes.length <= 40;
  const hullPath = (pts: { x: number; y: number }[]) => {
    const h = convexHull(pts);
    return h.length ? `M${h.map((q) => `${q.x},${q.y}`).join("L")}Z` : "";
  };
  const thumbR = compact ? 13 : 18;

  return (
    <div className={cx("relative h-full min-h-0 w-full overflow-hidden bg-panel", className)}>
      <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
        {groups.map((g) => {
          const pts = (g.campaign_ids ?? []).flatMap((c) => byCampaign.get(c) ?? []);
          if (pts.length < 2) return null;
          return <path key={g.id} d={hullPath(pts)} fill="var(--color-muted)" fillOpacity={0.08} stroke="var(--color-muted)" strokeWidth={compact ? 64 : 96} strokeOpacity={0.08} strokeLinejoin="round" />;
        })}
        {[...byCampaign.entries()].filter(([, pts]) => pts.length > 1).map(([cid, pts]) => (
          <path key={cid} d={hullPath(pts)} fill={campaignColors[cid] ?? NEUTRAL[theme]} fillOpacity={0.16} stroke={campaignColors[cid] ?? NEUTRAL[theme]} strokeOpacity={0.16} strokeWidth={compact ? 36 : 56} strokeLinejoin="round" />
        ))}
      </svg>
      <div ref={container} className="absolute inset-0" />
      {showThumbs && (
        <div className="pointer-events-none absolute inset-0" aria-hidden="true">
          {nodes.filter((n) => n.thumbnail && n.kind !== "indicator").map((n) => {
            const p = overlay.pos[n.id];
            if (!p) return null;
            const col = (n.group && campaignColors[n.group]) || NEUTRAL[theme];
            return (
              <img key={n.id} src={artifactContentUrl(n.thumbnail!)} alt="" loading="lazy" className="absolute rounded-full object-cover" style={{ left: p.x - thumbR, top: p.y - thumbR, width: 2 * thumbR, height: 2 * thumbR, boxShadow: `0 0 0 3px ${col}` }} />
            );
          })}
        </div>
      )}
      {!compact && (
        <>
          <form className="absolute top-3 left-3 flex gap-2" onSubmit={(e) => { e.preventDefault(); const q = query.trim().toLowerCase(); const hit = nodes.find((n) => n.id.toLowerCase().includes(q) || (n.title ?? "").toLowerCase().includes(q)); if (hit) focus(hit.id); }}>
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Znajdź węzeł (Enter)" aria-label="Znajdź węzeł" className="h-9 w-56 rounded-md border border-line bg-panel px-3 text-sm" />
          </form>
          <div className="absolute top-3 right-3 flex flex-col overflow-hidden rounded-md border border-line bg-panel">
            {[["+", 1 / 1.5], ["−", 1.5]].map(([l, f]) => <button key={String(l)} type="button" aria-label={l === "+" ? "Przybliż" : "Oddal"} className="size-9 text-muted hover:bg-hover" onClick={() => { const c = sigmaRef.current?.getCamera(); if (c) void c.animate({ ratio: c.getState().ratio * (f as number) }, { duration: 200 }); }}>{l}</button>)}
            <button type="button" aria-label="Dopasuj" className="size-9 border-t border-line text-muted hover:bg-hover" onClick={() => void sigmaRef.current?.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1, angle: 0 }, { duration: 300 })}>⤢</button>
          </div>
          <Minimap nodes={nodes} graph={graph} overlay={overlay} colors={campaignColors} onPick={focus} theme={theme} />
          <div className="absolute bottom-3 left-3 max-w-[380px] rounded-md border border-line bg-panel px-3 py-2 text-xs text-muted">
            <div className="flex flex-wrap gap-x-3 gap-y-1">
              {Object.keys(fam).map((f) => <span key={f} className="inline-flex items-center gap-1"><span className="inline-block h-1 w-4 rounded" style={{ backgroundColor: fam[f] }} />{f} {familyLabel(f)}</span>)}
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3">
              <span>szerokość = bity</span><span>przerywana = szablon / tylko grupa</span><span>blada = poniżej progu</span><span>barwne tło = kampania · szare tło = grupa kampanii</span>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Minimap({ nodes, graph, overlay, colors, onPick, theme }: { nodes: RGNode[]; graph: Graph; overlay: Overlay; colors: Record<string, string>; onPick: (id: string) => void; theme: "light" | "dark" }) {
  const W = 168, H = 112;
  const xs = nodes.map((n) => (graph.hasNode(n.id) ? graph.getNodeAttribute(n.id, "x") : 0));
  const ys = nodes.map((n) => (graph.hasNode(n.id) ? graph.getNodeAttribute(n.id, "y") : 0));
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const sx = (x: number) => 8 + ((x - x0) / Math.max(1e-6, x1 - x0)) * (W - 16);
  const sy = (y: number) => H - 8 - ((y - y0) / Math.max(1e-6, y1 - y0)) * (H - 16);
  const vis = Object.keys(overlay.pos).filter((k) => { const p = overlay.pos[k]!; return p.x >= 0 && p.y >= 0 && p.x <= overlay.w && p.y <= overlay.h; }).length;
  return (
    <div className="absolute right-3 bottom-3 rounded-md border border-line bg-panel p-1">
      <svg width={W} height={H} role="img" aria-label="Minimapa grafu">
        {nodes.map((n, i) => (
          <circle key={n.id} cx={sx(xs[i]!)} cy={sy(ys[i]!)} r={n.kind === "indicator" ? 1.5 : 3} fill={(n.group && colors[n.group]) || NEUTRAL[theme]} className="cursor-pointer" onClick={() => onPick(n.id)}>
            <title>{n.id}</title>
          </circle>
        ))}
      </svg>
      <div className="px-1 text-2xs text-subtle">w widoku {vis}/{nodes.length} węzłów</div>
    </div>
  );
}
