import { useEffect, useMemo, useRef, useState } from "react";
import { SigmaContainer, useCamera, useRegisterEvents, useSigma } from "@react-sigma/core";
import { DEFAULT_STYLES } from "sigma";
import { layerDashed } from "sigma/rendering";
import type { EdgeDisplayData, NodeDisplayData, SigmaOptions } from "sigma/types";
import type { GraphEdge, GraphNode, GraphPayload } from "../api/types";
import { cx } from "../lib/cx";
import { formatCount } from "../lib/format";
import {
  buildGraph,
  DIMMED_EDGE_COLOR,
  DIMMED_NODE_COLOR,
  neighbourhood,
  runLayout,
  TRANSPARENT,
  type AnalystGraph,
  type BuildGraphResult,
  type EdgeAttributes,
  type NodeAttributes,
} from "../lib/graph";
import { EmptyState } from "./EmptyState";

export interface GraphCanvasProps {
  data: GraphPayload;
  onNodeClick?: (node: GraphNode) => void;
  onEdgeClick?: (edge: GraphEdge) => void;
  /** Click on empty canvas (typically clears the selection). */
  onStageClick?: () => void;
  /** Focused node: it and its neighbours stay lit, everything else dims. */
  selectedNodeId?: string | null;
  /** Focused edge: drawn on top, everything else dims. */
  selectedEdgeId?: string | null;
  /** ForceAtlas2 iterations (bounded by graph size, run once per data change). */
  layoutIterations?: number;
  /** Force labels on for every node instead of density-based labelling. */
  showLabels?: boolean;
  /** Edge kinds drawn dashed. Default: alternate solid/dashed over the sorted kinds. */
  dashedKinds?: readonly string[];
  /** Explicit colours per node group. */
  groupColors?: Readonly<Record<string, string>>;
  /** Edge colour per kind. */
  kindColors?: Readonly<Record<string, string>>;
  /** Readable names for groups in the legend (id -> label). */
  groupLabels?: Readonly<Record<string, string>>;
  showLegend?: boolean;
  className?: string;
}

const LABEL_COLOR = "#0f1419";
const LABEL_FONT = '"Inter Variable", system-ui, sans-serif';

export function hasWebGL2(): boolean {
  try {
    const canvas = document.createElement("canvas");
    return Boolean(canvas.getContext("webgl2"));
  } catch {
    return false;
  }
}

interface Focus {
  nodes: Set<string> | null;
  edge: string | null;
  selectedNode: string | null;
}

function computeFocus(graph: AnalystGraph, selectedNodeId?: string | null, selectedEdgeId?: string | null): Focus {
  if (selectedEdgeId && graph.hasEdge(selectedEdgeId)) {
    return { nodes: new Set(graph.extremities(selectedEdgeId)), edge: selectedEdgeId, selectedNode: null };
  }
  if (selectedNodeId && graph.hasNode(selectedNodeId)) {
    return { nodes: neighbourhood(graph, selectedNodeId), edge: null, selectedNode: selectedNodeId };
  }
  return { nodes: null, edge: null, selectedNode: null };
}

