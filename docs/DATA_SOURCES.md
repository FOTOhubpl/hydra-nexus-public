# Data and metadata sources

NASK task §8 allows extra data and public sources if their origin is stated; §10 values "a list of useful metadata
sources"; §4 asks for licences and usage limits of external services. This file lists every source HYDRA reads,
what each one gives for campaign linking, and its terms. Licences of libraries and models: `docs/LICENSES.md`.

Sources of this page: the infrastructure analyst's notes from the live enrichment run on the sample (2026-11-21,
outside git), the terms pages read on 2026-11-21 (URLs below), ADR-007. Rows marked *unverified* were not
confirmed against a terms page.

## Principles (ADR-007)

1. The analysis core reads only stored material. Lookups are an opt-in layer (`HYDRA_ENRICH=1`,
   `HYDRA_OFFLINE=0`); the default is replay of the cache.
2. Only hostnames and IP addresses extracted from the material are sent out. No organiser file, image or text
   leaves the machine.
3. The scam pages themselves are never fetched over HTTP. HTTP goes only to registries, CT logs and search
   services. DNS goes through public resolvers; the only packets reaching operator infrastructure are the
   resolvers' recursive queries to the domain's authoritative nameservers.
4. Every response is cached as JSON with `source, key, url, fetched_at (UTC ISO-8601), status, data` and a sha256.
   A replay run reads only the cache; a missing entry becomes `unavailable`, never an error and never evidence
   against a link. Transient failures are stored separately and retried, not cached as truth.
5. Facts are true as of `fetched_at`; registration, DNS and certificates change.

## Offline sources (organiser material)

| # | Source | What it gives | Terms |
|---|---|---|---|
| O1 | HAR network logs in the incidents | full request graph, `serverIPAddress`, redirects (3xx + JS/meta), response headers, POST bodies, response bodies, tracker/pixel requests | organiser data, NASK only, never public |
| O2 | Saved landing pages (HTML, `_files/`, MHTML, page-capture manifest) | DOM skeleton, inline JS, forms, asset sha256, favicon (Shodan-compatible mmh3), the `saved from url=` comment | organiser data |
| O3 | Ad creatives, feed screenshots, screen recordings, ad videos | visual genes, OCR text, faces, logos | organiser data |
| O4 | Platform metadata (`.info.json` and similar) | ad/page ids, uploader, dates, title, description | organiser data |

## Online sources used by the enrichment layer

