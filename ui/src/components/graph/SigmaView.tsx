/**
 * Sigma 4 (WebGL) host for the graph explorer. Filters, hover, selection and the evidence path go through
 * node/edge reducers (no graph rebuild), so the 6-7k node public corpus stays interactive.
 * Overlays synced on every render: campaign / group discs with labels (far zoom), circular thumbnails with short
 * titles (near zoom), flowing particles along the evidence path, the lasso polygon.
 */
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import type Graph from "graphology";
import Sigma, { DEFAULT_STYLES } from "sigma";
import { layerDashed } from "sigma/rendering";
import { artifactContentUrl } from "../../api/client";
import type { Theme } from "../../lib/theme";
import { hexA, inPolygon, type Disc } from "./layout";

export interface ViewState {
  nodeHidden: (id: string) => boolean;
  edgeHidden: (id: string) => boolean;
  edgeFaded: (id: string) => boolean;
  selected: ReadonlySet<string>;
  pathNodes: ReadonlySet<string>;
  pathEdges: ReadonlySet<string>;
  focus: string | null;
  /** Selected edge (inspector): lit with its two incidents. */
  focusEdge?: string | null;
  key: string; // changes whenever any of the above changes
}

export interface LabeledDisc extends Disc { label: string; color: string; size: number; kind: "campaign" | "group" }

export interface SigmaViewHandle {
  focusNode: (id: string, ratio?: number) => void;
  fit: () => void;
  zoom: (factor: number) => void;
  sigma: () => Sigma | null;
}

export interface SigmaViewProps {
  graph: Graph;
  theme: Theme;
  view: ViewState;
  discs?: LabeledDisc[];
  /** Incident-like nodes may show thumbnails at near zoom. */
  thumbnails?: boolean;
  /** "positions": node sizes in layout units (campaign bubbles = packed disc radius). */
  sizes?: "screen" | "positions";
  /** Draw campaign discs (incident view); group rings are always drawn. */
  campaignDiscs?: boolean;
  /** Camera frame in layout units (so packed discs, not just node centres, fit the viewport). */
  bbox?: { x: [number, number]; y: [number, number] } | null;
  lasso?: boolean;
  onHover?: (h: { kind: "node" | "edge"; id: string; x: number; y: number } | null) => void;
  onClickNode?: (id: string, e: { shift: boolean; alt: boolean; meta: boolean }) => void;
  onClickEdge?: (id: string) => void;
  onClickStage?: () => void;
  onLasso?: (ids: string[]) => void;
  onCamera?: (s: { ratio: number; unitPx: number }) => void;
}

const TRANSPARENT = "rgba(0,0,0,0)";
const DIM = { light: { node: "#dfe3e8", edge: "#eceff2" }, dark: { node: "#262c34", edge: "#1a1f25" } };
const PATH = { light: "#0f1419", dark: "#ffffff" };

interface Overlay { discs: { d: LabeledDisc; x: number; y: number; r: number }[]; thumbs: { id: string; x: number; y: number; r: number; src: string; color: string; label: string }[]; unitPx: number }

