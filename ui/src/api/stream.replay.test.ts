/**
 * DATAFLOW: "UI patch == refetch". The fixture holds real backend output (synthetic rows): GET payloads before a
 * burst of new material, the SSE delta messages the client received, and the GET payloads after it
 * (scripts/bench_dataflow.py latency --keep). Applying the deltas to the "before" payloads must give the "after"
 * payloads. Set HYDRA_STREAM_REPLAY_DIR=<latency --out dir> to replay a server run instead (timings are printed).
 *
 * Regenerate the fixture: seed a throwaway state with tests/core/dataflow_world.py (seed 3, 24 incidents),
 * `scripts/bench_dataflow.py latency --n 15 --rate 20 --keep`, and pack before-*.json, after-*.json and the
 * deltas of client.jsonl into src/test/fixtures/stream-replay/replay.json.
 */
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import type { DashboardData } from "./task";
import { patchCampaignPage, patchDashboard, patchOverview, type CampaignPage, type RawOverview, type StreamDelta } from "./stream";

interface Snap { path: string; body: unknown }
interface Replay { before: Snap[]; after: Snap[]; deltas: StreamDelta[] }

function load(): { name: string; data: Replay } {
  const dir = process.env.HYDRA_STREAM_REPLAY_DIR;
  if (dir && existsSync(join(dir, "client.jsonl"))) {
    const snap = (tag: string) => [0, 1, 2, 3].map((i) => JSON.parse(readFileSync(join(dir, `${tag}-${i}.json`), "utf8")) as Snap);
    const deltas = readFileSync(join(dir, "client.jsonl"), "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l))
      .filter((r) => r.event === "delta" && r.delta).map((r) => r.delta as StreamDelta);
    return { name: dir, data: { before: snap("before"), after: snap("after"), deltas } };
  }
  const file = join(__dirname, "../test/fixtures/stream-replay/replay.json");
  return { name: "fixture", data: JSON.parse(readFileSync(file, "utf8")) as Replay };
}

function at(xs: Snap[], i: number): Snap {
  const v = xs[i];
  if (!v) throw new Error(`snapshot ${i} missing`);
  return v;
}

const r3 = (v: unknown) => (typeof v === "number" ? Math.round(v * 1000) / 1000 : v);
const byId = <T extends { id: string }>(xs: T[]) => new Map(xs.map((x) => [x.id, x]));

function tableRows(t: { columns: string[]; rows: unknown[][] }, key: (r: Record<string, unknown>) => string) {
  const out = new Map<string, Record<string, unknown>>();
  for (const row of t.rows) {
    const o: Record<string, unknown> = {};
    t.columns.forEach((c, i) => { o[c] = r3(row[i]); });
    out.set(key(o), o);
  }
  return out;
}

function timed<T>(xs: StreamDelta[], init: T, fn: (acc: T, d: StreamDelta) => T): { out: T; ms: number[] } {
  const ms: number[] = [];
  let acc = init;
  for (const d of xs) {
    const t0 = performance.now();
    acc = fn(acc, d);
    ms.push(performance.now() - t0);
  }
  return { out: acc, ms };
}

const pct = (xs: number[], p: number) => {
  const s = [...xs].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.round((p / 100) * (s.length - 1)))] ?? NaN;
};

const { name, data } = load();

describe(`stream replay (${name})`, () => {
  it("has deltas", () => {
    expect(data.deltas.length).toBeGreaterThan(0);
  });

  it("dashboard: patch(before, deltas) == after", () => {
    const before = at(data.before, 0).body as DashboardData;
    const after = at(data.after, 0).body as DashboardData;
    const { out, ms } = timed(data.deltas, before, patchDashboard);
    const { new_incidents: _n, ...kpis } = out.kpis as DashboardData["kpis"] & { new_incidents?: number };
    expect(kpis).toEqual(after.kpis);
    const live = (x: DashboardData) => (x as DashboardData & { live?: { seq: number; version: number; new_incidents: number } }).live;
    expect(live(out)?.seq).toBe(live(after)?.seq);
    expect(live(out)?.version).toBe(live(after)?.version);
    expect(live(out)?.new_incidents).toBe(live(after)?.new_incidents);
    const fresh = (x: DashboardData) => x.previews.filter((p) => (p as { new?: boolean }).new)
      .map((p) => [p.id, p.campaign_id, (p as { decision?: string }).decision]).sort();
    expect(fresh(out)).toEqual(fresh(after));
    const camps = (x: DashboardData) => [...byId(x.campaigns).values()].map((c) => [c.id, c.size, (c as { new?: number }).new ?? 0,
      c.members.map((m) => m.id).sort()]).sort();
    expect(camps(out)).toEqual(camps(after));
    console.info(`dashboard patch ms p50 ${pct(ms, 50).toFixed(3)} p95 ${pct(ms, 95).toFixed(3)} (${ms.length} messages)`);
  });

  it("campaign list: patch(before, deltas) == after", () => {
    const { out } = timed(data.deltas, at(data.before, 1).body as CampaignPage, patchCampaignPage);
    const after = at(data.after, 1).body as CampaignPage;
    const rows = (p: CampaignPage) => p.items.map((c) => [c.id, (c as { size?: number }).size, (c as { new?: number }).new ?? 0]).sort();
    expect(rows(out)).toEqual(rows(after));
  });

  for (const i of [2, 3]) {
    it(`overview ${at(data.before, i).path}: patch(before, deltas) == after`, () => {
      const before = at(data.before, i).body as RawOverview;
      const after = at(data.after, i).body as RawOverview;
      const { out, ms } = timed(data.deltas, before, patchOverview);
      const camps = (x: RawOverview) => tableRows(x.campaigns, (o) => String(o.id));
      expect(camps(out)).toEqual(camps(after));
      const links = (x: RawOverview) => tableRows(x.links, (o) => [o.source, o.target].sort().join("|"));
      expect(links(out)).toEqual(links(after));
      const nodes = (x: RawOverview) => (x.live?.nodes ?? []).map((n) => [n.id, n.map_campaign, r3(n.x), r3(n.y)]).sort();
      expect(nodes(out)).toEqual(nodes(after));
      const edges = (x: RawOverview) => (x.live?.edges ?? []).map((e) => [e.source, e.target].sort().join("|")).sort();
      expect(edges(out)).toEqual(edges(after));
      console.info(`overview patch ms p50 ${pct(ms, 50).toFixed(3)} p95 ${pct(ms, 95).toFixed(3)} (${ms.length} messages)`);
    });
  }
});
