# Live data stream — `GET /v1/stream` (DATAFLOW)

Owner: DATAFLOW (`hydra/dataflow.py`, `hydra/api_stream.py`, `ui/src/api/stream.ts`). Design: ADR-0009.
Status: contract v1, 2026-11-21. Additive: every field below is new; existing fields keep their meaning except
`size` of campaigns in `/v1/dashboard`, `/v1/campaigns` and `/v1/graph/overview`, which now counts new material
assigned since the analysis as well (`new` says how many of them are new).

## Model

- An analyse run is an immutable snapshot ("base"). Everything that arrives later and is attached to it (assign
  runs with `params.base_run = run`, i.e. upload, `assign-batch`, scheduler inbox; analyst reviews; exports) is a
  **delta** over that base. Deltas are materialised in the database by the single Writer (migration v5,
  `agg_*` tables) in the same transaction as the event that causes them.
- Every delta has the `seq` of the hash-chained audit event (`events.seq`) that caused it and bumps the run's
  aggregate `version` by one. `seq` is the SSE `id`.
- Material that is new since the analysis is marked `new: true` (nodes) or counted in `new` (counts, campaign
  sizes, links). UI label: **"nowe od analizy"**. It stays marked until the next full analyse (a new base).
- Counters in deltas are **absolute** (`[n, n_new]`), lists are **upserts by key**. Applying a delta twice, or
  an older delta after a refetch, is harmless when the client skips deltas with `seq <= live.seq` of its cache.

## GET /v1/stream

Query: `run_id` (default: latest analyse run, resolved at connect), `last_event_id` (alternative to the
`Last-Event-ID` header, for clients that cannot set headers). Local-only guard as every endpoint (Host must be
loopback); a request with a non-local `Origin` is refused with 403.

Response: `text/event-stream`, not gzip-compressed, `Cache-Control: no-cache`, `X-Accel-Buffering: no`. The
first line is `retry: 3000`.

### Events

| event | `id` | data | when |
|---|---|---|---|
| `hello` | — | `{run_id, latest_run, version, seq, status, base_finished_at, resumed, replayed, heartbeat_s, coalesce_ms, max_message_bytes}` | once, right after connect |
| `delta` | last `seq` included | Delta (below), possibly several coalesced | new material / review / export of this run |
| `reset` | `seq` | `{reason, run_id, latest_run}` | the client must refetch its run-scoped queries (`reason`: `new_run` a newer analyse run exists; `rebuilt` aggregates of this run were rebuilt; `gap` resume point no longer available; `overflow` the client was too slow and messages were dropped; `too_large` a delta exceeded the message cap) |
| `job` | — | `{id, kind, status, total, done, failed, stage, run_id, progress}` | job row changed (throttled ≤ 4/s per job) |
| `watch` | — | `{alert_seq, event_id, host_defanged, first_seen, source, replay, level, campaigns: [...], note_pl}` | HYDRA Watch fingerprint alert (candidate, never a confirmed member) |
| comment `: hb <unix>` | — | — | heartbeat every 15 s |

Resume: on reconnect the browser sends `Last-Event-ID`; the server replays stored deltas with `seq` greater than
it (coalesced) before going live, `hello.replayed` = number of deltas replayed. When the id is older than the
retained deltas of the run, or the aggregates were rebuilt after it, the server sends `reset` with `gap`.

### Delta (`event: delta`)