| # | Source | Endpoint | Gives (for linking) | Auth | Limits and politeness used | Terms |
|---|---|---|---|---|---|---|
| E1 | RDAP via the rdap.org bootstrap, then the authoritative registry or RIR RDAP server | `https://rdap.org/domain/<d>`, `https://rdap.org/ip/<ip>` | registration, expiry and last-changed dates, registrar name and IANA id, EPP status, nameservers, abuse contact; IP netname, range, country. Shared registrar + registration window + NS set across rotated domains | none | rdap.org: 10 requests per 10 s per client (Cloudflare), 429 above, 403 on abuse (about.rdap.org, read 2026-11-21). Registries throttle separately. Used: 1 request/s, 2 tries, backoff 4 s / 8 s | rdap.org is run privately by Gavin Brown, no formal terms. Registry RDAP data is public; registrant fields are GDPR-redacted; each registry's notices (in the response) typically forbid bulk harvesting and marketing use |
| E2 | DNS via the system `dig` binary (BIND `dnsutils`, MPL-2.0; called as a process with list arguments, validated names, 15 s timeout); `socket.getaddrinfo` for A/AAAA when `dig` is absent (`hydra/enrich/dns.py`) | public resolvers 1.1.1.1, 8.8.8.8 | A, AAAA, CNAME for every host; NS, MX, TXT, SOA for apex domains; Team Cymru TXT. Shared IPs, NS sets, MX, SPF/verification TXT | none | one query per record type per name, cached | Cloudflare and Google public-resolver terms (*unverified* for limits); DNS data is public |
| E2b | DNS consensus and wildcard probe (`hydra/enrich/dns_consensus.py`, same `dig` call, ADR-007 amendment 2026-11-21) | public resolvers 1.1.1.1, 8.8.8.8, 9.9.9.9 (Quad9) | per host: A/AAAA on all three resolvers plus 3 random control names per parent level; class wildcard / current / not current / disputed, resolver agreement. Wildcard zones stop random subdomains from counting as host-level IP matches | none | about 24 queries per host (one control depth), 0.05 s apart, budget `HYDRA_DNS_BUDGET` (20,000 per process); one cached record per host | Cloudflare, Google and Quad9 public-resolver terms (*unverified* for limits); the random control labels reach the zone's authoritative servers |
| E8 | TLS handshakes to a landing host (`hydra/enrich/tlsprobe.py`, analyst-triggered via `scripts/enrich_plus.py tls`) | the landing host, port 443, public IPs only | six handshake results (version, cipher, ALPN) hashed to `tls_probe_hash`: server TLS stack/configuration hint | none | six connections per host, 5 s timeout each, 1 s between hosts; never automatic | contacts the operator's server (no HTTP request is sent); run only for hosts an analyst chose, on an isolated host |
| E3 | Team Cymru IP-to-ASN mapping, DNS interface | `<reversed-ip>.origin.asn.cymru.com TXT`, `AS<n>.asn.cymru.com TXT` | ASN, BGP prefix, country, RIR, allocation date, AS name (hosting provider) | none | "Free, forever"; use bulk mode or DNS at volume; bulk runs of a few thousand addresses; WHOIS hammering is null-routed (team-cymru.com/ip-asn-mapping, read 2026-11-21) | free community service |
| E4 | crt.sh (Sectigo) Certificate Transparency search | `https://crt.sh/?q=<d>&output=json` | every logged certificate: issuer, SAN names (sibling domains on one certificate), not_before (first TLS go-live) | none | no published limit or SLA; frequent 502/timeouts under load. Used: 3 s between calls, 60 s timeout, 3 tries. Volume users can use the public PostgreSQL replica (*unverified*) | no published terms found (*unverified*); CT data is public by design (RFC 6962) |
| E5 | CERT Polska warning list (Lista ostrzeżeń) | `https://hole.cert.pl/domains/v2/domains.json`; local snapshot 2026-11-20T18:58:20Z, 126,730 entries (`models.lock.json` key `cert-domains`) | is the exact host (not only the registrable domain) on the official Polish warning list, with insert/delete dates | none | static file, no runtime calls; refresh with `scripts/fetch_models.py --refresh-external` | free for any use, attribution not required; must not imply endorsement by CERT Polska |
| E5b | KNF Lista ostrzeżeń publicznych (PL-SCAM) | `https://www.knf.gov.pl/dla_konsumenta/ostrzezenia_publiczne` (one HTML page, 13 tables); snapshot 2026-11-21T18:43:04Z, 669 warnings, 386 domains in 230 entries (`$HYDRA_DATA_DIR/external/knf/`) | the host or its registrable domain is named in a KNF warning (unauthorised financial activity), with category and list position; label only | none | static snapshot, no runtime calls; refresh with `scripts/fetch_public_warnings.py --sources knf` on the server | public-sector register; exact re-use terms not published on the page [U]; entries can name natural persons, so the snapshot stays out of git |
| E5c | MF Rejestr domen (illegal gambling, PL-SCAM) | `https://hazard.mf.gov.pl/api/Register` (XML); snapshot 2026-11-21T18:43:09Z, 60,231 domains (`$HYDRA_DATA_DIR/external/mf_hazard/`) | the host is on the statutory register of domains offering illegal gambling, with entry date; label only | none | static snapshot; refresh with `scripts/fetch_public_warnings.py --sources mf` | statutory public register [U exact re-use terms] |
| E6 | Public Suffix List | local copy `data/external/psl/` (`models.lock.json` key `psl`) | registrable-domain split including private suffixes (free hosts, page platforms) via tldextract `include_psl_private_domains=True` | none | static | MPL-2.0 |
| E6b | MISP warninglists (`hydra/enrich/warninglists.py`, applied in `GeneSink` and `operator_ids.phone_profile`) | https://github.com/MISP/misp-warninglists at commit `40e2b570fda2e5443a2d6fa1d1db16c37ab0a956` (2026-10-29); local snapshot `data/external/misp-warninglists/<commit>/` (`current` symlink), retrieved 2026-11-21 by `scripts/fetch_misp_warninglists.py` (`just models-fetch`): 206 of the 231 upstream lists, 576,537 entries, 16,007,698 bytes (16 MB on disk), sha256 per list in `SNAPSHOT.json`; the 25 unused lists (REASON in the module, e.g. the 1M popularity rankings and 57 MB Windows hashes) are not downloaded | known shared or special values (CDN/cloud front doors, public DNS, special-use ranges and names, sinkholes, real bank sites, Tor/VPN/private-relay egress, reserved phone numbers, popular sites) that lower a gene's tier (boilerplate, label, parked, platform) or add a note (hosters, scanners) | none | static files, no runtime calls; the same snapshot is on the laptop and the server | CC0-1.0 (repository README); X4BNet datacenter/VPN lists MIT (notice in the list description) |
| E7 | Meta Ad Library API (bonus, `hydra/enrich/adlibrary.py`, used by the scheduler) | `graph.facebook.com/v21.0/ads_archive` | ads by keyword, country and date: id, page, creative text, link URLs, snapshot URL, delivery dates, platforms; flagged by rules (keywords, invented platform names, known campaign hosts or URL skeletons, new-domain age) | user token (Meta developer account + identity confirmation) via `META_AD_LIBRARY_TOKEN` (or `HYDRA_META_TOKEN`, or `~/.config/hydra/meta.env`); never in the repo, never printed | 2 s between calls; 3 attempts per page with 1/2/4 s backoff on exceptions, HTTP 429/5xx and Graph transient codes; up to 5 pages of 100 per query; watermark (last complete run minus 2 days), resume cursor, page-id sweep at most 10 ids per request (since 2026-11-21) | Meta Ad Library API terms; the token is never logged, cached or stored. [U] whether the API returns all ads delivered in Poland or only political/issue ads (EU DSA transparency scope) |

