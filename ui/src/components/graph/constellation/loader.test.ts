import { describe, expect, it } from "vitest";
import { CampaignLoader } from "./loader";

interface Pending { id: string; signal: AbortSignal; resolve: (v: number[]) => void; reject: (e: unknown) => void }

function fakeFetch() {
  const pending: Pending[] = [];
  const fetch = (id: string, signal: AbortSignal) => new Promise<number[]>((resolve, reject) => pending.push({ id, signal, resolve, reject }));
  return { pending, fetch };
}
const tick = () => new Promise((r) => setTimeout(r, 0));

describe("CampaignLoader", () => {
  it("loads wanted campaigns up to the concurrency limit, highest priority first", async () => {
    const f = fakeFetch();
    const l = new CampaignLoader<number[]>({ fetch: f.fetch, concurrency: 2 });
    l.want(["a", "b", "c"]);
    expect(f.pending.map((p) => p.id)).toEqual(["a", "b"]);
    expect(l.status("a")).toBe("loading");
    expect(l.status("c")).toBe("idle");
    f.pending[0]!.resolve([1, 2]);
    await tick();
    expect(l.status("a")).toBe("ready");
    expect(l.get("a")).toEqual([1, 2]);
    expect(f.pending.map((p) => p.id)).toEqual(["a", "b", "c"]);
  });

  it("aborts requests for campaigns that are no longer wanted and ignores their late answers", async () => {
    const f = fakeFetch();
    const l = new CampaignLoader<number[]>({ fetch: f.fetch });
    l.want(["a", "b"]);
    l.want(["b"]);
    expect(f.pending[0]!.signal.aborted).toBe(true);
    expect(l.stats.cancelled).toBe(1);
    f.pending[0]!.resolve([9]);
    await tick();
    expect(l.status("a")).toBe("idle");
    expect(l.get("a")).toBeUndefined();
  });

  it("evicts least recently used campaigns above the weight budget but never wanted ones", async () => {
    const f = fakeFetch();
    const l = new CampaignLoader<number[]>({ fetch: f.fetch, weight: (v) => v.length, budget: 5 });
    l.want(["a"]);
    f.pending[0]!.resolve([1, 1, 1]);
    await tick();
    l.want(["b"]);
    f.pending[1]!.resolve([1, 1, 1]);
    await tick();
    // a (3) + b (3) > 5: a is not wanted any more and is the oldest
    expect(l.loadedIds()).toEqual(["b"]);
    expect(l.stats.evicted).toBe(1);
    l.want(["b", "c"]);
    f.pending[2]!.resolve([1, 1, 1]);
    await tick();
    // both wanted: kept over budget rather than dropping what is on screen
    expect(l.loadedIds().sort()).toEqual(["b", "c"]);
  });

  it("records errors, does not retry automatically, and retries on demand", async () => {
    const f = fakeFetch();
    const l = new CampaignLoader<number[]>({ fetch: f.fetch });
    let events = 0;
    l.subscribe(() => events++);
    l.want(["a"]);
    f.pending[0]!.reject(new Error("HTTP 500"));
    await tick();
    expect(l.status("a")).toBe("error");
    expect(l.error("a")).toBe("HTTP 500");
    l.want(["a"]);
    expect(f.pending).toHaveLength(1);
    l.retry("a");
    expect(f.pending).toHaveLength(2);
    expect(events).toBeGreaterThan(0);
  });

  it("serves a cached campaign again without a request (cache hit)", async () => {
    const f = fakeFetch();
    const l = new CampaignLoader<number[]>({ fetch: f.fetch });
    l.want(["a"]);
    f.pending[0]!.resolve([1]);
    await tick();
    l.want([]);
    l.want(["a"]);
    expect(f.pending).toHaveLength(1);
    expect(l.stats.hits).toBe(1);
  });
});
