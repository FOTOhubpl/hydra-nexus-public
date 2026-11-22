# ADR-007: Online enrichment layer with a replayable evidence cache
Status: Accepted (architect, 2026-11-21, after reading the official NASK task)

Context: the task (`docs/REQUIREMENTS_SUMMARY.md`) asks the tool to collect domains, redirects, links, page
structure and public domain and infrastructure metadata. "Features and data sources" carry 30% of the score.
ADR-003 and ADR-004 assumed a fully offline runtime.

Decision:
- The analysis core (genes from stored material, rarity, retrieval, scoring, clustering, assignment) stays
  offline and deterministic.
- A separate module `hydra/enrich/` performs opt-in lookups against public sources only: RDAP/WHOIS, DNS,
  certificate transparency (crt.sh), IP→ASN, the CERT Polska warning list, and optionally an HTTP
  redirect-chain fetch in an isolated, no-JS client.
- Every response is stored as an evidence record with source, query, timestamp and sha256. Replay mode reads
  only the cache, so evaluation and the demo work without network.
- A failed or unreachable lookup becomes gene status `unavailable`, never an error, and never counts as
  evidence against a link.
- Sources and their terms are listed in `docs/DATA_SOURCES.md`.
- ADR-003 still holds: no external inference APIs. Enrichment returns facts, not model outputs.
- `HYDRA_OFFLINE=1` (default) blocks enrichment. `HYDRA_ENRICH=1` enables it.

Consequences: results depend on the lookup date, so every enrichment fact carries its timestamp. Rate limits
are respected with a local cache and backoff. Organiser data never leaves the machine through enrichment:
only hostnames and IPs extracted from the material are queried.

## Amendment 2026-11-21 (ENRICH-PLUS; extra free sources)

Status: proposed by the ENRICH-PLUS builder; the architect accepts it at merge. Clean-room: items 1-3 were
implemented only from `docs/cleanroom/dns_wildcard_consensus.md`, `parking_page_detector.md` and `addownloader.md`;
the studied GPL/unlicensed sources were not opened by the builders.

1. **Wildcard-aware DNS with a three-resolver majority** (`hydra/enrich/dns_consensus.py`, source `dns_consensus`).
   Every host is asked on 1.1.1.1, 8.8.8.8 and 9.9.9.9 (A and AAAA; the CNAME chain comes with the answer). Three
   random control names (`hx` + 32 hex) are asked at the registrable domain and at each parent level up to the
   host. A host whose answer equals a control answer of the deepest level on at least two resolvers is `wildcard`;
   otherwise two of three resolvers decide `current` or `not_current`; anything else is `disputed`. One cache
   record per host holds every individual answer and the control names, so a replay is deterministic and offline.
   Builder decisions beyond the spec: (a) the wildcard match needs two resolvers, so one filtering resolver that
   answers a block-page IP for every name cannot fake a wildcard; (b) equal non-empty CNAME targets count as equal
   (CDN rotation); (c) a query budget (`HYDRA_DNS_BUDGET`, default 20,000 per process) makes an unfinished probe
   `disputed` and uncached. Gene effects in `inf_enrich`: `dns_wildcard` (platform tier, the zone), host-level
   `dns_ip`/`dns_ip24`/`dns_cname` of a wildcard host move to `inf:boilerplate:dns_wildcard` (0 bits), and the zone's
   answer is kept once as `dns_zone_ip`/`dns_zone_cname` (operator tier, same `ip`/`cname` dependency group as the
   host genes). `not_current` and `disputed` hosts give `dns_ip` status `unavailable`. Without a consensus record
   (old caches) the single-resolver `dns` answers are used as before.
2. **Parked / for-sale / hosting-default detector** (`hydra/genome/parked.py`). Content rules on the saved landing
   HTML (parking and for-sale phrases in PL/EN/DE/FR/ES, provider and ad-feed markers, link-text ratio, single
   provider iframe, thin text), plus parking infrastructure from MISP lists only when the replay-cached A answer
   meets the IPs seen in the capture. No live DNS during scoring. A parked incident gets gene `inf_parked` (label
   tier, 0 bits, rules and indicators in the evidence note); its kit/platform genes in `inf_dom` and `inf_http` go to
   `inf:boilerplate:parked` (0 bits) and its operator-tier server/DNS IPs (`inf_har`, `inf_enrich`) to
   `inf:platform:parked_ip`. Operator-tier tracker ids and contacts are kept.
