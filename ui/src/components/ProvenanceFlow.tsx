/**
 * Evidence provenance: artefakt → ekstraktor → gen → rodzina → hipoteza, band width = bits of the stored reasons.
 * Clicking a gene node filters the list; each row jumps to the exact place in the artifact viewer.
 */
import { useMemo, useState } from "react";
import { Link } from "react-router";
import type { Provenance } from "../api/visual";
import { cx } from "../lib/cx";
import { familyColors, formatBits, HYPOTHESIS_LABEL, ROLE_LABEL } from "../lib/evidence";
import { openGene } from "../lib/geneExplain";
import { useTheme } from "../lib/theme";
import { FamChip, GeneValue } from "./evidence";

const COLS = ["art", "ext", "gene", "fam", "hyp"] as const;
const COL_LABEL = ["artefakt (rola)", "ekstraktor", "gen", "rodzina", "hipoteza"];

function label(id: string): string {
  const [p, ...rest] = id.split(":");
  const v = rest.join(":");
  if (p === "art") return ROLE_LABEL[v] ?? v;
  if (p === "hyp") return HYPOTHESIS_LABEL[v] ?? v;
  return v;
}

function jumpHref(it: Provenance["items"][number]): string | null {
  const p = (it.pointer_a ?? {}) as Record<string, unknown>;
  const art = (p.artifact_id as string | undefined) ?? it.artifact_id;
  if (!art) return null;
  const q = new URLSearchParams({ label: `${it.kind}: ${String(it.value ?? "").slice(0, 40)}` });
  if (Array.isArray(p.bbox)) q.set("bbox", (p.bbox as number[]).map((v) => Math.round(v)).join(","));
  if (p.har_entry !== null && p.har_entry !== undefined) q.set("har", String(p.har_entry));
  if (Array.isArray(p.char_range)) q.set("chars", (p.char_range as number[]).join("-"));
  if (p.dom_path) q.set("dom", String(p.dom_path));
  if (p.timecode_s !== null && p.timecode_s !== undefined) q.set("t", String(p.timecode_s));
  return `/item/${art}?${q.toString()}`;
}

