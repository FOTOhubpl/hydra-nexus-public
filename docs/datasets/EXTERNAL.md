# External datasets (DATA-HUNT, 2026-11-21)

Clarification received during the event (2026-11-21 ~16:00): larger external datasets may train/calibrate
HYDRA and enlarge the corpus, if they stay clearly separated from organiser data (corpus flag) and their origin and
licence are stated. This file is that statement. Data never enters git; it lives on the server under
`~/HydraNexus/data/external-big/<dir>/`, each folder with a `SOURCE.md` (URL, licence, verdict, sha256 per file).

## Licence policy

The winners transfer economic copyright to NASK (regulamin §4), so:

| Verdict | Meaning |
|---|---|
| **SHIP-SAFE** | permissive / CC BY / CC0 / MIT. May be used for anything we ship (rarity priors, lexicons, thresholds, fusion models), with attribution in docs/LICENSES.md and DISCLOSURE. Page or ad bytes are third-party content: never redistributed, never shown in public material. |
| **EVAL-ONLY** | no open licence or non-commercial / no-derivatives terms that still allow research evaluation. Used only to *measure* (numbers may be cited with attribution). Nothing shipped is fitted on it. |
| **EXCLUDED** | login, form, e-mail request, or terms that forbid our use. Not downloaded. |

## Candidates checked (2026-11-21)

Sources: Hugging Face API (`cardData.license`, `gated`), Zenodo API (`metadata.license`), Mendeley public API, GitHub
API (`license.spdx_id`), raw READMEs. "Grouping" = a label that says which items belong together (brand target, kit,
advertiser page, source site); these labels are what calibrates and evaluates linking.

### Downloaded (pinned in `hydra/adapters/datasets/datasets.lock.json`)

