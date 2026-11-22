/** Runs the constellation layout in a web worker when available (main-thread fallback in tests / blocked workers). */
import type { OvCampaign, OvGroup } from "../../../api/graph";
import { layoutConstellation, type Constellation, type Disc } from "./geometry";

export interface LayoutRequest { id: number; campaigns: OvCampaign[]; groups: OvGroup[]; positioned: boolean }
export interface LayoutReply { id: number; campaigns: Disc[]; groups: Disc[]; bbox: Constellation["bbox"]; scale: number | null; ms: number }
export interface LayoutResult extends Constellation { ms: number; worker: boolean }

let seq = 0;

export async function computeLayout(campaigns: OvCampaign[], groups: OvGroup[], positioned: boolean, signal?: AbortSignal): Promise<LayoutResult> {
  const t0 = performance.now();
  // API positions only need scaling, and small inputs cost less than a worker round trip: stay on the main thread
  if (typeof Worker === "undefined" || positioned || campaigns.length < 150) {
    return { ...layoutConstellation(campaigns, groups, positioned), ms: performance.now() - t0, worker: false };
  }
  const worker = new Worker(new URL("./layout.worker.ts", import.meta.url), { type: "module" });
  const id = ++seq;
  try {
    const reply = await new Promise<LayoutReply>((resolve, reject) => {
      signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")), { once: true });
      worker.onmessage = (e: MessageEvent<LayoutReply>) => {
        if (e.data.id === id) resolve(e.data);
      };
      worker.onerror = (e) => reject(new Error(e.message || "layout worker failed"));
      worker.postMessage({ id, campaigns, groups, positioned } satisfies LayoutRequest);
    });
    return {
      campaigns: new Map(reply.campaigns.map((d) => [d.id, d])),
      groups: new Map(reply.groups.map((d) => [d.id, d])),
      bbox: reply.bbox,
      scale: reply.scale,
      ms: performance.now() - t0,
      worker: true,
    };
  } catch (e) {
    if (signal?.aborted) throw e;
    // worker blocked (CSP, bundling): the same layout on the main thread
    return { ...layoutConstellation(campaigns, groups, positioned), ms: performance.now() - t0, worker: false };
  } finally {
    worker.terminate();
  }
}
