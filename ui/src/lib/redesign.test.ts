import { describe, expect, it } from "vitest";
import { packCircles } from "../components/charts";
import { convexHull } from "./geometry";
import { extSource } from "./ext";
import { runSelection, withRun } from "./runSelection";

describe("redesign helpers", () => {
  it("convex hull drops interior points", () => {
    const h = convexHull([{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 2, y: 2 }, { x: 0, y: 2 }, { x: 1, y: 1 }]);
    expect(h).toHaveLength(4);
    expect(h).not.toContainEqual({ x: 1, y: 1 });
  });

  it("packs circles without overlap, deterministically", () => {
    const items = [{ id: "a", r: 30 }, { id: "b", r: 20 }, { id: "c", r: 20 }, { id: "d", r: 14 }];
    const p = packCircles(items);
    expect(packCircles(items)).toEqual(p);
    for (let i = 0; i < p.length; i++) for (let j = i + 1; j < p.length; j++) {
      expect(Math.hypot(p[i]!.x - p[j]!.x, p[i]!.y - p[j]!.y)).toBeGreaterThanOrEqual(p[i]!.r + p[j]!.r);
    }
  });

  it("badges only external datasets", () => {
    expect(extSource("nask-sample")).toBeNull();
    expect(extSource("upload")).toBeNull();
    expect(extSource("phishtank")).toBe("ext:phishtank");
  });

  it("adds run_id only to run-scoped paths when a run is selected", () => {
    runSelection.set(null);
    expect(withRun("/v1/graph")).toBe("/v1/graph");
    runSelection.set("r1");
    expect(withRun("/v1/graph?min_p=0")).toBe("/v1/graph?min_p=0&run_id=r1");
    expect(withRun("/v1/reviews")).toBe("/v1/reviews");
    expect(withRun("/v1/runs/latest?kind=eval")).toBe("/v1/runs/latest?kind=eval");
    runSelection.set(null);
  });
});
