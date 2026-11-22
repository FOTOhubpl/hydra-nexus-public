/**
 * Small SVG charts for the dashboard (dataviz rules: thin marks, 2 px surface gaps, recessive axes,
 * hover titles on every mark, legends for ≥ 2 series, text in ink tokens, colour by entity not rank).
 */
import { useMemo } from "react";
import { plNumber } from "../lib/evidence";

export function Sparkline({ values, width = 120, height = 28, label }: { values: number[]; width?: number; height?: number; label: string }) {
  if (values.length < 2) return null;
  const max = Math.max(1, ...values);
  const pts = values.map((v, i) => `${(i / (values.length - 1)) * (width - 4) + 2},${height - 2 - (v / max) * (height - 6)}`).join(" ");
  return (
    <svg width={width} height={height} role="img" aria-label={label}>
      <title>{label}</title>
      <polyline points={pts} fill="none" stroke="var(--color-accent)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

/** Greedy spiral circle packing (deterministic): biggest first, each circle at the first free spot on a spiral. */
export function packCircles(items: { id: string; r: number }[], gap = 4): { id: string; x: number; y: number; r: number }[] {
  const placed: { id: string; x: number; y: number; r: number }[] = [];
  for (const it of [...items].sort((a, b) => b.r - a.r || a.id.localeCompare(b.id))) {
    if (placed.length === 0) { placed.push({ ...it, x: 0, y: 0 }); continue; }
    for (let t = 0; t < 4000; t++) {
      const a = t * 0.35, rad = 2 * t * 0.35;
      const x = rad * Math.cos(a), y = rad * Math.sin(a);
      if (placed.every((p) => Math.hypot(p.x - x, p.y - y) >= p.r + it.r + gap)) { placed.push({ ...it, x, y }); break; }
    }
  }
  return placed;
}

export function PackedClusters({ clusters, colors, onPick, height = 260 }: {
  clusters: { id: string; label: string; size: number; group_id: string | null }[];
  colors: Record<string, string>;
  onPick: (id: string) => void;
  height?: number;
}) {
  const packed = useMemo(() => packCircles(clusters.map((c) => ({ id: c.id, r: 14 + 18 * Math.sqrt(c.size) }))), [clusters]);
  if (packed.length === 0) return null;
  const pad = 12;
  const minX = Math.min(...packed.map((p) => p.x - p.r)) - pad, maxX = Math.max(...packed.map((p) => p.x + p.r)) + pad;
  const minY = Math.min(...packed.map((p) => p.y - p.r)) - pad, maxY = Math.max(...packed.map((p) => p.y + p.r)) + pad;
  const byId = new Map(clusters.map((c) => [c.id, c]));
  const groups = new Map<string, typeof packed>();
  for (const p of packed) { const g = byId.get(p.id)?.group_id; if (g) groups.set(g, [...(groups.get(g) ?? []), p]); }
  return (
    <svg viewBox={`${minX} ${minY} ${maxX - minX} ${maxY - minY}`} width="100%" height={height} role="img" aria-label="Kampanie: wielkość koła = liczba incydentów">
      {[...groups.entries()].filter(([, ps]) => ps.length > 1).map(([g, ps]) => {
        const cx = ps.reduce((s, p) => s + p.x, 0) / ps.length, cy = ps.reduce((s, p) => s + p.y, 0) / ps.length;
        const r = Math.max(...ps.map((p) => Math.hypot(p.x - cx, p.y - cy) + p.r)) + 6;
        return <circle key={g} cx={cx} cy={cy} r={r} fill="none" stroke="var(--color-muted)" strokeDasharray="5 4" strokeWidth={1.2}><title>grupa {g}</title></circle>;
      })}
      {packed.map((p) => {
        const c = byId.get(p.id)!;
        return (
          <g key={p.id} className="cursor-pointer" onClick={() => onPick(p.id)}>
            <circle cx={p.x} cy={p.y} r={p.r} fill={colors[p.id] ?? "var(--color-unknown)"} stroke="var(--color-panel)" strokeWidth={2} />
            <text x={p.x} y={p.y + 4} textAnchor="middle" fontSize={12} className="pointer-events-none fill-white font-mono font-semibold">{c.size}</text>
            <title>{`${c.id} · ${c.label} · ${c.size} incydentów${c.group_id ? ` · grupa ${c.group_id}` : ""}`}</title>
          </g>
        );
      })}
    </svg>
  );
}

export interface StackSeries { key: string; label: string; color: string }

/** Horizontal stacked bars (one row per item), 2 px surface gaps between segments, rounded data end. */
export function StackedBars({ rows, series, onPick, unit }: {
  rows: { id: string; label: string; values: Record<string, number> }[];
  series: StackSeries[];
  onPick?: (id: string) => void;
  unit: string;
}) {
  const max = Math.max(1, ...rows.map((r) => series.reduce((s, x) => s + Math.max(0, r.values[x.key] ?? 0), 0)));
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-3 text-xs text-muted">{series.map((s) => <span key={s.key} className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rounded-sm" style={{ backgroundColor: s.color }} />{s.label}</span>)}<span className="text-subtle">jednostka: {unit}</span></div>
      {rows.map((r) => {
        const total = series.reduce((s, x) => s + Math.max(0, r.values[x.key] ?? 0), 0);
        return (
          <button key={r.id} type="button" onClick={() => onPick?.(r.id)} className="grid w-full grid-cols-[150px_1fr_56px] items-center gap-3 text-left text-xs hover:bg-hover">
            <span className="truncate font-mono text-fg">{r.label}</span>
            <span className="flex h-3.5 gap-[2px]" style={{ width: `${(100 * total) / max}%` }}>
              {series.map((s, i) => {
                const v = Math.max(0, r.values[s.key] ?? 0);
                if (!v) return null;
                return <span key={s.key} title={`${s.label}: ${plNumber(v, 1)} ${unit}`} className={i === series.length - 1 ? "rounded-r-[3px]" : ""} style={{ flexGrow: v, backgroundColor: s.color }} />;
              })}
            </span>
            <span className="text-right font-mono text-muted">{plNumber(total, 1)}</span>
          </button>
        );
      })}
    </div>
  );
}

