# Graph API — level of detail (API-PERF)

Owner: API-PERF (backend, `hydra/api_graph.py`, `hydra/graph_lod.py`). Consumer: GRAPH-2 (graph frontend).
Status: contract v1, 2026-11-21. Additive: `GET /v1/graph/explore` keeps its shape until GRAPH-2 switches.

Why: on run `analyse-20261121T164214-b8633e` (corpus `ext:extended`, 837 incidents, 89,357 pairs, 17,894
non-independent links, 545 campaigns) `/v1/graph/explore` returned 21.5 MB in 23.2 s (architect, 20:05 via
tunnel). The explorer needs the campaign map first and incident detail only where the analyst looks.

## Common rules

- Every endpoint takes optional `run_id` (default: latest analyse run, as everywhere). The UI keeps sending it via
  `withRun` (`/v1/graph` prefix is run-scoped already).
- Runs are immutable. The per-run graph is computed once, stored under `<state>/work/cache/api/<run_id>/` and kept
  in memory; analyst reviews (must-link / cannot-link / unsure) are overlaid live on every request, so a review is
  visible immediately without invalidation. Response header `X-Hydra-Cache: hit|miss`.
- Responses are gzip-compressed when the client sends `Accept-Encoding: gzip` (browsers do).
- Tables are compact: `{"columns": [...], "rows": [[...], ...]}`. Column order is fixed by this document; read by
  name (`columns.indexOf`) so added columns at the end never break the UI.
- Coordinates: the server runs the same deterministic hierarchical packing as `ui/src/components/graph/layout.ts`
  (`packLayout`: sunflower of incidents inside a campaign disc, campaign discs spiral-packed inside group discs,
  groups and ungrouped campaigns packed at the top). Units are the layout's (incident spacing = 1). Incident
  `x, y` are absolute; a campaign disc contains its members.
- Pseudo-campaigns: an incident without a campaign gets the campaign id `solo:<incident_id>` (as in `packLayout`).
- Levels: `campaign` (same_campaign), `kit` (shared_template, or weak/asset with kit_bits ≥ 6), `asset`
  (shared_asset), `weak` (abstain), `review` (analyst must-link on a pair the scorer left independent).
  Independent pairs are never returned; they are not links.

### Edge table (`EDGE_COLUMNS`, shared by campaign / neighbors / viewport)

| # | column | type | note |
|---|---|---|---|
| 0 | `source` | str | incident id, `source < target` |
| 1 | `target` | str | incident id |
| 2 | `bits` | float | `pairs.total_bits`, 3 decimals (heuristic weight of evidence, not a probability) |
| 3 | `p` | float | `pairs.probability` |
| 4 | `hypothesis` | str | scorer hypothesis |
| 5 | `level` | str | see Levels |
| 6 | `family` | str\|null | dominant positive family |
| 7 | `family_bits` | object | `{VIS: 12.3, ...}` non-zero only |
| 8 | `campaign_bits` | float\|null | |
| 9 | `kit_bits` | float\|null | |
| 10 | `asset_bits` | float\|null | |
| 11 | `inferred` | bool | > 50 % of positive bits from enrichment records |
| 12 | `robust` | `[robust: bool, critical: str[], link: str\|null]`\|null | leave-one-family-out robustness |
| 13 | `review` | `"must_link"\|"cannot_link"\|"unsure"\|null` | live overlay |
| 14 | `reasons` | `[[family, kind, bits, value, doc_freq, text], ...]` | top `top_reasons` by bits (default 3, max 5; full list: `/edge`) |

Edge id for selection / URLs stays `${source}|${target}`.

### Node table (`NODE_COLUMNS`)

`id, title, campaign_id, group_id, observed_at, registered_at, corpus, thumbnail, degree, x, y`
(`degree` = number of non-independent links of the incident in the whole run; `thumbnail` = artifact id for
`/v1/artifacts/{id}/thumb`-style URLs, as in `explore`).

## GET /v1/graph/overview

First paint. Campaign-level aggregate: one node per campaign (incl. `solo:` pseudo-campaigns), one link per
campaign pair joined by cross-campaign evidence. Target: < 300 KB gzip, < 1 s cold-from-disk-cache.

Query: `run_id`, `levels` (default `kit,campaign,review` — which cross-campaign pair levels aggregate into
links), `min_bits` (default 0, drop pairs below), `min_pairs` (default 1).

