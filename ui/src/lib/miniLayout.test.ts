import { describe, expect, it } from "vitest";
import { miniLayout } from "./miniLayout";

describe("miniLayout", () => {
  it("is deterministic, keeps nodes apart and clusters by campaign", () => {
    const nodes = [{ id: "a", group: "c1" }, { id: "b", group: "c1" }, { id: "c", group: "c2" }, { id: "d", group: null }];
    const edges = [{ source: "a", target: "b", weight: 1 }];
    const p = miniLayout(nodes, edges);
    expect(miniLayout(nodes, edges)).toEqual(p);
    const dist = (u: string, v: string) => Math.hypot(p[u]!.x - p[v]!.x, p[u]!.y - p[v]!.y);
    expect(dist("a", "b")).toBeGreaterThan(0.03);
    expect(dist("a", "b")).toBeLessThan(dist("a", "c"));
  });
});