function buildOptions(focusRef: { current: Focus }, showLabels: boolean): Partial<SigmaOptions> {
  const nodeReducer = (key: string, _data: NodeDisplayData, _attrs: unknown): Partial<NodeDisplayData> => {
    const focus = focusRef.current;
    if (!focus.nodes) return {};
    if (key === focus.selectedNode) return { highlighted: true, zIndex: 3, labelVisibility: "visible" };
    if (focus.nodes.has(key)) return { zIndex: 2, labelVisibility: "visible" };
    return { color: DIMMED_NODE_COLOR, labelVisibility: "hidden", zIndex: 0 };
  };

  const edgeReducer = (key: string, _data: EdgeDisplayData, attrs: unknown): Partial<EdgeDisplayData> => {
    const focus = focusRef.current;
    if (!focus.nodes) return {};
    const edge = attrs as EdgeAttributes;
    const lit = focus.edge
      ? key === focus.edge
      : focus.selectedNode !== null && (edge.raw.source === focus.selectedNode || edge.raw.target === focus.selectedNode);
    if (lit) return { zIndex: 2 } as Partial<EdgeDisplayData>;
    return { zIndex: 0, color: DIMMED_EDGE_COLOR, gapColor: edge.dashed ? TRANSPARENT : DIMMED_EDGE_COLOR } as Partial<EdgeDisplayData>;
  };

  const options = {
    primitives: {
      edges: {
        variables: { gapColor: { type: "color", default: TRANSPARENT } },
        // One dashed layer for every edge: solid edges paint their gaps with
        // their own colour, dashed edges leave the gaps transparent.
        layers: [
          layerDashed({
            dashSize: { value: 6, mode: "pixels" },
            gapSize: { value: 4, mode: "pixels" },
            gapColor: { attribute: "gapColor", default: TRANSPARENT },
          }),
        ],
      },
    },
    styles: {
      nodes: {
        ...DEFAULT_STYLES.nodes,
        labelColor: LABEL_COLOR,
        labelFont: LABEL_FONT,
        labelSize: 12,
        cursor: "pointer",
        backdropColor: "#ffffff",
        backdropShadowColor: "rgba(15, 20, 25, 0.18)",
        backdropShadowBlur: 8,
        labelVisibility: showLabels ? "visible" : { whenState: "isHovered", then: "visible", else: "auto" },
      },
      edges: {
        ...DEFAULT_STYLES.edges,
        color: { attribute: "color", defaultValue: "#a6abb3" },
        gapColor: { attribute: "gapColor", defaultValue: TRANSPARENT },
        cursor: "pointer",
      },
    },
    settings: {
      enableEdgeEvents: true,
      // Sigma 4 defaults to graph-unit sizes; analyst graphs need pixel sizes independent of layout spread.
      itemSizesReference: "screen",
      zoomToSizeRatioFunction: (ratio: number) => Math.sqrt(ratio),
      renderLabels: true,
      labelRenderedSizeThreshold: showLabels ? 0 : 7,
      labelDensity: showLabels ? 1 : 0.6,
      hideEdgesOnMove: false,
      stagePadding: 32,
      minCameraRatio: 0.02,
      maxCameraRatio: 20,
      edgePickingPadding: 3,
    },
    nodeReducer,
    edgeReducer,
  };
  return options as unknown as Partial<SigmaOptions>;
}

/** Wires sigma events to the latest callbacks without re-registering on every render. */
function GraphEvents({
  graph,
  onNodeClick,
  onEdgeClick,
  onStageClick,
}: {
  graph: AnalystGraph;
  onNodeClick?: (node: GraphNode) => void;
  onEdgeClick?: (edge: GraphEdge) => void;
  onStageClick?: () => void;
}) {
  const registerEvents = useRegisterEvents();
  const handlers = useRef({ onNodeClick, onEdgeClick, onStageClick });
  handlers.current = { onNodeClick, onEdgeClick, onStageClick };

  useEffect(() => {
    registerEvents({
      clickNode: ({ node }) => {
        if (graph.hasNode(node)) handlers.current.onNodeClick?.((graph.getNodeAttributes(node) as NodeAttributes).raw);
      },
      clickEdge: ({ edge }) => {
        if (graph.hasEdge(edge)) handlers.current.onEdgeClick?.((graph.getEdgeAttributes(edge) as EdgeAttributes).raw);
      },
      clickStage: () => handlers.current.onStageClick?.(),
    });
  }, [registerEvents, graph]);

  return null;
}

/** Re-runs the reducers when the focus changes. */
function FocusSync({ focusKey }: { focusKey: string }) {
  const sigma = useSigma();
  useEffect(() => {
    sigma.refresh({ skipIndexation: true });
  }, [sigma, focusKey]);
  return null;
}

function CameraControls() {
  const { zoomIn, zoomOut, reset } = useCamera({ duration: 200, factor: 1.5 });
  const button = "flex size-7 items-center justify-center text-sm text-muted hover:bg-hover hover:text-fg";
  return (
    <div className="absolute top-2 right-2 z-10 flex flex-col overflow-hidden rounded-md border border-line bg-raised">
      <button type="button" className={button} onClick={() => zoomIn()} aria-label="Przybliż" title="Przybliż">
        +
      </button>
      <button type="button" className={cx(button, "border-y border-line")} onClick={() => zoomOut()} aria-label="Oddal" title="Oddal">
        −
      </button>
      <button type="button" className={button} onClick={() => reset()} aria-label="Dopasuj graf do widoku" title="Dopasuj graf do widoku">
        ⤢
      </button>
    </div>
  );
}

