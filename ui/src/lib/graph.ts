/**
 * Pure helpers that turn the generic `{nodes, edges}` payload into a styled
 * graphology graph. Kept free of sigma/WebGL so it can be unit-tested.
 */
import Graph from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import type { GraphEdge, GraphNode, GraphPayload } from "../api/types";

/**
 * Categorical slots, fixed order, validated for CVD/normal-vision separation
 * on the light canvas (DESIGN.md §3 cluster slots K1..K8). Groups past the 8th fold
 * into OTHER_COLOR instead of generating new hues.
 */
export const GROUP_PALETTE = [
  "#2a78d6", // blue
  "#eb6834", // orange
  "#1baf7a", // aqua
  "#eda100", // yellow
  "#e87ba4", // magenta
  "#008300", // green
  "#6250d6", // violet
  "#e34948", // red
] as const;

/** Dark-mode steps of the same eight slots (dataviz reference palette, validated on #111418). */
export const GROUP_PALETTE_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#0ca30c", "#9085e9", "#e66767"] as const;

export const OTHER_GROUP = "Other";
export const OTHER_COLOR = "#b9bec6";
export const UNGROUPED_COLOR = "#8a9099";
export const EDGE_COLOR = "#a6abb3";
export const DIMMED_NODE_COLOR = "#e3e6ea";
export const DIMMED_EDGE_COLOR = "#eef0f3";
export const TRANSPARENT = "rgba(0,0,0,0)";

export interface NodeAttributes {
  label: string;
  x: number;
  y: number;
  size: number;
  color: string;
  group: string | null;
  raw: GraphNode;
  [key: string]: unknown;
}

export interface EdgeAttributes {
  size: number;
  color: string;
  /** Colour painted in the gaps of the dash pattern: the edge colour for solid edges, transparent for dashed. */
  gapColor: string;
  kind: string | null;
  weight: number | null;
  dashed: boolean;
  raw: GraphEdge;
  [key: string]: unknown;
}

export type AnalystGraph = Graph<NodeAttributes, EdgeAttributes>;

export interface LegendGroup {
  name: string;
  color: string;
  count: number;
}

export interface LegendKind {
  name: string;
  count: number;
  dashed: boolean;
  color: string;
}

export interface BuildGraphOptions {
  /** Explicit colours per group; wins over the palette. */
  groupColors?: Readonly<Record<string, string>>;
  /** Edge kinds drawn dashed. Default: alternate solid/dashed over the sorted kind list. */
  dashedKinds?: readonly string[];
  /** Edge colour per kind (e.g. dominant gene family); default EDGE_COLOR. */
  kindColors?: Readonly<Record<string, string>>;
}

export interface BuildGraphResult {
  graph: AnalystGraph;
  groups: LegendGroup[];
  kinds: LegendKind[];
  /** Edges whose source/target is not a known node. */
  skippedEdges: number;
  /** Repeated node/edge ids (first occurrence wins). */
  duplicates: number;
}

function normaliseKey(value: string | null | undefined): string | null {
  if (value === null || value === undefined) return null;
  const trimmed = String(value).trim();
  return trimmed ? trimmed : null;
}

function positiveNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
}

/** Assign palette colours to groups in alphabetical order (stable: colour follows the group name). */
export function assignGroupColors(
  groupNames: Iterable<string>,
  overrides: Readonly<Record<string, string>> = {},
  palette: readonly string[] = GROUP_PALETTE,
): Map<string, string> {
  const sorted = Array.from(new Set(groupNames)).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const colors = new Map<string, string>();
  let slot = 0;
  for (const name of sorted) {
    const override = overrides[name];
    if (override) {
      colors.set(name, override);
      continue;
    }
    colors.set(name, slot < palette.length ? palette[slot]! : OTHER_COLOR);
    slot += 1;
  }
  return colors;
}

/** Deterministic sunflower spiral: a good, reproducible starting point for ForceAtlas2. */
function seedPosition(index: number): { x: number; y: number } {
  const radius = Math.sqrt(index + 0.5) * 10;
  const angle = index * 2.399963229728653; // golden angle
  return { x: radius * Math.cos(angle), y: radius * Math.sin(angle) };
}

