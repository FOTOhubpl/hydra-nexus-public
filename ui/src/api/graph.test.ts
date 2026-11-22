import { describe, expect, it, vi } from "vitest";
import type { Explore, GEdge, GNode } from "../components/graph/api";
import { indexExplore, legacySource, lodSource, normOverview, resolveSource, UNASSIGNED } from "./graph";

const node = (id: string, campaign_id: string | null, extra: Partial<GNode> = {}): GNode => ({
  id, title: id, campaign_id, group_id: null, observed_at: null, registered_at: null, corpus: "nask", thumbnail: null, degree: 0, ...extra,
});
const edge = (source: string, target: string, level: GEdge["level"], bits: number, family = "INF"): GEdge => ({
  id: `${source}|${target}`, source, target, bits, p: 1, hypothesis: "x", level, family, family_bits: { [family]: bits }, campaign_bits: null,
  kit_bits: null, asset_bits: null, inferred: false, robustness: null, review: null, reasons: [],
});
const explore = (): Explore => ({
  run_id: "r1", corpus: "nask", synthetic: false, thresholds: { link_bits: 10, kit_bits: 6 },
  nodes: [node("a", "C1", { thumbnail: "art1" }), node("b", "C1"), node("c", "C2"), node("d", null)],
  edges: [edge("a", "b", "campaign", 20), edge("b", "c", "kit", 8, "VIS"), edge("a", "c", "asset", 3, "TXT"), edge("c", "d", "weak", 2)],
  campaigns: [
    { id: "C1", label: "one", confidence: 0.9, group_id: "G", first_seen: "2026-01-01", last_seen: null, scheme: null, size: 2 },
    { id: "C2", label: "two", confidence: 0.5, group_id: "G", first_seen: null, last_seen: null, scheme: null, size: 1 },
  ],
  groups: [{ id: "G", label: "grp", campaign_ids: ["C1", "C2"] }],
  stats: null,
});

describe("graph adapter (legacy explore)", () => {
  it("derives the overview: sizes, family bits of internal links, inter-campaign aggregates, unassigned bucket", () => {
    const i = indexExplore(explore());
    const ov = i.overview;
    expect(ov.source).toBe("legacy");
    expect(ov.positioned).toBe(false);
    const c1 = ov.campaigns.find((c) => c.id === "C1")!;
    expect(c1.size).toBe(2);
    expect(c1.family_bits).toEqual({ INF: 20 });
    expect(c1.thumbnail).toBe("art1");
    const none = ov.campaigns.find((c) => c.id === UNASSIGNED)!;
    expect(none.size).toBe(1);
    expect(none.unassigned).toBe(true);
    // C1-C2 via b|c (kit, 8 b, VIS) and a|c (asset, 3 b, TXT): strongest level kit, max 8, family by bit sum = VIS
    expect(ov.links).toEqual([{ id: "C1|C2", source: "C1", target: "C2", pairs: 2, max_bits: 8, level: "kit", family: "VIS" }]);
    expect(i.internal.get("C1")!.map((e) => e.id)).toEqual(["a|b"]);
    expect(i.external.get("C2")!.map((e) => e.other_campaign).sort()).toEqual(["C1", "C1", UNASSIGNED]);
  });

  it("fetches /explore once for overview, campaign detail and neighbours", async () => {
    const fetchExplore = vi.fn(async () => explore());
    const s = legacySource(fetchExplore);
    const [ov, d, nb] = await Promise.all([s.overview(), s.campaign("C1"), s.neighbours("c")]);
    expect(fetchExplore).toHaveBeenCalledTimes(1);
    expect(ov.campaigns).toHaveLength(3);
    expect(d.nodes.map((n) => n.id)).toEqual(["a", "b"]);
    expect(d.external.map((e) => e.id).sort()).toEqual(["a|c", "b|c"]);
    expect(nb.campaign_id).toBe("C2");
    expect(nb.edges).toHaveLength(3);
  });

  it("detaches an aborted caller without cancelling the shared download", async () => {
    let release!: (x: Explore) => void;
    const s = legacySource(() => new Promise<Explore>((r) => (release = r)));
    const ctrl = new AbortController();
    const first = s.campaign("C1", ctrl.signal);
    const second = s.campaign("C2");
    ctrl.abort();
    release(explore());
    await expect(first).rejects.toThrow(/Abort/);
    expect((await second).nodes.map((n) => n.id)).toEqual(["c"]);
  });
});

