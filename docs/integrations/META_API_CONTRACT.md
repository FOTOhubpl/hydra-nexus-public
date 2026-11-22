# Meta Ad Library API contract (META-COLLECT)

Date: 2026-11-22 (Europe/Warsaw). Code: `hydra/meta/contract.py`. Branch.

This file separates two kinds of statements:
- **Documentation (read 2026-11-22)**: what the public reference pages say.
- **Own-token observation**: what our token actually returned (live run 2026-11-22 04:46 CET, laptop, isolated
  state; internal notes, not published).

## 1. Sources

| Id | URL | Read | Version shown |
|---|---|---|---|
| S1 | https://developers.facebook.com/docs/graph-api/reference/ads_archive/ | 2026-11-22 ~04:00, `curl` of the public page | selector v21.0–v27.0 |
| S2 | https://developers.facebook.com/docs/graph-api/reference/archived-ad/ | 2026-11-22 ~04:00 | v27.0 |
| S3 | https://developers.facebook.com/docs/graph-api/overview/rate-limiting/ | 2026-11-22 ~04:00 | v27.0 |
| S4 | https://www.facebook.com/ads/library/api/ | not re-read (terms page; earlier 403 recorded in `hydra/integrations/registry.py`) | — |

## 2. Pinned version

| Item | Value | Basis |
|---|---|---|
| Graph API version | `v26.0` (`HYDRA_META_GRAPH_VERSION` overrides) | documented in S1 (selector) and the prompt's reading of S1; S2/S3 render v27.0. The previous code used `v21.0` (released 2024; not re-verified as still served). |
| Verified working for our app | yes, 2026-11-22 04:46 CET | diagnostics layers 1–5 pass (§5) |

## 3. Fields actually requested

Minimal importer set (`REQUIRED_FIELDS`, every one listed in S2): `id`, `page_id`, `page_name`, `ad_creation_time`,
`ad_creative_bodies`, `ad_creative_link_titles`, `ad_creative_link_captions`, `ad_creative_link_descriptions`,
`ad_delivery_start_time`, `ad_delivery_stop_time`, `ad_snapshot_url`, `publisher_platforms`, `languages`.

Optional (EU transparency, requested only after diagnostics layer 4 saw them accepted): `eu_total_reach`,
`beneficiary_payers`, `target_ages`, `target_gender`, `target_locations`, `age_country_gender_reach_breakdown`,
`total_reach_by_location`. S2: "Available only for ads delivered to the EU" / "UK & EU".

**Removed:** `ad_creative_link_urls`. It was in `hydra/enrich/adlibrary.py:FIELDS` (v21.0) but is not an
`ArchivedAd` field in S2. An unknown field makes Graph reject the whole query (code 100), so it is no longer
requested by any path (collector, diagnostics, legacy hint lookup, legacy scheduler job). Landing URLs now come only
from URLs written in the ad text (`url_in_text`), an opt-in snapshot link, an analyst attachment or (synthetic
replay only) `fixture_link`. A link caption is display text, stored as `display_domains` with provenance
`link_caption`, never as a landing URL.

## 4. Query parameters

| Parameter | Value | Note (S1) |
|---|---|---|
| `ad_reached_countries` | `["PL"]` (configurable) | country = where the ad was delivered, not the advertiser's seat; ads that did not reach the EU are returned only for social issues / elections / politics |
| `ad_type` | `ALL` | not limited to a special category; the whole monitoring is not restricted to financial ads |
| `ad_active_status` | `ALL` (configurable) | default of the API is ACTIVE |
| `search_terms` + `search_type` | ≤ 100 characters; multi-word = `KEYWORD_EXACT_PHRASE`, else `KEYWORD_UNORDERED` | blank space = AND |
| `search_page_ids` | ≤ 10 ids per request | |
| `ad_delivery_date_min` | window start = watermark − overlap (default 2 days), first pass: today − 7 days | late-indexed ads; not a complete incremental stream |
| `languages` | **not sent by default** | an explicit filter only (a `pl` filter would narrow Polish reach) |
| `limit` | 50 | |
| `after` | cursor from `paging.cursors.after` only | `paging.next` (it embeds the token) is never followed |

## 5. Layered diagnostics (`hydra/meta/diagnose.py`, `POST /v1/watch/rt/meta/diagnose`, Integrations → Testuj)

| Layer | Test | Own-token result |
|---|---|---|
| 1 token accepted | `fields=id`, `limit=1` without Graph code 190/102/463/467 | PASS |
| 2 PL + ALL supported | same request answers HTTP 200 | PASS |
| 3 minimal importer fields | `fields=<REQUIRED_FIELDS>`; code 100 = import broken (Integrations shows "Import niesprawny") | PASS (13 fields) |
| 4 optional fields | all optional at once; on rejection each one alone (≤ 7 requests) -> available / unavailable | PASS: all 7 accepted in one request |
| 5 normalise + store | one returned record stored in the live space; empty result = `skipped`, never `pass` | PASS (record stored) |

At most 3 + 7 requests, counted in the HYDRA budget. The previous Integrations test (`fields=id` only) could report
"OK" while the importer's field set was rejected; tested in `tests/meta_collection` (synthetic transport).

## 6. Limits and errors

S3 gives no universal per-token number (the often-quoted "200 calls/hour per user token" is not documented there).
HYDRA uses its own pilot budget: 60 HTTP requests to Meta per rolling hour, every purpose counted (collection,
diagnostics, retries, poll-now, snapshot and media). This is our configuration, not a Meta quota or a measurement.
Usage headers `X-App-Usage`, `X-Business-Use-Case-Usage`, `X-Ad-Account-Usage` are stored (numbers only) when present;
absence = no information.

| Answer | Handling |
|---|---|
| HTTP 429, Graph 4 / 17 / 32 / 341 / 613 / 80004 | provider back-off persisted: `Retry-After`, else BUC `estimated_time_to_regain_access`, else 300 s × 2^(n−1) (max 1 h) + 0–20 % jitter; no request until it expires |
| 190 / 102 / 463 / 467, HTTP 401 | `token_invalid`: collector needs configuration; no retries until a different token is saved |
| 10 / 200 / 2332, HTTP 403 | `permission` (identity confirmation / terms): needs configuration |
| 100 / 1009 / 2500, HTTP 400 | `params`: this query fails, others continue |
| 5xx, 1 / 2, transport errors | `transient`: page kept (cursor), retried next cycle; no inline retry loop |

## 7. Media and storage (S2, `ad_snapshot_url`)

Documentation quote: "String with URL link which displays the archived ad. This displays uncompressed images and
videos from the ad. While you cannot currently download a batch of archived ads, you can download ad creative such as
images and text for an individual ad. If you do so, it must be for analysis and you must comply with the data storage
terms in our Terms of Service."

Decision: individual download for analysis is documented as allowed, but the storage terms (S4 / Terms of Service)
were not re-read. Automatic snapshot/media download is therefore **off by default** (`media_fetch=false`), switchable
only by an administrator, counted in the budget, ≤ `max_media_per_ad` files per revision, Meta CDN hosts only, no JS,
no login, no workaround of blocks. Retention: see `META_COLLECTION.md` §6. No unlimited storage is claimed.
