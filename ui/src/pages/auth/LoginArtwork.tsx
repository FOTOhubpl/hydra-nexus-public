/**
 * Login visual: five flat, poster-like DNA scenes generated locally in SVG (no images, no network), crossfading every
 * 6 s, each mapped to a HYDRA concept: incident genome (helix, base pairs coloured by gene family, legend), rare gene
 * (one highlighted base pair, example readout in bits), same campaign (two aligned helices, shared genes linked), DNA
 * knot (strands bound into a campaign group), evidence sequence (family-coded blocks and the signature seal). The
 * monospace chips are illustrative readouts (the caption says "ilustracja"): no corpus data, no measured numbers. Rotation pauses on hover, on the pause button and
 * while the tab is hidden; under prefers-reduced-motion it does not start (first scene, static).
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { cx } from "../../lib/cx";
import { FAMILY_LABEL } from "../../lib/evidence";

/** mulberry32: tiny deterministic PRNG (every render draws the same scenes). */
export function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const W = 800;
const H = 900;
const CREAM = "oklch(0.96 0.02 85)";
const YELLOW = "oklch(0.88 0.17 95)";
const INK = "oklch(0.18 0.03 265)";
const MONO = "JetBrains Mono Variable, JetBrains Mono, ui-monospace, Menlo, monospace";

/** Gene families in the order of the product legend, with flat poster hues (always paired with the code). */
export const FAMILIES = ["VIS", "TXT", "INF", "ID", "NAR", "META"] as const;
const FAM_FILL = [
  "oklch(0.74 0.16 295)", // VIS violet
  "oklch(0.84 0.13 172)", // TXT mint
  "oklch(0.8 0.17 140)", // INF green
  "oklch(0.82 0.12 350)", // ID pink
  "oklch(0.75 0.17 45)", // NAR orange
  "oklch(0.88 0.16 92)", // META yellow
];

export interface Rung { x1: number; x2: number; y: number; fam: number; pair: number }
export interface Helix { a: string; b: string; rungs: Rung[] }

/** Flat double helix along y: two sine strands in antiphase and base-pair rungs coloured by gene family. */
export function buildHelix({ cx, y0, y1, amp, period, phase = 0, step = 26, seed = 1 }: {
  cx: number; y0: number; y1: number; amp: number; period: number; phase?: number; step?: number; seed?: number;
}): Helix {
  const rnd = mulberry32(seed);
  const t = (y: number) => ((y - y0) / period) * Math.PI * 2 + phase;
  const pa: string[] = [];
  const pb: string[] = [];
  for (let y = y0; y <= y1 + 0.1; y += 6) {
    const s = Math.sin(t(y));
    pa.push(`${(cx + amp * s).toFixed(1)} ${y.toFixed(1)}`);
    pb.push(`${(cx - amp * s).toFixed(1)} ${y.toFixed(1)}`);
  }
  const rungs: Rung[] = [];
  for (let y = y0 + step / 2; y < y1; y += step) {
    const s = Math.sin(t(y));
    if (Math.abs(s) < 0.3) continue;
    const fam = Math.floor(rnd() * FAMILIES.length);
    rungs.push({ x1: cx + amp * s, x2: cx - amp * s, y, fam, pair: (fam + 1 + Math.floor(rnd() * 4)) % FAMILIES.length });
  }
  return { a: `M${pa.join("L")}`, b: `M${pb.join("L")}`, rungs };
}

function Svg({ children, bg }: { children: ReactNode; bg: string }) {
  return (
    <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid slice" className="absolute inset-0 h-full w-full" aria-hidden="true" focusable="false">
      <rect width={W} height={H} fill={bg} />
      {children}
    </svg>
  );
}

/** Monospace "data" chip (illustrative readout); hidden on the small banner where it would be unreadable. */
function Chip({ x, y, text, dot, tone = "dark" }: { x: number; y: number; text: string; dot?: string; tone?: "dark" | "light" }) {
  const w = Math.round(text.length * 7.9 + (dot ? 34 : 22));
  return (
    <g className="login-art-chip" transform={`translate(${x} ${y})`}>
      <rect width={w} height={28} rx={14} fill={tone === "dark" ? INK : CREAM} />
      {dot && <circle cx={15} cy={14} r={5} fill={dot} />}
      <text x={dot ? 27 : 11} y={18.5} fontSize={13} fontFamily={MONO} fill={tone === "dark" ? CREAM : INK}>{text}</text>
    </g>
  );
}