/** Two-column Sankey: left = sources (stacked in the given order), right = targets; band width ∝ value. */
export function Sankey({ links, leftOrder, leftColor, leftLabel, rightLabel, unit, height = 280, onPickRight }: {
  links: { left: string; right: string; value: number }[];
  leftOrder: readonly string[];
  leftColor: (k: string) => string;
  leftLabel: (k: string) => string;
  rightLabel: (k: string) => string;
  unit: string;
  height?: number;
  onPickRight?: (k: string) => void;
}) {
  const W = 640, H = height, nodeW = 12, gap = 8, colL = 130, colR = W - 190;
  const lefts = leftOrder.filter((k) => links.some((l) => l.left === k));
  const rights = Array.from(new Set(links.map((l) => l.right))).sort((a, b) => links.filter((l) => l.right === b).reduce((s, l) => s + l.value, 0) - links.filter((l) => l.right === a).reduce((s, l) => s + l.value, 0));
  const total = links.reduce((s, l) => s + l.value, 0) || 1;
  const usable = (n: number) => H - 16 - gap * Math.max(0, n - 1);
  const place = (keys: string[], side: "left" | "right") => {
    let y = 8;
    const k2 = usable(keys.length) / total;
    return new Map(keys.map((k) => {
      const v = links.filter((l) => (side === "left" ? l.left : l.right) === k).reduce((s, l) => s + l.value, 0);
      const node = { y, h: Math.max(2, v * k2), v, used: 0 };
      y += node.h + gap;
      return [k, node] as const;
    }));
  };
  const L = place(lefts, "left"), R = place(rights, "right");
  const k = usable(Math.max(lefts.length, rights.length)) / total;
  const sorted = [...links].sort((a, b) => lefts.indexOf(a.left) - lefts.indexOf(b.left) || rights.indexOf(a.right) - rights.indexOf(b.right));
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="max-w-full" role="img" aria-label={`Przepływ: rodziny genów → kampanie (${unit})`}>
      {sorted.map((l, i) => {
        const a = L.get(l.left)!, b = R.get(l.right)!;
        const t = Math.max(1, l.value * k);
        const y0 = a.y + a.used + t / 2, y1 = b.y + b.used + t / 2;
        a.used += t; b.used += t;
        const x0 = colL + nodeW, x1 = colR;
        return (
          <path key={i} d={`M${x0},${y0} C${(x0 + x1) / 2},${y0} ${(x0 + x1) / 2},${y1} ${x1},${y1}`} fill="none" stroke={leftColor(l.left)} strokeOpacity={0.35} strokeWidth={t}>
            <title>{`${leftLabel(l.left)} → ${rightLabel(l.right)}: ${plNumber(l.value, 1)} ${unit}`}</title>
          </path>
        );
      })}
      {[...L.entries()].map(([key, n]) => (
        <g key={key}>
          <rect x={colL} y={n.y} width={nodeW} height={n.h} rx={2} fill={leftColor(key)}><title>{`${leftLabel(key)}: ${plNumber(n.v, 1)} ${unit}`}</title></rect>
          <text x={colL - 8} y={n.y + n.h / 2 + 4} textAnchor="end" fontSize={12} className="fill-fg">{leftLabel(key)}</text>
        </g>
      ))}
      {[...R.entries()].map(([key, n]) => (
        <g key={key} className={onPickRight ? "cursor-pointer" : undefined} onClick={() => onPickRight?.(key)}>
          <rect x={colR} y={n.y} width={nodeW} height={n.h} rx={2} fill="var(--color-muted)" />
          <text x={colR + nodeW + 8} y={n.y + n.h / 2 + 4} fontSize={12} className="fill-fg font-mono">{rightLabel(key)}</text>
          <title>{`${rightLabel(key)}: ${plNumber(n.v, 1)} ${unit}`}</title>
        </g>
      ))}
    </svg>
  );
}

