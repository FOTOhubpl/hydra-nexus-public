/**
 * Relationship-graph explorer ("Graf powiązań"): four views over the same evidence (campaign bubbles, incidents,
 * shared indicators, timeline), semantic zoom, hover neighbourhoods, inspector, strongest evidence path, lasso
 * selection with review / export actions, chip filters, time replay and view statistics.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";
import type Graph from "graphology";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import forceAtlas2 from "graphology-layout-forceatlas2";
import { FAMILIES } from "../../api/task";
import { cx } from "../../lib/cx";
import { plNumber } from "../../lib/evidence";
import { useTheme } from "../../lib/theme";
import { Button } from "../Button";
import { EmptyState } from "../EmptyState";
import { FamChip } from "../evidence";
import { hasWebGL2 } from "../GraphCanvas";
import { LEVEL_LABEL, LEVELS, useEvidencePath, useExplore, useGraphIndicators, type Explore, type GEdge, type Level } from "./api";
import { buildCampaignGraph, buildIncidentGraph, buildIndicatorGraph, campaignPalette, discsFor, packBBox, shortTitle } from "./build";
import { campaignLinks, components, nodeDate, packLayout } from "./layout";
import { NodeInspector, PairInspector, PathPanel, ReasonList, ReviewBadgeG, SelectionPanel, StatsPanel, TooltipCard, type VisibleStats } from "./Panels";
import { SigmaView, type SigmaViewHandle, type ViewState } from "./SigmaView";
import { TimelineView } from "./TimelineView";

export type Mode = "campaigns" | "incidents" | "indicators" | "timeline";
const MODES: { id: Mode; label: string; key: string }[] = [
  { id: "campaigns", label: "Kampanie", key: "1" },
  { id: "incidents", label: "Incydenty", key: "2" },
  { id: "indicators", label: "Wskaźniki", key: "3" },
  { id: "timeline", label: "Oś czasu", key: "4" },
];
const CORPUS_LABEL: Record<string, string> = { nask: "NASK", public: "publiczny", ext: "zewnętrzny", synthetic: "syntetyczny" };

type Hover = { kind: "node" | "edge"; id: string; x: number; y: number } | null;

function useSize(ref: React.RefObject<HTMLDivElement | null>) {
  const [s, setS] = useState({ w: 0, h: 0 });
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setS({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return s;
}

export function GraphExplorer() {
  const q = useExplore();
  if (q.isLoading) return <EmptyState tone="pending" title="Wczytywanie grafu powiązań…" />;
  if (q.isError || !q.data) return <EmptyState tone="error" title="Nie udało się pobrać grafu" description="Endpoint /v1/graph/explore nie odpowiedział. Sprawdź, czy serwer API działa." />;
  if (q.data.run_id === null) return <EmptyState title="Brak przebiegu analizy" description="Graf powstaje po pierwszej analizie zbioru (Analiza → Uruchom)." />;
  if (q.data.nodes.length === 0) return <EmptyState title="Graf jest pusty" description="Przebieg nie zawiera incydentów." />;
  if (!hasWebGL2()) return <EmptyState tone="error" title="WebGL2 niedostępny" description="Graf wymaga WebGL2 (akceleracja sprzętowa w przeglądarce)." />;
  return <Explorer data={q.data} />;
}

function Explorer({ data }: { data: Explore }) {
  const theme = useTheme();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [mode, setMode] = useState<Mode>((params.get("view") as Mode) || "campaigns");
  const [families, setFamilies] = useState<Set<string>>(new Set());
  const [levels, setLevels] = useState<Set<Level>>(new Set(["campaign", "kit", "asset", "weak", "review"]));
  const [minBits, setMinBits] = useState(data.thresholds?.link_bits ?? 10);
  const campaign = params.get("campaign");
  const corpora = useMemo(() => [...new Set(data.nodes.map((n) => n.corpus))].sort(), [data.nodes]);
  const [corpusOff, setCorpusOff] = useState<Set<string>>(new Set());
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [robustOnly, setRobustOnly] = useState(false);
  const [force, setForce] = useState(false);
  const [lasso, setLasso] = useState(false);
  const [pathMode, setPathMode] = useState(false);
  const [pathA, setPathA] = useState<string | null>(null);
  const [pathB, setPathB] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [edgeSel, setEdgeSel] = useState<string | null>(null);
  const [nodeSel, setNodeSel] = useState<string | null>(params.get("focus"));
  const [hover, setHover] = useState<Hover>(null);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState<number | null>(null);
  const [playing, setPlaying] = useState(false);
  const [help, setHelp] = useState(false);
  const [cam, setCam] = useState(0);
  const sigma = useRef<SigmaViewHandle>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const area = useRef<HTMLDivElement>(null);
  const size = useSize(area);

  const ind = useGraphIndicators(mode === "indicators");
  const path = useEvidencePath(pathA, pathB);

  const colors = useMemo(() => campaignPalette(data, theme), [data, theme]);
  const pack = useMemo(() => packLayout(data.nodes, data.campaigns, data.groups), [data]);
  const discs = useMemo(() => discsFor(pack, data, colors), [pack, data, colors]);
  const bbox = useMemo(() => packBBox(pack), [pack]);
  const nodeById = useMemo(() => new Map(data.nodes.map((n) => [n.id, n])), [data.nodes]);
  const edgeById = useMemo(() => new Map(data.edges.map((e) => [e.id, e])), [data.edges]);
  const campById = useMemo(() => new Map(data.campaigns.map((c) => [c.id, c])), [data.campaigns]);

  const graph: Graph | null = useMemo(() => {
    if (mode === "incidents") return buildIncidentGraph(data, pack, theme, colors);
    if (mode === "campaigns") return buildCampaignGraph(data, pack, theme, colors);
    if (mode === "indicators" && ind.data) return buildIndicatorGraph(data, ind.data, pack, theme, colors);
    return null;
  }, [mode, data, pack, theme, colors, ind.data, force]); // eslint-disable-line react-hooks/exhaustive-deps

  // ForceAtlas2 in a web worker (incidents / indicators), bounded run, then frozen.
  useEffect(() => {
    if (!graph || !force || mode === "campaigns" || mode === "timeline") return;
    const settings = { ...forceAtlas2.inferSettings(graph), barnesHutOptimize: graph.order > 400, gravity: 0.4, scalingRatio: 6, slowDown: 4 };
    const fa2 = new FA2Layout(graph, { settings, getEdgeWeight: "size" });
    fa2.start();
    const t = window.setTimeout(() => fa2.stop(), Math.min(9000, 2500 + graph.order));
    return () => {
      window.clearTimeout(t);
      fa2.kill();
    };
  }, [graph, force, mode]);

  // time replay: unique dates (observation, else domain registration); run order when nothing is dated
  const timeline = useMemo(() => {
    const dates = [...new Set(data.nodes.map(nodeDate).filter((d): d is string => Boolean(d)))].sort();
    return { dates, byRunOrder: dates.length < 2 };
  }, [data.nodes]);
  const steps = timeline.byRunOrder ? Math.min(60, data.nodes.length) : timeline.dates.length;
  useEffect(() => {
    if (!playing) return;
    const t = window.setInterval(() => {
      setCursor((c) => {
        const n = (c ?? -1) + Math.max(1, Math.ceil(steps / 60)); // a replay lasts ~27 s whatever the date span
        if (n >= steps - 1) {
          setPlaying(false);
          return steps - 1;
        }
        return n;
      });
    }, 450);
    return () => window.clearInterval(t);
  }, [playing, steps]);

  // ------------------------------------------------------------------ filters -> visible sets
  const neighbourCampaign = useMemo(() => {
    if (!campaign) return null;
    const keep = new Set(data.nodes.filter((n) => n.campaign_id === campaign).map((n) => n.id));
    for (const e of data.edges) {
      if (keep.has(e.source)) keep.add(e.target);
      else if (keep.has(e.target)) keep.add(e.source);
    }
    return keep;
  }, [campaign, data]);

  const visibleNodes = useMemo(() => {
    const out = new Set<string>();
    const orderIndex = cursor !== null && timeline.byRunOrder ? Math.ceil(((cursor + 1) / steps) * data.nodes.length) : null;
    data.nodes.forEach((n, i) => {
      if (corpusOff.has(n.corpus)) return;
      const d = nodeDate(n);
      if ((from || to) && !d) return;
      if (from && d! < from) return;
      if (to && d! > to) return;
      if (cursor !== null) {
        if (orderIndex !== null) {
          if (i >= orderIndex) return;
        } else if (!d || d > timeline.dates[cursor]!) return;
      }
      if (neighbourCampaign && !neighbourCampaign.has(n.id)) return;
      out.add(n.id);
    });
    return out;
  }, [data.nodes, corpusOff, from, to, cursor, timeline, steps, neighbourCampaign]);

  const edgeOk = useCallback((e: GEdge) => {
    if (!levels.has(e.level)) return false;
    if (families.size > 0 && !families.has(e.family ?? "")) return false;
    if (robustOnly && !(e.robustness?.robust || e.review === "must_link")) return false;
    return visibleNodes.has(e.source) && visibleNodes.has(e.target);
  }, [levels, families, robustOnly, visibleNodes]);

  const visibleEdges = useMemo(() => data.edges.filter(edgeOk), [data.edges, edgeOk]);
  const visibleEdgeIds = useMemo(() => new Set(visibleEdges.map((e) => e.id)), [visibleEdges]);

  const visibleStats: VisibleStats = useMemo(() => {
    const lv: Partial<Record<Level, number>> = {};
    const fam: Record<string, number> = {};
    let robust = 0;
    let scored = 0;
    for (const e of visibleEdges) {
      lv[e.level] = (lv[e.level] ?? 0) + 1;
      if (e.family) fam[e.family] = (fam[e.family] ?? 0) + 1;
      if (e.robustness) {
        scored++;
        if (e.robustness.robust) robust++;
      }
    }
    return { nodes: visibleNodes.size, edges: visibleEdges.length, components: components(visibleNodes, visibleEdges), levels: lv, families: fam, robust, scored };
  }, [visibleNodes, visibleEdges]);

  const visibleCampaigns = useMemo(() => {
    const s = new Set<string>();
    for (const id of visibleNodes) {
      const c = nodeById.get(id)?.campaign_id;
      if (c) s.add(c);
    }
    return s;
  }, [visibleNodes, nodeById]);

  const pathEdges = useMemo(() => new Set(path.data?.found ? path.data.steps.map((s) => s.edge_id) : []), [path.data]);
  const pathNodes = useMemo(() => new Set(path.data?.found ? path.data.nodes.map((n) => n.id) : []), [path.data]);
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const indById = useMemo(() => new Map((ind.data?.indicators ?? []).map((i) => [`ind:${i.id}`, i])), [ind.data]);

  const view: ViewState = useMemo(() => {
    const key = [mode, visibleNodes.size, visibleEdges.length, minBits, cursor, [...families].join(), [...levels].join(), robustOnly, selected.length, pathEdges.size, nodeSel, edgeSel, from, to, campaign, [...corpusOff].join()].join("|");
    if (mode === "campaigns") {
      return {
        key,
        nodeHidden: (id) => !visibleCampaigns.has(id),
        edgeHidden: (id) => {
          const [a, b] = id.split("|");
          return !visibleCampaigns.has(a!) || !visibleCampaigns.has(b!);
        },
        edgeFaded: () => false,
        selected: new Set<string>(),
        pathNodes: new Set<string>(),
        pathEdges: new Set<string>(),
        focus: nodeSel && campById.has(nodeSel) ? nodeSel : null,
      };
    }
    if (mode === "indicators") {
      return {
        key,
        nodeHidden: (id) => {
          if (id.startsWith("ind:")) {
            const it = indById.get(id);
            if (!it) return true;
            if (families.size > 0 && !families.has(it.family)) return true;
            return it.incidents.filter((i) => visibleNodes.has(i)).length < 2;
          }
          return !visibleNodes.has(id);
        },
        edgeHidden: () => false,
        edgeFaded: () => false,
        selected: selectedSet,
        pathNodes: new Set<string>(),
        pathEdges: new Set<string>(),
        focus: nodeSel,
      };
    }
    return {
      key,
      nodeHidden: (id) => !visibleNodes.has(id),
      edgeHidden: (id) => !visibleEdgeIds.has(id),
      edgeFaded: (id) => (edgeById.get(id)?.bits ?? 0) < minBits,
      selected: selectedSet,
      pathNodes,
      pathEdges,
      focus: nodeSel,
      focusEdge: edgeSel,
    };
  }, [mode, visibleNodes, visibleEdges.length, visibleEdgeIds, visibleCampaigns, edgeById, minBits, cursor, families, levels, robustOnly, selectedSet, selected.length, pathNodes, pathEdges, nodeSel, edgeSel, campById, indById, from, to, campaign, corpusOff]);

  // ------------------------------------------------------------------ actions
  const clearAll = useCallback(() => {
    setSelected([]);
    setEdgeSel(null);
    setNodeSel(null);
    setPathA(null);
    setPathB(null);
    setPathMode(false);
    setLasso(false);
  }, []);

  const pickNode = useCallback((id: string, mods?: { shift: boolean; alt: boolean; meta: boolean }) => {
    if (mode === "campaigns") {
      // drill down: campaign -> its incidents
      setParams((p) => {
        const n = new URLSearchParams(p);
        n.set("campaign", id);
        n.set("view", "incidents");
        return n;
      });
      setMode("incidents");
      return;
    }
    if (id.startsWith("ind:")) {
      const it = indById.get(id);
      if (it) setSelected(it.incidents.filter((i) => visibleNodes.has(i)));
      return;
    }
    if (pathMode || mods?.alt) {
      if (!pathA || (pathA && pathB)) {
        setPathA(id);
        setPathB(null);
      } else setPathB(id);
      return;
    }
    if (mods?.shift || mods?.meta) {
      setSelected((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]));
      return;
    }
    setEdgeSel(null);
    setNodeSel(id);
  }, [mode, pathMode, pathA, pathB, indById, visibleNodes, setParams]);

  const focusSearch = () => {
    const s = query.trim().toLowerCase();
    if (!s) return;
    if (mode === "campaigns") {
      const c = data.campaigns.find((x) => x.id.toLowerCase().includes(s) || x.label.toLowerCase().includes(s));
      if (c) {
        setNodeSel(c.id);
        sigma.current?.focusNode(c.id, 0.3);
      }
      return;
    }
    const hit = data.nodes.find((n) => visibleNodes.has(n.id) && (n.id.toLowerCase() === s || n.id.toLowerCase().includes(s) || (n.title ?? "").toLowerCase().includes(s)));
    if (hit) {
      setNodeSel(hit.id);
      setEdgeSel(null);
      sigma.current?.focusNode(hit.id);
    }
  };

  const switchMode = (m: Mode) => {
    setMode(m);
    setHover(null);
    setParams((p) => {
      const n = new URLSearchParams(p);
      n.set("view", m);
      return n;
    });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) {
        if (e.key === "Escape") t.blur();
        return;
      }
      if (e.metaKey || e.ctrlKey) return;
      const m = MODES.find((x) => x.key === e.key);
      if (m) return switchMode(m.id);
      switch (e.key) {
        case "/":
          e.preventDefault();
          searchRef.current?.focus();
          break;
        case "f":
          sigma.current?.fit();
          break;
        case "+":
        case "=":
          sigma.current?.zoom(1 / 1.5);
          break;
        case "-":
          sigma.current?.zoom(1.5);
          break;
        case "l":
          setLasso((v) => !v);
          break;
        case "p":
          setPathMode((v) => !v);
          break;
        case " ":
          e.preventDefault();
          setPlaying((v) => !v);
          if (cursor === null || cursor >= steps - 1) setCursor(0);
          break;
        case "?":
          setHelp((v) => !v);
          break;
        case "Escape":
          clearAll();
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }); // re-bound every render: handlers read fresh state

  // ------------------------------------------------------------------ render helpers
  const chip = (on: boolean) => cx("inline-flex h-7 items-center gap-1 rounded-full border px-2.5 text-xs transition-colors", on ? "border-fg/70 bg-active text-fg" : "border-line text-muted hover:bg-hover hover:text-fg");
  const toggle = <T,>(set: Set<T>, v: T) => {
    const n = new Set(set);
    if (n.has(v)) n.delete(v);
    else n.add(v);
    return n;
  };
  const rob = data.stats?.robustness_scored ?? 0;
  const selEdge = edgeSel ? edgeById.get(edgeSel) : undefined;
  const selNode = nodeSel ? nodeById.get(nodeSel) : undefined;
  const cursorLabel = cursor === null ? null : timeline.byRunOrder ? `krok ${cursor + 1} z ${steps} (kolejność przebiegu)` : timeline.dates[cursor];

  const tooltip = (() => {
    if (!hover || !size.w) return null;
    if (hover.kind === "edge") {
      if (mode === "campaigns") {
        const l = campaignLinks(data.nodes, data.edges).find((x) => x.id === hover.id);
        if (!l) return null;
        return (
          <TooltipCard x={hover.x} y={hover.y} w={size.w} h={size.h}>
            <div className="font-medium text-fg">{l.pairs} par na poziomie zestawu / szablonu</div>
            <div className="text-muted">najsilniejsza {plNumber(l.maxBits, 1)} b{l.family ? `, rodzina ${l.family}` : ""}</div>
          </TooltipCard>
        );
      }
      const e = edgeById.get(hover.id);
      if (!e) return null;
      return (
        <TooltipCard x={hover.x} y={hover.y} w={size.w} h={size.h}>
          <div className="mb-1 flex items-center gap-2"><span className="font-mono font-semibold text-fg">{plNumber(e.bits, 1)} b</span><span className="text-muted">{LEVEL_LABEL[e.level]}</span>{e.family && <FamChip family={e.family} />}<ReviewBadgeG r={e.review} /></div>
          <ReasonList reasons={e.reasons} />
          <div className="mt-1 text-2xs text-subtle">kliknij, aby otworzyć kaskadę bitów</div>
        </TooltipCard>
      );
    }
    if (mode === "campaigns") {
      const c = campById.get(hover.id);
      if (!c) return null;
      return (
        <TooltipCard x={hover.x} y={hover.y} w={size.w} h={size.h}>
          <div className="font-medium text-fg">{c.label}</div>
          <div className="text-muted"><span className="font-mono text-fg">{c.size}</span> incydentów{c.group_id ? `, grupa ${c.group_id}` : ""}{c.first_seen ? `, od ${c.first_seen.slice(0, 10)}` : ""}</div>
          <div className="mt-1 text-2xs text-subtle">kliknij, aby zobaczyć incydenty kampanii</div>
        </TooltipCard>
      );
    }
    if (hover.id.startsWith("ind:")) {
      const it = indById.get(hover.id);
      if (!it) return null;
      return (
        <TooltipCard x={hover.x} y={hover.y} w={size.w} h={size.h}>
          <div className="text-muted">{it.kind_pl}</div>
          <div className="font-mono break-all text-fg">{it.value}</div>
          <div className="mt-1 text-muted">w <span className="font-mono text-fg">{it.df}</span> incydentach</div>
          {it.over_correlating && <div className="mt-1 text-abstain-ink">nadmiernie łączy: {it.reason_pl}</div>}
        </TooltipCard>
      );
    }
    const n = nodeById.get(hover.id);
    if (!n) return null;
    const top = data.edges.filter((e) => (e.source === n.id || e.target === n.id) && visibleEdgeIds.has(e.id)).sort((a, b) => b.bits - a.bits);
    const reasons = top.flatMap((e) => e.reasons).sort((a, b) => b.bits - a.bits);
    const seen = new Set<string>();
    const uniq = reasons.filter((r) => (seen.has(r.text) ? false : (seen.add(r.text), true)));
    const c = n.campaign_id ? campById.get(n.campaign_id) : undefined;
    return (
      <TooltipCard x={hover.x} y={hover.y} w={size.w} h={size.h}>
        <div className="font-medium text-fg">{n.title ?? n.id}</div>
        <div className="mb-1 text-muted">{c ? shortTitle(c.label, c.id, 40) : "bez kampanii"}{n.observed_at ? `, ${n.observed_at}` : ""}, {top.length} powiązań</div>
        <ReasonList reasons={uniq} />
      </TooltipCard>
    );
  })();

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="graph-explorer">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-panel px-4 py-2">
        <div role="tablist" aria-label="Widok grafu" className="inline-flex rounded-md border border-line bg-canvas p-0.5">
          {MODES.map((m) => (
            <button key={m.id} type="button" role="tab" aria-selected={mode === m.id} onClick={() => switchMode(m.id)} title={`Skrót: ${m.key}`}
              className={cx("h-7 rounded-[5px] px-3 text-sm transition-colors", mode === m.id ? "bg-panel font-medium text-fg shadow-sm" : "text-muted hover:text-fg")}>
              {m.label}
            </button>
          ))}
        </div>
        <form className="flex items-center" onSubmit={(e) => { e.preventDefault(); focusSearch(); }}>
          <input ref={searchRef} value={query} onChange={(e) => setQuery(e.target.value)} placeholder={mode === "campaigns" ? "Znajdź kampanię ( / )" : "Znajdź incydent ( / )"} aria-label="Szukaj w grafie" className="h-8 w-60 rounded-md border border-line bg-canvas px-3 text-sm text-fg placeholder:text-subtle" />
        </form>
        <div className="flex items-center gap-1">
          <Button size="sm" variant={lasso ? "primary" : "secondary"} aria-pressed={lasso} disabled={mode === "campaigns" || mode === "timeline"} onClick={() => setLasso((v) => !v)} title="Zaznaczanie lassem (L)">Lasso</Button>
          <Button size="sm" variant={pathMode ? "primary" : "secondary"} aria-pressed={pathMode} disabled={mode === "campaigns" || mode === "indicators"} onClick={() => setPathMode((v) => !v)} title="Wybierz dwa węzły: najsilniejsza ścieżka dowodowa (P, albo Alt+klik)">Ścieżka dowodowa</Button>
          {(mode === "incidents" || mode === "indicators") && (
            <Button size="sm" variant={force ? "primary" : "secondary"} aria-pressed={force} onClick={() => setForce((v) => !v)} title="Układ siłowy ForceAtlas2 w wątku roboczym zamiast układu hierarchicznego">Układ siłowy</Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setHelp((v) => !v)} aria-expanded={help} title="Skróty klawiszowe (?)">Skróty</Button>
        </div>
        <span className="ml-auto text-xs text-muted">
          {data.synthetic && <span className="mr-2 rounded-[4px] border border-abstain px-1.5 py-0.5 text-abstain-ink">dane SYNTETYCZNE</span>}
          przebieg <span className="font-mono text-fg">{data.run_id}</span>
        </span>
      </div>
      {/* filters */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-panel px-4 py-2" aria-label="Filtry">
        <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Rodzina">
          {FAMILIES.map((f) => <button key={f} type="button" aria-pressed={families.has(f)} className={chip(families.has(f))} onClick={() => setFamilies((s) => toggle(s, f))}><FamChip family={f} withName /></button>)}
        </div>
        <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Poziom dowodu">
          {LEVELS.map((l) => <button key={l} type="button" aria-pressed={levels.has(l)} className={chip(levels.has(l))} onClick={() => setLevels((s) => toggle(s, l))}>{LEVEL_LABEL[l]}<span className="font-mono text-2xs text-subtle">{data.stats?.levels[l] ?? 0}</span></button>)}
        </div>
        <label className="flex items-center gap-2 text-xs text-muted">
          próg
          <input type="range" min={0} max={60} step={1} value={minBits} onChange={(e) => setMinBits(Number(e.target.value))} aria-label="Minimalne bity (słabsze krawędzie wyblakłe)" className="w-28" />
          <span className="w-10 font-mono text-fg">{minBits} b</span>
        </label>
        <label className="flex items-center gap-2 text-xs text-muted">
          kampania
          <select value={campaign ?? ""} onChange={(e) => setParams((p) => { const n = new URLSearchParams(p); if (e.target.value) n.set("campaign", e.target.value); else n.delete("campaign"); return n; })} className="h-7 max-w-52 rounded-md border border-line bg-canvas px-2 text-xs text-fg" aria-label="Kampania">
            <option value="">wszystkie</option>
            {data.campaigns.filter((c) => c.size > 1).map((c) => <option key={c.id} value={c.id}>{shortTitle(c.label, c.id, 40)} ({c.size})</option>)}
          </select>
        </label>
        {corpora.length > 1 && (
          <div className="flex items-center gap-1" role="group" aria-label="Korpus">
            {corpora.map((c) => <button key={c} type="button" aria-pressed={!corpusOff.has(c)} className={chip(!corpusOff.has(c))} onClick={() => setCorpusOff((s) => toggle(s, c))}>{CORPUS_LABEL[c] ?? c}</button>)}
          </div>
        )}
        <label className="flex items-center gap-1 text-xs text-muted">od <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="h-7 rounded-md border border-line bg-canvas px-1.5 text-xs text-fg" aria-label="Data od" /></label>
        <label className="flex items-center gap-1 text-xs text-muted">do <input type="date" value={to} onChange={(e) => setTo(e.target.value)} className="h-7 rounded-md border border-line bg-canvas px-1.5 text-xs text-fg" aria-label="Data do" /></label>
        <label className={cx("flex items-center gap-1.5 text-xs", rob === 0 ? "text-subtle" : "text-muted")} title={rob === 0 ? "Ten przebieg nie zawiera oceny odporności (test pomijania rodzin)" : "Tylko powiązania, które utrzymują się bez każdej pojedynczej rodziny, oraz decyzje analityka"}>
          <input type="checkbox" checked={robustOnly} disabled={rob === 0} onChange={(e) => setRobustOnly(e.target.checked)} />
          pokaż tylko odporne powiązania{rob === 0 ? " (brak oceny)" : ""}
        </label>
        {campaign && <Button size="sm" variant="ghost" onClick={() => setParams((p) => { const n = new URLSearchParams(p); n.delete("campaign"); return n; })}>pokaż wszystkie kampanie</Button>}
      </div>
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <div ref={area} className="relative min-h-72 min-w-0 flex-1 overflow-hidden bg-canvas">
          {mode === "timeline" ? (
            <TimelineView data={data} colors={colors} theme={theme} view={view} onHover={setHover} onClickNode={(id) => pickNode(id)} />
          ) : graph ? (
            <SigmaView
              ref={sigma}
              graph={graph}
              theme={theme}
              view={view}
              discs={force && mode !== "campaigns" ? [] : discs.filter((d) => d.kind === "group" || visibleCampaigns.has(d.id))}
              campaignDiscs={mode === "incidents" || mode === "indicators"}
              sizes={mode === "campaigns" ? "positions" : "screen"}
              bbox={force && mode !== "campaigns" ? null : bbox}
              thumbnails={mode !== "campaigns"}
              lasso={lasso}
              onHover={setHover}
              onClickNode={pickNode}
              onClickEdge={(id) => {
                if (mode === "incidents") {
                  setEdgeSel(id);
                  setNodeSel(null);
                }
              }}
              onClickStage={() => {
                setEdgeSel(null);
                setNodeSel(null);
              }}
              onLasso={(ids) => {
                setSelected(ids.filter((i) => !i.startsWith("ind:")));
                setLasso(false);
              }}
              onCamera={() => setCam((c) => (c + 1) % 1_000_000)}
            />
          ) : (
            <EmptyState tone="pending" title="Wczytywanie wskaźników…" />
          )}
          {mode === "indicators" && ind.data && ind.data.indicators.length > 0 && (
            <div className="absolute top-3 left-3 z-10 rounded-md border border-line bg-panel px-3 py-1.5 text-xs text-muted">
              <span className="font-mono text-fg">{ind.data.indicators.length}</span> wspólnych wskaźników (≥ 2 incydenty);{" "}
              <span className="font-mono text-fg">{ind.data.indicators.filter((i) => i.over_correlating).length}</span> nadmiernie łączących (szare: lista MISP, boilerplate, wartość trywialna)
            </div>
          )}
          {mode === "indicators" && ind.data && ind.data.indicators.length === 0 && (
            <div className="absolute inset-x-0 top-16 mx-auto w-fit rounded-md border border-line bg-panel px-3 py-2 text-sm text-muted">Brak wskaźników wspólnych dla co najmniej dwóch incydentów.</div>
          )}
          {mode === "incidents" && visibleEdges.length === 0 && visibleNodes.size > 0 && (
            <div className="absolute inset-x-0 top-4 mx-auto w-fit rounded-md border border-line bg-panel px-3 py-2 text-sm text-muted">Żadne powiązanie nie spełnia filtrów; widoczne są same incydenty.</div>
          )}
          {tooltip}
          {/* zoom controls */}
          {mode !== "timeline" && (
            <div className="absolute top-3 right-3 z-10 flex flex-col overflow-hidden rounded-md border border-line bg-panel">
              <button type="button" aria-label="Przybliż" title="Przybliż (+)" className="size-8 text-muted hover:bg-hover hover:text-fg" onClick={() => sigma.current?.zoom(1 / 1.5)}>+</button>
              <button type="button" aria-label="Oddal" title="Oddal (−)" className="size-8 border-y border-line text-muted hover:bg-hover hover:text-fg" onClick={() => sigma.current?.zoom(1.5)}>−</button>
              <button type="button" aria-label="Dopasuj widok" title="Dopasuj (F)" className="size-8 text-xs text-muted hover:bg-hover hover:text-fg" onClick={() => sigma.current?.fit()}>fit</button>
            </div>
          )}
          {mode !== "timeline" && graph && <Minimap graph={graph} sigma={sigma} tick={cam} theme={theme} />}
          {pathMode && <div className="absolute top-3 left-1/2 z-10 -translate-x-1/2 rounded-md border border-accent bg-panel px-3 py-1.5 text-xs text-fg">Tryb ścieżki: kliknij dwa incydenty ({pathA ? (pathB ? "gotowe" : "wybierz drugi") : "wybierz pierwszy"})</div>}
          {lasso && <div className="absolute top-3 left-1/2 z-20 -translate-x-1/2 rounded-md border border-accent bg-panel px-3 py-1.5 text-xs text-fg">Lasso: obrysuj węzły przytrzymując przycisk myszy (Esc, aby anulować)</div>}
          {help && (
            <div className="absolute top-14 right-14 z-30 w-72 rounded-md border border-line-strong bg-panel p-3 text-xs shadow-sm">
              <div className="mb-1 font-semibold text-fg">Skróty klawiszowe</div>
              <dl className="grid grid-cols-[64px_minmax(0,1fr)] gap-y-1 text-muted">
                {[["1–4", "widok: kampanie, incydenty, wskaźniki, oś czasu"], ["/", "szukaj i przybliż"], ["F", "dopasuj widok"], ["+ / −", "przybliż / oddal"], ["L", "lasso"], ["P", "ścieżka dowodowa (albo Alt+klik)"], ["Shift+klik", "dodaj do zaznaczenia"], ["Spacja", "odtwarzanie w czasie"], ["Esc", "wyczyść zaznaczenie"]].map(([k, v]) => (
                  <div key={k} className="contents"><dt className="font-mono text-fg">{k}</dt><dd>{v}</dd></div>
                ))}
              </dl>
            </div>
          )}
          {/* time replay */}
          <div className="absolute bottom-3 left-3 z-10 flex w-[min(560px,calc(100%-220px))] items-center gap-2 rounded-md border border-line bg-panel px-2.5 py-1.5">
            <Button size="sm" variant="secondary" aria-label={playing ? "Pauza" : "Odtwórz wzrost grafu"} onClick={() => { if (!playing && (cursor === null || cursor >= steps - 1)) setCursor(0); setPlaying((v) => !v); }}>{playing ? "pauza" : "odtwórz"}</Button>
            <input type="range" min={0} max={Math.max(0, steps - 1)} value={cursor ?? Math.max(0, steps - 1)} onChange={(e) => { setPlaying(false); setCursor(Number(e.target.value)); }} aria-label="Oś odtwarzania" className="min-w-0 flex-1" disabled={steps < 2} />
            <span className="w-40 truncate text-right font-mono text-2xs text-muted">{cursorLabel ?? (timeline.byRunOrder ? "kolejność przebiegu" : `${timeline.dates[0] ?? "?"} → ${timeline.dates.at(-1) ?? "?"}`)}</span>
            {cursor !== null && <Button size="sm" variant="ghost" onClick={() => { setPlaying(false); setCursor(null); }}>wszystko</Button>}
          </div>
        </div>
        {/* right panel */}
        <aside className="max-h-[45%] w-full shrink-0 overflow-y-auto border-t border-line bg-panel md:max-h-none md:w-[380px] md:border-t-0 md:border-l" aria-label="Inspektor grafu">
          {(pathA || pathMode) && (
            <PathPanel a={pathA} b={pathB} path={path.data} loading={path.isFetching} data={data} onClear={() => { setPathA(null); setPathB(null); setPathMode(false); }} onStep={(id) => { setEdgeSel(id); setNodeSel(null); }} />
          )}
          {selected.length > 0 && <SelectionPanel ids={selected} data={data} onClear={() => setSelected([])} onCompare={(a, b) => navigate(`/pair/${encodeURIComponent(a)}/${encodeURIComponent(b)}`)} />}
          {selEdge ? (
            <PairInspector edge={selEdge} data={data} onClose={() => setEdgeSel(null)} />
          ) : selNode ? (
            <NodeInspector node={selNode} data={data} onClose={() => setNodeSel(null)} onPickEdge={(id) => { setEdgeSel(id); }} onPath={(role, id) => { if (role === "a") { setPathA(id); setPathB(null); } else setPathB(id); setPathMode(true); }} />
          ) : null}
          <StatsPanel stats={visibleStats} data={data} onFocusCampaign={(id) => { if (mode === "campaigns") { setNodeSel(id); sigma.current?.focusNode(id, 0.4); } else setParams((p) => { const n = new URLSearchParams(p); n.set("campaign", id); return n; }); }} />
        </aside>
      </div>
    </div>
  );
}