function HelixShape({ h, back, front, rungWidth = 10, highlight }: { h: Helix; back: string; front: string; rungWidth?: number; highlight?: (r: Rung, i: number) => string | null }) {
  return (
    <g strokeLinecap="round" fill="none">
      <path d={h.b} stroke={back} strokeWidth={16} />
      {h.rungs.map((r, i) => {
        const mid = (r.x1 + r.x2) / 2;
        const hl = highlight?.(r, i) ?? null;
        return (
          <g key={i}>
            <line x1={r.x1} y1={r.y} x2={mid} y2={r.y} stroke={hl ?? FAM_FILL[r.fam]} strokeWidth={hl ? rungWidth + 6 : rungWidth} />
            <line x1={mid} y1={r.y} x2={r.x2} y2={r.y} stroke={hl ?? FAM_FILL[r.pair]} strokeWidth={hl ? rungWidth + 6 : rungWidth} />
          </g>
        );
      })}
      <path d={h.a} stroke={front} strokeWidth={16} />
    </g>
  );
}

/** Scene 1: incident genome, a large diagonal helix with base pairs coloured by gene family and a legend. */
function GenomeScene() {
  const h = useMemo(() => buildHelix({ cx: 500, y0: -60, y1: 980, amp: 118, period: 330, seed: 11, step: 24 }), []);
  return (
    <Svg bg="oklch(0.3 0.12 270)">
      <g transform="rotate(-16 500 450)">
        <HelixShape h={h} back="oklch(0.45 0.13 270)" front={CREAM} />
      </g>
      <g className="login-art-chip" transform="translate(56 110)">
        <rect width={214} height={6 * 30 + 52} rx={18} fill={INK} />
        <text x={18} y={30} fontSize={12} fontFamily={MONO} fill={CREAM} fillOpacity={0.7}>rodziny genów</text>
        {FAMILIES.map((f, i) => (
          <g key={f} transform={`translate(18 ${50 + i * 30})`}>
            <rect width={22} height={12} rx={6} fill={FAM_FILL[i]} />
            <text x={34} y={11} fontSize={13} fontFamily={MONO} fill={CREAM}>{f}</text>
            <text x={84} y={11} fontSize={13} fill={CREAM} fillOpacity={0.75}>{FAMILY_LABEL[f]}</text>
          </g>
        ))}
      </g>
      <Chip x={56} y={620} text="VIS·pHash 0x9f3a…" dot={FAM_FILL[0]} />
      <Chip x={56} y={660} text="INF·domena" dot={FAM_FILL[2]} />
      <Chip x={56} y={700} text="TXT·OCR „wypłata”" dot={FAM_FILL[1]} />
    </Svg>
  );
}

/** Scene 2: rare gene, one highlighted base pair with an example readout in bits. */
function RareScene() {
  const h = useMemo(() => buildHelix({ cx: 290, y0: -30, y1: 930, amp: 105, period: 300, seed: 23, step: 26, phase: 0.6 }), []);
  const target = h.rungs.reduce((best, r, i) => (Math.abs(r.y - 450) < Math.abs(h.rungs[best]!.y - 450) ? i : best), 0);
  const r = h.rungs[target]!;
  const right = Math.max(r.x1, r.x2);
  return (
    <Svg bg="oklch(0.6 0.2 32)">
      <HelixShape h={h} back="oklch(0.48 0.17 30)" front={CREAM} highlight={(_, i) => (i === target ? YELLOW : "oklch(0.78 0.09 40)")} />
      <circle cx={(r.x1 + r.x2) / 2} cy={r.y} r={46} fill="none" stroke={YELLOW} strokeWidth={6} />
      <line x1={(r.x1 + r.x2) / 2 + 46} y1={r.y} x2={right + 110} y2={r.y - 70} stroke={INK} strokeWidth={4} strokeLinecap="round" />
      <g transform={`translate(${right + 110} ${r.y - 118})`}>
        <rect width={232} height={96} rx={20} fill={INK} />
        <text x={20} y={34} fontSize={13} fontFamily={MONO} fill={YELLOW}>INF·tracker_id</text>
        <text x={20} y={66} fontSize={24} fontFamily={MONO} fill={CREAM}>b = 10,5 bitu</text>
        <text x={20} y={86} fontSize={11} fill={CREAM} fillOpacity={0.7}>przykład, to nie prawdopodobieństwo</text>
      </g>
      <Chip x={right + 110} y={r.y + 20} text="rzadki w korpusie" tone="light" />
    </Svg>
  );
}