export function ProvenanceFlow({ p }: { p: Provenance }) {
  const theme = useTheme();
  const fam = familyColors(theme);
  const [focus, setFocus] = useState<string | null>(null);
  const W = 1100, H = 360, nodeW = 10, pad = 6;
  const layout = useMemo(() => {
    const val = new Map<string, number>();
    for (const l of p.links) {
      val.set(l.source, Math.max(val.get(l.source) ?? 0, 0) + 0);
      val.set(l.target, 0);
    }
    const inSum = new Map<string, number>(), outSum = new Map<string, number>();
    for (const l of p.links) { outSum.set(l.source, (outSum.get(l.source) ?? 0) + l.bits); inSum.set(l.target, (inSum.get(l.target) ?? 0) + l.bits); }
    const nodes = Array.from(new Set(p.links.flatMap((l) => [l.source, l.target])));
    const value = (n: string) => Math.max(inSum.get(n) ?? 0, outSum.get(n) ?? 0);
    const total = Math.max(1e-6, ...COLS.map((c) => nodes.filter((n) => n.startsWith(`${c}:`)).reduce((s, n) => s + value(n), 0)));
    const pos = new Map<string, { x: number; y: number; h: number; col: number; inUsed: number; outUsed: number }>();
    COLS.forEach((c, ci) => {
      const col = nodes.filter((n) => n.startsWith(`${c}:`)).sort((a, b) => value(b) - value(a));
      const k = (H - pad * Math.max(0, col.length - 1) - 30) / total;
      let y = 22;
      for (const n of col) { const h = Math.max(3, value(n) * k); pos.set(n, { x: 70 + ci * ((W - 160) / (COLS.length - 1)), y, h, col: ci, inUsed: 0, outUsed: 0 }); y += h + pad; }
    });
    const scale = (H - 30) / total;
    return { pos, scale };
  }, [p.links]);
  const color = (n: string) => (n.startsWith("fam:") ? fam[n.slice(4)] ?? "var(--color-muted)" : "var(--color-muted)");
  const items = focus ? p.items.filter((i) => `gene:${i.kind}` === focus || `fam:${i.family}` === focus || `ext:${i.extractor}` === focus || `art:${i.role}` === focus) : p.items;
  const links = [...p.links].sort((a, b) => (layout.pos.get(a.source)?.y ?? 0) - (layout.pos.get(b.source)?.y ?? 0));
  return (
    <div className="space-y-3">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="Przepływ dowodu: artefakt, ekstraktor, gen, rodzina, hipoteza">
        {COLS.map((c, i) => <text key={c} x={70 + i * ((W - 160) / (COLS.length - 1))} y={12} fontSize={11} textAnchor={i === COLS.length - 1 ? "end" : "start"} dx={i === COLS.length - 1 ? nodeW : 0} className="fill-subtle">{COL_LABEL[i]}</text>)}
        {links.map((l, i) => {
          const s = layout.pos.get(l.source)!, t = layout.pos.get(l.target)!;
          const w = Math.max(1, l.bits * layout.scale);
          const y0 = s.y + s.outUsed + w / 2, y1 = t.y + t.inUsed + w / 2;
          s.outUsed += w; t.inUsed += w;
          const x0 = s.x + nodeW, x1 = t.x;
          const dim = focus && l.source !== focus && l.target !== focus;
          const famKey = l.target.startsWith("fam:") ? l.target : l.source.startsWith("fam:") ? l.source : null;
          return <path key={i} d={`M${x0},${y0} C${(x0 + x1) / 2},${y0} ${(x0 + x1) / 2},${y1} ${x1},${y1}`} fill="none" stroke={famKey ? color(famKey) : "var(--color-accent)"} strokeOpacity={dim ? 0.08 : 0.32} strokeWidth={w}><title>{`${label(l.source)} → ${label(l.target)}: ${formatBits(l.bits)}`}</title></path>;
        })}
        {[...layout.pos.entries()].map(([n, v]) => (
          <g key={n} className="cursor-pointer" onClick={() => setFocus(focus === n ? null : n)}>
            <rect x={v.x} y={v.y} width={nodeW} height={v.h} rx={2} fill={n.startsWith("fam:") ? color(n) : focus === n ? "var(--color-accent)" : "var(--color-fg)"} />
            <text x={v.col === COLS.length - 1 ? v.x - 5 : v.x + nodeW + 5} y={v.y + Math.min(v.h / 2 + 4, v.h + 10)} fontSize={11} textAnchor={v.col === COLS.length - 1 ? "end" : "start"} className="fill-fg">{label(n).slice(0, 26)}</text>
            <title>{label(n)}</title>
          </g>
        ))}
      </svg>
      <div className="flex items-center gap-2 text-xs text-muted">{focus ? <>filtr: <b>{label(focus)}</b> <button type="button" className="text-accent hover:underline" onClick={() => setFocus(null)}>wyczyść</button></> : "kliknij węzeł, aby zawęzić listę"}</div>
      <ul className="space-y-1 text-xs">
        {items.slice(0, 16).map((it, i) => {
          const href = jumpHref(it);
          return (
            <li key={i} className={cx("flex items-center gap-2 rounded-md px-1 py-0.5", "hover:bg-hover")}>
              <FamChip family={it.family} />
              <button type="button" onClick={() => it.value && openGene(it.kind, it.value)} className="w-32 shrink-0 truncate text-left text-muted hover:underline">{it.kind}</button>
              <span className="min-w-0 flex-1 truncate"><GeneValue value={it.value} max={50} /></span>
              <span className="shrink-0 font-mono text-subtle">{it.extractor}</span>
              <span className="w-16 shrink-0 text-right font-mono">{formatBits(it.bits)}</span>
              {href ? <Link to={href} className="shrink-0 text-accent hover:underline">pokaż w artefakcie</Link> : <span className="shrink-0 text-subtle">bez wskaźnika</span>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
