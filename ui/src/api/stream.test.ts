import { execFileSync } from "node:child_process";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import {
  aggregateLink,
  applyDelta,
  backoffDelay,
  fnv1a32,
  getPatchLatencies,
  isGap,
  liveStatus,
  LiveStream,
  patchCampaignPage,
  patchDashboard,
  patchDashboardJob,
  patchOverview,
  placeNode,
  resetPatchStats,
  streamUrl,
  type CampaignPage,
  type DeltaLink,
  type EventSourceCtor,
  type RawOverview,
  type StreamDelta,
} from "./stream";
import { normOverview, type Overview } from "./graph";
import { dashboardInterval, type DashboardData } from "./task";
import { runAwareQueryKeyHash, runSelection } from "../lib/runSelection";

// ------------------------------------------------------------------ fixtures (synthetic, shapes of the API)

const RUN = "analyse-1";

function dashboard(): DashboardData {
  return {
    run: { id: RUN, created_at: null, finished_at: null, alerts: [] },
    kpis: { incidents: 2, artifacts: 4, artifacts_by_role: {}, genes: 10, genes_by_family: {}, campaigns: 1, multi_member_campaigns: 1, groups: 0, assignments: 0, enriched_incidents: 0 },
    previews: [
      { id: "inc-1", title: "a", observed_at: null, thumbnail: "t1", scheme: null, campaign_id: "cmp-1", video: null },
      { id: "inc-2", title: "b", observed_at: null, thumbnail: "t2", scheme: null, campaign_id: "cmp-1", video: null },
    ],
    campaigns: [{ id: "cmp-1", label: "C1", size: 2, confidence: 0.9, group_id: null, members: [{ id: "inc-1", thumbnail: "t1" }, { id: "inc-2", thumbnail: "t2" }], pivots: [] }],
    groups: [],
    coverage: [],
    jobs: [{ id: "job-1", kind: "assign", status: "running", total: 3, done: 1, failed: 0, error_code: null, params: {}, created_at: "2026-11-21", finished_at: null }],
    live: { run_id: RUN, version: 10, seq: 100, status: "ready", new_incidents: 0, by_decision: {}, label_pl: "nowe od analizy" },
  };
}

function delta(over: Partial<StreamDelta> = {}): StreamDelta {
  return {
    run_id: RUN, v: 11, seq: 120, from_seq: 120, n: 1, kinds: ["assign"],
    kpis: { ...dashboard().kpis, incidents: 3, assignments: 1 },
    counts: { decision: { assign: [1, 1] } },
    campaigns: [{ id: "cmp-1", label: "C1", size: 3, new: 1, group_id: null, solo: false, internal_edges: 3, max_internal_bits: 31.23456, family: "INF" }],
    nodes: [{ id: "inc-9", title: "nowy", campaign_id: "cmp-1", map_campaign: "cmp-1", decision: "assign", observed_at: "2026-11-01", corpus: "nask", thumbnail: "t9", new: true }],
    edges: [{ source: "inc-9", target: "inc-2", bits: 12.3, p: 0.9, hypothesis: "same_campaign", level: "campaign", family: "VIS", new: true }],
    ...over,
  };
}

function overview(): RawOverview {
  return {
    run_id: RUN,
    bounds: { x0: -20, y0: -12, x1: 15, y1: 30 },
    levels: "kit,campaign,review",
    campaigns: {
      columns: ["id", "label", "confidence", "group_id", "size", "first_seen", "last_seen", "scheme", "x", "y", "r", "internal_edges", "max_internal_bits", "family", "thumbnail", "solo"],
      rows: [
        ["cmp-1", "C1", 0.9, null, 2, null, null, null, 10, -3, 5, 1, 20, "VIS", "t1", false],
        ["cmp-7", "C7", 0.8, null, 4, null, null, null, -10, 3, 6, 5, 25, "TXT", null, false],
        ["cmp-8", "C8", 0.8, null, 2, null, null, null, 0, 20, 3, 1, 15, "INF", null, false],
      ],
    },
    links: {
      columns: ["source", "target", "pairs", "max_bits", "sum_bits", "family", "level"],
      rows: [
        ["cmp-1", "cmp-7", 2, 11, 20, "TXT", "kit"],
        ["cmp-1", "cmp-8", 1, 9, 9, "INF", "kit"],
      ],
    },
    live: { run_id: RUN, version: 10, seq: 100, nodes: [], edges: [] },
  };
}