```jsonc
{
  "run_id": "analyse-…", "corpus": "extended", "synthetic": false,  // native names unprefixed since PIPE-FIX
  "thresholds": {"link_bits": 10.0, "kit_bits": 6.0},
  "stats": { /* exactly the `stats` object of /explore: nodes, edges, components, campaigns, multi_campaigns,
               groups, levels, largest_campaign, robustness_scored, robust, must_link, cannot_link, inferred */ },
  "bounds": {"x0": -120.5, "y0": -98.1, "x1": 120.5, "y1": 98.1},
  "groups": {"columns": ["id", "label", "n_campaigns", "size", "x", "y", "r"], "rows": [...]},
  "campaigns": {"columns": ["id", "label", "confidence", "group_id", "size", "first_seen", "last_seen", "scheme",
                            "x", "y", "r", "internal_edges", "max_internal_bits", "family", "thumbnail", "solo"],
                "rows": [...]},
  "links": {"columns": ["source", "target", "pairs", "max_bits", "sum_bits", "family", "level"], "rows": [...]}
}
```

- `campaigns.size` = members in this run; `internal_edges` = non-independent links inside the campaign;
  `family` = dominant family of internal links (by summed positive bits), null for singletons; `thumbnail` = the
  first member's preview; `solo` = true for pseudo-campaigns.
- `links`: `source < target` (campaign ids), `pairs` = number of incident pairs aggregated, `max_bits`,
  `sum_bits`, `family` = dominant family by bits, `level` = strongest level among them
  (`review` > `campaign` > `kit` > `asset` > `weak`). Same aggregation idea as `campaignLinks` in layout.ts, but
  server-side over all links instead of only the loaded ones. Pairs an analyst marked cannot-link are left out of
  `links` (they still appear, flagged, in the incident-level tables).
- `stats` counts every link of the run (all levels, with the review overlay), so it matches `/explore`.

## GET /v1/graph/campaign/{campaign_id}

Expand one campaign (or several: `campaign_id` may be a comma-separated list, max 50). Members + internal links
+ links that leave the expanded set (so the UI can draw stubs to collapsed campaigns).

Query: `run_id`, `top_reasons` (0–5, default 3), `external` (bool, default true), `external_limit`
(default 300, strongest first), `min_bits` (default 0, applies to external links only).

```jsonc
{
  "run_id": "…",
  "campaigns": [ {"id": "…", "label": "…", "confidence": 0.9, "group_id": "…", "size": 12, "first_seen": "…",
                   "last_seen": "…", "scheme": "…", "x": 1.0, "y": 2.0, "r": 4.1} ],
  "nodes": {"columns": NODE_COLUMNS, "rows": [...]},          // members of the expanded campaign(s)
  "edges": {"columns": EDGE_COLUMNS, "rows": [...]},          // both ends inside the expanded set
  "external": {"columns": EDGE_COLUMNS, "rows": [...]},       // one end inside, one outside
  "external_nodes": {"columns": NODE_COLUMNS, "rows": [...]}, // far ends of `external`
  "truncated": {"external": 0}                                // links dropped by external_limit
}
```

404 when no campaign of that id exists in the run (`solo:<id>` is accepted for a singleton incident).

## GET /v1/graph/neighbors/{incident_id}

Ego network for one incident (click / hover / search hit).

Query: `run_id`, `min_bits` (default 0), `levels` (comma list, default all), `limit` (default 200, strongest
first), `top_reasons` (0–5, default 3).

```jsonc
{"run_id": "…", "incident": {/* NODE_COLUMNS as an object */},
 "nodes": {"columns": NODE_COLUMNS, "rows": [...]},   // the neighbours (not the incident itself)
 "edges": {"columns": EDGE_COLUMNS, "rows": [...]},
 "total": 57, "truncated": 0}
```

404 when the incident is not in the run.

## GET /v1/graph/edge/{a}/{b}

One link in full, for the side panel and the bit waterfall. Order of `a`, `b` does not matter. Reads one
`pairs` row by primary key; also answers for independent pairs (level `null`).

```jsonc
{"run_id": "…", "source": "a", "target": "b", "bits": 31.2, "p": 0.97, "hypothesis": "same_campaign",
 "level": "campaign", "family": "INF", "family_bits": {...}, "campaign_bits": 25.0, "kit_bits": 3.1,
 "asset_bits": 0.0, "inferred": false, "robustness": {"robust": true, "critical": [], "link": "same_campaign"},
 "review": null, "missing_families": [...], "contradictions": [...],
 "campaigns": {"a": "cmp-…", "b": "cmp-…"},
 "reasons": [{"family", "kind", "bits", "value_a", "value_b", "doc_freq", "text"}, ...],   // all, by bits desc
 "waterfall": [{"family": "INF", "kind": "tracker_id", "bits": 12.0, "cumulative": 12.0}, ...]}
```

