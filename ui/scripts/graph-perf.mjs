#!/usr/bin/env node
/**
 * Graph page ("/graph") load-time and runtime-smoothness measurement (Playwright, headless Chromium).
 *
 *   node ui/scripts/graph-perf.mjs --url http://127.0.0.1:4810/graph[?view=incidents] --out <dir> --label <name>
 *        [--theme light|dark] [--pan-seconds 4] [--marks] [--run-id <analyse-run-id>]
 *        [--timeout 180] [--settle-ms 1500] [--channel chromium|chromium-headless-shell]
 *
 * Writes <out>/<label>.json (also printed), <out>/<label>.png (after load, before interaction) and
 * <out>/<label>-after.png (after the scripted interaction). No package.json dependency: Playwright is
 * resolved from $PLAYWRIGHT_MODULE or the cached npx copy.
 *
 * First meaningful paint:
 *   - "mark": performance mark "hydra:graph:first-paint" (emitted by the new explorer), its startTime;
 *   - "classic" fallback: `:is([data-testid=graph-explorer],[data-testid=constellation]) canvas` present AND network idle (no request in flight
 *     for 500 ms); reported time = max(first canvas seen, last resource responseEnd before idle), both relative
 *     to navigationStart (performance.now() time origin).
 * Long tasks (buffered PerformanceObserver) are split by start time: until_first_paint, load (navigation ..
 * interaction start, includes --settle-ms) and interaction (the --pan-seconds window). FPS comes from a rAF
 * counter during the interaction: fps_avg, frame_ms_p95 (fps_p5 = 1000 / frame_ms_p95), frame_ms_max.
 * The FPS numbers depend on the WebGL renderer, so the JSON records it (webgl_renderer).
 * --run-id appends run_id=<id> to run-scoped GET /api/v1/* requests that have none (Playwright route, same prefix
 * list as the top-bar run picker), so the page shows a chosen analysis run without clicking the picker (the API
 * otherwise picks the newest analyse run).
 */
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import os from "node:os";

const require = createRequire(import.meta.url);
const PW = process.env.PLAYWRIGHT_MODULE || "playwright";
const { chromium } = require(PW);

function parseArgs(argv) {
  const a = { theme: "light", panSeconds: 4, marks: false, timeout: 180, settleMs: 1500, channel: "chromium", runId: null };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const v = () => argv[++i];
    if (k === "--url") a.url = v();
    else if (k === "--out") a.out = v();
    else if (k === "--label") a.label = v();
    else if (k === "--theme") a.theme = v();
    else if (k === "--pan-seconds") a.panSeconds = Number(v());
    else if (k === "--marks") a.marks = true;
    else if (k === "--run-id") a.runId = v();
    else if (k === "--timeout") a.timeout = Number(v());
    else if (k === "--settle-ms") a.settleMs = Number(v());
    else if (k === "--channel") a.channel = v();
    else throw new Error(`unknown argument ${k}`);
  }
  if (!a.url || !a.out || !a.label) throw new Error("usage: --url <url> --out <dir> --label <name> [--theme light|dark] [--pan-seconds 4] [--marks] [--run-id <id>]");
  if (!["light", "dark"].includes(a.theme)) throw new Error("--theme must be light or dark");
  return a;
}

// Same prefixes as ui/src/lib/runSelection.ts (the top-bar run picker), so --run-id behaves like picking the run.
const RUN_SCOPED = ["/v1/campaigns", "/v1/graph", "/v1/pairs", "/v1/incidents", "/v1/assignments", "/v1/dashboard",
  "/v1/simulation", "/v1/analytics/overview", "/v1/campaign-groups", "/v1/runs/latest"];

const r1 = (x) => (x == null || Number.isNaN(x) ? null : Math.round(x * 10) / 10);
const pct = (sorted, p) => (sorted.length ? sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))] : null);

