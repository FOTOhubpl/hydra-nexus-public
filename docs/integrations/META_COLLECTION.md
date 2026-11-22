# META-COLLECT: automated Meta Ad Library collection and analysis

Date: 2026-11-22 (Europe/Warsaw). Branch (from ). Code: `hydra/meta/`,
`hydra/api_meta.py`, WATCH-RT adapter `hydra/watch/rt/sources.py:poll_meta`, UI `/watch?view=meta`
(`ui/src/pages/task/WatchMeta.tsx`). Contract: `META_API_CONTRACT.md`. Acceptance: internal notes, not published.

Status: implemented and tested on a synthetic transport and synthetic fixtures; one bounded **live** acceptance run
against the real Meta API on 2026-11-22 (laptop, isolated state, operator's token): results in
internal notes, not published, kept apart from the synthetic results. §9 = how to run it
24/7 in the app.

## 1. Flow

```
config (queries: keywords / page ids, PL, window, budgets)        meta_state / meta_queries (v8)
  -> WATCH-RT engine (single owner, source "meta-adlibrary", interval 30 min, jitter, back-off, circuit breaker)
     poll_meta -> hydra.meta.collector.run_cycle (process + thread lock: one active Meta collector per state dir)
       -> MetaTransport: offline gate, token, provider back-off, HYDRA budget -> http.request (enabled flag, netguard,
          per-minute limit, audit row with path only) | WATCH-RT fetch (same http.request)
       -> page -> normalise + redact -> ONE Writer transaction: meta_ads, meta_revisions (content hash), meta_ad_queries,
          meta_observations, meta_outbox (idempotency key) and the query cursor
  -> MetaWorker (thread, lease 15 min, outside SQLite transactions)
       -> media.acquire (attachments, synthetic fixture media, opt-in snapshot/CDN media, opt-in landing page)
       -> build incident folder -> import_incident(source ext:meta-adlibrary[:recorded|:synthetic])
       -> pipeline.extract -> pipeline.assign_stored (open set, conformal; scoring untouched)
       -> ONE Writer transaction: assign run (assignments row) + meta_analyses (UNIQUE idem_key) + outbox done
       -> audit events META_POLL / META_ANALYSIS / META_REVIEW, stream events meta.poll / meta.analysis
  -> UI /watch?view=meta, /v1/watch/rt/meta/*, reports (JSON / Markdown)
```

Old paths: the scheduler job `adlibrary_pull` delegates to `run_cycle` once the collector has a query
(`hydra.scheduler.job_adlibrary_scheduled`); otherwise the legacy job runs, with its HTTP through the same
`MetaTransport` budget. The Integrations "Run" button runs one `run_cycle`; "Test" runs the layered diagnostics.
The token comes from one provider, `hydra.integrations.secrets.meta_token` (env `META_AD_LIBRARY_TOKEN` /
`HYDRA_META_TOKEN`, then the connector file: `$HYDRA_META_ENV_FILE` if set, else `$HYDRA_SECRETS_DIR/meta-adlibrary.env`
if set, else `~/.config/hydra/meta.env`; the Integrations form writes the same file),
for the collector, diagnostics, Integrations and the legacy module alike.

## 2. Configuration (`GET/PUT /v1/watch/rt/meta/config`, admin; UI "Konfiguracja")

| Key | Default (HYDRA proposal, not a Meta limit) | Note |
|---|---|---|
| `countries` | `["PL"]` | reach country |
| `queries` | none | ≤ 10 active; keyword ≤ 100 chars or ≤ 10 page ids; operator-typed only |
| `ad_active_status` | ALL | |
| `languages` | `[]` | deliberate filter only |
| `date_window_days` / `overlap_days` | 7 / 2 | window policy, not completeness |
| `budget_per_h` | 60 | every HTTP request to Meta, all purposes |
| `max_pages_per_query` | 2 per cycle | pass continues next cycle from the cursor; 20 pages max per pass |
| `interval_s` | 1800 | WATCH-RT source interval (≥ 900) |
| `max_analyses_per_h` | 300 | further jobs `deferred` 10 min (live run: ~160 new ads per cycle, text-only analysis ~0.1 s per ad on the laptop) |
| `max_media_bytes` / `max_media_per_ad` / `max_media_total_bytes` | 25 MB / 4 / 2 GB | |
| `media_fetch` | false | opt-in snapshot + CDN media (§5) |
| `landing_fetch` | false | opt-in landing page via `hydra.enrich.fetch.fetch_url` |
| `expansion` | off, depth 1, 5 pages | P1 (§8) |

## 2a. Search rules (BONUS-AUTO, 2026-11-22)

Sent to Meta (only operator-typed values): `queries` (keywords / page ids), `countries`, `ad_active_status`,
`languages`, the rolling window `date_window_days` ("ostatnie N dni", with `overlap_days` on the next pass),
`date_from` (a floor under the window / watermark, `ad_delivery_date_min`), `date_to` (`ad_delivery_date_max`) and
`platforms` (`publisher_platforms`, one of FACEBOOK, INSTAGRAM, AUDIENCE_NETWORK, MESSENGER, WHATSAPP, OCULUS,
THREADS; empty = all). Applied locally by the triage, never sent: `rules.keywords` (added to the built-in bait list),
`rules.celebrities` (possible impersonation), `rules.page_name_patterns` (case-insensitive regular expressions,
validated on save, reason `page_name_pattern`, counts as a pattern family), `rules.same_text_min_pages` (default 3)
and `rules.alert_min_priority` (low / medium / high, default medium; an `assign` decision is always an alert). Empty
strings clear `date_from` / `date_to`. The `date_to` / `platforms` parameters are taken from the S1 parameter list
and were not exercised live (internal notes, not published).

## 2b. Automatic search overview and new material linked to campaigns (BONUS-AUTO)

- `GET /v1/watch/rt/meta/auto?mode=` (UI Monitoring → Automatyczne wyszukiwanie, `/watch?view=auto`): sources
  (watch_sources), rules, collector state / next run / budget, the last cycles, totals (unique ads, revisions, assign /
  new pattern / abstain, alerts), triage distribution, ads linked to known campaigns, WATCH-RT matches of campaign
  indicators. Code `hydra/meta/auto.py`; every value is read from the DB.
- Cycle summary: one row per collection cycle (live `run_cycle`, replay ticks) in `meta_state` `cycles:<mode>` (last
  50): requests, records, ads seen, new, changed (new revision of a known ad), unchanged, with a delivery stop time
  (`ad_delivery_stop_time` present), queued analyses.
- `GET /v1/watch/rt/meta/campaigns/{id}/links?run=`: the campaign card section „Nowe reklamy z biblioteki Meta
  powiązane z tą kampanią" (ad id, mode, page, first seen, total bits, evidence, triage, review state, incident link)
  and the WATCH-RT matches of the campaign's indicators (source, indicator, feed item, strength, time).
- Top bar: the `meta.analysis` stream event now carries `decision`, `campaign_id`, `triage_priority`, `page_name`,
  `incident_id`; an `assign` produces the notification „Nowa reklama Meta powiązana z kampanią …" (labelled DANE
  SYNTETYCZNE / ODTWORZENIE outside live).
