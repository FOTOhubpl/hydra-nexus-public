import { describe, expect, it } from "vitest";
import type { OvCampaign } from "../../../api/graph";
import { campaignRadius, convexHull, edgeRanks, growthTimeline, incidentPositions, layoutConstellation, levelStyle, nearestInDirection, topKForZoom } from "./geometry";

const camp = (id: string, size: number, extra: Partial<OvCampaign> = {}): OvCampaign => ({
  id, label: id, size, group_id: null, first_seen: null, last_seen: null, confidence: null, x: null, y: null, r: null, family_bits: null, thumbnail: null, ...extra,
});

describe("constellation geometry", () => {
  it("packs campaigns without overlap and keeps group members inside the group disc", () => {
    const cs = Array.from({ length: 60 }, (_, i) => camp(`c${i}`, 1 + (i % 9) * 3));
    const L = layoutConstellation(cs, [{ id: "G", label: "G", campaign_ids: ["c1", "c2", "c3"], x: null, y: null, r: null }], false);
    const ds = [...L.campaigns.values()];
    expect(ds).toHaveLength(60);
    for (let i = 0; i < ds.length; i++) for (let j = i + 1; j < ds.length; j++) expect(Math.hypot(ds[i]!.x - ds[j]!.x, ds[i]!.y - ds[j]!.y)).toBeGreaterThanOrEqual(ds[i]!.r + ds[j]!.r);
    const g = L.groups.get("G")!;
    for (const c of ["c1", "c2", "c3"]) {
      const d = L.campaigns.get(c)!;
      expect(Math.hypot(d.x - g.x, d.y - g.y) + d.r).toBeLessThanOrEqual(g.r + 1e-6);
    }
    expect(L.campaigns.get("c0")!.r).toBeCloseTo(campaignRadius(1));
  });

  it("keeps API positions (only rescaled) when every campaign has one", () => {
    const cs = [camp("a", 4, { x: 0, y: 0, r: 1 }), camp("b", 4, { x: 10, y: 0, r: 1 })];
    const L = layoutConstellation(cs, [], true);
    const a = L.campaigns.get("a")!;
    const b = L.campaigns.get("b")!;
    expect(a.x).toBe(0);
    expect(b.y).toBe(0);
    expect(b.x / campaignRadius(4)).toBeCloseTo(10, 5);
  });

  it("puts the oldest incident in the centre of the growth rings and every incident inside the disc", () => {
    const nodes = [
      { id: "late", observed_at: "2026-10-09" },
      { id: "first", observed_at: "2026-01-01" },
      { id: "undated" },
      { id: "mid", registered_at: "2026-05-01" },
    ];
    const disc = { id: "c", x: 5, y: 5, r: campaignRadius(nodes.length) };
    const pos = incidentPositions(nodes, disc);
    const d = (id: string) => Math.hypot(pos.get(id)!.x - 5, pos.get(id)!.y - 5);
    expect(d("first")).toBeLessThan(d("mid"));
    expect(d("mid")).toBeLessThan(d("late"));
    expect(d("late")).toBeLessThan(d("undated"));
    for (const id of ["late", "first", "undated", "mid"]) expect(d(id)).toBeLessThanOrEqual(disc.r);
  });

  it("computes a convex hull", () => {
    const h = convexHull([{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 1, y: 1 }, { x: 2, y: 2 }, { x: 0, y: 2 }, { x: 1, y: 0.5 }]);
    expect(h).toHaveLength(4);
    expect(h).not.toContainEqual({ x: 1, y: 1 });
  });

  it("ranks edges so every node keeps its strongest link at far zoom", () => {
    const edges = [
      { id: "ab", source: "a", target: "b", bits: 40 },
      { id: "ac", source: "a", target: "c", bits: 30 },
      { id: "ad", source: "a", target: "d", bits: 20 },
      { id: "cd", source: "c", target: "d", bits: 5 },
    ];
    const r = edgeRanks(edges);
    const k = topKForZoom(3);
    expect(k).toBe(1);
    const kept = edges.filter((e) => r.get(e.id)! < k).map((e) => e.id).sort();
    // a and b keep ab, c keeps ac (its strongest), d keeps ad
    expect(kept).toEqual(["ab", "ac", "ad"]);
    expect(topKForZoom(100)).toBe(Infinity);
  });

  it("maps evidence level to line style", () => {
    expect(levelStyle("campaign")).toBe("solid");
    expect(levelStyle("review")).toBe("solid");
    expect(levelStyle("kit")).toBe("dashed");
    expect(levelStyle("asset")).toBe("dotted");
  });

  it("builds the growth timeline from first-seen days and counts undated campaigns apart", () => {
    const g = growthTimeline([{ first_seen: "2026-02-01T10:00:00" }, { first_seen: "2026-01-01" }, { first_seen: "2026-02-01" }, { first_seen: null }, { first_seen: "x", unassigned: true }]);
    expect(g.dates).toEqual(["2026-01-01", "2026-02-01"]);
    expect(g.counts).toEqual([1, 2]);
    expect(g.undated).toBe(1);
  });

  it("finds the nearest campaign in a direction", () => {
    const c = [{ id: "r", x: 10, y: 0 }, { id: "far-r", x: 30, y: 1 }, { id: "up", x: 0, y: -10 }, { id: "diag", x: 8, y: 8 }];
    expect(nearestInDirection({ x: 0, y: 0 }, c, "right")).toBe("r");
    expect(nearestInDirection({ x: 0, y: 0 }, c, "up")).toBe("up");
    expect(nearestInDirection({ x: 0, y: 0 }, c, "left")).toBeNull();
  });
});