### Ad library without a token (manual import)

Without a token (`META_AD_LIBRARY_TOKEN`, `HYDRA_META_TOKEN` or `~/.config/hydra/meta.env`) and without a cached response the connector returns `unavailable` (hint search: `not_configured`) with a Polish
explanation of how to obtain access. An analyst can instead import what they downloaded by hand: Ad Library CSV or
JSON exports, API-shaped JSON, a saved ad snapshot HTML page, a screenshot, or the yt-dlp `.info.json` of an Ad
Library video (the format of one sample incident). Each becomes an incident and goes through the normal pipeline.
The connector was tested on synthetic API responses only; no live call with a real token was made during the event.

## Optional online sources (off by default or dropped)

Used in the analysts' run on the sample; available as optional enrichment, not needed for evaluation.

| # | Source | Endpoint | Gives | Auth | Limits | Terms |
|---|---|---|---|---|---|---|
| X1 | Shodan InternetDB | `https://internetdb.shodan.io/<ip>` | open ports, hostnames, tags, CPEs, known vulns for an IP; weekly refresh | none | none published; used about 1 request/s | "free for non-commercial use"; commercial use needs a Shodan enterprise licence (internetdb.shodan.io, read 2026-11-21). **Flag: non-commercial** |
| X2 | urlscan.io Search API | `https://urlscan.io/api/v1/search/?q=domain:<d>` | earlier public scans by other users: first/last scan, IPs, ASNs, page titles, sibling domains | none for search; an API key raises quotas | unauthenticated users get "minor quotas" (docs.urlscan.io, read 2026-11-21); the response reported a 30-day search window; used 2.5 s between calls | urlscan.io terms of service; results are user-submitted scans. **Flag: quota and terms** |
| X4 | certstream-server-go (self-hosted, MIT, https://github.com/d-Rickyy-b/certstream-server-go), used by `hydra/watch/ct_recorder.py` | loopback websocket, e.g. `ws://127.0.0.1:8098/domains-only` | names on newly issued certificates (Watch module, new-domain lead time) | none (own host) | bounded by the CT logs the server polls; HYDRA only listens and records JSONL | CT log operators' usage policies; CT data is public by design (RFC 6962). Off by default: the demo replays recordings |
| X3 | Internet Archive Wayback CDX | `https://web.archive.org/cdx/search/cdx?url=<d>&matchType=domain&output=json` | first/last capture of a domain and its URLs (page age) | none | informal; heavy use gets 429; used 1.5 s between calls | Internet Archive terms of use (*unverified* for API limits); captures are third-party content |

## Considered, not used

| Source | Gives | Why not, or what is needed |
|---|---|---|
| Port-43 WHOIS | registrar and dates for ccTLDs without RDAP | RDAP covered every TLD in the sample; WHOIS is unstructured and rate-limited per registry. Fallback only (the `.pl` registry has RDAP too) |
| SecurityTrails, WhoisXML, DomainTools | passive DNS, historical WHOIS, reverse NS/IP | paid API keys; terms forbid redistribution. Possible plug-in |
| VirusTotal | passive DNS, detections, related URLs | free key with low quotas and non-commercial terms (*unverified* numbers). Optional |
| Shodan full API (favicon hash search) | other hosts serving the same favicon | needs a paid key; HYDRA already computes the Shodan-compatible mmh3 favicon hash so the pivot is ready |
| Censys | certificates, hosts | free account key with a monthly quota (*unverified* number) |
| Direct HTTP fetch of landing URLs (live redirect chain, live DOM) | current state of the page | hostile pages; not done in the analysts' run. In the product only as an opt-in, isolated, no-JavaScript fetcher (ADR-007) |

## Reliability in the analysts' live run and default setting (measured, sample, 2026-11-21)

Source: the infrastructure analyst's run of `infra_enrich.py` on the 10-incident sample (outside git); counts are
calls, not hosts. "Default" is the setting HYDRA ships with when `HYDRA_ENRICH=1`.

| Source | Result | Default | Observation |
|---|---|---|---|
| RDAP domain (E1) | 24 / 24 ok | on | every gTLD resolved through rdap.org; registrant fields redacted everywhere |
| RDAP IP (E1) | 67 / 67 ok | on | all CDN ranges |
| DNS (E2) | 266 queries, 6 failed | on | failures were SERVFAIL or timeouts (5 Team Cymru TXT, 1 AAAA), retried on the next run |
| Team Cymru ASN (E3) | ok (62 IPs) | on | every lure host is in one CDN's AS (Cloudflare), so ASN carries no operator signal there |
| crt.sh (E4) | 22 / 23 ok | on | 1 timeout; 5-30 s per query; run asynchronously with a long timeout |
| CERT Polska snapshot (E5) | ok | on | 2 of 28 operator hosts listed; used as a cluster label, not as a link |
| urlscan.io (X2) | 25 / 25 ok | optional, off | without a key the search covers only the last 30 days |
| Wayback CDX (X3) | 16 of 23 timed out | dropped | lowest value per second; fresh lure domains have almost no captures |
| Shodan InternetDB (X1) | 67 / 67 ok | off | for CDN IPs it returns unrelated co-hosted sites; non-commercial terms |

## Which enrichment facts helped (analyst review, sample)

| Fact | Tier | Finding on the sample |
|---|---|---|
| Registrar + registration date (within 3 days) | operator | three lure domains of the news-bait clones were registered on the same day at two registrars; registrar alone is boilerplate (large retail registrars recur across unrelated hosts) |
| Domain age at capture | label | lure domains were 0-30 days old; backend domains (CRM, push, tracker) were years old and are the better pivots |
| Authoritative nameserver pair | operator | Cloudflare assigns a fixed NS pair per account; a pair plus a time window is specific; no pair repeated across incidents in this sample |
| Certificate history (crt.sh) | label / operator | the first certificate coincides with registration (go-live); SAN inventories of a tracker domain listed per-buyer instances; issuers are boilerplate |
| IP, ASN, InternetDB | boilerplate | all lure hosts behind Cloudflare: 0 bits |
| Current DNS | liveness | shows takedowns and parking; not a link gene; changes daily |
| CERT Polska list | label | listed hosts propagate a "cluster has a listed host" label to unlisted siblings |

Reading: on CDN-fronted pages the IP, ASN and InternetDB answers describe the CDN, not the operator. The useful
online facts were registration dates and registrars, nameserver pairs, certificate history and warning-list
membership; the strongest infrastructure genes came from the offline HAR and saved pages (`docs/METHODOLOGY.md`
§3, infrastructure gene tiers).

## Public demonstration corpus (measured, server, 2026-11-21)

Source: internal notes, not published (merged in integration `818478b`). A separate corpus of
6,663 Polish-targeted phishing incidents (one per host) from three public feeds, enriched through the same
enrichment layer. It has no ground truth: its campaigns are candidates for an analyst, not verdicts.

| Feed | Licence / terms | Rows |
|---|---|---|
| CERT Polska warning list, domains inserted since 2026-11-18 | free for any use; no endorsement implied | 4,327 |
| PhishTank `online-valid.csv`, Polish-targeted | free use including commercial (PhishTank FAQ); attribution | 1,227 |
| PhiUSIIL Phishing URL Dataset (UCI 967), Polish-targeted | CC BY 4.0 | 1,556 |

OpenPhish was excluded by its licence. No page was fetched.

Enrichment of up to 2,500 registrable domains (two runs, about 40 min in total):

| Source | Result | Note |
|---|---|---|
| RDAP | 2,191 of 2,500 answered (88%; 1,801 records + 390 "no such domain"), 62 registry servers via the IANA bootstrap, 99 domains via rdap.org | 136 HTTP 429 from `rdap.dns.pl`, the `.top` and `.shop` registries; per-server backoff (1 request/s, 30 s x 2 on 429/403) |
| DNS (`dig`) | 19,256 answers for 3,491 hosts and their domains: 9,143 with records, 10,113 NXDOMAIN, 256 SERVFAIL | more than half of the names no longer exist: phishing domains are short-lived |
| Team Cymru ASN | 1,446 unique IPs, about 1,445 mapped | DNS TXT interface |
| crt.sh | 169 of 2,500 queued domains answered (7%), 2 HTTP 502 | about 5 domains/min with 2 threads and 3 s spacing: the bottleneck |

Result of the run (`analyse-20261121T150920-5e65ce`): 421 multi-member campaigns with 1,837 incidents (largest 65);
55 campaigns mix hosts on the CERT Polska list with unlisted ones, giving 161 unlisted hosts as review candidates;
full pipeline 3 min 36 s, peak RSS 4.9 GB on the 64-vCPU server. Limits stated in the source document: campaign
groups chain at this scale (one group spans 1,431 campaigns) and are not shown for this corpus until CORE's group
guard lands; the two largest clusters (65 and 49) are tenants of shared-hosting parent domains, being fixed.

## HYDRA Watch: CERT Polska live feed (in progress)

| Source | What it gives | Terms | Status |
|---|---|---|---|
| CERT Polska warning list, live (`hole.cert.pl/domains/v2/domains.json`, polled) | new and removed warning-list domains since the last poll; each change is written to the Watch actions log with the poll timestamp and matched against known campaigns | free for any use; no endorsement implied; poll politely (the list is a static file) | being built; poll interval, log location and matching rules are recorded here when it lands [U] |

## External labelled datasets (calibration and evaluation, not campaign truth)

Merged from DATA-HUNT (`docs/datasets/EXTERNAL.md`). These are offline datasets, not live services; they are
listed here because they are the main public metadata and label sources available for linking.

| Dataset (`ext:<name>`) | Source | Licence / terms | Verdict and use |
|---|---|---|---|
| phreshphish | https://huggingface.co/datasets/phreshphish/phreshphish @eca603e; paper arXiv 2507.10854 | CC BY 4.0; the card adds "should only be used for anti-phishing research" (noted; HYDRA's use is anti-phishing) | SHIP-SAFE: calibration, priors, evaluation; pages never redistributed |
| phreshphish-2k | https://huggingface.co/datasets/gt2026workshop/phreshphish-2k @7ceeea2 | CC BY 4.0 | SHIP-SAFE: calibration and evaluation; screenshots never redistributed |
| purino-scam-ads | https://huggingface.co/datasets/Purino/scam-ads-detection-dataset @484306e | card says MIT, but the ad creatives are third-party content (the uploader's claim) | calibration and evaluation only; creatives never redistributed or shown in public material |
| phishing-websites-putra | https://zenodo.org/records/8041387 | CC BY 4.0 (Zenodo) | SHIP-SAFE: calibration and evaluation; pages never redistributed |
| phish-screens | https://huggingface.co/datasets/shresthsamyak/phishing-website-screenshots @757b719 | "other": defensive research and education; review third-party rights before redistribution or commercial use | EVAL-ONLY: nothing shipped is fitted on it |
| phishdecloaker-kits | https://zenodo.org/records/11228974 (`phishing-kits-dataset.zip`) | CC BY 4.0 | SHIP-SAFE; stored zipped, no adapter yet |

| Label kind | Datasets | What it can calibrate |
|---|---|---|
| target brand | phreshphish, phreshphish-2k, putra, phish-screens | visual and logo genes, campaign groups; too coarse for same-campaign |
| advertiser page id | purino-scam-ads | closest public analogue of "same campaign" for ad creatives; weak |
| source site | phish-screens (legitimate part) | template and asset sharing as a hard positive; cross-brand false-merge trap |
| kit | phishdecloaker-kits (no adapter yet) | kit-level truth for DOM and asset genes |

No public Polish scam-ad or Polish phishing-page corpus with screenshots was found; the Polish material is the
PhreshPhish `lang=pl` slice and the Allegro kit in PhishDecloaker.

Checked, not used: Phishpedia, PhishIntention and DynaPD (code repositories CC0 or unlicensed, **no data
licence**: evaluation-only if ever used, not downloaded); VisualPhish, PILWD-134K and the deepfake sets
(Deepfake-Eval-2024, Celeb-DF, FF++) excluded (e-mail request, form or gated access). Full list with URLs:
`docs/datasets/EXTERNAL.md`.

Rules for these datasets:

1. **Brand labels are group-level, not campaign truth.** Pages imitating one brand often come from different kits
   and operators; they calibrate visual and logo genes and the "similar campaigns" level, never same-campaign
   decisions. The closest public analogue of a campaign is the advertiser page id of purino-scam-ads, and it is
   a weak label too.
2. **Separated by the corpus flag.** Imports get `incidents.source = "ext:<dataset>"`; the default run is NASK-only
   and excludes them; they enter only with `--corpus extended|all|ext:<name>` (`hydra/corpus.py`). Labels never
   enter the incident files, so they cannot leak into genes.
3. **Nothing external ships inside the product** except, if enabled, derived priors (document-frequency priors,
   thresholds, fusion parameters) fitted on SHIP-SAFE sources. No page, screenshot or ad creative is
   redistributed; numbers from EVAL-ONLY sources may be cited with attribution.
4. Data lives outside git on the server (`~/HydraNexus/data/external-big/<dir>/`, each with a `SOURCE.md`,
   sha256 pinned in `hydra/adapters/datasets/datasets.lock.json`).

## Usefulness for linking (measured, internal notes, not published, run `eval-20261121T150324`)

- **Coverage on the sample** (share of the 10 incidents with the gene kind observed): registrar, registration
  day, nameserver set, certificate issuer and first month, ASN 0.900 each (the incident without a landing page has
  none); CERT Polska listed host 0.300.
- **Enrichment unreachable** (SYNTHETIC robustness case, replay with an empty cache, n = 10): assignment accuracy
  1.000, agreement with the undegraded run 1.000, gene stability 0.963. On the sample the decisions do not depend
  on the online lookups; the offline HAR and page genes carry them.
- **Infrastructure as a family** (SYNTHETIC `sample+syn` ablation): removing INF, which includes the enrichment
  genes, lowers campaign F1 by 0.641; INF alone reaches campaign F1 0.926.
- **Not measured**: a per-source ablation (RDAP vs DNS vs crt.sh); the evaluation drops whole families only.