export const SigmaView = forwardRef<SigmaViewHandle, SigmaViewProps>(function SigmaView(
  { graph, theme, view, discs = [], thumbnails = false, sizes = "screen", campaignDiscs = true, bbox = null, lasso = false, onHover, onClickNode, onClickEdge, onClickStage, onLasso, onCamera },
  ref,
) {
  const container = useRef<HTMLDivElement>(null);
  const particles = useRef<HTMLCanvasElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const viewRef = useRef(view);
  viewRef.current = view;
  const hovered = useRef<string | null>(null);
  const hoveredEdge = useRef<string | null>(null);
  const cb = useRef({ onHover, onClickNode, onClickEdge, onClickStage, onLasso, onCamera });
  cb.current = { onHover, onClickNode, onClickEdge, onClickStage, onLasso, onCamera };
  const bboxRef = useRef(bbox);
  bboxRef.current = bbox;
  const discsRef = useRef(discs);
  discsRef.current = discs;
  const [overlay, setOverlay] = useState<Overlay>({ discs: [], thumbs: [], unitPx: 0 });
  const [poly, setPolyState] = useState<{ x: number; y: number }[] | null>(null);
  const polyRef = useRef<{ x: number; y: number }[] | null>(null);
  const setPoly = (next: { x: number; y: number }[] | null | ((p: { x: number; y: number }[] | null) => { x: number; y: number }[] | null)) => {
    polyRef.current = typeof next === "function" ? next(polyRef.current) : next;
    setPolyState(polyRef.current);
  };

  useEffect(() => {
    if (!container.current) return;
    const dim = DIM[theme];
    const options = {
      allowInvalidContainer: true,
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
          backdropColor: theme === "dark" ? "#161a1f" : "#ffffff",
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
        enableEdgeEvents: true,
        renderLabels: true,
        itemSizesReference: sizes,
        zoomToSizeRatioFunction: (r: number) => Math.sqrt(r),
        labelRenderedSizeThreshold: sizes === "positions" ? 14 : 9,
        labelDensity: graph.order > 300 ? 0.12 : 0.6,
        labelGridCellSize: 180,
        stagePadding: 40,
        minCameraRatio: graph.order > 2000 ? 0.02 : 0.08,
        maxCameraRatio: 6,
        edgePickingPadding: 4,
        hideEdgesOnMove: graph.size > 20000,
      },
      nodeReducer: (key: string) => {
        const v = viewRef.current;
        if (v.nodeHidden(key)) return { visibility: "hidden" as const };
        if (v.pathNodes.size > 0) {
          if (v.pathNodes.has(key)) return { zIndex: 4, labelVisibility: "visible", highlighted: true };
          return { color: dim.node, labelVisibility: "hidden", zIndex: 0 };
        }
        if (v.focusEdge && graph.hasEdge(v.focusEdge) && !hovered.current) {
          const [fs, ft] = graph.extremities(v.focusEdge);
          return key === fs || key === ft ? { zIndex: 4, highlighted: true, labelVisibility: "visible" } : { color: dim.node, labelVisibility: "hidden", zIndex: 0 };
        }
        const h = hovered.current ?? v.focus;
        if (v.selected.size > 0 && v.selected.has(key)) return { zIndex: 3, highlighted: true, labelVisibility: h ? "hidden" : "auto" };
        if (h && graph.hasNode(h)) {
          if (key === h) return { highlighted: true, zIndex: 4, labelVisibility: "visible" };
          if (graph.areNeighbors(key, h)) return { zIndex: 3, labelVisibility: "visible" };
          return { color: dim.node, labelVisibility: "hidden", zIndex: 0 };
        }
        if (hoveredEdge.current && graph.hasEdge(hoveredEdge.current)) {
          const [s, t] = graph.extremities(hoveredEdge.current);
          if (key === s || key === t) return { zIndex: 3, labelVisibility: "visible" };
        }
        if (v.selected.size > 0) return { color: hexA(String(graph.getNodeAttribute(key, "color") ?? "#8a9099"), 0.35) };
        return {};
      },
      edgeReducer: (key: string) => {
        const v = viewRef.current;
        if (v.edgeHidden(key)) return { visibility: "hidden" as const };
        const [s, t] = graph.extremities(key);
        if (v.nodeHidden(s) || v.nodeHidden(t)) return { visibility: "hidden" as const };
        const attrs = graph.getEdgeAttributes(key);
        const solid = String(attrs.solidColor ?? attrs.color ?? "#8a9099");
        const dashed = Number(attrs.gapSize) > 0;
        const size = Number(attrs.size ?? 1);
        const dimmed = { color: dim.edge, gapColor: dashed ? TRANSPARENT : dim.edge, zIndex: 0 };
        if (v.pathEdges.size > 0) {
          if (v.pathEdges.has(key)) return { color: PATH[theme], gapColor: PATH[theme], gapSize: 0, size: Math.max(3, size + 1.5), zIndex: 5 };
          return dimmed;
        }
        if (v.focusEdge && graph.hasEdge(v.focusEdge) && !hovered.current) {
          return key === v.focusEdge ? { zIndex: 5, color: solid, gapColor: dashed ? TRANSPARENT : solid, size: size + 2 } : dimmed;
        }
        const h = hovered.current ?? v.focus;
        if (h && graph.hasNode(h)) return s === h || t === h ? { zIndex: 4, color: solid, gapColor: dashed ? TRANSPARENT : solid } : dimmed;
        if (hoveredEdge.current === key) return { zIndex: 5, color: solid, gapColor: dashed ? TRANSPARENT : solid, size: size + 1.5 };
        if (v.edgeFaded(key)) return dimmed;
        if (v.selected.size > 0 && !(v.selected.has(s) && v.selected.has(t))) return dimmed;
        return {};
      },
    };
    const sigma = new Sigma(graph, container.current, options as never);
    if (bboxRef.current) sigma.setCustomBBox(bboxRef.current);
    sigmaRef.current = sigma;
    if (import.meta.env.DEV) (window as unknown as Record<string, unknown>).__hydraGraph = { sigma, graph };
    let raf = 0;
    const sync = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const el = container.current;
        if (!el) return;
        const w = el.clientWidth;
        const h = el.clientHeight;
        const o = sigma.graphToViewport({ x: 0, y: 0 });
        const u = sigma.graphToViewport({ x: 1, y: 0 });
        const unitPx = Math.hypot(u.x - o.x, u.y - o.y);
        const ds: Overlay["discs"] = [];
        for (const d of discsRef.current) {
          const c = sigma.graphToViewport({ x: d.x, y: d.y });
          const r = d.r * unitPx;
          if (c.x + r < 0 || c.y + r < 0 || c.x - r > w || c.y - r > h) continue;
          ds.push({ d, x: c.x, y: c.y, r });
        }
        const th: Overlay["thumbs"] = [];
        if (thumbnails && unitPx >= 26) {
          const v = viewRef.current;
          graph.forEachNode((n, a) => {
            if (th.length > 160 || !a.thumbnail || v.nodeHidden(n)) return;
            const p = sigma.graphToViewport({ x: a.x as number, y: a.y as number });
            if (p.x < -30 || p.y < -30 || p.x > w + 30 || p.y > h + 30) return;
            th.push({ id: n, x: p.x, y: p.y, r: Math.min(24, unitPx * 0.42), src: artifactContentUrl(a.thumbnail as string), color: a.color as string, label: (a.label as string) ?? n });
          });
        }
        setOverlay({ discs: ds, thumbs: th, unitPx });
        cb.current.onCamera?.({ ratio: sigma.getCamera().getState().ratio, unitPx });
      });
    };
    sigma.on("afterRender", sync);
    sigma.on("enterNode", ({ node, event }) => {
      hovered.current = node;
      sigma.refresh({ skipIndexation: true });
      cb.current.onHover?.({ kind: "node", id: node, x: event.x, y: event.y });
    });
    sigma.on("leaveNode", () => {
      hovered.current = null;
      sigma.refresh({ skipIndexation: true });
      cb.current.onHover?.(null);
    });
    sigma.on("enterEdge", ({ edge, event }) => {
      hoveredEdge.current = edge;
      sigma.refresh({ skipIndexation: true });
      cb.current.onHover?.({ kind: "edge", id: edge, x: event.x, y: event.y });
    });
    sigma.on("leaveEdge", () => {
      hoveredEdge.current = null;
      sigma.refresh({ skipIndexation: true });
      cb.current.onHover?.(null);
    });
    sigma.on("clickNode", ({ node, event }) => {
      const o = event.original as MouseEvent;
      cb.current.onClickNode?.(node, { shift: o.shiftKey, alt: o.altKey, meta: o.metaKey || o.ctrlKey });
    });
    sigma.on("clickEdge", ({ edge }) => cb.current.onClickEdge?.(edge));
    sigma.on("clickStage", () => cb.current.onClickStage?.());
    sync();
    return () => {
      cancelAnimationFrame(raf);
      sigma.kill();
      sigmaRef.current = null;
    };
  }, [graph, theme, thumbnails, sizes]);

  useEffect(() => {
    sigmaRef.current?.refresh();
  }, [view.key]);

  // flowing particles along the evidence path
  useEffect(() => {
    const canvas = particles.current;
    const sigma = sigmaRef.current;
    if (!canvas || !sigma) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    const edges = [...view.pathEdges].filter((e) => graph.hasEdge(e));
    const order = [...view.pathNodes];
    let raf = 0;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
    const draw = (t: number) => {
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (canvas.width !== w * dpr) {
        canvas.width = w * dpr;
        canvas.height = h * dpr;
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = theme === "dark" ? "#fab219" : "#d03b3b";
      for (const e of edges) {
        const [s0, t0] = graph.extremities(e);
        // flow from the earlier path node to the later one
        const [s, tt] = order.indexOf(s0) <= order.indexOf(t0) ? [s0, t0] : [t0, s0];
        const a = sigma.graphToViewport({ x: graph.getNodeAttribute(s, "x"), y: graph.getNodeAttribute(s, "y") });
        const b = sigma.graphToViewport({ x: graph.getNodeAttribute(tt, "x"), y: graph.getNodeAttribute(tt, "y") });
        const len = Math.hypot(b.x - a.x, b.y - a.y);
        const n = Math.max(2, Math.min(14, Math.floor(len / 28)));
        for (let i = 0; i < n; i++) {
          const f = reduce ? i / n : (i / n + t / 1600) % 1;
          ctx.beginPath();
          ctx.arc(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f, 3, 0, Math.PI * 2);
          ctx.fill();
        }
      }
      if (!reduce && edges.length) raf = requestAnimationFrame(draw);
    };
    if (edges.length) raf = requestAnimationFrame(draw);
    else ctx.clearRect(0, 0, canvas.width, canvas.height);
    const onRender = () => {
      if (reduce && edges.length) draw(0);
    };
    sigma.on("afterRender", onRender);
    return () => {
      cancelAnimationFrame(raf);
      sigma.off("afterRender", onRender);
    };
  }, [view.pathEdges, view.pathNodes, graph, theme]);

  useImperativeHandle(ref, () => ({
    sigma: () => sigmaRef.current,
    fit: () => void sigmaRef.current?.getCamera().animate({ x: 0.5, y: 0.5, ratio: 1, angle: 0 }, { duration: 300 }),
    zoom: (f: number) => {
      const c = sigmaRef.current?.getCamera();
      if (c) void c.animate({ ratio: c.getState().ratio * f }, { duration: 200 });
    },
    focusNode: (id: string, ratio = 0.12) => {
      const s = sigmaRef.current;
      const d = s?.getNodeDisplayData(id);
      if (!s || !d) return;
      void s.getCamera().animate({ x: d.x, y: d.y, ratio }, { duration: 450 });
    },
  }), []);

  const lassoHandlers = useMemo(() => ({
    onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => {
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      const r = e.currentTarget.getBoundingClientRect();
      setPoly([{ x: e.clientX - r.left, y: e.clientY - r.top }]);
    },
    onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => {
      if (!e.buttons) return;
      const r = e.currentTarget.getBoundingClientRect();
      const p = { x: e.clientX - r.left, y: e.clientY - r.top };
      setPoly((prev) => (prev ? [...prev, p] : prev));
    },
    onPointerUp: () => {
      const prev = polyRef.current;
      setPoly(null);
      const s = sigmaRef.current;
      if (prev && prev.length > 2 && s) {
        const v = viewRef.current;
        const ids: string[] = [];
        graph.forEachNode((n, a) => {
          if (v.nodeHidden(n)) return;
          const p = s.graphToViewport({ x: a.x as number, y: a.y as number });
          if (inPolygon(p.x, p.y, prev)) ids.push(n);
        });
        cb.current.onLasso?.(ids);
      }
    },
  }), [graph]); // eslint-disable-line react-hooks/exhaustive-deps

  const near = overlay.unitPx >= 26;
  return (
    <div className="absolute inset-0">
      <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
        {overlay.discs.map(({ d, x, y, r }) =>
          d.kind === "campaign" && !campaignDiscs ? null : d.kind === "group" ? (
            <circle key={`g:${d.id}`} cx={x} cy={y} r={r} fill="none" stroke="var(--color-muted)" strokeOpacity={0.45} strokeWidth={1.25} strokeDasharray="5 4" />
          ) : (
            <circle key={`c:${d.id}`} cx={x} cy={y} r={r} fill={d.color} fillOpacity={near ? 0.05 : 0.1} stroke={d.color} strokeOpacity={0.55} strokeWidth={1} />
          ),
        )}
      </svg>
      <div ref={container} className="absolute inset-0" />
      <canvas ref={particles} className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true" />
      <div className="pointer-events-none absolute inset-0 overflow-hidden" aria-hidden="true">
        {overlay.discs
          .filter(({ d, r }) => (d.kind === "group" ? r > 60 : campaignDiscs && r > 26 && d.size > 1))
          .sort((a, b) => b.r - a.r)
          .slice(0, 40)
          .map(({ d, x, y, r }) => (
            <div
              key={`l:${d.kind}:${d.id}`}
              className="absolute max-w-[220px] -translate-x-1/2 truncate rounded-[4px] border border-line bg-panel/90 px-1.5 py-0.5 text-2xs text-fg"
              style={{ left: x, top: d.kind === "group" ? y - r - 10 : near ? y - r - 22 : y - 9, borderColor: d.kind === "campaign" ? d.color : undefined }}
            >
              {d.kind === "group" ? `grupa: ${d.label}` : `${d.label} (${d.size})`}
            </div>
          ))}
        {overlay.thumbs.map((t) => (
          <div key={t.id} className="absolute flex -translate-x-1/2 -translate-y-1/2 flex-col items-center" style={{ left: t.x, top: t.y }}>
            <img src={t.src} alt="" loading="lazy" className="rounded-full bg-panel object-cover" style={{ width: 2 * t.r, height: 2 * t.r, boxShadow: `0 0 0 2px var(--color-panel), 0 0 0 4px ${t.color}` }} />
          </div>
        ))}
      </div>
      {lasso && (
        <div className="absolute inset-0 z-10 cursor-crosshair" {...lassoHandlers} data-testid="lasso-layer">
          {poly && (
            <svg className="pointer-events-none absolute inset-0 h-full w-full" aria-hidden="true">
              <path d={`M${poly.map((p) => `${p.x},${p.y}`).join("L")}Z`} fill="var(--color-accent)" fillOpacity={0.08} stroke="var(--color-accent)" strokeWidth={1.5} strokeDasharray="4 3" />
            </svg>
          )}
        </div>
      )}
    </div>
  );
});