/** In-page instrumentation, installed before any page script runs. */
function initScript({ theme }) {
  try { localStorage.setItem("hydra.ui.theme", JSON.stringify(theme)); } catch { /* storage blocked */ }
  const P = (window.__hydraPerf = { longtasks: [], canvasAt: null, frames: null, interactionStart: null, interactionEnd: null });
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) P.longtasks.push({ start: e.startTime, dur: e.duration });
    }).observe({ type: "longtask", buffered: true });
  } catch { P.longtaskUnsupported = true; }
  const check = () => {
    if (P.canvasAt == null && document.querySelector(":is([data-testid=graph-explorer],[data-testid=constellation]) canvas")) {
      P.canvasAt = performance.now();
      mo.disconnect();
    }
  };
  const mo = new MutationObserver(check);
  const start = () => { mo.observe(document.documentElement, { childList: true, subtree: true }); check(); };
  if (document.documentElement) start(); else document.addEventListener("DOMContentLoaded", start);
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const outDir = resolve(args.out);
  mkdirSync(outDir, { recursive: true });

  const browser = await chromium.launch({ headless: true, channel: args.channel, args: ["--enable-precise-memory-info"] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1, colorScheme: args.theme });
  await context.addInitScript(initScript, { theme: args.theme });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Performance.enable");

  // --- network: CDP wire bytes for /api/v1/graph*, in-flight tracking for "network idle"
  const cdpReq = new Map();
  cdp.on("Network.requestWillBeSent", (e) => cdpReq.set(e.requestId, { url: e.request.url, t0: e.timestamp, method: e.request.method }));
  cdp.on("Network.responseReceived", (e) => { const r = cdpReq.get(e.requestId); if (r) { r.status = e.response.status; r.tResp = e.timestamp; } });
  cdp.on("Network.loadingFinished", (e) => { const r = cdpReq.get(e.requestId); if (r) { r.t1 = e.timestamp; r.encoded = e.encodedDataLength; } });
  cdp.on("Network.loadingFailed", (e) => { const r = cdpReq.get(e.requestId); if (r) { r.t1 = e.timestamp; r.failed = e.errorText; } });

  let inflight = 0;
  let lastNetChange = Date.now();
  const ignored = (req) => ["eventsource", "websocket"].includes(req.resourceType());
  page.on("request", (req) => { if (!ignored(req)) { inflight++; lastNetChange = Date.now(); } });
  const done = (req) => { if (!ignored(req)) { inflight = Math.max(0, inflight - 1); lastNetChange = Date.now(); } };
  page.on("requestfinished", done);
  page.on("requestfailed", done);

  const consoleErrors = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text().slice(0, 300)); });
  page.on("pageerror", (e) => consoleErrors.push(`pageerror: ${String(e.message).slice(0, 300)}`));

  let rewritten = 0;
  if (args.runId) {
    await page.route(/\/api\/v1\//, (route) => {
      const req = route.request();
      const u = new URL(req.url());
      const path = u.pathname.replace(/^\/api/, "");
      if (req.method() !== "GET" || u.searchParams.has("run_id") || u.searchParams.get("kind") === "eval"
        || !RUN_SCOPED.some((p) => path.startsWith(p))) return route.continue();
      u.searchParams.set("run_id", args.runId);
      rewritten++;
      return route.continue({ url: u.toString() });
    });
  }

  // --- load
  const wallStart = Date.now();
  await page.goto(args.url, { waitUntil: "commit", timeout: args.timeout * 1000 });
  const deadline = wallStart + args.timeout * 1000;
  let criterion = null;
  let idleSince = null;
  while (Date.now() < deadline) {
    const st = await page.evaluate(() => ({
      mark: performance.getEntriesByName("hydra:graph:first-paint").length > 0,
      canvas: window.__hydraPerf?.canvasAt != null,
    })).catch(() => ({ mark: false, canvas: false }));
    if (st.mark) { criterion = "mark"; break; }
    const quiet = inflight === 0 && Date.now() - lastNetChange >= 500;
    if (st.canvas && quiet) {
      idleSince ??= Date.now();
      // Give a late "hydra:graph:first-paint" mark a brief chance before falling back.
      if (Date.now() - idleSince >= 300) { criterion = "classic"; break; }
    } else idleSince = null;
    await new Promise((r) => setTimeout(r, 50));
  }
  const wallObserved = Date.now() - wallStart;

  const load = await page.evaluate(() => {
    const P = window.__hydraPerf;
    const nav = performance.getEntriesByType("navigation")[0];
    const res = performance.getEntriesByType("resource");
    const mark = performance.getEntriesByName("hydra:graph:first-paint")[0];
    return {
      markAt: mark ? mark.startTime : null,
      canvasAt: P.canvasAt,
      lastResponseEnd: res.reduce((m, r) => Math.max(m, r.responseEnd), 0),
      domContentLoaded: nav ? nav.domContentLoadedEventEnd : null,
      loadEvent: nav ? nav.loadEventEnd : null,
      graph: res.filter((r) => /\/api\/v1\/graph/.test(r.name)).map((r) => ({
        url: r.name, start: r.startTime, ttfb: r.responseStart, end: r.responseEnd, duration: r.duration,
        transfer: r.transferSize, encoded: r.encodedBodySize, decoded: r.decodedBodySize,
      })),
      webgl: (() => {
        try {
          const gl = document.createElement("canvas").getContext("webgl2") || document.createElement("canvas").getContext("webgl");
          const ext = gl && gl.getExtension("WEBGL_debug_renderer_info");
          return gl ? (ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER)) : null;
        } catch { return null; }
      })(),
    };
  });
  let firstPaint = null;
  if (criterion === "mark") firstPaint = load.markAt;
  else if (criterion === "classic") firstPaint = Math.max(load.canvasAt, load.lastResponseEnd);

  const heap = async () => {
    const m = await cdp.send("Performance.getMetrics");
    const g = (n) => m.metrics.find((x) => x.name === n)?.value ?? null;
    const pm = await page.evaluate(() => (performance.memory ? { used: performance.memory.usedJSHeapSize, total: performance.memory.totalJSHeapSize } : null));
    return { cdp_js_heap_used_mb: r1(g("JSHeapUsedSize") / 2 ** 20), cdp_js_heap_total_mb: r1(g("JSHeapTotalSize") / 2 ** 20),
      perf_memory_used_mb: pm ? r1(pm.used / 2 ** 20) : null, perf_memory_total_mb: pm ? r1(pm.total / 2 ** 20) : null, dom_nodes: g("Nodes") };
  };

  await page.waitForTimeout(args.settleMs);
  const heapAfterLoad = await heap();
  await page.screenshot({ path: join(outDir, `${args.label}.png`), fullPage: true });

  // --- interaction: wheel zoom in/out + drag pans over the canvas centre while a rAF counter runs
  const urlBefore = page.url();
  const box = await page.evaluate(() => {
    const cs = [...document.querySelectorAll(":is([data-testid=graph-explorer],[data-testid=constellation]) canvas")];
    let best = null;
    for (const c of cs) { const b = c.getBoundingClientRect(); if (!best || b.width * b.height > best.width * best.height) best = { x: b.x, y: b.y, width: b.width, height: b.height }; }
    return best;
  });
  let interaction = { skipped: "no canvas" };
  if (box && box.width > 0 && args.panSeconds > 0) {
    await page.evaluate(() => {
      const P = window.__hydraPerf;
      P.interactionStart = performance.now();
      P.frames = [];
      const tick = (t) => { if (P.frames) { P.frames.push(t); requestAnimationFrame(tick); } };
      requestAnimationFrame(tick);
    });
    const cx = box.x + box.width / 2;
    const cy = box.y + box.height / 2;
    const tEnd = Date.now() + args.panSeconds * 1000;
    let cycles = 0;
    while (Date.now() < tEnd) {
      await page.mouse.move(cx, cy);
      for (let i = 0; i < 3 && Date.now() < tEnd; i++) { await page.mouse.wheel(0, -240); await page.waitForTimeout(60); }
      for (let i = 0; i < 3 && Date.now() < tEnd; i++) { await page.mouse.wheel(0, 240); await page.waitForTimeout(60); }
      if (Date.now() >= tEnd) break;
      const dx = cycles % 2 ? -160 : 160;
      const dy = cycles % 2 ? -90 : 90;
      await page.mouse.move(cx, cy);
      await page.mouse.down();
      for (let s = 1; s <= 12; s++) { await page.mouse.move(cx + (dx * s) / 12, cy + (dy * s) / 12); await page.waitForTimeout(16); }
      await page.mouse.up();
      cycles++;
    }
    const fr = await page.evaluate(() => {
      const P = window.__hydraPerf;
      const f = P.frames;
      P.frames = null;
      P.interactionEnd = performance.now();
      return { frames: f, start: P.interactionStart, end: P.interactionEnd };
    });
    const gaps = [];
    for (let i = 1; i < fr.frames.length; i++) gaps.push(fr.frames[i] - fr.frames[i - 1]);
    const sorted = [...gaps].sort((x, y) => x - y);
    const span = fr.frames.length > 1 ? fr.frames[fr.frames.length - 1] - fr.frames[0] : null;
    const p95 = pct(sorted, 95);
    interaction = {
      seconds: r1((fr.end - fr.start) / 1000), cycles, frames: fr.frames.length,
      fps_avg: span ? r1(((fr.frames.length - 1) / span) * 1000) : null,
      frame_ms_p50: r1(pct(sorted, 50)), frame_ms_p95: r1(p95), fps_p5: p95 ? r1(1000 / p95) : null,
      frame_ms_max: r1(sorted[sorted.length - 1] ?? null),
      frames_over_50ms: gaps.filter((g) => g > 50).length,
      max_gap_at_ms: gaps.length ? r1(fr.frames[gaps.indexOf(sorted[sorted.length - 1]) + 1] - fr.start) : null,
      url_changed: page.url() !== urlBefore ? page.url() : false,
    };
  }
  const heapAfterInteraction = await heap();
  await page.screenshot({ path: join(outDir, `${args.label}-after.png`), fullPage: true });

  await page.waitForTimeout(250); // let pending PerformanceObserver callbacks deliver late long-task entries
  const final = await page.evaluate((wantMarks) => ({
    longtasks: window.__hydraPerf.longtasks,
    interactionStart: window.__hydraPerf.interactionStart, interactionEnd: window.__hydraPerf.interactionEnd,
    longtaskUnsupported: !!window.__hydraPerf.longtaskUnsupported,
    marks: wantMarks ? performance.getEntries().filter((e) => (e.entryType === "mark" || e.entryType === "measure") && e.name.startsWith("hydra:"))
      .map((e) => ({ name: e.name, type: e.entryType, start: Math.round(e.startTime * 10) / 10, duration: Math.round(e.duration * 10) / 10 })) : undefined,
  }), args.marks);

  // Phases by timestamp (observer callbacks arrive late): load = navigation .. interaction start,
  // interaction = the scripted pan/zoom window, until_first_paint = navigation .. first paint.
  const iS = final.interactionStart ?? Infinity;
  const iE = final.interactionEnd ?? Infinity;
  const inPhase = { load: (t) => t.start < iS, interaction: (t) => t.start >= iS && t.start <= iE,
    until_first_paint: (t) => firstPaint != null && t.start < firstPaint };
  const ltStats = (phase) => {
    const xs = final.longtasks.filter(inPhase[phase]);
    return { count: xs.length, total_ms: r1(xs.reduce((s, t) => s + t.dur, 0)), max_ms: r1(xs.reduce((m, t) => Math.max(m, t.dur), 0)) };
  };

  // Merge Resource Timing (page clock) with CDP wire bytes, matching by URL without run_id, in order.
  const norm = (u) => { const x = new URL(u); x.searchParams.delete("run_id"); return x.pathname + x.search; };
  const cdpGraph = [...cdpReq.values()].filter((r) => /\/api\/v1\/graph/.test(r.url));
  const used = new Set();
  const graph = load.graph.map((g) => {
    const c = cdpGraph.find((r) => !used.has(r) && norm(r.url) === norm(g.url));
    if (c) used.add(c);
    return {
      path: norm(g.url), status: c?.status ?? null, start_ms: r1(g.start), ttfb_ms: r1(g.ttfb), end_ms: r1(g.end), duration_ms: r1(g.duration),
      body_bytes: g.decoded || g.encoded || null, wire_bytes: c?.encoded ?? g.transfer ?? null,
    };
  });
  // Graph requests issued after load (e.g. during interaction) are only known to CDP.
  const late = cdpGraph.filter((r) => !used.has(r)).map((r) => ({ path: norm(r.url), status: r.status ?? null, duration_ms: r.t1 ? r1((r.t1 - r.t0) * 1000) : null, wire_bytes: r.encoded ?? null, failed: r.failed }));
  const slowest = graph.reduce((m, g) => (!m || g.duration_ms > m.duration_ms ? g : m), null);

  const result = {
    label: args.label, url: args.url, theme: args.theme, run_id_override: args.runId, run_id_rewrites: rewritten,
    measured_at: new Date().toISOString(), host: os.hostname(), loadavg: os.loadavg().map((x) => r1(x)),
    browser: `${args.channel} ${browser.version()}`, webgl_renderer: load.webgl, viewport: "1440x900@1",
    first_paint: { criterion: criterion ?? "timeout", ms: r1(firstPaint), canvas_seen_ms: r1(load.canvasAt), mark_ms: r1(load.markAt),
      last_response_end_ms: r1(load.lastResponseEnd), dom_content_loaded_ms: r1(load.domContentLoaded), wall_observed_ms: wallObserved },
    graph_api: {
      requests: graph, late_requests: late, count: graph.length,
      total_body_bytes: graph.reduce((s, g) => s + (g.body_bytes || 0), 0), total_wire_bytes: graph.reduce((s, g) => s + (g.wire_bytes || 0), 0),
      slowest: slowest ? { path: slowest.path, duration_ms: slowest.duration_ms, end_ms: slowest.end_ms, body_bytes: slowest.body_bytes } : null,
    },
    longtasks: { until_first_paint: ltStats("until_first_paint"), load: ltStats("load"), interaction: ltStats("interaction"), unsupported: final.longtaskUnsupported },
    interaction,
    heap: { after_load: heapAfterLoad, after_interaction: heapAfterInteraction },
    console_errors: consoleErrors.slice(0, 20), console_error_count: consoleErrors.length,
    ...(args.marks ? { marks: final.marks } : {}),
  };
  writeFileSync(join(outDir, `${args.label}.json`), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
  if (!criterion) process.exitCode = 2;
}

main().catch((e) => { console.error(e); process.exit(1); });