const col = (t: { columns: string[] }, name: string) => t.columns.indexOf(name);

// ------------------------------------------------------------------ placement

function pyFnv(text: string): number | null {
  const code = "import sys\nh=2166136261\nfor b in sys.argv[1].encode('utf-8'):\n h^=b\n h=(h*16777619)&0xffffffff\nprint(h)";
  try {
    return Number(execFileSync("python3", ["-I", "-c", code, text], { encoding: "utf8" }).trim());
  } catch {
    return null;
  }
}

/** FNV-1a written out independently (BigInt), as a second reference. */
function refFnv(text: string): number {
  let h = 2166136261n;
  for (const b of Buffer.from(text, "utf8")) h = ((h ^ BigInt(b)) * 16777619n) & 0xffffffffn;
  return Number(h);
}

describe("fnv1a32 / placeNode", () => {
  it("matches the Python reference values", () => {
    expect(fnv1a32("")).toBe(2166136261);
    expect(fnv1a32("a")).toBe(3826002220);
    expect(fnv1a32("inc-9")).toBe(refFnv("inc-9"));
    expect(fnv1a32("inc-9")).toBe(2586818981); // python3 run 2026-11-21, same algorithm
    expect(fnv1a32("zażółć")).toBe(refFnv("zażółć"));
    const py = pyFnv("inc-9");
    if (py !== null) expect(fnv1a32("inc-9")).toBe(py);
    const pyUtf = pyFnv("zażółć");
    if (pyUtf !== null) expect(fnv1a32("zażółć")).toBe(pyUtf);
  });

  it("places members at 0.8 r around the disc and solo nodes on the outer ring", () => {
    const theta = (2 * Math.PI * (refFnv("inc-9") % 3600)) / 3600;
    const m = placeNode("inc-9", { x: 10, y: -3, r: 5 }, null);
    expect(m.x).toBeCloseTo(10 + 4 * Math.cos(theta), 9);
    expect(m.y).toBeCloseTo(-3 + 4 * Math.sin(theta), 9);
    expect(m.x).toBeCloseTo(11.88404752487764, 9); // python3 reference
    expect(m.y).toBeCloseTo(-6.52850746407067, 9);
    const s = placeNode("inc-9", null, { x0: -20, y0: -12, x1: 15, y1: 30 });
    expect(Math.hypot(s.x, s.y)).toBeCloseTo(34, 9);
    expect(s.x).toBeCloseTo(16.01440396145995, 9);
    expect(s.y).toBeCloseTo(-29.992313444600697, 9);
    expect(Math.hypot(...Object.values(placeNode("x", null, null)))).toBeCloseTo(4, 9);
  });
});

// ------------------------------------------------------------------ dashboard