/** Dot timeline by observed date; unknown dates are listed, never placed. */
export function Timeline({ items, colorOf, onPick, legend = [] }: {
  items: { id: string; observed_at: string | null; campaign_id: string | null; title: string | null }[];
  colorOf: (cid: string | null) => string;
  onPick: (id: string) => void;
  /** Campaigns to name in the legend (multi-member); everything else is "pojedyncze / bez kampanii". */
  legend?: { id: string; label: string }[];
}) {
  const dated = items.filter((i) => i.observed_at && !Number.isNaN(Date.parse(i.observed_at)));
  const undated = items.filter((i) => !dated.includes(i));
  if (dated.length === 0) return <div className="text-sm text-subtle">Brak dat w materiale.</div>;
  const ts = dated.map((i) => Date.parse(i.observed_at!));
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const W = 640, H = 120, x = (t: number) => 24 + ((t - t0) / Math.max(1, t1 - t0)) * (W - 48);
  const stack = new Map<number, number>();
  const fmt = (t: number) => new Date(t).toLocaleDateString("pl-PL", { day: "numeric", month: "short", year: "numeric" });
  return (
    <div>
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} className="max-w-full" role="img" aria-label="Oś czasu incydentów według daty obserwacji">
        <line x1={24} x2={W - 24} y1={H - 28} y2={H - 28} stroke="var(--color-line-strong)" />
        <text x={24} y={H - 8} fontSize={11} className="fill-subtle">{fmt(t0)}</text>
        <text x={W - 24} y={H - 8} fontSize={11} textAnchor="end" className="fill-subtle">{fmt(t1)}</text>
        {dated.map((i) => {
          const t = Date.parse(i.observed_at!);
          const bucket = Math.round(x(t) / 14);
          const k = (stack.get(bucket) ?? 0) + 1;
          stack.set(bucket, k);
          return (
            <circle key={i.id} cx={x(t)} cy={H - 28 - 12 * k} r={5} fill={colorOf(i.campaign_id)} stroke="var(--color-panel)" strokeWidth={2} className="cursor-pointer" onClick={() => onPick(i.id)}>
              <title>{`${i.id} · ${fmt(t)}${i.campaign_id ? ` · ${i.campaign_id}` : ""} · ${i.title ?? ""}`}</title>
            </circle>
          );
        })}
      </svg>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
        {legend.map((c) => <span key={c.id} className="inline-flex items-center gap-1.5" title={c.label}><span className="inline-block size-2.5 rounded-full" style={{ backgroundColor: colorOf(c.id) }} /><span className="font-mono">{c.id}</span></span>)}
        <span className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rounded-full" style={{ backgroundColor: colorOf(null) }} />pojedyncze / bez kampanii</span>
      </div>
      {undated.length > 0 && <div className="text-xs text-subtle">data nieznana: {undated.map((i) => i.id).join(", ")}</div>}
    </div>
  );
}

