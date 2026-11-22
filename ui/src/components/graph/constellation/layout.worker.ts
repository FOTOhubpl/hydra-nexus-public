/// <reference lib="webworker" />
/** Web worker: constellation layout off the main thread (large runs). Messages: see layoutClient.ts. */
import { layoutConstellation } from "./geometry";
import type { LayoutReply, LayoutRequest } from "./layoutClient";

self.onmessage = (e: MessageEvent<LayoutRequest>) => {
  const t0 = performance.now();
  const r = layoutConstellation(e.data.campaigns, e.data.groups, e.data.positioned);
  const reply: LayoutReply = { id: e.data.id, campaigns: [...r.campaigns.values()], groups: [...r.groups.values()], bbox: r.bbox, scale: r.scale, ms: performance.now() - t0 };
  (self as unknown as Worker).postMessage(reply);
};