describe("patchDashboard", () => {
  it("updates kpis, campaign size/new, members and previews, and bumps live", () => {
    const d = dashboard();
    const out = patchDashboard(d, delta());
    expect(out).not.toBe(d);
    expect(out.kpis.incidents).toBe(3);
    expect(out.campaigns[0]!.size).toBe(3);
    expect(out.campaigns[0]!.new).toBe(1);
    expect(out.campaigns[0]!.members.map((m) => [m.id, m.new ?? false])).toEqual([["inc-1", false], ["inc-2", false], ["inc-9", true]]);
    const p = out.previews.find((x) => x.id === "inc-9")!;
    expect(p).toMatchObject({ new: true, decision: "assign", campaign_id: "cmp-1", thumbnail: "t9" });
    expect(out.live).toMatchObject({ seq: 120, version: 11, new_incidents: 1, by_decision: { assign: 1 } });
    // immutable: the input is untouched
    expect(d.previews).toHaveLength(2);
    expect(d.live!.seq).toBe(100);
    expect(out.previews[0]).toBe(d.previews[0]);
  });

  it("is idempotent and skips deltas with seq <= live.seq", () => {
    const once = patchDashboard(dashboard(), delta());
    expect(patchDashboard(once, delta())).toBe(once);
    expect(patchDashboard(dashboard(), delta({ seq: 100 }))).toEqual(dashboard());
    expect(patchDashboard(dashboard(), delta({ seq: 90 })).live!.seq).toBe(100);
  });

  it("ignores deltas of another run", () => {
    const d = dashboard();
    expect(patchDashboard(d, delta({ run_id: "analyse-2" }))).toBe(d);
  });

  it("derives new_incidents from absolute decision counts", () => {
    const a = patchDashboard(dashboard(), delta({ counts: { decision: { assign: [5, 2], abstain: [3, 1] } } }));
    expect(a.live).toMatchObject({ new_incidents: 3, by_decision: { assign: 2, abstain: 1 } });
    const b = patchDashboard(a, delta({ seq: 130, v: 12, counts: { decision: { assign: [6, 3] } } }));
    expect(b.live).toMatchObject({ new_incidents: 4, by_decision: { assign: 3, abstain: 1 } });
  });

  it("clears the mark of removed nodes", () => {
    const a = patchDashboard(dashboard(), delta());
    const b = patchDashboard(a, delta({ seq: 140, v: 12, nodes: [], campaigns: [], removed_nodes: ["inc-9"] }));
    expect(b.previews.find((p) => p.id === "inc-9")!.new).toBe(false);
    expect(b.campaigns[0]!.members.find((m) => m.id === "inc-9")!.new).toBe(false);
  });

  it("updates job rows from job events", () => {
    const d = dashboard();
    const out = patchDashboardJob(d, { id: "job-1", status: "done", done: 3, total: 3, failed: 0 });
    expect(out.jobs[0]).toMatchObject({ status: "done", done: 3 });
    expect(patchDashboardJob(d, { id: "job-x", status: "done" })).toBe(d);
    expect(patchDashboardJob(out, { id: "job-1", status: "done", done: 3, total: 3, failed: 0 })).toBe(out);
  });
});

// ------------------------------------------------------------------ campaigns page

describe("patchCampaignPage", () => {
  const page = (): CampaignPage => ({
    items: [{ id: "cmp-1", label: "C1", members: ["inc-1", "inc-2"], core_genome: [], confidence: 0.9, status: "active", size: 2 }],
    total: 1, limit: 2000, offset: 0, run_id: RUN, live: { run_id: RUN, version: 10, seq: 100 },
  });

  it("updates size, new and members; idempotent", () => {
    const once = patchCampaignPage(page(), delta());
    expect(once.items[0]).toMatchObject({ size: 3, new: 1, members: ["inc-1", "inc-2", "inc-9"] });
    expect(once.live!.seq).toBe(120);
    expect(patchCampaignPage(once, delta())).toBe(once);
  });

  it("skips old deltas", () => {
    const p = page();
    expect(patchCampaignPage(p, delta({ seq: 99 }))).toBe(p);
  });
});

// ------------------------------------------------------------------ overview

