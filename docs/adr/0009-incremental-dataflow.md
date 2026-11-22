# ADR-0009: Incremental, event-driven per-run aggregates and a live stream

Status: Proposed (DATAFLOW, 2026-11-21). Migration v5 is part of 1.0. Builds on ADR-002 (single Writer) and on the API-PERF response cache
(`hydra/api_cache.py`, `docs/ui/GRAPH_API.md`).

## Context

- Operator requirement (2026-11-21): "dane muszą się agregować, ładować realtime — opracuj bardzo zoptymalizowane
  potoki danych".
- Before API-PERF every view recomputed its aggregates from all pairs and genes of a run (dashboard 5.9 s, flow
  8.4 s, graph 23 s on 837 incidents). API-PERF caches per run, keyed by a run fingerprint that includes the assign
  runs attached to the run, so every new assignment invalidates the cache and the next request recomputes:
  measured on a copy of state-int with the API-PERF code, the first request after one new assignment took dashboard
  0.63 s, flow 5.06 s, overview 2.29 s (all `miss`), and the UI saw the material only on its next 15 s poll.
- Runs are immutable snapshots. What changes later is attached to a run: assign runs (`params.base_run`; upload,
  `hydra assign-batch`, scheduler inbox), analyst reviews (pair decisions, unsure marks), exports, HYDRA Watch
  alerts (in memory, `hydra/watch/engine.py`).

## Decision

1. **Base and deltas.** An analyse run is the *base*. Material attached after it is a *delta*, marked
   "nowe od analizy" until the next full analyse (a new base). The base parts of the dashboard, flow and graph
   are cached once with a base-only fingerprint (`api_cache.run_fingerprint(..., base_only=True)`); new material
   never invalidates them.
2. **Materialised aggregates (migration v5, additive, derived only).** `agg_state` (status, `version`, `seq`,
   `base_seq`), `agg_counts` (dimension, key, `[n, n_new]`: corpus, source, day, role, gene family/status, tier,
   decision, group, review state, edge level, export kind, ...), `agg_incidents` (per-incident contribution and the
   current decision; `is_new` = delta layer), `agg_genes` (document frequency per distinct gene value, for tiers
   and top shared genes), `agg_pairs` (non-independent pairs with map campaigns and review state),
   `agg_campaigns` (disc size, new members, first/last seen, internal links), `agg_links` (campaign-to-campaign
   links per level), `agg_deltas` (the delta messages, keyed by `events.seq`). All rows can be rebuilt from the v2
   tables (`python -m hydra.dataflow rebuild|verify|status`).
3. **Writer-side deltas in the event's transaction.** `hydra.events.append` dispatches `RUN_FINISHED`, `REVIEW`,
   `REVIEW_UNSURE(_UNDONE)` and `EXPORT` to `hydra.dataflow.on_event` inside the same writer transaction, so the
   state change, its audit event and the aggregate delta commit together. Fresh material is applied as increments
   (counters, document frequencies, disc sizes, link count/sum/max); a removal (a re-decision of new material, a
   review that drops a link) recomputes only the touched keys (one campaign disc, one campaign pair). The tap runs
   under a savepoint: a failure never fails the business write, it marks the run `stale` and the next reader
   rebuilds it. `HYDRA_DATAFLOW=0` switches the taps and overlays off (API-PERF behaviour).
4. **Base build off the writer.** At the end of `pipeline.analyse` and of `review.apply_review` (and on first use
   of an older run), `dataflow.build` computes the base on a read-only connection in the caller's thread and
   installs it in one writer transaction that also catches up with every existing delta through the same delta
   functions. The writer is held only for the bulk insert (1.36 s for the 837-incident run).
5. **Live current view.** `/v1/dashboard`, `/v1/system/flow`, `/v1/graph/overview`, `/v1/campaigns` and the
   incident-level graph endpoints overlay the delta layer on the cached base (`dataflow.dashboard_view`,
   `flow_counts`, `overview_view`, `campaigns_page_view`, `live_graph`), carry a `live` block (`version`, `seq`,
   `new_incidents`, `by_decision`) and mark new material with `new`. Positions of new nodes follow a fixed rule
   (FNV-1a placement on the campaign disc or on a ring outside the base layout), the same in Python and TypeScript.
6. **Push.** `GET /v1/stream` (Server-Sent Events, `hydra/api_stream.py`): one pump per process woken by a
   post-commit listener of the Writer (`Writer.add_listener`, O(1)) and by a 1 s poll, reading `agg_deltas` on a
   read-only connection in a worker thread; per-client bounded queues; 250 ms coalescing window; 256 KB message cap;
   heartbeats every 15 s; resume from `Last-Event-ID` = `events.seq`; `reset` on gap, rebuild, new run, overflow or
   an oversized delta; job progress and Watch alerts as ephemeral events. Local-only guard as everywhere, plus a
   refusal of non-local `Origin`. Contract: `docs/ui/STREAM_API.md`.