```jsonc
{
  "run_id": "analyse-…", "v": 13, "seq": 4712, "from_seq": 4705, "n": 3,   // n = deltas coalesced
  "kinds": ["assign"],                       // assign | review | export
  "kpis": { /* absolute; exactly the shape of /v1/dashboard `kpis` */ },
  "counts": {                                // changed keys only, absolute [n, n_new]
    "decision": {"assign": [12, 3]}, "corpus": {"nask": [840, 3]}, "family": {"VIS": [9000, 40]},
    "day": {"2026-11-01": [17, 1]}, "tier": {"campaign": [1200, 5]}, "export": {"stix_run": [2, 0]}
  },
  "campaigns": [{"id": "cmp-1", "label": "…", "size": 13, "new": 1, "group_id": "grp-1" | null,
                 "first_seen": "2026-10-01", "last_seen": "2026-11-01", "solo": false,
                 "internal_edges": 40, "max_internal_bits": 31.2, "family": "INF" | null}],
  "groups": [{"id": "grp-1", "size": 20, "new": 1}],
  "links": [{"source": "cmp-1", "target": "cmp-7", "level": "kit", "pairs": 3, "max_bits": 12.1,
             "sum_bits": 30.2, "family_bits": {"VIS": 12.0}, "new": 1}],
  //  cross-campaign links, ONE ENTRY PER LEVEL, and for every changed (source, target) ALL its levels are sent;
  //  a level missing for a sent key means 0 pairs at that level; all levels 0 → the link is gone.
  "nodes": [{"id": "inc-9", "title": "…", "campaign_id": "cmp-1" | null, "map_campaign": "cmp-1" | "solo:inc-9",
             "decision": "assign", "observed_at": "2026-11-01" | null, "corpus": "nask", "thumbnail": "<sha>" | null,
             "assign_run": "assign-…", "created_at": "…", "new": true}],
  "removed_nodes": ["inc-3"],                // no longer new (e.g. now a base member) — rare
  "edges": [{"source": "inc-9", "target": "inc-2", "bits": 12.3, "p": 0.9, "hypothesis": "same_campaign",
             "level": "campaign", "family": "VIS", "new": true}],       // non-independent pairs of new nodes
  "top_genes": [{"family": "INF", "kind": "tracker_id", "value": "…", "df": 12, "new": 1, "level": "campaign"}],
  "exports": {"total": 4, "by_kind": {"stix_run": 2}},                   // present when kinds has export
  "batch": {"run_id": "assign_batch-…", "base_run": "…", "summary": {"items": 6, "new_patterns": 2},
            "new_patterns": [{"id": "N-1a2b3c4d", "size": 3, "members": ["new-…"]}],
            "items": [{"incident_id": "new-…", "decision": "new_campaign", "campaign_id": null,
                       "new_pattern_id": "N-1a2b3c4d", "duplicate_of": null}], "truncated": false}
  //  present when kinds has batch (merge-batch4): an `assign-batch` run against this base finished. Its items were
  //  already counted by their own `assign` deltas; this one carries the cross-comparison result (max 200 items).
  //  The UI invalidates `["task", "assign-batch", …]` queries on it.
}
```

GRAPH-2 constellation cache (`["task", "graph2", "overview", run, source]`, normalised `Overview`): the LOD
source keeps the raw payload in `overview.raw`; the client patches that with `patchOverview` (placement from
`base_bounds`) and normalises again (merge-batch4).

Message cap: 256 KB of JSON per SSE message. Coalescing window: 250 ms (first delta opens the window; the
message is sent when the window closes or the cap is reached). A single delta larger than the cap is replaced
by `reset` with `too_large`.

## `live` block in GET responses

`/v1/dashboard`, `/v1/graph/overview` and `/v1/campaigns` carry the state of the delta layer they include:

```jsonc
"live": {"run_id": "…", "version": 12, "seq": 4711, "status": "ready" | "building" | "missing",
         "base_finished_at": "…", "new_incidents": 3, "by_decision": {"assign": 2, "abstain": 1},
         "label_pl": "nowe od analizy"}
```

- `/v1/dashboard`: `kpis` are current totals (base + new); `previews` rows of new incidents have `new: true` and
  `decision`; `campaigns[].size` / `new`, `campaigns[].members[].new`.
- `/v1/campaigns`: `items[].size` (current), `items[].new`.
- `/v1/graph/overview`: `campaigns` table gets a trailing column `new`; `links` table gets a trailing column `new`
  (read columns by name); new solo pseudo-campaigns (`solo:<id>`, `solo: true`) are appended as rows; the payload
  has `levels` (the level filter it was built with) and `live.nodes` / `live.edges` (same objects as in deltas, with
  `x`, `y` computed by the placement rule below). When `min_bits > 0` the links are the base links (no new pairs).

### Placement of new nodes (server and client use the same rule)

`h = fnv1a32(id)` (32-bit FNV-1a over the UTF-8 bytes, offset 2166136261, prime 16777619), `θ = 2π·(h mod 3600)/3600`.
Member of a campaign disc `(cx, cy, r)`: `(cx + 0.8·r·cos θ, cy + 0.8·r·sin θ)`. Solo: `R = max(|x0|, |x1|, |y0|,
|y1|) + 4` over the overview `base_bounds` (the box of the base layout; `bounds` is widened to cover the new solo
discs, so a viewport request with `bounds` returns them), `(R·cos θ, R·sin θ)`, and the solo campaign row gets
`r = 0.8`; the solo node sits at its disc centre.

## Client (`ui/src/api/stream.ts`)

- One `EventSource` per tab for the selected run (`withRun`), reconnect with exponential backoff (1 s → 30 s,
  jitter) and `Last-Event-ID` (the browser sends it automatically on its own reconnects; on manual reconnects the
  client passes `last_event_id`).
- Patches react-query caches in place (`setQueryData`, no refetch): `["task", "dashboard"]`, `["task", "campaigns"]`,
  raw `/v1/graph/overview` payloads. `reset` → `invalidateQueries` of run-scoped keys. `job` → job queries.
  Other run-scoped views (flow, analytics) are invalidated (their heavy part is cached server-side).
- Indicator "na żywo" (connected), "łączenie…" (reconnecting), "odświeżanie co 15 s" (fallback to polling after
  repeated failures or without EventSource support).
