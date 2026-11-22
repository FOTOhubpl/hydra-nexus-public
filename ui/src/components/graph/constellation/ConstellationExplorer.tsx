/**
 * "Konstelacja dowodów": the lazy relationship-graph explorer (GRAPH-2).
 *
 * First paint = the campaign constellation from one small overview request (positions from the API, or a worker
 * layout). Incidents of a campaign are fetched only when the analyst expands it or zooms into it (CampaignLoader:
 * cancellation, concurrency limit, LRU under an incident budget). Focus+context on hover with the radial "why" ring,
 * time scrubber, mini-map, command palette, breadcrumb, keyboard navigation and a side inspector.
 */
import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { Link, useSearchParams } from "react-router";
import { useQuery } from "@tanstack/react-query";
import { resolveSource, UNASSIGNED, type CampaignDetail, type GEdge, type GNode, type GraphSource, type Level, type Overview } from "../../../api/graph";
import { cx } from "../../../lib/cx";
import { useSelectedRun } from "../../../lib/runSelection";
import { plNumber } from "../../../lib/evidence";
import { useTheme } from "../../../lib/theme";
import { Button } from "../../Button";
import { EmptyState } from "../../EmptyState";
import { FamChip } from "../../evidence";
import { LEVEL_LABEL, LEVELS, useCampaignSummary } from "../api";
import { LineKey } from "../Panels";
import { campaignColors, hasWebGL2 } from "./colors";
import type { CameraInfo, CanvasView, ConstellationHandle, Hit, RingData, Shown } from "./ConstellationCanvas";
import { convexHull, growthTimeline, incidentPositions, levelStyle, nearestInDirection } from "./geometry";
import { GraphPalette, type PaletteHit } from "./GraphPalette";
import { CampaignPanel, EdgePanel, IncidentPanel, OverviewPanel } from "./Inspector";
import { computeLayout } from "./layoutClient";
import { CampaignLoader } from "./loader";
import { Minimap } from "./Minimap";
import { TimeScrubber } from "./TimeScrubber";

// The WebGL host (sigma) is its own chunk: it downloads while the overview request is in flight.
const ConstellationCanvas = lazy(() => import("./ConstellationCanvas").then((m) => ({ default: m.ConstellationCanvas })));

/** Incidents kept in memory across loaded campaigns (LRU evicts beyond it, never the ones on screen). */
export const INCIDENT_BUDGET = 6000;
/** A campaign zoomed to at least this on-screen radius loads its incidents automatically. */
const AUTO_EXPAND_PX = 70;
const AUTO_EXPAND_MAX = 8;

function toggled<T>(set: ReadonlySet<T>, v: T): Set<T> {
  const next = new Set(set);
  if (next.has(v)) next.delete(v);
  else next.add(v);
  return next;
}

type Sel = { kind: "campaign" | "incident" | "edge"; id: string } | null;

function mark(name: string, detail?: Record<string, unknown>) {
  try {
    performance.mark(name, detail ? { detail } : undefined);
  } catch {
    /* Performance API unavailable */
  }
}

function useReducedMotion(): boolean {
  const q = typeof window !== "undefined" && window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
  return useSyncExternalStore(
    (fn) => {
      q?.addEventListener?.("change", fn);
      return () => q?.removeEventListener?.("change", fn);
    },
    () => q?.matches ?? false,
    () => false,
  );
}

export function ConstellationExplorer() {
  const [params] = useSearchParams();
  const forced = (params.get("source") as "lod" | "legacy" | null) ?? undefined;
  const run = useSelectedRun();
  useEffect(() => {
    void import("./ConstellationCanvas");
  }, []);
  const q = useQuery({
    queryKey: ["task", "graph2", "overview", run, forced ?? "auto"],
    queryFn: async ({ signal }) => {
      mark("hydra:graph:overview-start");
      const r = await resolveSource(signal, forced);
      mark("hydra:graph:overview-loaded", { source: r.source.kind, campaigns: r.overview.campaigns.length });
      return r;
    },
    staleTime: 60_000,
    structuralSharing: false,
    retry: false,
  });
  if (q.isLoading) return <ConstellationSkeleton />;
  if (q.isError || !q.data) {
    return <EmptyState tone="error" title="Nie udało się pobrać grafu" description="Serwer API nie zwrócił przeglądu kampanii (/v1/graph/overview ani /v1/graph/explore). Sprawdź, czy API działa, i odśwież stronę." />;
  }
  const ov = q.data.overview;
  if (ov.run_id === null) return <EmptyState title="Brak przebiegu analizy" description="Graf powstaje po pierwszej analizie zbioru (Analiza → Uruchom)." />;
  if (ov.campaigns.length === 0) return <EmptyState title="Graf jest pusty" description="Ten przebieg nie zawiera kampanii ani incydentów." />;
  if (!hasWebGL2()) return <EmptyState tone="error" title="WebGL2 niedostępny" description="Konstelacja wymaga WebGL2 (akceleracja sprzętowa w przeglądarce). Lista kampanii jest dostępna na ekranie Kampanie." />;
  return <Constellation source={q.data.source} overview={ov} />;
}