/** Horizontal bars, 1–2 series (thin, rounded data end, 2 px gap), value labels in ink. */
export function BarList({ rows, series, max, format = (v: number) => plNumber(v, 3) }: {
  rows: { label: string; values: (number | null)[] }[];
  series: { label: string; color: string }[];
  max?: number;
  format?: (v: number) => string;
}) {
  const m = max ?? Math.max(1e-9, ...rows.flatMap((r) => r.values.map((v) => Math.abs(v ?? 0))));
  return (
    <div className="space-y-2">
      {series.length > 1 && <div className="flex gap-4 text-xs text-muted">{series.map((s) => <span key={s.label} className="inline-flex items-center gap-1.5"><span className="inline-block size-2.5 rounded-sm" style={{ backgroundColor: s.color }} />{s.label}</span>)}</div>}
      {rows.map((r) => (
        <div key={r.label} className="grid grid-cols-[170px_1fr] items-center gap-3 text-xs">
          <span className="truncate font-mono text-fg" title={r.label}>{r.label}</span>
          <div className="space-y-[2px]">
            {r.values.map((v, i) => (
              <div key={i} className="flex items-center gap-2" title={`${series[i]?.label ?? ""}: ${v === null ? "—" : format(v)}`}>
                <span className="h-2.5 rounded-r-[3px]" style={{ width: `${v === null ? 0 : Math.max(0.5, (100 * Math.abs(v)) / m)}%`, backgroundColor: series[i]?.color }} />
                <span className="font-mono text-muted">{v === null ? "—" : format(v)}</span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

/** Small line chart with its own x axis (for small multiples); marker at the chosen x. */
export function MiniLine({ points, chosen, title, yMax = 1 }: { points: { x: number; y: number }[]; chosen?: number | null; title: string; yMax?: number }) {
  const W = 260, H = 120, l = 34, r = 8, t = 8, b = 22;
  if (points.length < 2) return <div className="text-xs text-subtle">{title}: za mało punktów</div>;
  const xs = points.map((p) => p.x), x0 = Math.min(...xs), x1 = Math.max(...xs);
  const sx = (x: number) => l + ((x - x0) / Math.max(1e-9, x1 - x0)) * (W - l - r);
  const sy = (y: number) => t + (1 - y / yMax) * (H - t - b);
  const path = points.map((p, i) => `${i ? "L" : "M"}${sx(p.x)},${sy(p.y)}`).join("");
  return (
    <figure className="min-w-0">
      <figcaption className="mb-1 truncate font-mono text-xs text-fg">{title}</figcaption>
      <svg width={W} height={H} className="max-w-full" role="img" aria-label={`${title}: F1 kampanii względem progu`}>
        {[0, 0.5, 1].map((g) => <g key={g}><line x1={l} x2={W - r} y1={sy(g * yMax)} y2={sy(g * yMax)} stroke="var(--color-line)" /><text x={l - 6} y={sy(g * yMax) + 4} textAnchor="end" fontSize={10} className="fill-subtle">{plNumber(g * yMax, 1)}</text></g>)}
        <text x={l} y={H - 6} fontSize={10} className="fill-subtle">{plNumber(x0, 2)}</text>
        <text x={W - r} y={H - 6} fontSize={10} textAnchor="end" className="fill-subtle">{plNumber(x1, 2)}</text>
        {chosen !== null && chosen !== undefined && <line x1={sx(chosen)} x2={sx(chosen)} y1={t} y2={H - b} stroke="var(--color-muted)" strokeDasharray="3 3"><title>wybrany próg: {plNumber(chosen, 2)}</title></line>}
        <path d={path} fill="none" stroke="var(--color-accent)" strokeWidth={2} strokeLinejoin="round" />
        {points.map((p, i) => <circle key={i} cx={sx(p.x)} cy={sy(p.y)} r={4} fill="var(--color-accent)" stroke="var(--color-panel)" strokeWidth={2}><title>{`próg ${plNumber(p.x, 2)} → F1 ${plNumber(p.y, 3)}`}</title></circle>)}
      </svg>
    </figure>
  );
}