| name (`ext:<name>`) | URL | size on disk | modality | labels (grouping) | licence | verdict |
|---|---|---|---|---|---|---|
| phreshphish | https://huggingface.co/datasets/phreshphish/phreshphish @eca603e | 36.6 GB, 666,315 pages | HTML + URL, date, language | phish/benign; **target brand** for 163,336 phish pages (3,614 brands, 2,203 with >= 2 pages); Polish slice: 1,366 pages (985 phish, 714 with a brand, e.g. allegro 201) | CC BY 4.0 ("should only be used for anti-phishing research") | SHIP-SAFE |
| phreshphish-2k | https://huggingface.co/datasets/gt2026workshop/phreshphish-2k @7ceeea2 | 88 MB, 2,000 | **screenshot** + URL (+ HTML joined from phreshphish by sha256) | phish/benign; target brand (402 phish with a named brand in 92 brands, 34 with >= 2) | CC BY 4.0 | SHIP-SAFE |
| purino-scam-ads | https://huggingface.co/datasets/Purino/scam-ads-detection-dataset @484306e | 706 MB, 3,705 ads | **ad creative image + ad text** | scam/non_scam; **advertiser page_id**: 1,010 scam ads, 145 pages with >= 2 scam ads (461 ads). The 2,695 non-scam rows are generic banners under a few synthetic page ids | card: MIT (uploader's claim over third-party ad creatives) | SHIP-SAFE by card, used for calibration/eval; creatives never redistributed |
| phishing-websites-putra | https://zenodo.org/records/8041387 | 8.8 GB (phishing half), 5,151 sites | **screenshot + original HTML + assets** + URL, WHOIS/IP/ASN/TLS in CSV | **target brand** (85 brands; microsoft 1,107, facebook 406, genericemail 256, paypal 241, ...) | CC BY 4.0 (Zenodo) | SHIP-SAFE |
| phish-screens | https://huggingface.co/datasets/shresthsamyak/phishing-website-screenshots @757b719 | 5.8 GB, 8,370 PNG | screenshot + URL / final URL | phishing by target brand; legitimate by **site** (homepage/login/signup/checkout of one brand) | "other": defensive research and education, review third-party rights before redistribution or commercial use | EVAL-ONLY |
| phishdecloaker-kits | https://zenodo.org/records/11228974 (`phishing-kits-dataset.zip`) | 173 MB, 100 kits | defanged PHP phishing **kits** | brand per kit (Allegro among them) | CC BY 4.0 | SHIP-SAFE; stored zipped, no adapter yet (kits are code, not captured pages) |
| web-legit-tranco | Tranco list 46YVX (https://tranco-list.eu/list/46YVX, created 2026-11-20) + the homepage of each selected domain, fetched once 2026-11-21 by `scripts/fetch_web_legit.py` | 171 MB (zstd parquet, HTML gzip-compressed), 3,149 pages (pl 2,082 + global 1,067) of 5,000 attempted | HTML (raw bytes, <= 2 MB) + final URL, status, content type, fetch time | none (all benign, `group_label ext:web-legit-tranco:benign`) | Tranco: no licence of its own stated, citation requested; upstream Umbrella (free), Majestic CC BY 3.0, Farsight, CrUX CC BY-SA 4.0, Cloudflare Radar CC BY-NC 4.0; pages: third-party content | SHIP-SAFE: derived hashed document-frequency statistics only; page content never redistributed; operator instruction 2026-11-21 |

Earlier lab downloads (mvp-lab/scale, `data/external-lab`): CERT PL list, PhishTank, PhiUSIIL, OpenPhish, DISC21,
Copydays, POLfake, MIPD, polish-news, Smishing IMC25. Verdicts there: `mvp-lab/scale/RESULTS.md`. The CERT PL /
PhishTank URL corpus is built by the PUBLIC-DATA stream (`public:<name>`), not here.

### Checked, not downloaded

| name | URL | modality / labels | licence | verdict |
|---|---|---|---|---|
| Phishpedia 30k phish / 30k benign | Google Drive 12ypEMPRQ43zGRqHGut0Esq2z5en0DH4g / 1yORUeSrF5vGcgxYrsCoqXcpOUHt-iHq_ (github.com/lindsey98/Phishpedia) | screenshot + HTML + URL, brand folders; 18 + 20 GB | repo CC0-1.0; data has no stated licence | EVAL-ONLY (next candidate: best brand benchmark) |
| PhishIntention | Drive folder 1zaHuX4brbhmTsa3MoAD9zPVUybklwJC1 | screenshot + HTML, brand | repo CC0-1.0, data unstated | EVAL-ONLY |
| DynaPD (DynaPhish / PhishLLM) | Drive 1o2Hgr3SvtcsVsMiB4gnSafMezc_4FSLa (21 GB) | 6k live phishing kits | no licence | EVAL-ONLY |
| KnowPhish TR-OP | Dropbox link in github.com/imethanlee/KnowPhish | 5k phish / 5k benign, brand [unverified] | no licence | EVAL-ONLY |
| Phish-IRIS | Mendeley 10.17632/tk4fkswtj5 (845 MB) | screenshots, 14 brands + other | Mendeley: CC BY 4.0, official page: "academic purposes only" | EVAL-ONLY (conflicting terms) |
| 7 Days Later / What's in Phishers | Zenodo 14885725, 14880589 (12 + 40 GB) | longitudinal screenshots + HTML | CC BY 4.0 | SHIP-SAFE, not fetched (time) |
| PhishRepo; Phishing Websites 80k; CompPhish v4 | Mendeley ttmmtsgbs8, n96ncsr5g4, fmbs4kp9wz | raw pages / URL + HTML, binary labels | CC BY 4.0 | SHIP-SAFE, no grouping labels |
| AU 2022 election FB ads | Zenodo 13661563 | Ad Library text/metadata, funding entity | CC BY 4.0 | SHIP-SAFE, political, no creatives [unverified] |
| MIPD (NASK, EMNLP 2024) | github.com/ArkadiusDS/MIPD | 15k Polish articles, disinfo techniques, 400+ sources | CC BY-NC-ND 4.0 | EVAL-ONLY (already in external-lab) |
| POLfake; CheckThat! 2025 task 2 (Polish) | github.com/MateuszWalczakTUL/POLfake; gitlab checkthat_lab | Polish claims, tweet → claim id | none stated | EVAL-ONLY |
| HiDF deepfake | Zenodo 16140829 (42 GB) | deepfake video | CC BY-NC 4.0 | EVAL-ONLY, not relevant enough to fetch |
| VisualPhish, Phish-IRIS official, PILWD-134K, PhiKitA-500, Deepfake-Eval-2024, Celeb-DF, FF++ | – | – | e-mail / form / gated | EXCLUDED |
| Meta Ad Library dumps | – | – | API token + identity check | EXCLUDED (the scheduler connector uses the API with the operator's token) |

No public Polish scam-ad or Polish phishing-page corpus with screenshots was found; the Polish material available
is the PhreshPhish `lang=pl` slice (allegro, facebook, whatsapp, punktum.dk lures) and the Allegro kit in
PhishDecloaker.

## Fetch

```
python3 scripts/fetch_external_big.py                 # fetch missing, verify sha256, write SOURCE.md, mirror the lock
python3 scripts/fetch_external_big.py --verify-only   # offline check
python3 scripts/fetch_external_big.py --only phreshphish-2k,purino-scam-ads
```

Stdlib only, idempotent, no login. HF files are pinned at a commit with their LFS sha256; Zenodo publishes md5,
which is checked, and sha256 is pinned on first fetch. Anonymous HF downloads hit HTTP 429 under load; the script
backs off and retries (8,370 small PNGs took ~25 min).

## Adapters and corpus flag

`hydra/adapters/datasets/` (registry `list_datasets`, `iter_incidents(name, root, limit=None, **select)`,
`materialise`, `import_corpus`). Each item becomes an incident folder that `hydra.adapters.nask_folder` imports
unchanged:

| dataset field | file in the incident folder | artifact role |
|---|---|---|
| page screenshot | `screenshot.<ext>` | `landing_screenshot` (explicit role override) |
| page HTML | `landing.html` | `landing_html` |
| saved page assets (putra) | `assets/<file>` | `landing_asset` |
| URL | `url.txt` | `url` |
| ad image | `ad_creative.<ext>` | `ad_creative` |
| ad text | `ad_text.txt` | `text` |

Import sets `incidents.source = "ext:<dataset>"` (corpus `ext:<dataset>` in `hydra.corpus`: excluded from the default
NASK-only run, included by `--corpus extended|all|ext:<name>`), `incidents.label = "ext:<dataset>:<label>"` and
`meta.group_label = "ext:<dataset>:<class>"` (+ `meta.ext` = dataset, label kind, verdict), the fields
`hydra train --corpus ext:<name>` reads. Labels never enter the incident files (no leakage into genes).

```
python -m hydra.adapters.datasets list
python -m hydra.adapters.datasets stats "phreshphish:lang=pl,min_group=2"
python -m hydra.adapters.datasets materialise --root <external-big> --out <external-big>/corpus-demo \
    --mix "phreshphish-2k:limit=360,min_group=3,per_group_cap=30,unlabelled=60" \
    --mix "phreshphish:limit=240,min_group=2,per_group_cap=40,unlabelled=40,lang=pl" \
    --mix "purino-scam-ads:limit=300,min_group=3,per_group_cap=12,scam_only=true"
python -m hydra.adapters.datasets import <external-big>/corpus-demo
hydra analyse --corpus ext:phreshphish-2k,ext:phreshphish,ext:purino-scam-ads
```

Materialised on the server (2026-11-21, `~/HydraNexus/data/external-big/`):

| corpus | incidents | content | label groups (all >= 2 members) | open-set negatives |
|---|---|---|---|---|
| `corpus-demo` (321 MB) | 825 | phreshphish-2k 289 (screenshot + HTML + URL), phreshphish `lang=pl` 239 (HTML + URL), purino-scam-ads 297 (ad image + text) | 19 brands / 14 brands (Polish slice) / 66 advertiser pages; largest 30 / 40 / 12 | 60 + 40 unlabelled (benign, phish without a brand); 0 page overlap between the two PhreshPhish parts |
| `corpus-putra` (1.6 GB) | 500 | phishing-websites-putra: screenshot + original HTML + assets + URL | 20 brands, 25 sites each | none |

Selection (`_common.select`) is deterministic: label groups with >= `min_group` members, at most `per_group_cap`
per group, whole groups first, plus `unlabelled` open-set negatives (benign pages, phish without a named brand).

**Do not run `hydra analyse <corpus folder>`**: path imports are tagged `nask-sample` by `import_paths_as_incidents`.
Contract request (for CORE): honour `<root>/hydra_corpus.json` (source, label, roles per folder) in
`import_paths_as_incidents`, so a path import of an external corpus keeps its flag.

## Background legit corpus for the TRAIN document-frequency prior (2026-11-21)

Purpose: the TRAIN stream counts in how many documents each (hashed) gene value occurs over a large fraud + legit
corpus, so values that are common on the open web (CDNs, tracker loader defaults, CMS templates, cookie / RODO
banners, shop and news footers) do not look rare on a 10-incident local corpus. Fraud pages were plentiful
(PhreshPhish, putra, purino); legit, especially Polish legit, pages were missing. Only hashed counts derived from
these pages may ship.

`web-legit-tranco` (server: `~/HydraNexus/data/external-big/web-legit-tranco/`, `SOURCE.md`, `stats.json`):

1. Seed: Tranco list 46YVX (default list, 30 days to 2026-11-20, PLD, providers crux/farsight/majestic/radar/umbrella),
   downloaded once 2026-11-21 17:54Z. Selection in rank order: the first 3,000 `.pl` domains (rank <= 393,722) and the
   first 2,000 non-`.pl` domains (rank <= 2,336), after a keyword filter that skipped 401 hosts (global: infra/CDN/API
   228, adult 94, gambling 4, malware 1; pl: gambling 40, infra 30, malware 3, adult 1). No Polish `.com`/`.eu` sites
   were added.
2. Fetch (measured, `stats.json`): `https://<domain>/` once, redirects followed, 15 s deadline, 2 MB cap, 16 threads,
   UA `Mozilla/5.0 (compatible; research-crawler)`, `robots.txt` checked first (a `/` disallow, or HTTP 401/403 on
   robots.txt, skips the domain), no JS, no cookies.

| group | attempted | ok (200 + text/html) | HTML (ok) | main failure reasons |
|---|---|---|---|---|
| pl | 3,000 | 2,082 | 595 MB raw, median 162 KB | HTTP 403 289, TLS 150, robots 403 147, robots disallow 114, timeout 72, DNS 58 |
| global | 2,000 | 1,067 | 404 MB raw, median 203 KB | DNS 278 (API/infra apex without a web host), robots 403 177, HTTP 403 127, timeout 93, robots disallow 92, TLS 49 |

   3,149 ok pages have 3,078 distinct bodies (71 duplicates: parked or identical landing pages); 32 bodies hit the
   2 MB cap; 36 `.pl` pages redirect to a non-`.pl` host.
3. Caveats: the `.pl` tail (ranks above ~100k) contains spam/SEO domains (the filter removed 40 casino names, more may
   remain); a homepage is not a landing page of an ad; anti-bot walls (Cloudflare 403) bias against large shops.
   The list is used only to choose which public homepages to fetch and is not redistributed.
4. Adapter `hydra/adapters/datasets/web_legit.py` (`ext:web-legit-tranco`, label kind `none`, modality html+url): one
   page = `landing.html` (raw bytes) + `url.txt` (final URL); no labels, so select with `unlabelled=N`; option
   `tld=pl|global`. Example: `python -m hydra.adapters.datasets stats "web-legit-tranco:unlabelled=100000,tld=pl"`.

Other legit material checked the same day (aggregates from the adapter index / CSV on the server):

| source | legit (benign) rows | of which Polish | phish rows | of which Polish | has HTML |
|---|---|---|---|---|---|
| phreshphish (index, `lang` field) | 367,989 (train 276,729, test 91,260; en 326,957, ja 18,048, it 6,179) | 381 (`lang=pl`) | 298,326 | 985 | yes |
| PhiUSIIL (`data/external-lab/phiusiil`, UCI 967, CC BY 4.0) | 134,850 (label 1) | 538 (`TLD=pl`) | 100,945 (label 0) | 801 | no: URL, title and 54 numeric features only; no adapter |

## What the labels are worth

| label kind | datasets | what it calibrates |
|---|---|---|
| brand target | phreshphish, phreshphish-2k, putra, phish-screens (phish) | coarse: pages imitating one brand share logos/layout but often come from different kits/operators. Good for VIS/logo genes and for "similar campaign" (group level); too coarse as a same-campaign truth |
| advertiser page | purino-scam-ads | ads from one advertiser page: closest public analogue of "same campaign" for ad creatives (variants of the same product pitch) |
| source site | phish-screens (legitimate) | several pages of one site: template/asset sharing as a hard positive, and a false-merge trap across brands |
| kit | phishdecloaker-kits | kit-level truth for DOM/asset genes (brand per kit; 6 brands with >= 2 kits) |

Smoke run (server, 2026-11-21, 80 incidents: 40 phreshphish-2k + 40 purino scam ads, isolated state dir):
import 200 artifacts, 0 errors; `hydra analyse --corpus ext:phreshphish-2k,ext:purino-scam-ads` finished in 2 min 9 s
with 8 extraction workers. Linking quality on these corpora is EVAL's measurement, not reported here.

## DATA-MASS additions (2026-11-21)

To train the system to its limits, DATA-MASS added bulk fraud + legitimate datasets and a parallel bulk
materialiser (`scripts/materialise_mass.py`: per-class quotas in hash order, parallel across parquet shards,
skips the eval corpora). Full ready-list with counts: `~/HydraNexus/data/external-big/READY.md`.

### New adapters / datasets (all with tests under `tests/datasets/`)

| name (`ext:<name>`) | dir | modality | label kind | n available | licence | verdict |
|---|---|---|---|---|---|---|
| certpl-warnings | certpl-warnings | url | none | 126,509 (all Polish-targeted) | public list, free use (CERT Polska, no endorsement) | SHIP-SAFE |
| phishtank | phishtank | url | brand_target | 43,127 (66 brands >= 2; 40 .pl) | PhishTank/Cisco Talos: free incl. commercial, attribution (FAQ) | SHIP-SAFE |
| phiusiil | phiusiil | url | none | 235,795 (134,850 legit / 100,945 phish; 1,339 .pl) | CC BY 4.0 (UCI id 967) | SHIP-SAFE |
| smishing-imc25 | smishing-imc25 | text | named_entity | 33,869 SMS (1,474 entities >= 2; 133 Polish) | CC BY 4.0 | SHIP-SAFE |
| phishdecloaker-kits | phishdecloaker-kits | kit (page+assets+indicators) | kit | 100 kits (93 brands, 6 with >= 2; 99 with exfil indicators; kit19 Allegro, lang=pl) | CC BY 4.0 (Zenodo 11228974) | SHIP-SAFE |

The feed URL/text sets (certpl, phishtank, phiusiil, smishing) reuse files already fetched to `external-lab`
(PhiUSIIL, PhishTank, Smishing IMC25) plus a fresh CERT PL / PhishTank snapshot (2026-11-21 18:29 UTC); each
dir has its own `SOURCE.md`. phishdecloaker-kits now has an adapter: the kit ZIP is read with `zipfile` only and
never executed; each kit yields an inert `landing.html` (PHP blocks stripped), `assets/` (static files, vendored
libs skipped), `file_tree.txt` and `kit_indicators.txt` (regex-extracted Telegram/e-mail/exfil sinks). Brand per
kit is the positional list from the Zenodo record (label kept out of incident files, no gene leakage).

### Materialised + imported (own states, verification + ready corpora)

`~/HydraNexus/data/external-big/mass-<name>/` imported into `~/HydraNexus/state-mass-<name>/` (own import
states, never mixed with organiser data). phreshphish mass corpus = phish 25k + benign 25k + full Polish slice
(51,127 incidents). URL/text feed corpora: certpl 59,994 (Polish), phishtank 43,127, phiusiil 59,121 (legit+phish),
smishing 33,869, kits 100. Counts per dataset and import status: `READY.md`. The intermediate `mass-*` folders are
disposable after import (the state CAS holds the content; TRAIN re-materialises with its own quotas).

### Polish-language additions (subagent cluster, 2026-11-21)

Found by searching HF/Zenodo/GitHub/dane.gov.pl. Conclusion: no public Polish scam-*ad* corpus (ad creatives or
ad text) exists; the Polish material available is e-mail, fact-check claims, a few reconstructed scam pages, and
domain lists. Already-known Polish sets (MIPD, POLfake, CheckThat) remain in `external-lab` (EVAL-ONLY).

| name (`ext:<name>`) | dir | modality | label kind | n | by class | lang=pl | licence | verdict |
|---|---|---|---|---|---|---|---|---|
| phishplens | phishplens | email text | none | 755 | phishing 377 / spam 132 / ham 246 | 461 | CC BY 4.0 (Zenodo 23579138, AGH) | SHIP-SAFE |
| xfact-pl | xfact-pl | text (claim) | claimant | 1,768 | true 1,034 / false 413 / partly 242 / complicated 79 | 1,768 | MIT (utahnlp/x-fact) | SHIP-SAFE |
| scamworld | scamworld | screenshot+html+url | source_site | 430 (5 Polish) | scam 377 / legit 53 | 5 | CC BY-NC 4.0 | EVAL-ONLY |
| kad-pl | kad-pl | url | none | 2,610 | scam/suspicious by section | 2,610 (targeted) | CC BY-SA 4.0 | EVAL-ONLY (share-alike conflicts with §4 copyright transfer) |

- phishplens: pseudonymised university phishing/spam/ham e-mails with 7 explanation attributes; the best Polish
  phishing corpus found (461 Polish messages, 249 phishing). Incident = `ad_text.txt` (Subject+From+body).
- xfact-pl: Polish claims fact-checked by demagog.org.pl; 258 claimants with >= 2 claims (claimant grouping);
  claimant kept out of incident text.
- scamworld: research *reconstructions* of scam sites with planted fake elements; only the 5 Polish scam tasks
  fetched (ORLEN fake-investment clone, Steam prize scam, 2 fake shops, Allegro Lokalnie). EVAL-ONLY (non-commercial).
- kad-pl: Polish anti-scam filter list (KAD); url-only; CERT-mirror section skipped (duplicates certpl-warnings).
  EVAL-ONLY: CC BY-SA share-alike is incompatible with transferring economic copyright to NASK (regulamin §4).

### Batch 2 (2026-11-21 evening): pages, operator-requested Polish sources, leakage check

| name (`ext:<name>`) | dir | modality | label kind | n (imported) | licence | verdict |
|---|---|---|---|---|---|---|
| phishing-websites-putra-legit | phishing-websites-putra-legit | screenshot+html+assets+url | source_site | 5,244 legit sites | CC BY 4.0 (Zenodo 8041387, same record as the phishing half) | SHIP-SAFE |
| phishing-websites-80k | phishing-websites-80k | html+url | source_site | 80,000 (50,000 legit / 30,000 phish) | CC BY 4.0 (Mendeley n96ncsr5g4 v1) | SHIP-SAFE |
| phishing-alive-7days | phishing-alive-7days | screenshot+html | same_url_over_time | 6,569 re-crawls of 2,217 URLs | CC BY 4.0 (Zenodo 14885725, streamed sample) | SHIP-SAFE |
| certpl-tollfraud-2026 | certpl-tollfraud-2026 | indicators+url | operation | 103 (34 apps, 60 advertiser profiles, 9 hosts) | no open licence ((c) 2026 NASK) | EVAL-ONLY + **LEAKAGE-RISK** |
| csirt-knf | csirt-knf | ad/page/SMS figures + Polish text | named case | 616 (30 monthly reviews) | (c) UKNF; KNF nota prawna: reuse with source + link | EVAL-ONLY |
| polfake | polfake | text | claim_id | 7,227 (5,774 claims + 1,453 tweets) | no licence | EVAL-ONLY |
| mipd | mipd | text | none (techniques in meta) | 15,356 | CC BY-NC-ND 4.0 | EVAL-ONLY |
| political-ads-pl | political-ads-pl | text | none | 1,701 | CC BY-NC-SA 4.0 | EVAL-ONLY |
| AdReport | - | - | - | - | commercial access | EXCLUDED |

- **certpl-tollfraud-2026** is built from CERT Polska's public post on the toll-fraud fake-ad operation
  (https://cert.pl/posts/2026/09/analiza-tollfraud/, 2026-09-23). The 898 ad files mentioned in the post are not
  published, so the adapter parses only the post's tables and IOC lists with regular expressions. It produces one
  incident per advertised app (package, rule/C2 hosts and IPs, APK/DEX/certificate sha256, Ad Library ids,
  promoting profiles), per Meta advertiser profile and per API host. All incidents carry the label
  `operation:tollfraud-2026`. The organiser (NASK) published it, so it is marked `leakage_risk` in the lock and in
  meta. It is used for reference and evaluation only and is never folded into shipped priors or thresholds. If the
  operator provides a Meta Ad Library token, ENRICH-PLUS can fetch the Ad Library ids.
- **Leakage check**: `scripts/ioc_overlap.py` prints counts only; no values and no file names. csirt-knf images vs
  the NASK sample: 4,300 NASK images against 689 KNF images, 0 sha256 hits and 0 pHash near-duplicates
  (Hamming <= 10). CERT toll-fraud: no images were published, so no pHash check is possible; the IOC overlap is in
  READY.md.
- The **CERT Polska warning list** was refreshed at 2026-11-21 19:19 UTC: 126,584 entries, 126,539 active. The
  adapter reads the newest snapshot. The mass-certpl corpus (59,994 incidents) was built from the 18:29 snapshot.
- **Incident ids**: adapters whose natural keys share long prefixes (phishing-alive-7days, csirt-knf) prefix the key
  with a 12-hex hash (`_common.unique_key`). Without it, 48-char slug truncation merged 1,391 and 157 incidents.
- **Imported total**: 387,705 incidents across 20 own states (`~/HydraNexus/state-mass-*`). Of these,
  28,445 have Polish-language content and 62,983 are Polish-targeted URLs. Per-dataset table:
  `~/HydraNexus/data/external-big/READY.md`.