/** Placeholder while the overview loads: a faint shimmer of where the constellation will be (no numbers). */
function ConstellationSkeleton() {
  return (
    <div className="relative flex h-full min-h-0 flex-col" aria-busy="true" aria-live="polite">
      <div className="h-11 border-b border-line bg-panel" />
      <div className="relative flex-1 overflow-hidden bg-canvas">
        <div className="absolute inset-0 grid place-items-center">
          <div className="flex flex-col items-center gap-3">
            <div className="relative size-40">
              {[0, 1, 2, 3, 4, 5].map((i) => (
                <span key={i} className="absolute rounded-full bg-line-strong/60 motion-safe:animate-pulse" style={{ width: 22 + (i % 3) * 14, height: 22 + (i % 3) * 14, left: `${20 + 55 * Math.cos(i)}%`, top: `${40 + 35 * Math.sin(i * 1.7)}%`, animationDelay: `${i * 120}ms` }} />
              ))}
            </div>
            <p className="text-sm text-muted">Wczytywanie konstelacji kampanii…</p>
          </div>
        </div>
      </div>
    </div>
  );
}

function Constellation({ source, overview }: { source: GraphSource; overview: Overview }) {
  const theme = useTheme();
  const reducedMotion = useReducedMotion();
  const [params, setParams] = useSearchParams();
  const canvas = useRef<ConstellationHandle>(null);
  const stage = useRef<HTMLDivElement>(null);

  // ------------------------------------------------------------------ layout (worker) + colours
  const layoutQ = useQuery({
    queryKey: ["task", "graph2", "layout", overview.run_id, overview.source, overview.campaigns.length],
    queryFn: ({ signal }) => computeLayout(overview.campaigns, overview.groups, overview.positioned, signal),
    staleTime: Infinity,
    structuralSharing: false,
  });
  const layout = layoutQ.data;
  useEffect(() => {
    if (layout) mark("hydra:graph:layout-ready", { ms: Math.round(layout.ms), worker: layout.worker });
  }, [layout]);
  const colors = useMemo(() => campaignColors(overview, theme), [overview, theme]);
  const campById = useMemo(() => new Map(overview.campaigns.map((c) => [c.id, c])), [overview]);
  const groupOf = useMemo(() => {
    const m = new Map<string, string>();
    for (const g of overview.groups) for (const c of g.campaign_ids) m.set(c, g.id);
    for (const c of overview.campaigns) if (c.group_id && !m.has(c.id)) m.set(c.id, c.group_id);
    return m;
  }, [overview]);
  const growth = useMemo(() => growthTimeline(overview.campaigns), [overview]);

  // ------------------------------------------------------------------ lazy loader
  const loader = useMemo(() => new CampaignLoader<CampaignDetail>({
    fetch: (id, signal) => source.campaign(id, signal),
    weight: (d) => d.nodes.length,
    budget: INCIDENT_BUDGET,
    capacity: 80,
    concurrency: 4,
    onLoaded: (id, d, ms) => mark("hydra:graph:campaign-loaded", { id, nodes: d.nodes.length, edges: d.edges.length, ms: Math.round(ms) }),
  }), [source]);
  useEffect(() => () => loader.dispose(), [loader]);
  const version = useSyncExternalStore(loader.subscribe, loader.getVersion);

  const [pinned, setPinned] = useState<string[]>(() => (params.get("c") ? [params.get("c")!] : []));
  const [auto, setAuto] = useState<string[]>([]);
  const wanted = useMemo(() => [...new Set([...pinned, ...auto])], [pinned, auto]);
  useEffect(() => loader.want(wanted), [loader, wanted]);

  const shownCache = useRef(new WeakMap<CampaignDetail, Shown>());
  const shown = useMemo(() => {
    const out = new Map<string, Shown>();
    if (!layout) return out;
    for (const id of wanted) {
      const d = loader.get(id);
      const disc = layout.campaigns.get(id);
      if (!d || !disc) continue;
      let s = shownCache.current.get(d);
      if (!s) {
        const pos = incidentPositions(d.nodes, disc, layout.scale);
        s = { detail: d, pos, hull: convexHull([...pos.values()]) };
        shownCache.current.set(d, s);
      }
      out.set(id, s);
    }
    return out;
  }, [wanted, loader, layout, version]); // eslint-disable-line react-hooks/exhaustive-deps
  const loading = useMemo(() => new Set(loader.inflightIds()), [loader, version]); // eslint-disable-line react-hooks/exhaustive-deps

  // incidents / edges of everything shown (inspector lookups)
  const nodeIndex = useMemo(() => {
    const nodes = new Map<string, GNode>();
    const edges = new Map<string, GEdge>();
    const byNode = new Map<string, GEdge[]>();
    for (const s of shown.values()) {
      for (const n of s.detail.nodes) nodes.set(n.id, n);
      for (const e of [...s.detail.edges, ...s.detail.external]) {
        if (edges.has(e.id)) continue;
        edges.set(e.id, e);
        for (const k of [e.source, e.target]) byNode.set(k, [...(byNode.get(k) ?? []), e]);
      }
    }
    return { nodes, edges, byNode };
  }, [shown]);

  // ------------------------------------------------------------------ interaction state
  const [sel, setSel] = useState<Sel>(() => (params.get("i") ? { kind: "incident", id: params.get("i")! } : params.get("c") ? { kind: "campaign", id: params.get("c")! } : null));
  const [hover, setHover] = useState<Hit | null>(null);
  const [kbCampaign, setKbCampaign] = useState<string | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [palette, setPalette] = useState(false);
  const [highlight, setHighlight] = useState<Set<string>>(new Set());
  const [cam, setCam] = useState<CameraInfo | null>(null);
  const [tick, setTick] = useState(0);
  const [help, setHelp] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // weak (abstain) links are hidden by default: they are "to review", not evidence of a shared operator
  const [levels, setLevels] = useState<Set<Level>>(() => new Set<Level>(["campaign", "kit", "asset", "review"]));
  const firstPaint = useRef(false);

  const selCampaign = sel?.kind === "campaign" ? sel.id : sel?.kind === "incident" ? nodeIndex.nodes.get(sel.id)?.campaign_id ?? null : null;
  const hoverCampaign = hover?.campaign ?? null;
  const focusCampaign = hoverCampaign ?? kbCampaign ?? selCampaign;
  const cursorDate = cursor === null ? null : growth.dates[cursor] ?? null;

  // URL mirrors the drill-down (shareable, back button friendly)
  useEffect(() => {
    setParams((p) => {
      const n = new URLSearchParams(p);
      const c = sel?.kind === "campaign" ? sel.id : selCampaign;
      if (c) n.set("c", c);
      else n.delete("c");
      if (sel?.kind === "incident") n.set("i", sel.id);
      else n.delete("i");
      return n.toString() === p.toString() ? p : n;
    }, { replace: true });
  }, [sel, selCampaign, setParams]);

  // camera -> auto-expanded campaigns (zoomed into), debounced so a fly-through does not fire requests
  useEffect(() => {
    if (!cam) return;
    const t = window.setTimeout(() => {
      const next = cam.visible.filter((v) => v.rPx >= AUTO_EXPAND_PX && (campById.get(v.id)?.size ?? 0) > 0).sort((a, b) => b.rPx - a.rPx).slice(0, AUTO_EXPAND_MAX).map((v) => v.id);
      setAuto((prev) => (prev.length === next.length && prev.every((x, i) => x === next[i]) ? prev : next));
    }, 140);
    return () => window.clearTimeout(t);
  }, [cam, campById]);

  // radial "why" ring: family bits from the overview, top genes from the rule-based summary (both server values)
  const ringCampaign = focusCampaign && focusCampaign !== UNASSIGNED ? focusCampaign : null;
  const [ringDebounced, setRingDebounced] = useState<string | null>(null);
  useEffect(() => {
    const t = window.setTimeout(() => setRingDebounced(ringCampaign), 120);
    return () => window.clearTimeout(t);
  }, [ringCampaign]);
  const summary = useCampaignSummary(ringDebounced);
  const ring: RingData | null = useMemo(() => {
    if (!ringCampaign) return null;
    const c = campById.get(ringCampaign);
    if (!c) return null;
    const fb = summary.data && summary.data.campaign_id === ringCampaign && Object.keys(summary.data.family_bits ?? {}).length ? summary.data.family_bits : c.family_bits ?? {};
    const families = Object.entries(fb).filter(([, b]) => b > 0).sort((a, b) => b[1] - a[1]) as [string, number][];
    const genes = summary.data && summary.data.campaign_id === ringCampaign ? summary.data.core_genes.filter((g) => typeof g.bits === "number").map((g) => ({ label: g.text || g.kind, bits: g.bits, family: g.family })) : [];
    return { campaign: ringCampaign, families, genes };
  }, [ringCampaign, campById, summary.data]);

  const view: CanvasView = useMemo(() => ({
    hoverCampaign: hoverCampaign ?? kbCampaign,
    focusCampaign: null,
    selected: sel,
    cursorDate,
    highlight,
    levels,
    key: [hoverCampaign, kbCampaign, sel?.kind, sel?.id, cursorDate, [...highlight].join(), [...levels].join()].join("|"),
  }), [hoverCampaign, kbCampaign, sel, cursorDate, highlight, levels]);

  // ------------------------------------------------------------------ actions
  const expand = useCallback((id: string) => {
    setPinned((p) => (p.includes(id) ? p : [id, ...p].slice(0, 12)));
  }, []);
  const flyToCampaign = useCallback((id: string, fill = 0.6) => {
    const d = layout?.campaigns.get(id);
    if (d) canvas.current?.zoomToDisc(d, fill);
  }, [layout]);
  const selectCampaign = useCallback((id: string, fly = true) => {
    setSel({ kind: "campaign", id });
    setKbCampaign(null);
    expand(id);
    if (fly) flyToCampaign(id);
  }, [expand, flyToCampaign]);

  // incident picked before its campaign was loaded: fly to it once its position is known
  const pendingFly = useRef<string | null>(params.get("i"));
  useEffect(() => {
    const id = pendingFly.current;
    if (!id) return;
    for (const s of shown.values()) {
      const p = s.pos.get(id);
      if (p) {
        pendingFly.current = null;
        canvas.current?.focusPoint(p, 46);
        return;
      }
    }
  }, [shown]);

  const locateIncident = useCallback(async (id: string) => {
    setSel({ kind: "incident", id });
    for (const s of shown.values()) {
      const p = s.pos.get(id);
      if (p) {
        canvas.current?.focusPoint(p, 46);
        return;
      }
    }
    try {
      const nb = await source.neighbours(id);
      if (!nb.campaign_id) {
        const none = campById.get(UNASSIGNED);
        if (none) {
          pendingFly.current = id;
          expand(UNASSIGNED);
          flyToCampaign(UNASSIGNED);
        } else setNotice(`Incydent ${id} nie należy do żadnej kampanii tego przebiegu.`);
        return;
      }
      pendingFly.current = id;
      expand(nb.campaign_id);
      flyToCampaign(nb.campaign_id);
    } catch {
      setNotice(`Nie udało się ustalić kampanii incydentu ${id}.`);
    }
  }, [shown, source, campById, expand, flyToCampaign]);

  const onPalette = useCallback(async (h: PaletteHit) => {
    setNotice(null);
    if (h.kind === "campaign") {
      setHighlight(new Set([h.id]));
      selectCampaign(h.id);
      return;
    }
    if (h.kind === "incident") {
      setHighlight(new Set());
      void locateIncident(h.id);
      return;
    }
    // gene value: light up the campaigns of the incidents sharing it (first 12 looked up)
    const ids = h.incidents.slice(0, 12);
    const camps = new Set<string>();
    const results = await Promise.allSettled(ids.map((i) => (nodeIndex.nodes.get(i)?.campaign_id ? Promise.resolve(nodeIndex.nodes.get(i)!.campaign_id) : source.neighbours(i).then((n) => n.campaign_id))));
    for (const r of results) if (r.status === "fulfilled") camps.add(r.value ?? UNASSIGNED);
    const present = [...camps].filter((c) => campById.has(c));
    setHighlight(new Set(present));
    setNotice(`„${h.label}”: ${h.incidents.length} incydent(ów) w ${present.length} kampanii${h.incidents.length > ids.length ? ` (sprawdzono pierwsze ${ids.length})` : ""}. Kampanie podświetlone pulsującym pierścieniem.`);
    if (present[0]) selectCampaign(present[0]);
  }, [selectCampaign, locateIncident, nodeIndex, source, campById]);

  const goUp = useCallback(() => {
    if (sel?.kind === "edge" || sel?.kind === "incident") {
      const c = sel.kind === "incident" ? nodeIndex.nodes.get(sel.id)?.campaign_id : nodeIndex.nodes.get(nodeIndex.edges.get(sel.id)?.source ?? "")?.campaign_id;
      if (c) {
        setSel({ kind: "campaign", id: c });
        return;
      }
    }
    if (sel) {
      setSel(null);
      setHighlight(new Set());
      canvas.current?.fit();
    }
  }, [sel, nodeIndex]);

  const onClick = useCallback((h: Hit | null, mods: { shift: boolean }) => {
    if (!h) {
      setSel(null);
      setKbCampaign(null);
      return;
    }
    if (h.kind === "campaign") selectCampaign(h.id, !shown.has(h.id) || mods.shift);
    else if (h.kind === "incident") setSel({ kind: "incident", id: h.id });
    else if (h.kind === "edge") setSel({ kind: "edge", id: h.id });
    else if (h.kind === "link") {
      const [a, b] = h.id.split("|");
      if (a && b) {
        expand(a);
        expand(b);
        setNotice(`Powiązanie kampanii: wczytuję incydenty obu kampanii, aby pokazać łączące je pary.`);
      }
    }
  }, [selectCampaign, shown, expand]);

  // keyboard: arrows move between campaigns on screen, Enter expands, Esc goes up the breadcrumb
  const onStageKey = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const dirs: Record<string, "left" | "right" | "up" | "down"> = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" };
    const dir = dirs[e.key];
    if (dir && cam) {
      e.preventDefault();
      const cur = kbCampaign ?? selCampaign;
      const from = cur ? cam.visible.find((v) => v.id === cur) : null;
      const origin = from ?? { x: cam.width / 2, y: cam.height / 2 };
      const cands = cam.visible.filter((v) => v.id !== cur);
      const next = from ? nearestInDirection(origin, cands, dir) : cands.sort((a, b) => Math.hypot(a.x - origin.x, a.y - origin.y) - Math.hypot(b.x - origin.x, b.y - origin.y))[0]?.id ?? null;
      if (next) {
        setKbCampaign(next);
        const d = layout?.campaigns.get(next);
        const v = cam.visible.find((x) => x.id === next);
        if (d && v && (v.x < 60 || v.y < 60 || v.x > cam.width - 60 || v.y > cam.height - 60)) canvas.current?.panToGraph(d);
      }
      return;
    }
    if (e.key === "Enter" && (kbCampaign ?? hoverCampaign)) {
      e.preventDefault();
      selectCampaign((kbCampaign ?? hoverCampaign)!);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      if (kbCampaign) setKbCampaign(null);
      else goUp();
    }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        // graph palette wins over the global one while this screen is open
        e.preventDefault();
        e.stopImmediatePropagation();
        setPalette((v) => !v);
        return;
      }
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      switch (e.key) {
        case "/":
          e.preventDefault();
          setPalette(true);
          break;
        case "f":
        case "0":
          canvas.current?.fit();
          break;
        case "+":
        case "=":
          canvas.current?.zoom(1 / 1.6);
          break;
        case "-":
          canvas.current?.zoom(1.6);
          break;
        case "t":
          if (cursor === null || cursor >= growth.dates.length - 1) setCursor(0);
          setPlaying((v) => !v);
          break;
        case "?":
          setHelp((v) => !v);
          break;
        case "Escape":
          if (document.activeElement !== stage.current && !palette) goUp();
          break;
      }
    };
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }); // re-bound every render: reads fresh state

  // ------------------------------------------------------------------ derived panels
  const selCamp = selCampaign ? campById.get(selCampaign) : undefined;
  const selGroup = selCamp ? overview.groups.find((g) => g.id === groupOf.get(selCamp.id)) ?? null : null;
  const selNode = sel?.kind === "incident" ? nodeIndex.nodes.get(sel.id) : undefined;
  const selEdge = sel?.kind === "edge" ? nodeIndex.edges.get(sel.id) : undefined;
  const announce = (() => {
    const id = kbCampaign ?? hoverCampaign;
    const c = id ? campById.get(id) : undefined;
    if (!c) return "";
    const links = overview.links.filter((l) => l.source === c.id || l.target === c.id).length;
    return `${c.unassigned ? "Incydenty bez kampanii" : c.solo ? `Incydent bez kampanii ${c.label}` : `Kampania ${c.label}`}. Liczba incydentów: ${c.size}. Powiązania z innymi kampaniami: ${links}. Enter: rozwiń.`;
  })();

  const tooltip = (() => {
    if (!hover || !stage.current) return null;
    const w = stage.current.clientWidth;
    const style = { left: Math.min(hover.x + 14, w - 300), top: hover.y + 14 };
    if (hover.kind === "campaign") {
      const c = campById.get(hover.id);
      if (!c) return null;
      const st = loader.status(c.id);
      return (
        <div className="pointer-events-none absolute z-20 w-72 rounded-md border border-line-strong bg-panel/95 px-3 py-2 text-xs shadow-md" style={style}>
          <div className="font-medium text-fg">{c.unassigned ? "Incydenty bez kampanii" : c.label}</div>
          <div className="text-muted"><span className="font-mono text-fg">{c.size}</span> incydentów{c.first_seen ? `, od ${c.first_seen.slice(0, 10)}${c.first_seen_from_incidents ? " (najwcześniejszy incydent)" : ""}` : ", bez daty"}{groupOf.get(c.id) ? `, grupa ${overview.groups.find((g) => g.id === groupOf.get(c.id))?.label ?? groupOf.get(c.id)}` : ""}</div>
          <div className="mt-1 text-2xs text-subtle">{st === "ready" ? "incydenty wczytane" : st === "loading" ? "wczytywanie incydentów…" : "kliknij lub przybliż, aby wczytać incydenty"}</div>
        </div>
      );
    }
    if (hover.kind === "incident") {
      const n = nodeIndex.nodes.get(hover.id);
      if (!n) return null;
      const es = (nodeIndex.byNode.get(n.id) ?? []).slice().sort((a, b) => b.bits - a.bits);
      return (
        <div className="pointer-events-none absolute z-20 w-72 rounded-md border border-line-strong bg-panel/95 px-3 py-2 text-xs shadow-md" style={style}>
          <div className="font-medium text-fg">{n.title ?? n.id}</div>
          <div className="text-muted">{n.observed_at ?? n.registered_at ?? "bez daty"} · {es.length} powiązań</div>
          {es[0] && <div className="mt-1 text-muted">najsilniejsze: <span className="font-mono text-fg">{plNumber(es[0].bits, 1)} b</span> ({LEVEL_LABEL[es[0].level]})</div>}
        </div>
      );
    }
    if (hover.kind === "edge") {
      const e = nodeIndex.edges.get(hover.id);
      if (!e) return null;
      return (
        <div className="pointer-events-none absolute z-20 w-72 rounded-md border border-line-strong bg-panel/95 px-3 py-2 text-xs shadow-md" style={style}>
          <div className="flex items-center gap-2"><span className="font-mono font-semibold text-fg">{plNumber(e.bits, 1)} b</span><span className="text-muted">{LEVEL_LABEL[e.level]}</span>{e.family && <FamChip family={e.family} />}</div>
          {e.reasons.slice(0, 3).map((r, i) => <div key={i} className="truncate text-muted">{r.text} <span className="font-mono">{plNumber(r.bits, 1)} b</span></div>)}
          <div className="mt-1 text-2xs text-subtle">kliknij: kaskada bitów</div>
        </div>
      );
    }
    const l = overview.links.find((x) => x.id === hover.id);
    if (!l) return null;
    return (
      <div className="pointer-events-none absolute z-20 w-72 rounded-md border border-line-strong bg-panel/95 px-3 py-2 text-xs shadow-md" style={style}>
        <div className="font-medium text-fg">{l.pairs} par incydentów między kampaniami</div>
        <div className="text-muted">poziom: {LEVEL_LABEL[l.level]}, najsilniejsza para <span className="font-mono text-fg">{plNumber(l.max_bits, 1)} b</span>{l.family ? `, rodzina ${l.family}` : ""}</div>
        <div className="mt-1 text-2xs text-subtle">kliknij: wczytaj obie kampanie</div>
      </div>
    );
  })();

  const crumbs: { label: string; onClick?: () => void }[] = [{ label: "Konstelacja", onClick: () => { setSel(null); setHighlight(new Set()); canvas.current?.fit(); } }];
  if (selCamp) {
    const g = groupOf.get(selCamp.id);
    if (g) crumbs.push({ label: `grupa ${overview.groups.find((x) => x.id === g)?.label ?? g}`, onClick: () => { const d = layout?.groups.get(g); if (d) canvas.current?.zoomToDisc(d, 0.8); } });
    crumbs.push({ label: selCamp.unassigned ? "bez kampanii" : selCamp.label, onClick: () => selectCampaign(selCamp.id) });
  }
  if (selNode) crumbs.push({ label: selNode.title ?? selNode.id, onClick: () => void locateIncident(selNode.id) });
  if (selEdge) crumbs.push({ label: `para ${plNumber(selEdge.bits, 1)} b` });

  if (layoutQ.isError) return <EmptyState tone="error" title="Nie udało się ułożyć konstelacji" description={String(layoutQ.error)} />;

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="constellation">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-panel px-4 py-2">
        <nav aria-label="Ścieżka zagłębiania" className="min-w-0 flex-1">
          <ol className="flex min-w-0 items-center gap-1 text-sm">
            {crumbs.map((c, i) => (
              <li key={i} className="flex min-w-0 items-center gap-1">
                {i > 0 && <span aria-hidden="true" className="text-subtle">›</span>}
                {c.onClick && i < crumbs.length - 1 ? (
                  <button type="button" onClick={c.onClick} className="max-w-64 truncate rounded px-1 text-muted hover:bg-hover hover:text-fg">{c.label}</button>
                ) : (
                  <span aria-current={i === crumbs.length - 1 ? "location" : undefined} className="max-w-72 truncate px-1 font-medium text-fg">{c.label}</span>
                )}
              </li>
            ))}
          </ol>
        </nav>
        <button type="button" onClick={() => setPalette(true)} className="inline-flex h-8 w-52 items-center gap-2 rounded-md border border-line bg-canvas px-3 text-sm text-subtle hover:border-line-strong" aria-label="Szukaj w grafie (Ctrl+K)">
          <span className="flex-1 truncate text-left">Szukaj w grafie…</span>
          <kbd className="rounded border border-line px-1 font-mono text-2xs">⌘K</kbd>
        </button>
        <Button size="sm" variant="ghost" onClick={() => setHelp((v) => !v)} aria-expanded={help}>Skróty</Button>
        <Link to="/graph?classic=1" className="text-xs text-muted underline-offset-2 hover:text-fg hover:underline" title="Poprzedni eksplorator: cztery widoki, lasso, ścieżka dowodowa (wczytuje cały graf naraz)">widok klasyczny</Link>
        <span className="text-xs text-muted">
          {overview.synthetic && <span className="mr-2 rounded-[4px] border border-abstain px-1.5 py-0.5 text-abstain-ink">dane SYNTETYCZNE</span>}
          przebieg <span className="font-mono text-fg">{overview.run_id}</span>
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line bg-panel px-4 py-1.5">
        <span className="text-xs text-muted">powiązania:</span>
        <div role="group" aria-label="Poziom dowodu powiązań" className="flex flex-wrap items-center gap-1">
          {LEVELS.map((l) => {
            const on = levels.has(l);
            const n = overview.stats?.levels?.[l];
            return (
              <button key={l} type="button" aria-pressed={on} title={l === "weak" ? "Słabe powiązania (do oceny) nie świadczą o wspólnym operatorze; domyślnie ukryte" : `Pokaż lub ukryj powiązania: ${LEVEL_LABEL[l]}`}
                onClick={() => setLevels((cur) => toggled(cur, l))}
                className={cx("inline-flex h-7 items-center gap-1.5 rounded-full border px-2.5 text-xs transition-colors", on ? "border-fg/60 bg-active text-fg" : "border-line text-muted hover:bg-hover hover:text-fg")}>
                <LineKey style={levelStyle(l)} />
                {LEVEL_LABEL[l]}
                {typeof n === "number" && <span className="font-mono text-2xs text-subtle">{n.toLocaleString("pl-PL")}</span>}
              </button>
            );
          })}
        </div>
        <span className="ml-auto text-2xs text-subtle" title={overview.source === "lod" ? "Najpierw przegląd kampanii, incydenty kampanii dopiero po rozwinięciu lub przybliżeniu" : "Serwer nie ma punktów końcowych poziomu szczegółów: cały graf pobrano raz i dzielony jest w przeglądarce"}>{overview.source === "lod" ? "wczytywanie warstwami" : "pełny graf pobrany jednorazowo"}</span>
      </div>
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <div
          ref={stage}
          className="relative min-h-80 min-w-0 flex-1 overflow-hidden bg-canvas focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
          tabIndex={0}
          role="application"
          aria-roledescription="konstelacja kampanii"
          aria-label="Konstelacja kampanii. Strzałki: przejście między kampaniami, Enter: rozwiń kampanię, Escape: poziom wyżej, Ctrl+K: szukaj, plus i minus: przybliżenie, F: cały widok, T: odtwarzanie w czasie."
          onKeyDown={onStageKey}
        >
          {layout ? (
            <Suspense fallback={<div className="absolute inset-0 grid place-items-center text-sm text-muted">Przygotowanie widoku WebGL…</div>}>
            <ConstellationCanvas
              ref={canvas}
              theme={theme}
              overview={overview}
              layout={layout}
              colors={colors}
              shown={shown}
              loading={loading}
              view={view}
              ring={ring}
              reducedMotion={reducedMotion}
              onHover={setHover}
              onClick={onClick}
              onCamera={(c) => {
                setCam(c);
                setTick((t) => (t + 1) % 1_000_000);
              }}
              onFirstPaint={() => {
                if (firstPaint.current) return;
                firstPaint.current = true;
                mark("hydra:graph:first-paint", { campaigns: overview.campaigns.length, source: overview.source, layoutMs: Math.round(layout.ms), worker: layout.worker });
                // deep link (?c= / ?i=): fly there once the constellation is on screen
                const ci = params.get("i");
                const cc = params.get("c");
                window.setTimeout(() => {
                  if (ci) void locateIncident(ci);
                  else if (cc && campById.has(cc)) flyToCampaign(cc);
                }, 0);
              }}
            />
            </Suspense>
          ) : (
            <div className="absolute inset-0 grid place-items-center text-sm text-muted">Układanie konstelacji…</div>
          )}
          <div className="sr-only" aria-live="polite">{announce}</div>
          {tooltip}
          {notice && (
            <div role="status" className="absolute top-3 left-1/2 z-20 flex max-w-[min(640px,90%)] -translate-x-1/2 items-start gap-2 rounded-md border border-line-strong bg-panel px-3 py-2 text-xs text-fg shadow-sm">
              <span className="min-w-0 flex-1">{notice}</span>
              <button type="button" className="text-muted hover:text-fg" aria-label="Zamknij komunikat" onClick={() => setNotice(null)}>×</button>
            </div>
          )}
          {/* zoom controls */}
          <div className="absolute top-3 right-3 z-10 flex flex-col overflow-hidden rounded-md border border-line bg-panel shadow-sm">
            <button type="button" aria-label="Przybliż" title="Przybliż (+)" className="size-8 text-muted hover:bg-hover hover:text-fg" onClick={() => canvas.current?.zoom(1 / 1.6)}>+</button>
            <button type="button" aria-label="Oddal" title="Oddal (−)" className="size-8 border-y border-line text-muted hover:bg-hover hover:text-fg" onClick={() => canvas.current?.zoom(1.6)}>−</button>
            <button type="button" aria-label="Cały widok" title="Cały widok (F)" className="size-8 text-xs text-muted hover:bg-hover hover:text-fg" onClick={() => canvas.current?.fit()}>fit</button>
          </div>
          {/* level-of-detail hint */}
          {cam && (
            <div className="pointer-events-none absolute top-3 left-3 z-10 rounded-md border border-line bg-panel/90 px-2.5 py-1 text-2xs text-muted">
              {shown.size === 0 ? "Kampanie (przybliż lub kliknij, aby wczytać incydenty)" : `Rozwinięte kampanie: ${shown.size}`}
              {loading.size > 0 && <span className="ml-1.5 text-accent">· wczytywanie {loading.size}…</span>}
              {cam.unitPx < 30 && shown.size > 0 && <span className="ml-1.5">· pokazane najsilniejsze powiązania na węzeł</span>}
            </div>
          )}
          {help && (
            <div className="absolute top-14 right-14 z-30 w-80 rounded-md border border-line-strong bg-panel p-3 text-xs shadow-sm" role="dialog" aria-label="Skróty klawiszowe">
              <div className="mb-1 font-semibold text-fg">Skróty klawiszowe</div>
              <dl className="grid grid-cols-[84px_minmax(0,1fr)] gap-y-1 text-muted">
                {[["Ctrl/⌘K, /", "szukaj incydentu, domeny, telefonu, hasha"], ["Tab", "fokus na konstelacji"], ["← ↑ → ↓", "następna kampania w danym kierunku"], ["Enter", "rozwiń kampanię (wczytaj incydenty)"], ["Esc", "poziom wyżej w ścieżce"], ["+ / −", "przybliż / oddal"], ["F, 0", "cały widok"], ["T", "odtwarzanie wzrostu w czasie"], ["?", "ta pomoc"]].map(([k, v]) => (
                  <div key={k} className="contents"><dt className="font-mono text-fg">{k}</dt><dd>{v}</dd></div>
                ))}
              </dl>
            </div>
          )}
          <div className="pointer-events-none absolute bottom-3 left-3 z-10 sm:right-[212px]">
            <TimeScrubber growth={growth} cursor={cursor} setCursor={setCursor} playing={playing} setPlaying={setPlaying} reducedMotion={reducedMotion} />
          </div>
          {layout && <Minimap layout={layout} colors={colors} canvas={canvas} tick={tick} theme={theme} />}
        </div>
        <aside className={cx("max-h-[45%] w-full shrink-0 overflow-y-auto border-t border-line bg-panel md:max-h-none md:w-[400px] md:border-t-0 md:border-l")} aria-label="Inspektor grafu" data-testid="graph-inspector">
          {selEdge ? (
            <EdgePanel edge={selEdge} a={nodeIndex.nodes.get(selEdge.source)} b={nodeIndex.nodes.get(selEdge.target)} onClose={() => goUp()} onPickIncident={(id) => void locateIncident(id)} />
          ) : sel?.kind === "incident" ? (
            <IncidentPanel
              id={sel.id}
              node={selNode}
              edges={nodeIndex.byNode.get(sel.id) ?? []}
              onPickEdge={(e) => setSel({ kind: "edge", id: e.id })}
              onPickIncident={(id) => void locateIncident(id)}
              onClose={() => goUp()}
              titleOf={(id) => nodeIndex.nodes.get(id)?.title}
            />
          ) : selCamp ? (
            <CampaignPanel
              campaign={selCamp}
              group={selGroup}
              detail={loader.get(selCamp.id)}
              status={loader.status(selCamp.id)}
              error={loader.error(selCamp.id)}
              onRetry={() => loader.retry(selCamp.id)}
              onPickIncident={(id) => void locateIncident(id)}
              onPickEdge={(e) => setSel({ kind: "edge", id: e.id })}
              onClose={() => goUp()}
              campaignLabel={(id) => campById.get(id)?.label}
            />
          ) : (
            <OverviewPanel overview={overview} loaded={loader.loadedIds().length} loading={loading.size} budget={INCIDENT_BUDGET} onFocusCampaign={(id) => selectCampaign(id)} />
          )}
        </aside>
      </div>
      <GraphPalette open={palette} onClose={() => setPalette(false)} campaigns={overview.campaigns} onPick={(h) => void onPalette(h)} />
    </div>
  );
}