describe("patchOverview", () => {
  it("adds the trailing new column and updates campaign rows with internal stats", () => {
    const out = patchOverview(overview(), delta());
    const c = out.campaigns;
    expect(c.columns.at(-1)).toBe("new");
    const row = c.rows.find((r) => r[0] === "cmp-1")!;
    expect(row[col(c, "size")]).toBe(3);
    expect(row[col(c, "new")]).toBe(1);
    expect(row[col(c, "internal_edges")]).toBe(3);
    expect(row[col(c, "max_internal_bits")]).toBe(31.235);
    expect(row[col(c, "family")]).toBe("INF");
    expect(c.rows.find((r) => r[0] === "cmp-7")![col(c, "new")]).toBe(0);
    expect(out.links.columns.at(-1)).toBe("new");
  });

  it("recomputes a link from its levels under the payload's level filter", () => {
    const links: DeltaLink[] = [
      { source: "cmp-1", target: "cmp-7", level: "kit", pairs: 3, max_bits: 12.1, sum_bits: 30.2, family_bits: { VIS: 12, TXT: 10 }, new: 1 },
      { source: "cmp-1", target: "cmp-7", level: "campaign", pairs: 1, max_bits: 25.5, sum_bits: 25.5, family_bits: { TXT: 25.5 }, new: 1 },
      { source: "cmp-1", target: "cmp-7", level: "asset", pairs: 7, max_bits: 99, sum_bits: 300, family_bits: { META: 300 }, new: 0 },
    ];
    const out = patchOverview(overview(), delta({ links }));
    const l = out.links;
    const row = l.rows.find((r) => r[0] === "cmp-1" && r[1] === "cmp-7")!;
    // asset is outside "kit,campaign,review"
    expect(row[col(l, "pairs")]).toBe(4);
    expect(row[col(l, "max_bits")]).toBe(25.5);
    expect(row[col(l, "sum_bits")]).toBe(55.7);
    expect(row[col(l, "family")]).toBe("TXT"); // 35.5 vs VIS 12
    expect(row[col(l, "level")]).toBe("campaign");
    expect(row[col(l, "new")]).toBe(2);
    // the other link is untouched
    expect(l.rows.find((r) => r[1] === "cmp-8")).toEqual(["cmp-1", "cmp-8", 1, 9, 9, "INF", "kit", 0]);

    const wide = patchOverview({ ...overview(), levels: ["kit", "campaign", "asset", "review"] }, delta({ links }));
    const w = wide.links.rows.find((r) => r[1] === "cmp-7")!;
    expect(w[col(wide.links, "pairs")]).toBe(11);
    expect(w[col(wide.links, "family")]).toBe("META");
  });

  it("removes a link when all its levels are 0 and adds a new key", () => {
    const out = patchOverview(overview(), delta({ links: [
      { source: "cmp-8", target: "cmp-1", level: "kit", pairs: 0, max_bits: 0, sum_bits: 0, new: 0 },
      { source: "cmp-7", target: "cmp-8", level: "kit", pairs: 1, max_bits: 10.5, sum_bits: 10.5, family_bits: { VIS: 10.5 }, new: 1 },
    ] }));
    expect(out.links.rows.find((r) => r[0] === "cmp-1" && r[1] === "cmp-8")).toBeUndefined();
    expect(out.links.rows.find((r) => r[0] === "cmp-7" && r[1] === "cmp-8")).toEqual(["cmp-7", "cmp-8", 1, 10.5, 10.5, "VIS", "kit", 1]);
  });

  it("appends a new solo campaign row on the outer ring and places live nodes", () => {
    const out = patchOverview(overview(), delta({
      campaigns: [{ id: "solo:inc-10", label: "solo", size: 1, new: 1, group_id: null, solo: true, internal_edges: 0, max_internal_bits: null, family: null }],
      nodes: [
        { id: "inc-10", title: "s", campaign_id: null, map_campaign: "solo:inc-10", decision: "abstain", new: true },
        { id: "inc-9", title: "m", campaign_id: "cmp-1", map_campaign: "cmp-1", decision: "assign", new: true },
      ],
    }));
    const c = out.campaigns;
    const row = c.rows.find((r) => r[0] === "solo:inc-10")!;
    const ring = placeNode("inc-10", null, overview().bounds);
    expect(row[col(c, "solo")]).toBe(true);
    expect(row[col(c, "r")]).toBe(0.8);
    expect(row[col(c, "x")]).toBeCloseTo(ring.x, 9);
    expect(row[col(c, "y")]).toBeCloseTo(ring.y, 9);
    expect(row[col(c, "new")]).toBe(1);
    const solo = out.live!.nodes!.find((n) => n.id === "inc-10")!;
    expect([solo.x, solo.y]).toEqual([ring.x, ring.y]);
    const mem = out.live!.nodes!.find((n) => n.id === "inc-9")!;
    expect(mem.x).toBeCloseTo(11.88404752487764, 9);
    expect(mem.y).toBeCloseTo(-6.52850746407067, 9);
    expect(out.live!.edges).toHaveLength(1);
  });

  it("is idempotent (same delta twice equals once) and skips old seq", () => {
    const d = delta({ links: [{ source: "cmp-1", target: "cmp-7", level: "kit", pairs: 3, max_bits: 12, sum_bits: 30, new: 1 }] });
    const once = patchOverview(overview(), d);
    expect(patchOverview(once, d)).toBe(once);
    expect(patchOverview(once, d)).toEqual(patchOverview(overview(), d));
    const raw = overview();
    expect(patchOverview(raw, delta({ seq: 100 }))).toBe(raw);
  });

  it("keeps base links when the payload was built with min_bits > 0", () => {
    const raw = { ...overview(), min_bits: 5 };
    const out = patchOverview(raw, delta({ links: [{ source: "cmp-1", target: "cmp-7", level: "kit", pairs: 0, max_bits: 0, sum_bits: 0 }] }));
    expect(out.links.rows).toHaveLength(2);
  });

  it("aggregateLink returns null when no level of the filter has pairs", () => {
    expect(aggregateLink([{ source: "a", target: "b", level: "weak", pairs: 4, max_bits: 3, sum_bits: 9 }], new Set(["kit"]))).toBeNull();
  });
});