/** Scene 3: same campaign, two helices side by side with matching base pairs connected. */
function MatchScene() {
  const left = useMemo(() => buildHelix({ cx: 235, y0: 60, y1: 840, amp: 80, period: 260, seed: 41, step: 28 }), []);
  const right = useMemo(() => buildHelix({ cx: 565, y0: 60, y1: 840, amp: 80, period: 260, seed: 42, step: 28, phase: Math.PI }), []);
  // pairs of rungs at the same height in both helices; three of them become shared genes
  const shared = useMemo(() => {
    const out: { l: Rung; r: Rung; fam: number }[] = [];
    const want = [0, 2, 4];
    left.rungs.forEach((l, i) => {
      const r = right.rungs.find((x) => Math.abs(x.y - l.y) < 1);
      if (r && i % 4 === 1 && out.length < want.length) out.push({ l, r, fam: want[out.length]! });
    });
    return out;
  }, [left, right]);
  const sharedY = new Set(shared.map((p) => p.l.y));
  const tone = (r: Rung) => (sharedY.has(r.y) ? FAM_FILL[shared.find((p) => p.l.y === r.y)!.fam]! : "oklch(0.72 0.09 255)");
  return (
    <Svg bg="oklch(0.46 0.2 262)">
      {shared.map((p, i) => (
        <line key={i} x1={Math.max(p.l.x1, p.l.x2)} y1={p.l.y} x2={Math.min(p.r.x1, p.r.x2)} y2={p.r.y} stroke={YELLOW} strokeWidth={6} strokeDasharray="2 12" strokeLinecap="round" />
      ))}
      <HelixShape h={left} back="oklch(0.36 0.15 262)" front={CREAM} highlight={tone} />
      <HelixShape h={right} back="oklch(0.36 0.15 262)" front={CREAM} highlight={tone} />
      {shared.map((p, i) => (
        <Chip key={i} x={400 - 44} y={p.l.y - 40} text={FAMILIES[p.fam]!} tone="light" />
      ))}
      <Chip x={330} y={500} text="VIS · INF · NAR" />
      <Chip x={353} y={540} text="bity 12,4" />
    </Svg>
  );
}

/** Scene 4: a DNA knot binding several strands, the campaign group. */
function KnotScene() {
  const knot = useMemo(() => {
    const pts: string[] = [];
    for (let k = 0; k <= 240; k++) {
      const t = (k / 240) * Math.PI * 2;
      const x = Math.sin(t) + 2 * Math.sin(2 * t);
      const y = Math.cos(t) - 2 * Math.cos(2 * t);
      pts.push(`${(400 + x * 70).toFixed(1)} ${(440 + y * 70).toFixed(1)}`);
    }
    return `M${pts.join("L")}Z`;
  }, []);
  const ends: [number, number, number][] = [[120, 150, 0], [690, 170, 1], [720, 690, 2], [90, 720, 4], [400, 860, 5]];
  return (
    <Svg bg="oklch(0.4 0.1 165)">
      {ends.map(([x, y, fam], i) => {
        const h = buildHelix({ cx: 0, y0: 0, y1: Math.hypot(x - 400, y - 440) - 175, amp: 22, period: 90, seed: 60 + i, step: 18 });
        const ang = (Math.atan2(y - 440, x - 400) * 180) / Math.PI - 90;
        return (
          <g key={i}>
            <g transform={`translate(400 440) rotate(${ang.toFixed(1)}) translate(0 140)`}>
              <HelixShape h={h} back="oklch(0.32 0.08 165)" front={CREAM} rungWidth={6} />
            </g>
            <circle cx={x} cy={y} r={30} fill={FAM_FILL[fam]} />
            <circle cx={x} cy={y} r={11} fill="oklch(0.4 0.1 165)" />
          </g>
        );
      })}
      <path d={knot} fill="oklch(0.4 0.1 165)" stroke="oklch(0.3 0.08 165)" strokeWidth={46} strokeLinejoin="round" />
      <path d={knot} fill="none" stroke={CREAM} strokeWidth={34} strokeLinejoin="round" />
      <path d={knot} fill="none" stroke={YELLOW} strokeWidth={14} strokeDasharray="10 14" strokeLinejoin="round" />
      <Chip x={335} y={56} text="grupa kampanii" tone="light" />
    </Svg>
  );
}