`waterfall` is the reasons in descending bits with the running sum (negative reasons last); the final
`cumulative` equals the sum of reason bits, which may differ from `bits` when the scorer applies caps or
dependency-group discounts (shown as is, never adjusted).

## GET /v1/graph/viewport

Incidents whose layout position lies in a box, for zoomed-in rendering without expanding whole campaigns.

Query: `run_id`, `x0, y0, x1, y1` (required, layout units), `limit` (default 1500 nodes), `min_bits`
(default 0), `max_edges` (default 5000, strongest first), `top_reasons` (0–5, default 0).

```jsonc
{"run_id": "…", "nodes": {"columns": NODE_COLUMNS, "rows": [...]},
 "edges": {"columns": EDGE_COLUMNS, "rows": [...]},   // both ends inside the box
 "truncated": {"nodes": 0, "edges": 0}}
```

## GET /v1/graph/explore (legacy, unchanged shape)

Still returns the full payload (all nodes, all non-independent edges as verbose objects) for the current
explorer and exports, now from the per-run cache and gzip-compressed. Optional new filters: `levels` (comma
list) and `min_bits`. Remove the UI dependency once GRAPH-2 is on the endpoints above.

## Unchanged

`/v1/graph/path`, `/v1/graph/indicators`, `POST /v1/graph/export`, `/v1/graph/campaigns/{id}/summary` keep their
shapes (export and path now read the cached per-run graph where they can).

## Server-side cache (`hydra/api_cache.py`) — keys, invalidation, warm-up

For backend work that builds on the cache (e.g. DATAFLOW). Small API, all in `hydra/api_cache.py`:

| Call | Use |
|---|---|
| `cached(settings, conn, endpoint, run_id, params, compute, *, wrap=None, info=None) -> (value, hit)` | per-run computed view; `compute` returns JSON data; `wrap` builds the in-memory object (e.g. `GraphBase`); `info["fp"]` receives the run fingerprint |
| `memo_response(request, key, build, *, hit=None) -> Response` | serialised + gzip bytes in memory (256 MB cap) for a full response; `key` must include the fingerprint, params and `reviews_digest(reviews)` |
| `run_fingerprint(conn, run_id) -> str \| None` | `finished_at`, counts of pairs / campaigns / members, assign runs with assignment count and newest `created_at`; `None` = unfinished run, never cached |
| `invalidate(settings, run_id=None)` | drop one run (or all) from disk and memory |
| `warm(app_state, run_ids=None, *, all_runs=False, disk_only=False)` | compute graph base + overview/explore memo, flow, dashboard, analytics, dashboard graph tile |
| `precompute_run(settings, run_id)` | called by `hydra.pipeline.analyse` after the run is persisted (disk only); `HYDRA_API_PRECOMPUTE=0` disables |
| `warm_async(app_state, ..., all_runs=False)` | background thread; at API start with `all_runs=True` (newest run per corpus into memory, every older run to disk only); after an analyse job and after an applied review |

- Disk layout: `<state>/work/cache/api/<run_id>/<endpoint>-<sha1(endpoint, params, CACHE_VERSION, fingerprint)[:16]>.json.gz`
  plus `image_entropy.json` (per artifact sha256, valid for every state). Writes are atomic (tmp + rename); an
  unreadable file is recomputed. Bump `CACHE_VERSION` when a cached shape changes.
- Endpoints in the cache: `graph-base` (top 5 reasons), `system-flow`, `dashboard`, `analytics-overview`. Live per
  request (never cached): review state (`_pair_reviews`), jobs, exports, audit counts, enrichment store counts,
  assignment counts.
- Switches: `HYDRA_API_CACHE=0` (no cache at all), `HYDRA_API_WARM=0` (no background warm-up; set in
  `tests/conftest.py`), `HYDRA_API_PRECOMPUTE=0` (no precompute at the end of analyse).
- Measured (server copy, 837-incident run): cold compute graph base 2.5-4.5 s, flow 6 s, dashboard 3.6 s, analytics
  0.5 s; first compute after the 19:25 reboot under load reached 45-102 s because the pair JSON (385 MB) and CAS
  images were read from a cold disk, hence precompute + warm-up. From the cache: every view < 0.1 s over HTTP.