describe("isGap", () => {
  it("detects missed deltas between the cache and a coalesced message", () => {
    const live = { run_id: RUN, version: 10, seq: 100 };
    expect(isGap(live, { v: 11, n: 1, seq: 120 })).toBe(false);
    expect(isGap(live, { v: 13, n: 3, seq: 120 })).toBe(false);
    expect(isGap(live, { v: 13, n: 1, seq: 120 })).toBe(true);
    expect(isGap(live, { v: 13, n: 1, seq: 90 })).toBe(false);
    expect(isGap(undefined, { v: 13, n: 1, seq: 120 })).toBe(false);
  });
});

// ------------------------------------------------------------------ cache application

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, queryKeyHashFn: runAwareQueryKeyHash, staleTime: Infinity } } });
}

describe("applyDelta", () => {
  it("patches dashboard, campaigns and raw overview caches, leaves other runs alone", () => {
    const client = makeClient();
    client.setQueryData(["task", "dashboard"], dashboard());
    client.setQueryData(["task", "campaigns"], { items: [{ id: "cmp-1", label: "C1", members: [], core_genome: [], confidence: 1, status: "x", size: 2 }], total: 1, limit: 2000, offset: 0, run_id: RUN });
    client.setQueryData(["graph2", "overview"], overview());
    client.setQueryData(["graph2", "overview-other"], { ...overview(), run_id: "analyse-2", live: { run_id: "analyse-2", version: 1, seq: 1 } });
    expect(applyDelta(client, delta())).toBe(3);
    expect(client.getQueryData<DashboardData>(["task", "dashboard"])!.kpis.incidents).toBe(3);
    expect(client.getQueryData<CampaignPage>(["task", "campaigns"])!.items[0]!.new).toBe(1);
    expect(client.getQueryData<RawOverview>(["graph2", "overview"])!.live!.nodes).toHaveLength(1);
    expect(client.getQueryData<RawOverview>(["graph2", "overview-other"])!.live!.seq).toBe(1);
    expect(applyDelta(client, delta())).toBe(0);
  });

  it("patches the GRAPH-2 normalised overview cache through its raw payload (merge-batch4)", () => {
    const client = makeClient();
    const raw = overview();
    const key = ["task", "graph2", "overview", RUN, "auto"];
    client.setQueryData(key, { source: { kind: "lod" }, overview: { ...normOverview(raw, "lod"), raw } });
    expect(applyDelta(client, delta())).toBe(1);
    const got = client.getQueryData<{ overview: Overview & { raw: RawOverview } }>(key)!.overview;
    const expected = patchOverview(raw, delta());
    expect(got.raw).toEqual(expected);
    expect(got.campaigns).toEqual(normOverview(expected, "lod").campaigns);
    expect(applyDelta(client, delta())).toBe(0);
  });

  it("refetches a cache that missed deltas instead of patching it", () => {
    const client = makeClient();
    client.setQueryData(["task", "dashboard"], dashboard());
    const spy = vi.spyOn(client, "invalidateQueries");
    expect(applyDelta(client, delta({ v: 15, n: 1 }))).toBe(0);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(client.getQueryData<DashboardData>(["task", "dashboard"])!.live!.seq).toBe(100);
  });
});

// ------------------------------------------------------------------ status store