/** Scene 5: evidence sequence, sequencing-style blocks labelled with family codes, and the signature seal. */
function SequenceScene() {
  const rows = useMemo(() => {
    const rnd = mulberry32(77);
    return Array.from({ length: 5 }, () => Array.from({ length: 7 }, () => Math.floor(rnd() * FAMILIES.length)));
  }, []);
  const peaks = useMemo(() => {
    const rnd = mulberry32(78);
    return Array.from({ length: 14 }, (_, i) => ({ x: 100 + i * 44, h: 40 + rnd() * 90, fam: Math.floor(rnd() * FAMILIES.length) }));
  }, []);
  const ticks = Array.from({ length: 40 }, (_, i) => {
    const a = (i / 40) * Math.PI * 2;
    return { x1: 610 + Math.cos(a) * 92, y1: 752 + Math.sin(a) * 92, x2: 610 + Math.cos(a) * 106, y2: 752 + Math.sin(a) * 106 };
  });
  return (
    <Svg bg="oklch(0.36 0.16 292)">
      {peaks.map((p, i) => (
        <path key={i} d={`M${p.x - 34} 300Q${p.x} ${300 - p.h * 2} ${p.x + 34} 300Z`} fill={FAM_FILL[p.fam]} />
      ))}
      <rect x={80} y={300} width={640} height={6} rx={3} fill={CREAM} />
      {rows.map((row, r) => row.map((fam, c) => (
        <g key={`${r}-${c}`} transform={`translate(${80 + c * 92} ${336 + r * 60})`}>
          <rect width={82} height={46} rx={12} fill={FAM_FILL[fam]} />
          <text x={41} y={29} textAnchor="middle" fontSize={15} fontWeight={600} fontFamily={MONO} fill={INK}>{FAMILIES[fam]}</text>
        </g>
      )))}
      <circle cx={622} cy={764} r={84} fill="oklch(0.26 0.12 292)" />
      <circle cx={610} cy={752} r={84} fill={CREAM} />
      <g stroke={CREAM} strokeWidth={5} strokeLinecap="round">{ticks.map((t, i) => <line key={i} {...t} />)}</g>
      <path d="M572 754l26 26 50-56" fill="none" stroke="oklch(0.36 0.16 292)" strokeWidth={18} strokeLinecap="round" strokeLinejoin="round" />
      <Chip x={80} y={666} text="SHA-256 a41c…7e02" />
      <Chip x={80} y={706} text="Ed25519 · podpis" />
    </Svg>
  );
}

export const SCENES: { id: string; caption: string; render: () => ReactNode }[] = [
  { id: "genome", caption: "Genom incydentu", render: () => <GenomeScene /> },
  { id: "rare", caption: "Rzadki gen", render: () => <RareScene /> },
  { id: "match", caption: "Ta sama kampania", render: () => <MatchScene /> },
  { id: "knot", caption: "Węzeł DNA", render: () => <KnotScene /> },
  { id: "sequence", caption: "Sekwencja dowodów", render: () => <SequenceScene /> },
];

export const SCENE_MS = 6000;

function useReducedMotion(): boolean {
  const query = "(prefers-reduced-motion: reduce)";
  const [reduced, setReduced] = useState(() => typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(query).matches);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const on = () => setReduced(mq.matches);
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, []);
  return reduced;
}

/** Full-bleed scene card with a crossfade carousel and a small indicator (dots + active dash, caption, pause). */
export function SceneCarousel({ className }: { className?: string }) {
  const reduced = useReducedMotion();
  const [index, setIndex] = useState(0);
  const [hover, setHover] = useState(false);
  const [paused, setPaused] = useState(false);
  const running = !reduced && !hover && !paused;
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => {
      if (!document.hidden) setIndex((i) => (i + 1) % SCENES.length);
    }, SCENE_MS);
    return () => clearInterval(t);
  }, [running]);
  const scene = SCENES[index]!;
  return (
    <section
      aria-label="Ilustracje HYDRA NEXUS"
      onPointerEnter={() => setHover(true)}
      onPointerLeave={() => setHover(false)}
      className={cx("relative isolate overflow-hidden", className)}
      data-testid="login-artwork"
      data-scene={scene.id}
    >
      {SCENES.map((s, i) => (
        <div key={s.id} aria-hidden="true" className={cx("login-scene absolute inset-0", i === index ? "login-scene-on" : "")}>
          {s.render()}
        </div>
      ))}
      <div className="absolute bottom-4 left-4 flex items-center gap-3 rounded-full bg-[oklch(0.14_0.02_265/0.55)] py-1.5 pr-1.5 pl-3.5 text-white min-[900px]:bottom-6 min-[900px]:left-6">
        <span aria-hidden="true" className="flex items-center gap-1.5">
          {SCENES.map((s, i) => (
            <span key={s.id} className={cx("h-1.5 rounded-full bg-white transition-all duration-300", i === index ? "w-5" : "w-1.5 opacity-45")} />
          ))}
        </span>
        <span className="text-xs font-medium" aria-live="off">{scene.caption}<span className="opacity-70"> · ilustracja</span></span>
        {!reduced ? (
          <button
            type="button"
            onClick={() => setPaused((p) => !p)}
            aria-pressed={paused}
            aria-label={paused ? "Wznów zmianę ilustracji" : "Wstrzymaj zmianę ilustracji"}
            title={paused ? "Wznów" : "Wstrzymaj"}
            className="flex size-7 items-center justify-center rounded-full text-white hover:bg-white/15 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-white"
            data-testid="scene-pause"
          >
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true" fill="currentColor">
              {paused ? <path d="M3 1.5v9l7.5-4.5z" /> : <path d="M2.5 1.5h2.5v9H2.5zM7 1.5h2.5v9H7z" />}
            </svg>
          </button>
        ) : <span className="w-2" />}
      </div>
    </section>
  );
}