export function buildGraph(payload: GraphPayload, options: BuildGraphOptions = {}): BuildGraphResult {
  const graph: AnalystGraph = new Graph<NodeAttributes, EdgeAttributes>({
    type: "directed",
    multi: true,
    allowSelfLoops: true,
  });
  let duplicates = 0;
  let skippedEdges = 0;

  const nodes = Array.isArray(payload.nodes) ? payload.nodes : [];
  const edges = Array.isArray(payload.edges) ? payload.edges : [];

  // Groups and colours.
  const groupCounts = new Map<string, number>();
  for (const node of nodes) {
    const group = normaliseKey(node.group);
    if (group) groupCounts.set(group, (groupCounts.get(group) ?? 0) + 1);
  }
  const groupColors = assignGroupColors(groupCounts.keys(), options.groupColors);

  // Node sizes: explicit `size` (sqrt-scaled) when present, else degree.
  const maxSize = nodes.reduce((max, node) => Math.max(max, positiveNumber(node.size) ?? 0), 0);
  const degree = new Map<string, number>();
  for (const edge of edges) {
    degree.set(String(edge.source), (degree.get(String(edge.source)) ?? 0) + 1);
    degree.set(String(edge.target), (degree.get(String(edge.target)) ?? 0) + 1);
  }

  nodes.forEach((node, index) => {
    const id = String(node.id);
    if (graph.hasNode(id)) {
      duplicates += 1;
      return;
    }
    const group = normaliseKey(node.group);
    const explicit = positiveNumber(node.size);
    const size =
      maxSize > 0
        ? 3 + 13 * Math.sqrt((explicit ?? 0) / maxSize)
        : Math.min(14, 3 + 2 * Math.sqrt(degree.get(id) ?? 0));
    graph.addNode(id, {
      ...seedPosition(index),
      label: node.label ? String(node.label) : id,
      size,
      color: group ? (groupColors.get(group) ?? OTHER_COLOR) : UNGROUPED_COLOR,
      group,
      raw: node,
    });
  });

  // Edge kinds and dash style.
  const kindCounts = new Map<string, number>();
  for (const edge of edges) {
    const kind = normaliseKey(edge.kind);
    if (kind) kindCounts.set(kind, (kindCounts.get(kind) ?? 0) + 1);
  }
  const sortedKinds = Array.from(kindCounts.keys()).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const dashedSet = new Set(options.dashedKinds ?? sortedKinds.filter((_, i) => i % 2 === 1));

  const maxWeight = edges.reduce((max, edge) => Math.max(max, positiveNumber(edge.weight) ?? 0), 0);

  for (const edge of edges) {
    const id = String(edge.id);
    const source = String(edge.source);
    const target = String(edge.target);
    if (!graph.hasNode(source) || !graph.hasNode(target)) {
      skippedEdges += 1;
      continue;
    }
    if (graph.hasEdge(id)) {
      duplicates += 1;
      continue;
    }
    const kind = normaliseKey(edge.kind);
    const weight = typeof edge.weight === "number" && Number.isFinite(edge.weight) ? edge.weight : null;
    const dashed = kind !== null && dashedSet.has(kind);
    const w = positiveNumber(weight);
    const color = (kind !== null && options.kindColors?.[kind]) || EDGE_COLOR;
    graph.addEdgeWithKey(id, source, target, {
      size: maxWeight > 0 && w !== null ? 0.6 + 2.4 * (w / maxWeight) : 1,
      color,
      gapColor: dashed ? TRANSPARENT : color,
      kind,
      weight,
      dashed,
      raw: edge,
    });
  }

  const groups: LegendGroup[] = Array.from(groupColors.entries()).map(([name, color]) => ({
    name,
    color,
    count: groupCounts.get(name) ?? 0,
  }));
  const kinds: LegendKind[] = sortedKinds.map((name) => ({
    name,
    count: kindCounts.get(name) ?? 0,
    dashed: dashedSet.has(name),
    color: options.kindColors?.[name] ?? EDGE_COLOR,
  }));

  return { graph, groups, kinds, skippedEdges, duplicates };
}

/**
 * Cap the synchronous ForceAtlas2 run so big graphs cannot freeze the tab:
 * roughly a fixed budget of (nodes·log(nodes) + edges) work units.
 */
export function boundedIterations(requested: number, order: number, size: number): number {
  if (requested <= 0 || order < 2) return 0;
  const cost = order * Math.max(1, Math.log2(order)) + size;
  const budget = Math.max(10, Math.floor(4_000_000 / Math.max(1, cost)));
  return Math.min(Math.floor(requested), budget);
}

/** Run a bounded synchronous ForceAtlas2 layout in place. Returns the iterations actually run. */
export function runLayout(graph: AnalystGraph, requestedIterations: number): number {
  const iterations = boundedIterations(requestedIterations, graph.order, graph.size);
  if (iterations === 0) return 0;
  const inferred = forceAtlas2.inferSettings(graph);
  forceAtlas2.assign(graph, {
    iterations,
    settings: { ...inferred, barnesHutOptimize: graph.order > 400 },
    getEdgeWeight: (_edge, attributes) => positiveNumber((attributes as EdgeAttributes).weight) ?? 1,
  });
  return iterations;
}

/** Set of a node and its direct neighbours, for focus highlighting. */
export function neighbourhood(graph: AnalystGraph, node: string): Set<string> {
  if (!graph.hasNode(node)) return new Set();
  return new Set([node, ...graph.neighbors(node)]);
}