3. **Hardened ad-library scan** (`hydra/enrich/adlibrary.py` `page_through`, `hydra/scheduler.py`
   `job_adlibrary_pull`). Every page is one replayable cache entry. Up to 5 pages of 100 per query, 3 attempts per
   page with 1/2/4 s backoff (exceptions, HTTP 5xx, 429, Graph throttling codes), a token-free resume cursor
   (`<state>/scheduler/adlibrary/cursor.json`) when a page still fails (status `partial`), a per-query watermark
   (`watermarks.json`, next `ad_delivery_date_min` = last complete run minus 2 days), `search_type`
   `KEYWORD_EXACT_PHRASE` for multi-word terms and `KEYWORD_UNORDERED` for single words, a page-id sweep
   (`search_page_ids`, at most 10 per request, 50 per run) for watchlist page ids and pages of ads stored earlier,
   and dedup by ad id and by (page id, sha256 of the normalised body). Without a token and without a cache the job
   is `unavailable` with a Polish note pointing to manual import via `inbox/`. A removal for policy violation is
   read only from snapshot pages an analyst saved (no headless browser, no bulk crawling) and recorded as the
   platform-moderation fact `moderation: removed_policy_violation`, never as a linking gene. The scheduler's
   `reenrich_domains` job now calls `rdap.domain` (the former `rdap.lookup` did not exist).
4. **Extra free facts.** (a) `inf_http` (offline, from stored HARs): an HHHash-style hash of the response header
   names in received order (own implementation from the public HHHash description; names lower-cased and HTTP/2
   pseudo-headers dropped, so it is not byte-compatible with HHHash values), the security-header profile
   (boilerplate) and a hash of the normalised CSP (kit tier). (b) Favicon mmh3 (Shodan-compatible) already existed
   as `favicon_mmh3` in `inf_dom`; unchanged. (c) A JARM-like TLS probe (`hydra/enrich/tlsprobe.py`, gene
   `tls_probe_hash`, platform tier): six handshakes with the standard `ssl` module (version range, forward and
   reversed cipher order, CBC-only offer, ALPN list). JARM's raw ClientHello packets cannot be built with `ssl`, so
   this is an own, lower-resolution variant and is not comparable with JARM values; no JARM code was used. It
   contacts the landing server, so it never runs inside the pipeline: `scripts/enrich_plus.py tls HOST` fills the
   cache on an analyst's request (live mode only) and `inf_enrich` reads only the cache.
5. **Kill switch.** All live paths above (DNS consensus, TLS probe, ad-library pages) go through
   `Enricher.live`, which is false when `HYDRA_OFFLINE=1` or `HYDRA_ENRICH=0`; the scheduler forces it off even for
   a caller-supplied Enricher. Tests assert that the injected resolver, handshake and HTTP layers are never called
   in that state.
6. **Not implemented:** the Google Ads Transparency lookup (`docs/cleanroom/google_ads_transparency.md`). It needs
   undocumented endpoints and its compliance with Google's terms is unclear; it stays out until the operator decides.
7. **Ad Library wired into the hint search and the scheduler (GAP-AUDIT B16, 2026-11-21).** The token is read from
   `META_AD_LIBRARY_TOKEN`, then the older `HYDRA_META_TOKEN`, then `~/.config/hydra/meta.env` (`KEY=VALUE` lines;
   path override `HYDRA_META_ENV_FILE`); it is never printed, cached or returned. `pipeline.search_hint` calls
   `adlibrary.hint_lookup`: one page of up to 50 ads for the hint (exact phrase for several words), each scored by
   the rule engine; live only with a token and `HYDRA_ENRICH=1 HYDRA_OFFLINE=0`, otherwise the cached answer is
   replayed. `ad_library.status` is `not_configured` only when there is no token and nothing cached, `offline` with
   a token but no live mode and no cache, else `ok` (`mode` live or replay) or `partial`. The scheduled pull now
   runs `adlibrary.evaluate` with the watchlist (keywords on top of the default lexicon, `celebrities`,
   `platform_names`, domains as known hosts, optional `ad_date_min`/`ad_date_max`, `max_domain_age_days`, default
   30, and `adlib_min_reasons`, default 1); unflagged ads are counted and remembered, not stored. An Ad Library
   CSV/JSON export dropped into `inbox/` is split into one `adlib-<id>` folder per ad (reasons recorded). The
   `reenrich_domains` job now selects the observed kinds `registrable_domain` and `landing_host`.
   "Uruchom teraz" on the scheduler screen calls `POST /v1/scheduler/run`, which runs every job once in a background
   thread with the API's Writer; one run lock serialises it with the background loop (`running` in the status).
   `docker-compose.yml` starts the API with `HYDRA_SCHEDULER=1` (override with `HYDRA_SCHEDULER=0`); offline, the
   ad-library and re-enrichment jobs only replay or report `unavailable`/`skipped`.
