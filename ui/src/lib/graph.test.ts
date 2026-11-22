import { describe, expect, it } from "vitest";
import {
  assignGroupColors,
  boundedIterations,
  buildGraph,
  EDGE_COLOR,
  GROUP_PALETTE,
  neighbourhood,
  OTHER_COLOR,
  runLayout,
  TRANSPARENT,
  UNGROUPED_COLOR,
} from "./graph";

describe("buildGraph", () => {
  const payload = {
    nodes: [
      { id: "1", label: "one", group: "b" },
      { id: "2", label: "two", group: "a" },
      { id: "3", label: "three" },
      { id: "3", label: "duplicate" },
    ],
    edges: [
      { id: "e1", source: "1", target: "2", kind: "x", weight: 2 },
      { id: "e2", source: "2", target: "3", kind: "y" },
      { id: "e3", source: "1", target: "missing" },
      { id: "e1", source: "2", target: "1" },
    ],
  };

  it("adds nodes and edges, skipping dangling edges and duplicate ids", () => {
    const { graph, skippedEdges, duplicates } = buildGraph(payload);
    expect(graph.order).toBe(3);
    expect(graph.size).toBe(2);
    expect(skippedEdges).toBe(1);
    expect(duplicates).toBe(2);
    expect(graph.getNodeAttribute("3", "label")).toBe("three");
  });

  it("colours nodes by group in alphabetical palette order", () => {
    const { graph, groups } = buildGraph(payload);
    expect(graph.getNodeAttribute("2", "color")).toBe(GROUP_PALETTE[0]); // "a"
    expect(graph.getNodeAttribute("1", "color")).toBe(GROUP_PALETTE[1]); // "b"
    expect(graph.getNodeAttribute("3", "color")).toBe(UNGROUPED_COLOR);
    expect(groups.map((g) => g.name)).toEqual(["a", "b"]);
  });

  it("styles edges by kind: alternate kinds are dashed, solid edges fill their gaps", () => {
    const { graph, kinds } = buildGraph(payload);
    expect(kinds).toEqual([
      { name: "x", count: 1, dashed: false, color: EDGE_COLOR },
      { name: "y", count: 1, dashed: true, color: EDGE_COLOR },
    ]);
    expect(graph.getEdgeAttribute("e1", "gapColor")).toBe(EDGE_COLOR);
    expect(graph.getEdgeAttribute("e2", "gapColor")).toBe(TRANSPARENT);
  });

  it("honours explicit dashed kinds and group colours", () => {
    const { graph } = buildGraph(payload, { dashedKinds: ["x"], groupColors: { a: "#123456" } });
    expect(graph.getEdgeAttribute("e1", "dashed")).toBe(true);
    expect(graph.getEdgeAttribute("e2", "dashed")).toBe(false);
    expect(graph.getNodeAttribute("2", "color")).toBe("#123456");
  });

  it("tolerates an empty or malformed payload", () => {
    const { graph } = buildGraph({ nodes: [], edges: [] });
    expect(graph.order).toBe(0);
    // arrays missing entirely
    const loose = buildGraph({} as never);
    expect(loose.graph.order).toBe(0);
  });

  it("returns direct neighbours for focus highlighting", () => {
    const { graph } = buildGraph(payload);
    expect(neighbourhood(graph, "2")).toEqual(new Set(["1", "2", "3"]));
    expect(neighbourhood(graph, "nope").size).toBe(0);
  });
});

describe("assignGroupColors", () => {
  it("folds groups past the palette into the Other colour", () => {
    const names = Array.from({ length: GROUP_PALETTE.length + 2 }, (_, i) => `g${i}`);
    const colors = assignGroupColors(names);
    expect(new Set(Array.from(colors.values()).slice(0, GROUP_PALETTE.length)).size).toBe(GROUP_PALETTE.length);
    expect(colors.get(`g${GROUP_PALETTE.length + 1}`)).toBe(OTHER_COLOR);
  });
});

describe("layout", () => {
  it("bounds iterations by graph size", () => {
    expect(boundedIterations(200, 1, 0)).toBe(0);
    expect(boundedIterations(200, 50, 100)).toBe(200);
    expect(boundedIterations(200, 200_000, 400_000)).toBeLessThan(200);
    expect(boundedIterations(0, 50, 100)).toBe(0);
  });

  it("moves nodes deterministically", () => {
    const a = buildGraph({
      nodes: [{ id: "1", label: "1" }, { id: "2", label: "2" }, { id: "3", label: "3" }],
      edges: [{ id: "e", source: "1", target: "2" }],
    });
    const b = buildGraph({
      nodes: [{ id: "1", label: "1" }, { id: "2", label: "2" }, { id: "3", label: "3" }],
      edges: [{ id: "e", source: "1", target: "2" }],
    });
    expect(runLayout(a.graph, 50)).toBe(50);
    runLayout(b.graph, 50);
    expect(a.graph.getNodeAttributes("1").x).toBeCloseTo(b.graph.getNodeAttributes("1").x);
    expect(Number.isFinite(a.graph.getNodeAttributes("3").y)).toBe(true);
  });
});

describe("kind colours", () => {
  it("colours edges by kind when kindColors is given", () => {
    const { graph, kinds } = buildGraph(
      { nodes: [{ id: "1", label: "1" }, { id: "2", label: "2" }], edges: [{ id: "e", source: "1", target: "2", kind: "INF" }] },
      { kindColors: { INF: "#008300" }, dashedKinds: [] },
    );
    expect(graph.getEdgeAttribute("e", "color")).toBe("#008300");
    expect(kinds[0]?.color).toBe("#008300");
  });
});
