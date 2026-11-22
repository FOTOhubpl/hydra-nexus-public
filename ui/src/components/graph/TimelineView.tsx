/**
 * "Oś czasu": x = observation date (registration date when the observation date is missing), one swimlane per
 * campaign (largest multi-member campaigns; the rest share one lane), links drawn as arcs. Canvas 2D, so the
 * public corpus (6-7k dots, ~7k arcs) redraws in a frame. Incidents with no date are counted, never placed.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { Theme } from "../../lib/theme";
import type { Explore, GNode } from "./api";
import { edgeColor, NEUTRAL, shortTitle } from "./build";
import { hexA, nodeDate } from "./layout";
import type { ViewState } from "./SigmaView";

const LANE_CAP = 22;
const LEFT = 220;
const PAD = 28;

interface Props {
  data: Explore;
  colors: Record<string, string>;
  theme: Theme;
  view: ViewState;
  onHover?: (h: { kind: "node" | "edge"; id: string; x: number; y: number } | null) => void;
  onClickNode?: (id: string) => void;
}

const DAY = 86_400_000;

export function TimelineView({ data, colors, theme, view, onHover, onClickNode }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [hover, setHover] = useState<string | null>(null);

  useEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const model = useMemo(() => {
    const dated = data.nodes.filter((n) => nodeDate(n));
    const undated = data.nodes.length - dated.length;
    const camps = data.campaigns.filter((c) => c.size > 1).slice(0, LANE_CAP);
    const laneOf = new Map(camps.map((c, i) => [c.id, i]));
    const firstDate = new Map<string, string>();
    for (const n of dated) {
      const c = n.campaign_id ?? "";
      const d = nodeDate(n)!;
      if (!firstDate.has(c) || d < firstDate.get(c)!) firstDate.set(c, d);
    }
    camps.sort((a, b) => (firstDate.get(a.id) ?? "9").localeCompare(firstDate.get(b.id) ?? "9") || b.size - a.size);
    camps.forEach((c, i) => laneOf.set(c.id, i));
    const lanes = [...camps.map((c) => ({ id: c.id, label: shortTitle(c.label, c.id, 30), size: c.size })), { id: "__rest", label: "pozostałe kampanie", size: 0 }];
    const times = dated.map((n) => Date.parse(nodeDate(n)!));
    const t0 = times.length ? Math.min(...times) : 0;
    const t1 = times.length ? Math.max(...times) : 1;
    return { dated, undated, lanes, laneOf, t0, t1: t1 === t0 ? t0 + DAY : t1 };
  }, [data]);

  const pos = useMemo(() => {
    const { w, h } = size;
    const laneH = Math.max(18, (h - PAD - 90) / model.lanes.length);
    const sx = (t: number) => LEFT + ((t - model.t0) / (model.t1 - model.t0)) * (w - LEFT - PAD);
    const out = new Map<string, { x: number; y: number }>();
    const jitter = new Map<string, number>();
    for (const n of model.dated) {
      const lane = n.campaign_id !== null && model.laneOf.has(n.campaign_id) ? model.laneOf.get(n.campaign_id)! : model.lanes.length - 1;
      const x = sx(Date.parse(nodeDate(n)!));
      const k = `${lane}:${Math.round(x / 4)}`;
      const j = jitter.get(k) ?? 0;
      jitter.set(k, j + 1);
      const off = ((j % 5) - 2) * Math.min(4, laneH / 6);
      out.set(n.id, { x, y: PAD + 20 + lane * laneH + laneH / 2 + off });
    }
    return { out, laneH, sx };
  }, [size, model]);

  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = size.w * dpr;
    c.height = size.h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size.w, size.h);
    const css = getComputedStyle(c);
    const ink = css.getPropertyValue("--color-fg").trim() || "#0f1419";
    const muted = css.getPropertyValue("--color-subtle").trim() || "#5f6874";
    const line = css.getPropertyValue("--color-line").trim() || "#e3e6ea";
    ctx.font = '11px "Inter Variable", system-ui, sans-serif';
    // lanes
    model.lanes.forEach((l, i) => {
      const y = PAD + 20 + i * pos.laneH;
      ctx.fillStyle = line;
      ctx.fillRect(LEFT - 8, y + pos.laneH, size.w - LEFT - PAD + 16, 1);
      ctx.fillStyle = l.id === "__rest" ? muted : ink;
      const txt = l.id === "__rest" ? l.label : `${l.label} (${l.size})`;
      ctx.fillText(txt.length > 34 ? `${txt.slice(0, 33)}…` : txt, 12, y + pos.laneH / 2 + 4);
      if (l.id !== "__rest") {
        ctx.fillStyle = colors[l.id] ?? NEUTRAL[theme];
        ctx.fillRect(4, y + pos.laneH / 2 - 4, 4, 8);
      }
    });
    // date ticks (months)
    ctx.fillStyle = muted;
    const start = new Date(model.t0);
    const ticks: number[] = [];
    const spanDays = (model.t1 - model.t0) / DAY;
    const stepMonths = spanDays > 900 ? 6 : spanDays > 300 ? 2 : 1;
    for (let d = new Date(start.getFullYear(), start.getMonth(), 1); d.getTime() <= model.t1; d = new Date(d.getFullYear(), d.getMonth() + stepMonths, 1)) if (d.getTime() >= model.t0) ticks.push(d.getTime());
    if (ticks.length < 2) for (let i = 0; i <= 4; i++) ticks.push(model.t0 + ((model.t1 - model.t0) * i) / 4);
    for (const t of ticks) {
      const x = pos.sx(t);
      ctx.fillStyle = line;
      ctx.fillRect(x, PAD + 14, 1, size.h - 2 * PAD - 14);
      ctx.fillStyle = muted;
      ctx.fillText(new Date(t).toISOString().slice(0, spanDays < 62 ? 10 : 7), x + 3, PAD + 8);
    }
    const lit = hover ?? view.focus;
    // arcs
    for (const e of data.edges) {
      if (view.edgeHidden(e.id) || view.nodeHidden(e.source) || view.nodeHidden(e.target)) continue;
      const a = pos.out.get(e.source);
      const b = pos.out.get(e.target);
      if (!a || !b) continue;
      const onPath = view.pathEdges.has(e.id);
      const touching = lit !== null && (e.source === lit || e.target === lit);
      const faded = view.edgeFaded(e.id) || (lit !== null && !touching) || (view.pathEdges.size > 0 && !onPath);
      const col = edgeColor(e.family, theme);
      ctx.strokeStyle = onPath ? ink : hexA(col, faded ? 0.08 : 0.6);
      ctx.lineWidth = onPath ? 3 : Math.min(3, 0.6 + e.bits / 30);
      ctx.setLineDash(e.inferred ? [1.5, 3] : e.level === "campaign" || e.level === "review" ? [] : [6, 4]);
      const mx = (a.x + b.x) / 2;
      const lift = Math.min(160, Math.abs(b.x - a.x) * 0.35 + 12);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(mx, Math.min(a.y, b.y) - lift, b.x, b.y);
      ctx.stroke();
    }
    ctx.setLineDash([]);
    // dots
    for (const n of model.dated) {
      if (view.nodeHidden(n.id)) continue;
      const p = pos.out.get(n.id)!;
      const dim = (view.pathNodes.size > 0 && !view.pathNodes.has(n.id)) || (view.selected.size > 0 && !view.selected.has(n.id));
      ctx.fillStyle = hexA((n.campaign_id && colors[n.campaign_id]) || NEUTRAL[theme], dim ? 0.25 : 1);
      ctx.beginPath();
      ctx.arc(p.x, p.y, n.id === lit || view.pathNodes.has(n.id) ? 7 : 5, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = css.getPropertyValue("--color-panel").trim() || "#fff";
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  }, [size, pos, model, data.edges, colors, theme, view, hover]);

  const nearest = (x: number, y: number): GNode | null => {
    let best: GNode | null = null;
    let bd = 12 * 12;
    for (const n of model.dated) {
      if (view.nodeHidden(n.id)) continue;
      const p = pos.out.get(n.id)!;
      const d = (p.x - x) ** 2 + (p.y - y) ** 2;
      if (d < bd) {
        bd = d;
        best = n;
      }
    }
    return best;
  };

  return (
    <div ref={wrap} className="absolute inset-0">
      <canvas
        ref={canvas}
        className="absolute inset-0 h-full w-full"
        role="img"
        aria-label={`Oś czasu: ${model.dated.length} incydentów z datą w ${model.lanes.length - 1} pasach kampanii`}
        style={{ width: size.w, height: size.h }}
        onPointerMove={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const n = nearest(e.clientX - r.left, e.clientY - r.top);
          setHover(n?.id ?? null);
          onHover?.(n ? { kind: "node", id: n.id, x: e.clientX - r.left, y: e.clientY - r.top } : null);
        }}
        onPointerLeave={() => {
          setHover(null);
          onHover?.(null);
        }}
        onClick={(e) => {
          const r = e.currentTarget.getBoundingClientRect();
          const n = nearest(e.clientX - r.left, e.clientY - r.top);
          if (n) onClickNode?.(n.id);
        }}
      />
      {model.undated > 0 && (
        <div className="absolute right-4 bottom-3 rounded-md border border-line bg-panel px-2 py-1 text-xs text-muted">
          bez daty obserwacji ani rejestracji: <span className="font-mono text-fg">{model.undated}</span> incydentów (nie umieszczone na osi)
        </div>
      )}
    </div>
  );
}
