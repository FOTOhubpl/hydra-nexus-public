import { afterEach, describe, expect, it } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import { LiveStream, liveStatus, type EventSourceCtor } from "./stream";
import { rtKeys, watchRtBus, type RtStatus, type WatchRtEvent } from "./watchRt";
import { activity } from "../lib/activity";

class FakeES {
  static instances: FakeES[] = [];
  url: string;
  readyState = 0;
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
  close() { this.readyState = 2; }
  emit(name: string, data: unknown) {
    const ev = new MessageEvent(name, { data: JSON.stringify(data) });
    for (const fn of this.listeners.get(name) ?? []) fn(ev);
  }
}

const match = (over: Record<string, unknown> = {}) => ({
  id: 7, item_id: 3, source: "urlhaus", indicator_kind: "iban", indicator_display: "PL61 1090 …", origin: "campaign",
  campaign_id: "C-12", campaign_label: "Inwestycja PKO Bonus", strength: "strong", status: "new", created_at: "2026-11-22T02:00:00Z",
  replay: true, item: { value_defanged: "hxxps://pko-bonus[.]top/x" }, ...over,
});

describe("LiveStream: realtime watch events", () => {
  afterEach(() => {
    liveStatus.set("off");
    activity.reset();
    FakeES.instances = [];
  });

  it("publishes watch.item / watch.match on the bus, pushes a labelled activity entry and patches source state", () => {
    const client = new QueryClient();
    const status: RtStatus = {
      engine: { state: "replay", replay: null },
      sources: [{ id: "urlhaus", name_pl: "URLhaus", enabled: true, state: "ok", interval_s: 300, consecutive_errors: 0 }],
      totals: { items: 0, items_24h: 0, matches: 0, matches_new: 0, watchlist: 0 },
      sparkline: [], timeline: [],
    };
    client.setQueryData(rtKeys.status, status);
    const got: WatchRtEvent[] = [];
    const off = watchRtBus.subscribe((e) => got.push(e));
    const s = new LiveStream({ client, runId: null, EventSourceImpl: FakeES as unknown as EventSourceCtor, random: () => 0 });
    s.start();
    const es = FakeES.instances.at(-1)!;
    es.emit("watch.item", { item: { id: 3, source: "urlhaus", kind: "url", value_defanged: "hxxps://pko-bonus[.]top/x", first_seen: "2026-11-22T02:00:00Z", match_count: 0, replay: true } });
    es.emit("watch.match", { match: match() });
    es.emit("watch.source_state", { source: { ...status.sources[0], state: "backoff", consecutive_errors: 2 } });
    es.emit("watch.poll", { source: "urlhaus", run_id: 1, fetched: 5, new: 1, matched: 1, replay: true });
    es.emit("watch.match", { nonsense: true });
    off();
    s.stop();

    expect(got.map((e) => e.type)).toEqual(["item", "match", "source_state", "poll"]);
    const entry = activity.get().entries[0]!;
    expect(entry.title).toBe("ODTWORZENIE · Dopasowanie watchlisty: IBAN PL61 1090 …");
    expect(entry.detail).toContain("kampania C-12");
    expect(entry.to).toBe("/watch");
    expect(activity.get().entries).toHaveLength(1);
    const patched = client.getQueryData<RtStatus>(rtKeys.status)!;
    expect(patched.sources[0]!.state).toBe("backoff");
    expect(patched.sources[0]!.consecutive_errors).toBe(2);
  });

  it("a live (non-replay) match is not labelled ODTWORZENIE and the classic watch event still works", () => {
    const client = new QueryClient();
    const s = new LiveStream({ client, runId: null, EventSourceImpl: FakeES as unknown as EventSourceCtor, random: () => 0 });
    s.start();
    const es = FakeES.instances.at(-1)!;
    es.emit("watch.match", { match: match({ replay: false, campaign_id: null, campaign_label: null, indicator_kind: "domain", indicator_display: "pko-bonus[.]top" }) });
    es.emit("watch", { host_defanged: "a[.]pl" });
    s.stop();
    const [classic, rt] = activity.get().entries;
    expect(rt!.title).toBe("Dopasowanie watchlisty: domena pko-bonus[.]top");
    expect(rt!.detail).toContain("wpis ręczny");
    expect(classic!.title).toBe("Monitoring domen: a[.]pl");
  });
});