function Legend({
  built,
  iterations,
  groupLabels,
}: {
  built: BuildGraphResult;
  iterations: number;
  groupLabels?: Readonly<Record<string, string>>;
}) {
  const [open, setOpen] = useState(true);
  const { graph, groups, kinds, skippedEdges, duplicates } = built;
  return (
    <div className="absolute bottom-2 left-2 z-10 max-h-[60%] w-56 overflow-hidden rounded-md border border-line bg-raised text-xs">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-2.5 py-1.5 text-left text-muted hover:text-fg"
      >
        <span>
          <span className="font-mono text-fg">{formatCount(graph.order)}</span> węzłów ·{" "}
          <span className="font-mono text-fg">{formatCount(graph.size)}</span> krawędzi
        </span>
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="max-h-64 space-y-2 overflow-auto border-t border-line px-2.5 py-2">
          {groups.length > 0 && (
            <ul className="space-y-0.5" aria-label="Grupy węzłów">
              {groups.map((group) => (
                <li key={group.name} className="flex items-center gap-2">
                  <span aria-hidden="true" className="size-2.5 shrink-0 rounded-full" style={{ backgroundColor: group.color }} />
                  <span className="truncate text-fg" title={group.name}>{groupLabels?.[group.name] ?? group.name}</span>
                  <span className="ml-auto font-mono text-subtle">{formatCount(group.count)}</span>
                </li>
              ))}
            </ul>
          )}
          {kinds.length > 0 && (
            <ul className="space-y-0.5 border-t border-line pt-2" aria-label="Rodzaje krawędzi">
              {kinds.map((kind) => (
                <li key={kind.name} className="flex items-center gap-2">
                  <svg aria-hidden="true" width="18" height="6" className="shrink-0">
                    <line x1="0" y1="3" x2="18" y2="3" stroke={kind.color} strokeWidth="2" strokeDasharray={kind.dashed ? "4 3" : undefined} />
                  </svg>
                  <span className="truncate text-fg">{kind.name}</span>
                  <span className="ml-auto font-mono text-subtle">{formatCount(kind.count)}</span>
                </li>
              ))}
            </ul>
          )}
          <div className="border-t border-line pt-1.5 text-2xs text-subtle">
            ForceAtlas2 · {iterations} iter.
            {skippedEdges > 0 && <> · pominięto {formatCount(skippedEdges)} krawędzi bez węzłów</>}
            {duplicates > 0 && <> · {formatCount(duplicates)} powtórzonych id</>}
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Renders a generic `{nodes, edges}` payload with sigma (WebGL2). Node colour
 * follows `group`, edge dash style follows `kind`, edge width follows
 * `weight`. Layout: bounded synchronous ForceAtlas2 from a deterministic seed.
 */
export function GraphCanvas({
  data,
  onNodeClick,
  onEdgeClick,
  onStageClick,
  selectedNodeId = null,
  selectedEdgeId = null,
  layoutIterations = 200,
  showLabels = false,
  dashedKinds,
  groupColors,
  kindColors,
  groupLabels,
  showLegend = true,
  className,
}: GraphCanvasProps) {
  const webgl = useMemo(hasWebGL2, []);
  const dashedKey = dashedKinds ? dashedKinds.join("\u0000") : null;
  const colorsKey = JSON.stringify([groupColors ?? null, kindColors ?? null]);

  const { built, iterations } = useMemo(() => {
    const result = buildGraph(data, { dashedKinds, groupColors, kindColors });
    const ran = runLayout(result.graph, layoutIterations);
    return { built: result, iterations: ran };
    // dashedKinds/groupColors are compared by value via their string keys.
  }, [data, layoutIterations, dashedKey, colorsKey]);

  const focusRef = useRef<Focus>({ nodes: null, edge: null, selectedNode: null });
  focusRef.current = computeFocus(built.graph, selectedNodeId, selectedEdgeId);
  const focusKey = `${selectedNodeId ?? ""}|${selectedEdgeId ?? ""}`;

  const options = useMemo(() => buildOptions(focusRef, showLabels), [showLabels]);

  if (!webgl) {
    return (
      <EmptyState
        tone="error"
        title="WebGL2 niedostępny"
        description="Widok grafu wymaga WebGL2. Włącz akcelerację sprzętową w ustawieniach przeglądarki."
        className={className}
      />
    );
  }

  if (built.graph.order === 0) {
    return <EmptyState title="Graf jest pusty" description="Backend nie zwrócił żadnych węzłów." className={className} />;
  }

  return (
    <div className={cx("relative h-full min-h-0 w-full overflow-hidden bg-canvas", className)}>
      <SigmaContainer graph={built.graph} options={options} className="!bg-canvas" style={{ height: "100%", width: "100%" }}>
        <GraphEvents graph={built.graph} onNodeClick={onNodeClick} onEdgeClick={onEdgeClick} onStageClick={onStageClick} />
        <FocusSync focusKey={focusKey} />
        <CameraControls />
      </SigmaContainer>
      {showLegend && <Legend built={built} iterations={iterations} groupLabels={groupLabels} />}
    </div>
  );
}