describe("liveStatus", () => {
  afterEach(() => liveStatus.set("off"));
  it("notifies subscribers on change only", () => {
    const fn = vi.fn();
    const off = liveStatus.subscribe(fn);
    liveStatus.set("connecting");
    liveStatus.set("connecting");
    liveStatus.set("live");
    expect(fn).toHaveBeenCalledTimes(2);
    expect(liveStatus.get()).toBe("live");
    off();
    liveStatus.set("off");
    expect(fn).toHaveBeenCalledTimes(2);
  });
});

// ------------------------------------------------------------------ LiveStream with a fake EventSource

class FakeES {
  static instances: FakeES[] = [];
  url: string;
  readyState = 0;
  closed = false;
  onerror: ((ev: Event) => unknown) | null = null;
  onopen: ((ev: Event) => unknown) | null = null;
  private listeners = new Map<string, ((ev: MessageEvent) => void)[]>();
  constructor(url: string) {
    this.url = url;
    FakeES.instances.push(this);
  }
  addEventListener(name: string, fn: (ev: MessageEvent) => void) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), fn]);
  }
  close() {
    this.closed = true;
    this.readyState = 2;
  }
  emit(name: string, data: unknown, id = "") {
    const ev = new MessageEvent(name, { data: JSON.stringify(data), lastEventId: id });
    for (const fn of this.listeners.get(name) ?? []) fn(ev);
  }
  error() {
    this.onerror?.(new Event("error"));
  }
  static last() {
    return FakeES.instances.at(-1)!;
  }
}