- Offline replay `auto-search-demo` (`hydra/watch/rt/fixtures/auto-search-demo`, SYNTHETIC, .example domains): tick 1
  = 3 new ads + 2 CERT lines, tick 2 = 1 new, 1 changed, 1 with a stop date, 1 unchanged, plus 2 CERT campaign probes.
  Rows marked `hydra_campaign_probe` get, at replay time, values of one existing campaign (≥ 2 members, latest
  analyse run) taken from its members' genes: landing host and entry URL, operator phone, IBAN (from a phrase gene;
  hashed values are never used), tracker id, headline / phrases, plus one member ad image from the local CAS as the
  synthetic fixture media (local DB, read-only; nothing is written to git or sent). Without an analysed campaign the
  row is dropped. The assignment is the assigner's normal decision (measured on the e2e synthetic corpus,
  2026-11-22 07:27: `assign` to the "tani-buty" campaign, 32.0 bits at campaign level; with host + phone only it was
  `new_campaign`, 8.0 bits < threshold 10). UI button
  „Demonstracja offline (DANE SYNTETYCZNE)" (rebuilds the campaign watchlist, then starts the replay). `just
  demo-auto` starts a throwaway server with the synthetic e2e corpus (own temp state, free port, offline).

## 3. Identifier mapping (never mixed)

| Field | Where | Space |
|---|---|---|
| `source_ad_id` | `meta_ads.ad_id` | Ad Library id, always a string |
| `source_page_id` | revision content | Meta page id; shown, not turned into an operator gene (CR-META-COLLECT-1) |
| `query_id` + `config_version` | `meta_ad_queries` | our query provenance (several queries -> one ad) |
| `observation_id` | `meta_observations.id` | one sighting (retention 30 days) |
| `revision_id`, `normalized_content_sha256` | `meta_revisions` | our revision of the creative |
| `artifact_refs` | `meta_artifacts.artifact_id` | CAS sha256 (shared bytes stored once, every relation kept) |
| `collection_status` + reason | `meta_revisions`, `meta_artifacts` | metadata_available, asset_discovery_pending, asset_available, asset_unavailable, blocked_or_login_required, budget_deferred, invalid_or_unsupported_media |
| `incident_id` | `metaad-<mode>-<ad>-<sha8>` | HYDRA incident, corpus `ext:meta-adlibrary[:recorded|:synthetic]` |
| `base_run_id`, `context_version` | `meta_analyses` | base analyse run used by the decision |
| `hydra_campaign_id` | `meta_analyses` | campaign of the base run, NULL unless `assign` |
| `assignment_decision` | `meta_analyses.decision` | assign / new_campaign / abstain |
| `report_id` | `MR-<idem key prefix>` | deterministic per analysis |
| `mode` | every table | live / recorded / synthetic: separate data and counters |

## 4. Revisions

The content hash covers page id/name, the per-card text arrays (kept in source order, never re-paired), landing URLs
with provenance and attachment digests. Observation times, the snapshot URL, delivery dates, platforms, languages and
EU values are metadata: updated in place, no revision, no new extraction. Same text under two ids = two ads. A
changed text/link = new revision, new outbox row, history kept.

## 5. Materials

Default: metadata and text only, every gap recorded with a Polish reason ("Automatyczne pobieranie materiałów …
wyłączone"). Snapshot download is switched on by the admin toggle `media_fetch` (UI "Konfiguracja") or
`HYDRA_META_SNAPSHOTS=1` (operator consent 2026-11-22, `docs/DISCLOSURE.md`; switching it on changes the idempotency
key, so stored revisions can be re-analysed with materials). Order per ad: robots.txt of the snapshot host (fetched at
most once a day, cached, counted in the budget; a disallow means no request and status `blocked_or_login_required`
with the robots reason) -> at most one snapshot page every 2 s -> the page without JS through the budgeted transport
(the token goes only to facebook.com, never to a CDN) -> a token-scrubbed derived copy of the HTML in CAS (role
`snapshot_html`, never executed) -> destination links of the page (`l.facebook.com/l.php?u=` unwrapped) become landing
URLs with provenance `snapshot_link` (INF genes through the normal extractors) -> direct image/video links on `*.fbcdn.net`, `*.facebook.com`,
`*.cdninstagram.com` are downloaded (netguard on every address, no redirects followed, size cap, MIME by content,
PIL decode check, pixel cap; SVG/HTML never accepted). A page that needs JS or login = `blocked_or_login_required`,
no workaround. Analysts attach files (`POST /ads/{id}/attach`), which creates a new revision. No model is downloaded
by the collector; a missing model makes that extractor `unavailable` and the rest continues. Media go through the
normal pipeline extractors (OCR, ASR, visual, experimental DFK only when enabled by its own flags).

Live finding 2026-11-22: `www.facebook.com/robots.txt` ends with `User-agent: *` / `Disallow: /`; with robots.txt
respected HYDRA fetched no snapshot page (20 of 20 probe ads `blocked_or_login_required`). internal notes, not published.

## 6. Retention and disk

Kept: ads, revisions, artifact relations, analyses, reviews (evidence). Aged out after `HYDRA_WATCH_RETENTION_DAYS`
(default 30): `meta_observations`, finished `meta_polls` (WATCH-RT housekeeping, hourly). Downloaded media stop at
`max_media_total_bytes` (then `budget_deferred`). CAS originals are read-only and shared with incidents: HYDRA never
deletes a CAS blob automatically (consistency of references); a controlled purge tool is CR-META-COLLECT-3. Meta's
data storage terms were not re-read (S4): automatic media download stays off until the operator has read them; no
unlimited storage is claimed.

## 7. Analysis results (three, never merged)

- **Correlation**: the assigner's own decision, candidates, evidence (kind, value, bits), conformal set, base run.
  No separate "meta score".
- **Fraud indications (triage)**: `meta-triage@2` reasons: promise of profit, bait phrase, possible impersonation,
  known-case infrastructure, caption/landing mismatch, link to a known HYDRA campaign, and field patterns: brand or
  institution in the page name, "state company" investment (Orlen, Baltic Pipe, PGE…), endorsement story with a profit
  promise, a concrete income promise per time unit, contact via messenger, the same body text on ≥ 3 advertiser pages.
  Page age is not available (ArchivedAd has no page creation date). Priority unknown / low / medium / high (high needs
  known infrastructure / campaign, or three independent pattern families including the page brand and a profit
  promise); `review_status` unreviewed until an analyst confirms or dismisses (history in `meta_reviews`). A
  keyword, a famous name or a young domain alone never reach `high`; an assignment does not mean the campaign was
  verified as fraud.
- **Manipulation signals**: per modality image / video / audio: `not_applicable` (no file) or `unavailable` (no
  validated detector; DFK experimental genes stay off). `no_signal` is never produced and never means authentic.

Alerts = unreviewed analyses with priority ≥ medium or an `assign` decision.

## 8. P1

- Expansion (`expansion.enabled`, off by default): a new ad's page id becomes a `page_ids` query (origin `expansion`,
  depth, parent ad) within the shared budget, ≤ `max_pages` pages, ≤ `max_depth`; a hypothesis, not an operator link.
- "Nowa reklama, ten sam schemat" (`GET /ads/{id}/scheme`): versions with domain change, same-campaign ads with shared
  identifiers, and look-alikes (same image bytes or page name) that HYDRA did not merge, with the decision that kept
  them apart. Reads stored analyses only.

## 9. Offline, resume, live activation

- `HYDRA_OFFLINE=1`: no connection from collection, diagnostics, media or landing fetch (transport gate + process
  socket guard); status `offline`. Disabled source / paused collector: no polls.
- Resume after restart: outbox jobs in `running` with an expired lease are re-leased; queued jobs continue; cursors
  are per query and mode; the idempotency key prevents a second analysis, run or report for the same revision.
### 24/7 collection in the running app (operator steps)

1. Token: `META_AD_LIBRARY_TOKEN` in `~/.config/hydra/meta.env` (0600) on the machine that runs the API, or UI
   Integracje → Meta Ad Library → sekret (same file). Never paste it into chat or git.
2. Start the API online with the watch engine on at boot (it then restarts after every API restart):
   `HYDRA_OFFLINE=0 HYDRA_WATCH_RT=1 hydra serve` (choose the state dir and port deliberately;
   `HYDRA_ENRICH=1` is not needed for Meta, only for RDAP/DNS enrichment).
3. Log in as admin → `/watch?view=meta` → "Sprawdź kontrakt": layers 1–3 must be `pass` (live 2026-11-22: all 5 pass).
4. "Konfiguracja": up to 10 public Polish search terms (e.g. the live set: inwestycja, zarabiaj, pasywny dochód,
   kryptowaluty, Baltic Pipe, Orlen akcje, InPost dopłata, BLIK, wygrana, gwarantowany zysk), country PL, status
   ACTIVE or ALL, `interval_s` 1800, `budget_per_h` 60, `max_pages_per_query` 2, `max_analyses_per_h` 300;
   optional: `media_fetch` (snapshots; currently blocked by robots.txt), `expansion` (P1). Zapisz.
5. "Uruchom": enables the WATCH-RT source `meta-adlibrary` (and its connector), starts the engine if stopped and the
   analysis worker. The engine then polls every 30 min (+0–10 % jitter), within 60 requests/h; "Skanuj teraz" polls
   immediately through the same limits. "Wstrzymaj" disables the source (cursors kept).
6. Check every few hours: collector state, "Ostatni sukces", budget counter, queue (queued should drain), alerts.
   Budget arithmetic: 10 terms × 2 pages = ≤ 20 requests per cycle, 2 cycles/h = ≤ 40 requests/h < 60.

7. BONUS-AUTO: open Monitoring → Automatyczne wyszukiwanie (`/watch?view=auto`, mode "Na żywo"): rules, next run,
   budget, the cycle table (new / changed / unchanged / ended per cycle) and the list of ads assigned to known
   campaigns; an assignment also appears in the top-bar notifications and on the campaign card. For WATCH-RT matches
   against campaign indicators: `/watch` → "Lista obserwowanych" → "Przebuduj z kampanii", and enable CERT Polska /
   URLhaus / OpenPhish (online only).

## 10. Claims to avoid

No claim of live operation, detection rate, deepfake detection, completeness of the Ad Library stream, or common
operator from page id / page name / payer / identical picture. Synthetic results are labelled `DANE SYNTETYCZNE`
everywhere (UI banner, reports `synthetic: true`, corpus suffix).
