/** Mini-map of the whole constellation: campaign dots in their colour, the current viewport, click / drag to pan. */
import { useCallback, useEffect, useMemo, useRef } from "react";
import type { Theme } from "../../../lib/theme";
import type { ConstellationHandle } from "./ConstellationCanvas";
import type { Constellation } from "./geometry";

const W = 184;
const H = 124;

export function Minimap({ layout, colors, canvas, tick, theme }: { layout: Constellation; colors: Map<string, string>; canvas: React.RefObject<ConstellationHandle | null>; tick: number; theme: Theme }) {
  const base = useRef<HTMLCanvasElement>(null);
  const top = useRef<HTMLCanvasElement>(null);
  const fit = useMemo(() => {
    const { x: [x0, x1], y: [y0, y1] } = layout.bbox;
    const s = Math.min((W - 10) / Math.max(1e-6, x1 - x0), (H - 10) / Math.max(1e-6, y1 - y0));
    return { x0, y0, s, ox: (W - (x1 - x0) * s) / 2, oy: (H - (y1 - y0) * s) / 2 };
  }, [layout]);
  const tx = useCallback((x: number) => fit.ox + (x - fit.x0) * fit.s, [fit]);
  const ty = useCallback((y: number) => H - (fit.oy + (y - fit.y0) * fit.s), [fit]);

  useEffect(() => {
    const c = base.current;
    const ctx = c?.getContext("2d");
    if (!c || !ctx) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = W * dpr;
    c.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    for (const d of layout.campaigns.values()) {
      ctx.fillStyle = colors.get(d.id) ?? "#8a9099";
      ctx.beginPath();
      ctx.arc(tx(d.x), ty(d.y), Math.max(1, d.r * fit.s), 0, Math.PI * 2);
      ctx.fill();
    }
  }, [layout, colors, tx, ty, fit, theme]);

  useEffect(() => {
    const s = canvas.current?.sigma();
    const c = top.current;
    const ctx = c?.getContext("2d");
    if (!s || !c || !ctx) return;
    const dpr = window.devicePixelRatio || 1;
    c.width = W * dpr;
    c.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const { width, height } = s.getDimensions();
    const a = s.viewportToGraph({ x: 0, y: 0 });
    const b = s.viewportToGraph({ x: width, y: height });
    const x = Math.max(0, Math.min(tx(a.x), tx(b.x)));
    const y = Math.max(0, Math.min(ty(a.y), ty(b.y)));
    const w = Math.min(W, Math.max(tx(a.x), tx(b.x))) - x;
    const h = Math.min(H, Math.max(ty(a.y), ty(b.y))) - y;
    ctx.strokeStyle = theme === "dark" ? "#4c9ffe" : "#0b5cad";
    ctx.fillStyle = theme === "dark" ? "rgba(76,159,254,0.10)" : "rgba(11,92,173,0.08)";
    ctx.lineWidth = 1.5;
    ctx.fillRect(x, y, Math.max(4, w), Math.max(4, h));
    ctx.strokeRect(x, y, Math.max(4, w), Math.max(4, h));
  }, [tick, canvas, tx, ty, theme]);

  const pan = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const gx = fit.x0 + (e.clientX - r.left - fit.ox) / fit.s;
    const gy = fit.y0 + (H - (e.clientY - r.top) - fit.oy) / fit.s;
    canvas.current?.panToGraph({ x: gx, y: gy });
  };
  return (
    <div className="absolute right-3 bottom-3 z-10 hidden rounded-md border border-line bg-panel/95 p-1 shadow-sm sm:block">
      <div className="relative" style={{ width: W, height: H }}>
        <canvas ref={base} className="absolute inset-0 opacity-80" style={{ width: W, height: H }} aria-hidden="true" />
        <canvas
          ref={top}
          className="absolute inset-0 cursor-pointer"
          style={{ width: W, height: H }}
          role="img"
          aria-label="Minimapa konstelacji: kliknij lub przeciągnij, aby przesunąć widok"
          onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); pan(e); }}
          onPointerMove={(e) => { if (e.buttons) pan(e); }}
        />
      </div>
    </div>
  );
}