describe("LiveStream", () => {
  let now = 0;
  beforeEach(() => {
    vi.useFakeTimers();
    FakeES.instances = [];
    now = 1_000_000;
    resetPatchStats();
  });
  afterEach(() => {
    vi.useRealTimers();
    liveStatus.set("off");
    runSelection.set(null);
  });

  const make = (client = makeClient(), runId: string | null = null) =>
    new LiveStream({ client, runId, EventSourceImpl: FakeES as unknown as EventSourceCtor, random: () => 0, now: () => now });

  it("connects, goes live on hello and patches caches on delta", () => {
    const client = makeClient();
    client.setQueryData(["task", "dashboard"], dashboard());
    const s = make(client);
    s.start();
    expect(liveStatus.get()).toBe("connecting");
    expect(FakeES.last().url).toBe("/api/v1/stream");
    FakeES.last().emit("hello", { run_id: RUN, version: 10, seq: 100, resumed: false, replayed: 0 });
    expect(liveStatus.get()).toBe("live");
    const inv = vi.spyOn(client, "invalidateQueries");
    FakeES.last().emit("delta", delta(), "120");
    expect(client.getQueryData<DashboardData>(["task", "dashboard"])!.live!.seq).toBe(120);
    expect(inv).toHaveBeenCalledWith({ queryKey: ["task", "flow"] });
    expect(inv).toHaveBeenCalledWith({ queryKey: ["task", "analytics"] });
    expect(getPatchLatencies()).toHaveLength(1);
    expect((window as unknown as { __hydraLive: { deltas: number; lastSeq: number } }).__hydraLive).toMatchObject({ deltas: 1, lastSeq: 120 });
    s.stop();
    expect(FakeES.last().closed).toBe(true);
    expect(liveStatus.get()).toBe("off");
  });

  it("uses the selected run and passes last_event_id on its own reconnects with backoff", () => {
    runSelection.set(RUN);
    expect(streamUrl(null)).toBe(`/api/v1/stream?run_id=${RUN}`);
    const s = make(makeClient(), RUN);
    s.start();
    FakeES.last().emit("hello", { run_id: RUN, version: 10, seq: 100, resumed: false });
    FakeES.last().emit("delta", delta(), "120");
    FakeES.last().error();
    expect(liveStatus.get()).toBe("connecting");
    expect(FakeES.instances).toHaveLength(1);
    vi.advanceTimersByTime(999);
    expect(FakeES.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeES.instances).toHaveLength(2);
    expect(FakeES.last().url).toBe(`/api/v1/stream?last_event_id=120&run_id=${RUN}`);
    // second failure: 2 s
    FakeES.last().error();
    vi.advanceTimersByTime(1999);
    expect(FakeES.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeES.instances).toHaveLength(3);
    s.stop();
  });

  it("falls back to polling after 5 failures within 60 s and recovers on hello", () => {
    const s = make();
    s.start();
    for (let i = 0; i < 4; i++) {
      FakeES.last().error();
      expect(liveStatus.get()).toBe("connecting");
      now += 1000;
      vi.advanceTimersByTime(backoffDelay(i, () => 0));
    }
    FakeES.last().error();
    expect(liveStatus.get()).toBe("polling");
    vi.advanceTimersByTime(30_000);
    FakeES.last().emit("hello", { run_id: RUN, version: 1, seq: 1, resumed: false });
    expect(liveStatus.get()).toBe("live");
    s.stop();
  });

  it("does not fall back when failures are spread over more than 60 s", () => {
    const s = make();
    s.start();
    for (let i = 0; i < 6; i++) {
      FakeES.last().error();
      now += 20_000;
      vi.advanceTimersByTime(30_000);
    }
    expect(liveStatus.get()).toBe("connecting");
    s.stop();
  });

  it("polls without EventSource support", () => {
    const s = new LiveStream({ client: makeClient(), runId: null, EventSourceImpl: null });
    s.start();
    expect(liveStatus.get()).toBe("polling");
    s.stop();
  });

  it("reset invalidates run-scoped queries; new_run on latest reopens without last_event_id", () => {
    const client = makeClient();
    client.setQueryData(["task", "dashboard"], dashboard());
    client.setQueryData(["task", "status"], { ok: true });
    const s = make(client);
    s.start();
    FakeES.last().emit("hello", { run_id: RUN, version: 10, seq: 100, resumed: false });
    FakeES.last().emit("reset", { reason: "rebuilt", run_id: RUN, latest_run: RUN }, "130");
    expect(client.getQueryCache().find({ queryKey: ["task", "dashboard"] })!.state.isInvalidated).toBe(true);
    expect(client.getQueryCache().find({ queryKey: ["task", "status"] })!.state.isInvalidated).toBe(false);
    expect(FakeES.instances).toHaveLength(1);
    FakeES.last().emit("reset", { reason: "new_run", run_id: RUN, latest_run: "analyse-2" }, "140");
    expect(FakeES.instances).toHaveLength(2);
    expect(FakeES.instances[0]!.closed).toBe(true);
    expect(FakeES.last().url).toBe("/api/v1/stream");
    s.stop();
  });

  it("job events invalidate the job query and patch the dashboard job row", () => {
    const client = makeClient();
    client.setQueryData(["task", "dashboard"], dashboard());
    const s = make(client);
    s.start();
    const inv = vi.spyOn(client, "invalidateQueries");
    FakeES.last().emit("job", { id: "job-1", kind: "assign", status: "done", total: 3, done: 3, failed: 0 });
    expect(inv).toHaveBeenCalledWith({ queryKey: ["jobs", "job-1"] });
    expect(client.getQueryData<DashboardData>(["task", "dashboard"])!.jobs[0]!.status).toBe("done");
    s.stop();
  });

  it("refetches caches behind the stream on a fresh hello", () => {
    const client = makeClient();
    client.setQueryData(["task", "dashboard"], dashboard());
    const s = make(client);
    s.start();
    FakeES.last().emit("hello", { run_id: RUN, version: 12, seq: 150, resumed: false });
    expect(client.getQueryCache().find({ queryKey: ["task", "dashboard"] })!.state.isInvalidated).toBe(true);
    s.stop();
  });

  it("backoff grows from 1 s to 30 s with bounded jitter", () => {
    expect(backoffDelay(0, () => 0)).toBe(1000);
    expect(backoffDelay(0, () => 1)).toBe(1250);
    expect(backoffDelay(3, () => 0)).toBe(8000);
    expect(backoffDelay(10, () => 1)).toBe(30_000);
  });
});

describe("dashboard polling", () => {
  afterEach(() => liveStatus.set("off"));
  it("stops while the stream is live and polls every 15 s otherwise", () => {
    const query = { state: { data: dashboard() } } as unknown as Parameters<typeof dashboardInterval>[0];
    liveStatus.set("polling");
    expect(dashboardInterval(query)).toBe(15_000);
    liveStatus.set("live");
    expect(dashboardInterval(query)).toBe(false);
  });
});