function Minimap({ graph, sigma, tick, theme }: { graph: Graph; sigma: React.RefObject<SigmaViewHandle | null>; tick: number; theme: "light" | "dark" }) {
  const W = 180;
  const H = 120;
  const base = useRef<HTMLCanvasElement>(null);
  const top = useRef<HTMLCanvasElement>(null);
  const bounds = useMemo(() => {
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    graph.forEachNode((_, a) => {
      x0 = Math.min(x0, a.x); x1 = Math.max(x1, a.x); y0 = Math.min(y0, a.y); y1 = Math.max(y1, a.y);
    });
    const s = Math.min((W - 12) / Math.max(1e-6, x1 - x0), (H - 12) / Math.max(1e-6, y1 - y0));
    return { x0, y0, x1, y1, s, ox: (W - (x1 - x0) * s) / 2, oy: (H - (y1 - y0) * s) / 2 };
  }, [graph]);
  const tx = useCallback((x: number) => bounds.ox + (x - bounds.x0) * bounds.s, [bounds]);
  const ty = useCallback((y: number) => H - (bounds.oy + (y - bounds.y0) * bounds.s), [bounds]);
  useEffect(() => {
    const ctx = base.current?.getContext("2d");
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    base.current!.width = W * dpr;
    base.current!.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    graph.forEachNode((_, a) => {
      ctx.fillStyle = a.color as string;
      ctx.fillRect(tx(a.x) - 1, ty(a.y) - 1, 2, 2);
    });
  }, [graph, tx, ty, theme]);
  useEffect(() => {
    const s = sigma.current?.sigma();
    const ctx = top.current?.getContext("2d");
    if (!s || !ctx) return;
    const dpr = window.devicePixelRatio || 1;
    top.current!.width = W * dpr;
    top.current!.height = H * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const { width, height } = s.getDimensions();
    const a = s.viewportToGraph({ x: 0, y: 0 });
    const b = s.viewportToGraph({ x: width, y: height });
    ctx.strokeStyle = theme === "dark" ? "#4c9ffe" : "#0b5cad";
    ctx.lineWidth = 1.5;
    const x = Math.max(0, Math.min(tx(a.x), tx(b.x)));
    const y = Math.max(0, Math.min(ty(a.y), ty(b.y)));
    const w = Math.min(W, Math.max(tx(a.x), tx(b.x))) - x;
    const h = Math.min(H, Math.max(ty(a.y), ty(b.y))) - y;
    ctx.strokeRect(x, y, Math.max(4, w), Math.max(4, h));
  }, [tick, sigma, tx, ty, theme]);
  const onClick = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const s = sigma.current?.sigma();
    if (!s) return;
    const r = e.currentTarget.getBoundingClientRect();
    const gx = bounds.x0 + (e.clientX - r.left - bounds.ox) / bounds.s;
    const gy = bounds.y0 + (H - (e.clientY - r.top) - bounds.oy) / bounds.s;
    const framed = s.viewportToFramedGraph(s.graphToViewport({ x: gx, y: gy }));
    void s.getCamera().animate({ x: framed.x, y: framed.y }, { duration: 300 });
  };
  return (
    <div className="absolute right-3 bottom-3 z-10 rounded-md border border-line bg-panel p-1">
      <div className="relative" style={{ width: W, height: H }}>
        <canvas ref={base} className="absolute inset-0" style={{ width: W, height: H }} aria-hidden="true" />
        <canvas ref={top} className="absolute inset-0 cursor-pointer" style={{ width: W, height: H }} role="img" aria-label="Minimapa: kliknij, aby przesunąć widok" onClick={onClick} />
      </div>
    </div>
  );
}
