import { describe, expect, it } from "vitest";
import type { GEdge, GNode } from "./api";
import { campaignLinks, components, edgeStyle, inPolygon, packLayout, spiralPack } from "./layout";

const node = (id: string, campaign_id: string | null, group_id: string | null = null): GNode => ({
  id, title: id, campaign_id, group_id, observed_at: null, registered_at: null, corpus: "nask", thumbnail: null, degree: 0,
});
const edge = (source: string, target: string, level: GEdge["level"], bits = 12): GEdge => ({
  id: `${source}|${target}`, source, target, bits, p: 1, hypothesis: "x", level, family: "INF", family_bits: {}, campaign_bits: null,
  kit_bits: null, asset_bits: null, inferred: false, robustness: null, review: null, reasons: [],
});

describe("graph layout", () => {
  it("packs discs without overlap", () => {
    const items = Array.from({ length: 300 }, (_, i) => ({ id: `d${i}`, r: 1 + (i % 7) }));
    const { at } = spiralPack(items, 0.5);
    const list = items.map((it) => ({ ...at.get(it.id)!, r: it.r }));
    for (let i = 0; i < list.length; i++) {
      for (let j = i + 1; j < list.length; j++) {
        const a = list[i]!;
        const b = list[j]!;
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThanOrEqual(a.r + b.r + 0.49);
      }
    }
  });

  it("keeps campaign members inside their campaign disc and campaigns inside their group", () => {
    const nodes = [node("a", "C1"), node("b", "C1"), node("c", "C2"), node("d", null)];
    const res = packLayout(nodes, [], [{ id: "G1", campaign_ids: ["C1", "C2"] }]);
    const c1 = res.campaigns.get("C1")!;
    for (const id of ["a", "b"]) {
      const p = res.pos.get(id)!;
      expect(Math.hypot(p.x - c1.x, p.y - c1.y)).toBeLessThanOrEqual(c1.r);
    }
    const g = res.groups.get("G1")!;
    expect(Math.hypot(c1.x - g.x, c1.y - g.y) + c1.r).toBeLessThanOrEqual(g.r + 1e-6);
    expect(res.pos.size).toBe(4);
  });

  it("lays out 6k incidents quickly", () => {
    const nodes = Array.from({ length: 6600 }, (_, i) => node(`n${i}`, `C${i % 2200}`));
    const t = performance.now();
    const res = packLayout(nodes, [], []);
    expect(res.pos.size).toBe(6600);
    expect(performance.now() - t).toBeLessThan(3000);
  });

  it("aggregates only kit-level links between campaigns", () => {
    const nodes = [node("a", "C1"), node("b", "C2"), node("c", "C2")];
    const links = campaignLinks(nodes, [edge("a", "b", "kit"), edge("a", "c", "campaign"), edge("b", "c", "kit")]);
    expect(links).toHaveLength(1);
    expect(links[0]!.pairs).toBe(1);
  });

  it("edge styles, lasso and components", () => {
    expect(edgeStyle({ level: "campaign", inferred: false })).toBe("solid");
    expect(edgeStyle({ level: "kit", inferred: false })).toBe("dashed");
    expect(edgeStyle({ level: "campaign", inferred: true })).toBe("dotted");
    expect(inPolygon(1, 1, [{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 2, y: 2 }, { x: 0, y: 2 }])).toBe(true);
    expect(inPolygon(3, 1, [{ x: 0, y: 0 }, { x: 2, y: 0 }, { x: 2, y: 2 }, { x: 0, y: 2 }])).toBe(false);
    expect(components(["a", "b", "c"], [{ source: "a", target: "b" }])).toBe(2);
  });
});