describe("graph adapter (level-of-detail)", () => {
  it("normalises an overview with positions and tolerates missing fields", () => {
    const ov = normOverview({ run_id: "r", campaigns: [{ id: "C1", label: "x", size: 3, x: 1, y: 2, r: 0.5, family_bits: { VIS: 4, bad: "n" } }, { id: "C2", size: 1, pos: { x: 0, y: 0 } }], links: [{ a: "C1", b: "C2", level: "kit", pairs: 2, max_bits: 7 }, { source: "C1", target: "C1" }] }, "lod");
    expect(ov.positioned).toBe(true);
    expect(ov.campaigns[0]!.family_bits).toEqual({ VIS: 4 });
    expect(ov.campaigns[1]!.label).toBe("C2");
    expect(ov.links).toEqual([{ id: "C1|C2", source: "C1", target: "C2", level: "kit", pairs: 2, max_bits: 7, family: null }]);
    expect(ov.incidents_total).toBe(4);
    expect(ov.stats).toBeNull();
  });

  it("reads campaign detail through the fetcher", async () => {
    const fetcher = vi.fn(async (path: string) => ({ campaign_id: "C1", nodes: [{ id: "a", campaign_id: "C1" }], edges: [{ source: "a", target: "b", bits: 12, level: "campaign" }], external: [{ source: "a", target: "z", bits: 7, level: "kit", other_campaign: "C9" }], truncated: true, _path: path }));
    const s = lodSource(fetcher as never);
    const d = await s.campaign("C 1");
    expect(fetcher.mock.calls[0]![0]).toBe("/v1/graph/campaign/C%201?top_reasons=3");
    expect(d.edges[0]!.id).toBe("a|b");
    expect(d.external[0]!.other_campaign).toBe("C9");
    expect(d.truncated).toBe(true);
  });

  it("decodes the compact tables of the published contract (docs/ui/GRAPH_API.md)", async () => {
    const EDGE = ["source", "target", "bits", "p", "hypothesis", "level", "family", "family_bits", "campaign_bits", "kit_bits", "asset_bits", "inferred", "robust", "review", "reasons"];
    const NODE = ["id", "title", "campaign_id", "group_id", "observed_at", "registered_at", "corpus", "thumbnail", "degree", "x", "y"];
    const ov = normOverview({
      run_id: "r",
      groups: { columns: ["id", "label", "n_campaigns", "size", "x", "y", "r"], rows: [["G", "grp", 2, 5, 0, 0, 9]] },
      campaigns: { columns: ["id", "label", "confidence", "group_id", "size", "first_seen", "last_seen", "scheme", "x", "y", "r", "internal_edges", "max_internal_bits", "family", "thumbnail", "solo", "future_col"],
        rows: [["C1", "one", 0.9, "G", 4, "2026-01-01", null, null, 1, 2, 2.6, 3, 30.5, "VIS", "art", false, 1], ["solo:i9", "i9", null, null, 1, null, null, null, 9, 9, 0.8, 0, null, null, null, true, 1]] },
      links: { columns: ["source", "target", "pairs", "max_bits", "sum_bits", "family", "level"], rows: [["C1", "solo:i9", 2, 8.5, 12, "INF", "kit"]] },
    }, "lod");
    expect(ov.campaigns[0]).toMatchObject({ id: "C1", size: 4, family: "VIS", x: 1, y: 2, r: 2.6, solo: false });
    expect(ov.campaigns[1]!.solo).toBe(true);
    expect(ov.groups[0]!.campaign_ids).toEqual(["C1"]);
    expect(ov.links[0]).toMatchObject({ source: "C1", target: "solo:i9", pairs: 2, max_bits: 8.5, level: "kit", family: "INF" });
    const fetcher = vi.fn(async () => ({
      run_id: "r", campaigns: [{ id: "solo:i9" }],
      nodes: { columns: NODE, rows: [["i9", "t", null, null, "2026-02-02", null, "ext", null, 1, 9, 9]] },
      edges: { columns: EDGE, rows: [] },
      external: { columns: EDGE, rows: [["a1", "i9", 8.5, 0.7, "abstain", "kit", "INF", { INF: 8.5 }, null, 8.5, null, false, [true, [], "kit"], null, [["INF", "tracker_id", 8.5, "UA-1", 2, "Ten sam identyfikator"]]]] },
      external_nodes: { columns: NODE, rows: [["a1", "x", "C1", "G", null, null, "ext", null, 3, 1, 2]] },
      truncated: { external: 4 },
    }));
    const d = await lodSource(fetcher as never).campaign("solo:i9");
    expect(d.nodes[0]!.campaign_id).toBe("solo:i9");
    expect(d.external[0]).toMatchObject({ id: "a1|i9", other_campaign: "C1", level: "kit", robustness: { robust: true, critical: [], link: "kit" } });
    expect(d.external[0]!.reasons[0]).toMatchObject({ family: "INF", kind: "tracker_id", bits: 8.5, value: "UA-1", text: "Ten sam identyfikator" });
    expect(d.truncated).toBe(true);
    expect(d.truncated_external).toBe(4);
  });

  it("falls back to the explore payload only when the overview endpoint is missing (404)", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes("/v1/graph/overview")) return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404, headers: { "Content-Type": "application/json" } });
      return new Response(JSON.stringify(explore()), { status: 200, headers: { "Content-Type": "application/json" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    const r = await resolveSource();
    expect(r.source.kind).toBe("legacy");
    expect(r.overview.campaigns.map((c) => c.id)).toContain("C1");
  });

  it("reports a broken server instead of silently downgrading", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("boom", { status: 500 })));
    await expect(resolveSource()).rejects.toBeTruthy();
  });
});