7. **UI.** `ui/src/api/stream.ts` patches react-query caches in place (dashboard, campaign list, raw overview
   payloads), invalidates flow/analytics (cheap now), reconnects with backoff and falls back to 15 s polling; the
   status bar shows "na żywo" / "łączenie…" / "odświeżanie co 15 s".

## Correctness

- Property test (`tests/core/test_dataflow.py`, hypothesis + fixed seeds): after random sequences of new
  material, re-decisions of new and base incidents, reviews, undos, unsure marks and exports — written through
  `pipeline._write_run`, the review rows/events and `record_export` — the materialised tables equal
  `dataflow.recompute` (every aggregate from scratch), after every step, after a late build (catch-up) and after
  a rebuild. It found two real defects during development (group sizes counted twice within one change; link
  maxima not floored at 0 as the overview does).
- Patch == refetch (`ui/src/api/stream.replay.test.ts`): GET payloads before a burst, the SSE messages received,
  and the GET payloads after it, all real backend output on synthetic rows; applying the messages with the UI
  patch functions gives the "after" payloads (dashboard KPIs, previews, campaign sizes, overview campaign rows,
  links, node positions, edges).
- SSE tests against a real uvicorn server (`tests/ui_api/test_stream.py`): coalescing, ids = seq, resume, gap after
  a rebuild, reviews/exports/jobs, heartbeat, Host/Origin guard, new-run reset.

## Measurements (2026-11-21, server copies of state-int, run `analyse-20261121T164214-b8633e`, 837 incidents)

Commands: `scripts/bench_dataflow.py scenario|latency` (fresh process, `HYDRA_API_WARM=0`, `taskset -c 0-23`,
host load average 60-80 on 128 cores); full table in internal notes, not published (2026-11-21 21:40).

| Measure | Before (API-PERF code) | After |
|---|---|---|
| First request after 1 new assignment: dashboard / flow / overview | 0.63 / 5.06 / 2.29 s (recompute) | 0.034 / 0.021 / 0.072 s |
| Same after 10 more | 0.53 / 5.34 / 3.26 s | 0.031 / 0.022 / 0.21 s |
| Cold (fresh process, empty API cache): dashboard / flow / overview | 1.45 / 3.85 / 1.95 s | 2.14 / 3.56 / 1.84 s (aggregates already built) |
| New material visible in the UI | next 15 s poll + recompute | SSE p50 0.159 s, p95 0.293 s (1,000 assignments at 50/s) |
| Writer transaction per new assignment (incl. delta) | 1-13 ms | p50 6.0 ms, p95 43 ms |

- Base build: compute 2.66 s (reader), install 1.36 s (writer), +72.5 MB database, peak RSS 203 MB.
- 1,000 assignments at 50/s: 69 SSE messages (14.5 deltas each, largest 57 KB), server CPU 7.1 s over 20 s, client
  CPU 0.26 s; as fast as possible (125/s): p50 0.138 s, p95 0.252 s. UI patch per message (vitest replay of those
  69 messages): dashboard p50 0.14 ms, overview p50 0.81 ms; patched caches equal the GET payloads afterwards.
- `python -m hydra.dataflow verify` on that copy after 2,011 streamed deltas: materialised == full recompute
  (recompute 5.4 s).

## Consequences

- The first view of a run without aggregates materialises them once (cold dashboard 7.37 s in that case); the warm-up thread at API
  start and the end of analyse do it in the background in normal use.
- New material is a delta with its own open-set decision against the base; it never changes base campaigns or
  scores until the next full analyse. Overview `stats.components` and robustness counts stay base values.
- Disk: aggregates of the 837-incident run add 72.5 MB to the database (measured above).
- Edge-case semantics: gene values with a NULL and an empty dependency group count as one distinct value; ties in
  the dominant family of a link are broken by name (API-PERF: first seen).

## Alternatives rejected

- Recompute on every change with a shorter poll: the base recompute is 0.5-5 s per view on the large run.
- Change data capture by polling the `events` table and re-deriving deltas in the stream: deltas would be computed
  per client and could not be replayed exactly after a restart; materialising them once in the writer transaction
  keeps them consistent with the state they describe.
- WebSockets: one-way updates suffice; SSE has native reconnect and `Last-Event-ID`, passes the existing GET-only
  guard, and needs no extra dependency.
