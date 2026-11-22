# Disclosure — what existed before the task start (Sat 21.11.2026 11:00)

The event rules allowed a pre-prepared generic project skeleton (basic packages, environment configuration,
deployment); all task-related logic had to be written from Saturday 21.11.2026 11:00, and earlier or external
resources had to be cited. This file is that citation. HYDRA NEXUS was developed during HackNation 2026,
21-22.11.2026. This public repository is a single-commit snapshot of the 1.0 release; the development history is
kept in the team's private repository and can be shown to the organiser on request.

## Prepared before 11:00 (generic — would be identical for any of the 15 tasks)

| Area | What | Where |
|---|---|---|
| Environment | Python 3.11 + uv lockfile (macOS arm64 + Linux CPU), pnpm/Vite toolchain, offline env vars, per-OS caches | `pyproject.toml`, `uv.lock`, `scripts/env.sh`, `justfile` |
| Infrastructure | content-addressed file store, SQLite (WAL, single writer, migrations), hash-chained audit log, job queue | `hydra/storage.py`, `hydra/db.py`, `hydra/events.py`, `hydra/jobs.py` |
| Generic import | files/folders/ZIP, MIME by magic bytes, size limits, ZIP traversal/bomb guards, dedup by SHA-256, per-file status | `hydra/ingest.py` |
| Media utilities | EXIF-aware working image copy, ffmpeg frames/audio, PySceneDetect scenes | `hydra/media.py` |
| Model runtime | device/thread selection, offline loaders for downloaded embedding models, exact cosine top-k | `hydra/ml/` |
| API/CLI shell | FastAPI (health, import, jobs, artifacts, audit verify), `hydra` CLI | `hydra/api.py`, `hydra/cli.py` |
| UI shell | React/Vite/Tailwind app shell, generic components (table, dropzone, job progress, split pane, image overlay, graph canvas) — no task screens | `ui/` |
| QA / smoke | per-library stack tests on synthetic inputs, network guard for all tests (fails on any non-loopback connect/DNS), generic upload->job->audit->embedding throughput smoke | `tests/test_stack.py`, `tests/conftest.py`, `scripts/smoke_e2e.py` (`just smoke`) |
| Deployment | Dockerfile + compose for the Linux server | `Dockerfile`, `docker-compose.yml` |
| Models (downloaded, not trained) | DINOv2-small, multilingual-e5, YuNet, SFace, RapidOCR ONNX; CERT Polska warning list snapshot | `models.lock.json`, `docs/MODEL_REGISTRY.md` |
| Documents | design specification v2.0 and 3.0, research notes, prompts for the coding sessions | internal, not published |

## Feasibility measurements made before 11:00

On 2026-11-20 the team ran a throwaway feasibility check (outside the repo) to confirm that the
libraries in the spec install and run on both machines. It used about 1000 synthetic, generated
images with text overlays, not organiser data, and produced no product code. The measured numbers
that came out of it are recorded in the team's internal errata notes and in comments in `hydra/ml/device.py`. These numbers are library and
hardware facts (API names, throughput, threshold sensitivity), and they are listed here so the jury
can see exactly what was known before the start. All thresholds used by the product are set and
validated again on the event's data after 11:00.

## Pre-start lab work (2026-11-20, outside the repository, throwaway)

Before the start the team ran throwaway experiments in two directories outside this repository. They were
written to inform design decisions only. **No code from them is reused in HYDRA NEXUS**; only decisions and
measured numbers carried over, and every threshold is re-set on the event's data after 11:00.

| What | Where (not in git) | Data used | What carried over |
|---|---|---|---|
| Feasibility prototype of the linking idea (rarity bits, hypotheses, clustering) | outside the repository | about 400 synthetic generated ads with fictional brands, fictional domains and drawn faces | design choices: joint (dependency-grouped) bits instead of per-token sums, abstain band, baseline ladder |
| Feature lab (per-modality gene choices) | outside the repository | synthetic generated ads | gene picks per modality |
| Fusion lab (naive sum vs bits vs logistic regression + isotonic) | outside the repository | synthetic generated ads (400-8000) | LR + isotonic on per-family bits when labels exist; never transfer thresholds across corpus sizes |
| Scale lab | outside the repository | public datasets (DISC21 dev, Copydays, PhishTank, PhiUSIIL, OpenPhish, CERT Polska list, POLfake, MIPD, Smishing IMC25, polish-news), aggregates only | exact top-k instead of FAISS/LSH, URL label skeleton as the strongest infrastructure gene, CERT lookup by host, connected components need a size guard, thresholds scale with corpus size |
| Prior-art and licence survey | outside the repository | public web pages, 2026-11-20 | "what not to claim" list (see `docs/METHODOLOGY.md` §11), component licence verdicts |
| UI demo (static mock on lab data) | outside the repository | lab synthetic data | visual design reference only; lab data never ships |

None of these used organiser data (it was not available before 11:00). Lab numbers are synthetic or
public-data numbers and are never presented as results on NASK data.

## Written from 11:00 (task logic)

All task logic was written after the start (Saturday 21.11.2026 11:00 - Sunday 22.11.2026 10:30, Europe/Warsaw).
The private development history records every commit with its time.

Integrated in 1.0 (present in `hydra/`, `ui/`, `eval/` and covered by tests):

- NASK incident adapter (roles by content, MHTML split, nested bundles, misfiled HAR), safe import of URLs,
  screenshots, hints, files, folders and ZIPs; redaction of secrets in persisted and exported values;
- gene extractors: VIS (hashes, DINOv2, optional SigLIP2, faces with YuNet/SFace and a persona gallery, logos),
  TXT (OCR, normalisation, MinHash, entities, embeddings), INF (HAR, DOM, URL skeletons, trackers, TDS), NAR, META;
- enrichment with cache and replay (RDAP, DNS via `dig`, crt.sh, Team Cymru, CERT Polska list, isolated URL fetch,
  Meta Ad Library connector);
- rarity bits with tiers and per-family caps, candidate retrieval (inverted index, Hamming, exact kNN), pair
  scoring with hypotheses and the near-duplicate rules, campaigns and campaign groups with size and group guards,
  open-set assignment, leave-one-family-out robustness, conformal prediction sets (reported always; deciding
  only from 20 calibration items, so never on the 10-incident sample; `hydra/conformal.py`);
- process-parallel extraction, live progress, corpus flags, scheduler and watchlist, Neo4j and enriched-dataset
  exports, optional LLM analyst layer (ADR-008);
- task API and UI screens (analyse, campaigns, graph, pairs and evidence, incidents, search, scheduler,
  methodology, live analysis);
- evaluation harness with synthetic variants, baseline ladder, ablation, robustness and merge gate; public
  demonstration corpus builder.

Not integrated, although the design specification described them (see `docs/METHODOLOGY.md`, "Specification
3.0 vs the product"): geometric verification in the evidence path (the ORB/MAGSAC `same_asset`
function exists but is not called by the scoring pipeline) and the signed evidence ZIP with geometry. Integrated
since this list was first written: variant lineage in observation order (`hydra/lineage.py`, METHODOLOGY §8a),
STIX 2.1 and XARF exports (§14), and review decisions (accept / reject, move, merge, split, undo) that publish a
campaign revision (§11a).

## Third-party components

Open-source libraries and models listed in `uv.lock`, `ui/pnpm-lock.yaml` and
`docs/MODEL_REGISTRY.md` with their licenses. No external inference API is used at runtime, except the optional,
off-by-default LLM layer below (ADR-008).

Formats used by the exports (written 2026-11-21 from 11:00, CORE): XARF v4 (https://github.com/xarf/xarf-spec,
MIT; schemas v4.2.0 core, content-base, content-fraud, content-phishing studied and downloaded to
`~/HydraNexus/cache/xarf-spec/` outside git; our validator is an independent implementation of the emitted
subset) and STIX 2.1 via the `stix2` 3.0.2 library (BSD-3-Clause, OASIS Open; licence read from the installed
package metadata). Pivot queries are text for crt.sh, urlscan, Shodan and Censys; no call is made to those services.

Graph UI measurement (written 2026-11-21 from 20:00): `ui/scripts/graph-perf.mjs` drives Playwright
1.65.0 (Microsoft, Apache-2.0; the copy already in the local npx cache, not added to `ui/pnpm-lock.yaml`) with the
Chromium build Playwright downloads; development / QA only, never part of the app or the demo. The constellation
explorer uses only libraries already in the lockfile (sigma, graphology, React, TanStack Query).

Video signals (written 2026-11-21 from 17:30): video-PDQ temporal matching is our own code on top of
`pdqhash` (per-frame PDQ over the VIS keyframes); the vPDQ idea (Meta ThreatExchange, BSD) was studied from its
public description only, no vpdq code is used (the package is not installed). AV-sync suspicion uses MediaPipe
(`mediapipe` 1.2.3, Apache-2.0) with the Face Landmarker model bundle (Apache-2.0, `docs/MODEL_REGISTRY.md`), fetched
only with `HYDRA_MODEL_DOWNLOAD=1`; the correlation heuristic is our own. No deepfake-detector weights are used.
Tests use synthetic OpenCV-drawn videos only.

## External public data services (enrichment, written from 11:00)

Used by `hydra/enrich/` (ADR-007), only for hostnames and IPs extracted from the material, replayed from a local
cache by default; terms and limits in internal notes (not published) RDAP via rdap.org (registry public data), DNS via
public resolvers 1.1.1.1 / 8.8.8.8 (system `dig`; 9.9.9.9 as third resolver of the DNS consensus since 2026-11-21), Team Cymru IP-to-ASN (DNS interface, free community service),
crt.sh certificate transparency search (Sectigo, free), CERT Polska warning list snapshot of 2026-11-20 (public
list), Public Suffix List (MPL 2.0, local copy). The scam pages are fetched only when an analyst submits a URL and
`HYDRA_ENRICH=1` (`hydra/enrich/fetch.py`).

## Optional inference service (ADR-008, written from 11:00)

`hydra/llm/` writes `llm_narrative` NAR genes, campaign briefs and pair explanations. Off by default; enabled
with `HYDRA_LLM=1`. The default backend (`HYDRA_LLM_BACKEND` unset or `local`) is a local Bielik model (SpeakLeash,
Apache-2.0) behind llama.cpp `llama-server` (MIT) on 127.0.0.1: nothing leaves the machine. Claude models on Amazon
Bedrock (Anthropic, via AWS; SDK `anthropic[bedrock]` 2.12, Apache-2.0) or the Anthropic API are used only with
`HYDRA_LLM_BACKEND=bedrock` or `anthropic` (operator authorisation to process organiser data in the cloud,
2026-11-21); organiser text (redacted) and downscaled creative images are sent only in that mode. Every response is cached and replayable
(`HYDRA_LLM=replay`); the core analysis, the demo and the hidden-set run do not depend on it. Details and measured
cost: internal notes (not published).

## External datasets used by the evaluation (from 11:00)

| Dataset | Source | Licence / terms | How used |
|---|---|---|---|
| PhiUSIIL Phishing URL Dataset | https://archive.ics.uci.edu/dataset/967/phiusiil+phishing+url+dataset (retrieved 2026-11-20, sha256 0a639fd0…) | CC BY 4.0, cite Prasad & Chandra 2024 | `eval/variants.py`: phishing URLs + page titles become synthetic distractor incidents (eval only, not shipped) |
| PhishTank verified online URLs | http://data.phishtank.com/data/online-valid.csv (retrieved 2026-11-20) | PhishTank terms, attribution required | same as above; aggregate results only |

Generated data stays under `~/HydraNexus/data/nask/variants/` (outside git) and is labelled SYNTHETIC.
Method: internal notes (not published).

## Public demonstration corpus (from 11:00)

A second corpus built only from public sources (task §8: extra public data allowed if its origin is stated), no
organiser data, own state directory outside git. Details: internal notes (not published)
(`scripts/build_public_corpus.py`).

| Source | Retrieved | Licence / terms | Rows used |
|---|---|---|---|
| CERT Polska warning list, `hole.cert.pl/domains/v2/domains.json` (snapshot 2026-11-20 18:58 UTC, sha256 `bd904e8d…`) | 2026-11-20 | free for any use, no attribution required; must not imply endorsement by CERT Polska | 4,327 domains inserted since 2026-11-18 |
| PhishTank `online-valid.csv` (Cisco Talos) | 2026-11-20 22:09 UTC | PhishTank FAQ: free use including commercial; attribution given | 1,227 Polish-targeted URLs |
| PhiUSIIL Phishing URL Dataset (UCI 967, Prasad & Chandra 2024) | 2026-11-20 | CC BY 4.0 | 1,556 Polish-targeted phishing URLs |

Excluded: OpenPhish (its terms forbid showing derived results); Smishing IMC25 (rows contain sender identifiers).
No phishing page was fetched: only hostnames and IPs went to RDAP, DNS, Team Cymru and crt.sh (ADR-007), and the
analysis reads the cache only.

Local models for the same layer (default backend since 2026-11-21 ~17:00): Bielik 1.5B / 4.5B v3.0 Instruct and
Bielik-PL 11B v3.0 Instruct (SpeakLeash, Apache-2.0, GGUF from huggingface.co/speakleash, pinned revisions and
sha256 in internal notes (not published) and CR-LLM-5), served by llama.cpp `llama-server` release b12606 (MIT, ggml-org)
on 127.0.0.1. Q4_K_M copies for the Mac were quantized by us from the official fp16 GGUF. No organiser data
leaves the machine on this backend.

## External datasets for calibration and evaluation (DATA-HUNT, from 11:00)

Clarification received during the event (2026-11-21 ~16:00): larger external datasets may train,
calibrate and enlarge the corpus if they stay separated from organiser data and their origin and licence are
stated. Retrieved 2026-11-21. Details: `docs/datasets/EXTERNAL.md`.

| Dataset (`ext:<name>`) | Source | Licence / terms | Verdict and use |
|---|---|---|---|
| phreshphish | https://huggingface.co/datasets/phreshphish/phreshphish @eca603e; paper arXiv 2507.10854 | CC BY 4.0; the card adds "should only be used for anti-phishing research" (noted; HYDRA's use is anti-phishing) | SHIP-SAFE: calibration, priors, evaluation; pages never redistributed |
| phreshphish-2k | https://huggingface.co/datasets/gt2026workshop/phreshphish-2k @7ceeea2 | CC BY 4.0 | SHIP-SAFE: calibration and evaluation; screenshots never redistributed |
| purino-scam-ads | https://huggingface.co/datasets/Purino/scam-ads-detection-dataset @484306e | card says MIT, but the ad creatives are third-party content (the uploader's claim) | calibration and evaluation only; creatives never redistributed or shown in public material |
| phishing-websites-putra | https://zenodo.org/records/8041387 | CC BY 4.0 (Zenodo) | SHIP-SAFE: calibration and evaluation; pages never redistributed |
| phish-screens | https://huggingface.co/datasets/shresthsamyak/phishing-website-screenshots @757b719 | "other": defensive research and education; review third-party rights before redistribution or commercial use | EVAL-ONLY: nothing shipped is fitted on it |
| phishdecloaker-kits | https://zenodo.org/records/11228974 (`phishing-kits-dataset.zip`) | CC BY 4.0 | SHIP-SAFE; inert adapter (page/assets/file-tree/exfil indicators, PHP never executed), brand per kit |
| certpl-warnings | https://hole.cert.pl/domains/v2/domains.json (CERT Polska; retrieved 2026-11-21) | public list, free use, no endorsement implied | SHIP-SAFE: 126,509 Polish-targeted phishing domains; url-only incidents; derived stats with attribution |
| phishtank | http://data.phishtank.com/data/online-valid.csv (Cisco Talos; retrieved 2026-11-21) | free incl. commercial, attribution to PhishTank (FAQ) | SHIP-SAFE with attribution: 43,127 phishing URLs, brand target; aggregates only |
| phiusiil | https://archive.ics.uci.edu/dataset/967 (retrieved 2026-11-20) | CC BY 4.0, cite Prasad & Chandra 2024 | SHIP-SAFE: 235,795 URLs (134,850 legit / 100,945 phish); large legitimate counterpart |
| smishing-imc25 | github.com/reportsmishing/Smishing-Dataset-IMC25 @a617556 | CC BY 4.0 | SHIP-SAFE aggregates only: 33,869 smishing SMS (133 Polish); never show raw rows (phones/sender ids) |
| phishplens | https://zenodo.org/records/23579138 (AGH Krakow) | CC BY 4.0 | SHIP-SAFE: 755 pseudonymised phishing/spam/ham e-mails (461 Polish); best Polish phishing corpus found |
| xfact-pl | HF utahnlp/x-fact @f50e44f8, config pl | MIT (claims are third-party quotes) | SHIP-SAFE: 1,768 Polish fact-checked claims (demagog.org.pl), claimant grouping |
| scamworld | HF oscarwu/ScamWorld @c8fb34ef | CC BY-NC 4.0 (research/eval only) | EVAL-ONLY: 430 reconstructed scam/legit sites (5 Polish); nothing shipped fitted on it |
| kad-pl | github.com/FiltersHeroes/KAD @226d3639 | CC BY-SA 4.0 | EVAL-ONLY: 2,610 Polish anti-scam filter URLs; share-alike incompatible with regulamin §4 copyright transfer |
| phishing-websites-putra-legit | https://zenodo.org/records/8041387 (`not-phishing_*.zip`, slimmed) | CC BY 4.0 | SHIP-SAFE: 5,244 legitimate sites (HTML + screenshot + assets), legit counterpart of putra |
| phishing-websites-80k | https://data.mendeley.com/datasets/n96ncsr5g4/1 | CC BY 4.0 | SHIP-SAFE: 80,000 pages (50,000 legit / 30,000 phish), URL + HTML |
| phishing-alive-7days | https://zenodo.org/records/14885725 ("7 Days Later", streamed sample) | CC BY 4.0 | SHIP-SAFE: 6,569 re-crawls of 2,217 phishing URLs (same URL over time) |
| certpl-tollfraud-2026 | https://cert.pl/posts/2026/09/analiza-tollfraud/ (CERT Polska, 2026-09-23) | no open licence (c) NASK | EVAL-ONLY + LEAKAGE-RISK (organiser publication): IOC reference corpus (34 apps, 60 advertiser profiles, 9 hosts); never in shipped priors/thresholds |
| csirt-knf | https://cebrf.knf.gov.pl/komunikaty/artykuly-csirt-knf (30 monthly reviews) | (c) UKNF; KNF nota prawna: reuse with source + link; screenshots are third-party content | EVAL-ONLY: 616 Polish fraud-case figures/texts (ads, pages, SMS); never redistributed |
| polfake | github.com/MateuszWalczakTUL/POLfake @c63fdd2 | no licence | EVAL-ONLY: 5,774 Polish fact-checked claims + 1,453 linked tweets |
| mipd | github.com/ArkadiusDS/MIPD @51aac23 (EMNLP 2024) | CC BY-NC-ND 4.0 | EVAL-ONLY: 15,356 Polish articles, disinformation + manipulation techniques |
| political-ads-pl | HF laugustyniak/political-advertising-pl @ed0114d | CC BY-NC-SA 4.0 | EVAL-ONLY: 1,701 Polish campaign tweets, 9 ad categories |
| web-legit-tranco (TRAIN, 2026-11-21) | Tranco list 46YVX (https://tranco-list.eu/list/46YVX, created 2026-11-20; Le Pochat et al., NDSS 2019) + the public homepages (https://<domain>/) of ~3,000 `.pl` and ~2,000 top global domains on it, each fetched once on the server by `scripts/fetch_web_legit.py` (generic UA, robots.txt honoured) | Tranco: no licence of its own stated, citation requested; upstream rankings Majestic CC BY 3.0, CrUX CC BY-SA 4.0, Cloudflare Radar CC BY-NC 4.0, Umbrella free of charge, Farsight (unstated); the list is only a seed of public domain names and is not redistributed. Pages: third-party content | SHIP-SAFE: derived hashed document-frequency statistics only; page content never redistributed; operator instruction 2026-11-21 ("Tranco-listed sites' public landing HTML fetched once on the server for statistics only"). Background DF prior for the TRAIN stream |
| AdReport | commercial ad-intelligence service | commercial access | EXCLUDED: not requested, not downloaded |

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
## HYDRA Watch (real-time discovery, written from 2026-11-21 16:10)

| What | Source / licence | How used |
|---|---|---|
| CERT Polska Warning List action log `https://hole.cert.pl/domains/v2/actions_2026.log` | "może być wykorzystywana przez każdego użytkownika, w dowolnym celu, w tym w celach komercyjnych"; no endorsement implied | recorded (HTTP Range tail, every 90 s) on the team's Linux server into `~/HydraNexus/data/watch/recordings/` (outside git); scored, linked, replayed in the demo |
| certstream-server-go (d-Rickyy-b, MIT), Docker image `0rickyy0/certstream-server-go:latest` | MIT, run as a separate container on the server (loopback 127.0.0.1:8098) | Certificate Transparency names → `hydra/watch/ct_recorder.py` keeps only Polish-signal candidates (Stage A ≥ 6 bits) |
| phishing_catcher (GPL-3.0), openSquat (GPL-3.0), StreamingPhish (Apache-2.0) | behaviour studied from them by the research side (notes outside the repository) | independent implementation, no code, lists or data from them; similarity check `docs/cleanroom/WATCH_SIMILARITY.md` |
| confusable_homoglyphs (MIT), tldextract (BSD-3), websockets (BSD-3) | already in `uv.lock` | IDN look-alike folding, public-suffix split, CT websocket client |

No page is fetched automatically; RDAP/DNS/crt.sh lookups for candidates go through the existing `hydra.enrich`
layer (live only with `HYDRA_ENRICH=1`). Details: internal notes (not published).

## HUNT stream (2026-11-21)

| What | Source | Licence / terms | Use |
|---|---|---|---|
| CERT Polska warning-list action log recordings (`actions_2026.log`, tailed by the WATCH recorder, files `certpl*.jsonl` under `~/HydraNexus/data/watch/recordings/`, 9,383 block actions, ActionTime 2026-11-12 18:19 to 2026-11-21 15:49 UTC) | WATCH stream recorder, `hole.cert.pl` | "any use, including commercial"; no endorsement implied | read-only, offline TEMPORAL REPLAY of name-generator hypotheses (`hydra/hunt/replay.py`); not copied into git |
| Design | `audit-2026-11-21/INNOWACJE.md` (external audit design doc) | internal | the hypothesis contract, three times, rules §12, MVP §16 |

No new dependency, model or network call.

## AUD stream: speech transcripts and audio-track reuse (2026-11-21)

| What | Source | Licence / terms | Use |
|---|---|---|---|
| faster-whisper 1.2.1 (already a dependency in `pyproject.toml`) | https://github.com/SYSTRAN/faster-whisper | MIT | Polish ASR on CPU (int8), bundled Silero VAD (MIT) |
| `Systran/faster-whisper-small` weights, HF revision `536b0662742c`, `model.bin` sha256 `3e305921506d` (483.5 MB) | https://huggingface.co/Systran/faster-whisper-small | MIT (CTranslate2 conversion of OpenAI Whisper small, MIT; Whisper training data not public) | downloaded 2026-11-21 with `HYDRA_MODEL_DOWNLOAD=1` into `$HYDRA_MODELS_DIR/faster-whisper-small`, outside git |
| MinHash LSH banding and char n-gram TF-IDF retrieval (ADV-RETR, 2026-11-22, `hydra/retrieval_adv.py`) | textbook LSH banding (Leskovec, Rajaraman, Ullman, *Mining of Massive Datasets*, ch. 3); scikit-learn `TfidfVectorizer` (BSD-3-Clause), scipy sparse (BSD-3-Clause), both already in `uv.lock` | published algorithm; permissive libraries | own numpy/scipy code, no library code copied; candidate proposal only, flag `HYDRA_ADV_RETR`, default off |
| Audio landmark fingerprint | idea from A. Wang, "An Industrial-Strength Audio Search Algorithm" (ISMIR 2003) | published algorithm | own numpy/scipy implementation, `hydra/genome/aud.py`; no fingerprint library code (Chromaprint LGPL, dejavu) was opened or copied |
| SpeechBrain ECAPA (`speechbrain/spkrec-ecapa-voxceleb`) | https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb | Apache-2.0 code and weights; trained on VoxCeleb (YouTube audio, CC BY 4.0 annotations) | **not used**: training-data terms unclear and voice embeddings are biometric data; decision in internal notes (not published) |

Wording: these genes are signals of reused voice-over / soundtrack, not deepfake or synthetic-speech detection.
Organiser audio is processed locally only; transcripts and fingerprints are cached under `$HYDRA_CACHE_DIR`.

## SIG-PROVENANCE stream (2026-11-21 17:32-)

| What | Source | Licence | When | Use |
|---|---|---|---|---|
| c2pa-python 0.39.0 (c2pa-rs 0.92.0), already a dependency in `pyproject.toml` | https://github.com/contentauth/c2pa-python | MIT OR Apache-2.0 | 2026-11-21 | read C2PA manifests (`hydra/genome/prov.py`); tests sign a synthetic JPEG with a throwaway CA built in the test |
| invisible-watermark, DWT-DCT decoder (vendored, modified) | https://github.com/ShieldMnt/invisible-watermark @ 68d0376d | MIT | 2026-11-21 17:40 | `hydra/vendor/imwatermark/`; entry in THIRD_PARTY_NOTICES.md |
| Stability SD v1 / SDXL public watermark messages (constants only: bytes `StableDiffusionV1`, 48-bit 0xB3EC907BB19E) | Stability reference scripts (generative-models, MIT), as summarised in research 04 §2a | MIT | 2026-11-21 | match targets for the decoder; no Stability code copied |
| MediaPipe Face Landmarker model `face_landmarker.task` (float16/1) + mediapipe 1.2.3 (already a dependency) | https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task | Apache-2.0 (model card) [verify] | fetched 2026-11-21 17:37 | iris centres for the StyleGAN eye-geometry suspicion gene; sha256 in MODEL_REGISTRY; outside git |
| FFHQ eye positions (0.375, 0.475) / (0.625, 0.475) | own derivation from the published FFHQ alignment description; no FFHQ data or code used | n/a | 2026-11-21 | geometry constant |

No deepfake detector, no detector weights and no non-commercial training data are used. The card is rule-based.
Design notes are outside the repository.

## DEEPFAKE-2 stream: manipulation-suspicion genes (2026-11-21 19:20-)

| What | Source | Licence | When | Use |
|---|---|---|---|---|
| AudioSeal detector code (vendored, modified) | https://github.com/facebookresearch/audioseal @ 1496f766 | MIT | 2026-11-21 20:10 | `hydra/vendor/audioseal/` (detector only); entry in THIRD_PARTY_NOTICES.md |
| AudioSeal detector weights `detector_base.pth`, HF `facebook/audioseal` @ 3c19eba5, converted to safetensors | https://huggingface.co/facebook/audioseal | MIT (model card); training data VoxPopuli (CC0) per the paper | 2026-11-21 (server) | `dfk_audio_wm` gene; pinned in models.lock.json, outside git |
| AudioSeal generator weights `generator_base.pth` and the `audioseal` 0.2.1 pip package | same | MIT | server eval venv only | embedding marks in test audio for the evaluation; not shipped, not a project dependency |
| RIGID idea (representation stability under noise) | https://github.com/IBM/RIGID @ 55af3cc2 (notebook read) | Apache-2.0 | 2026-11-21 | behaviour re-implemented in `hydra/genome/dfk.py` (`instability_scores`); no code copied |
| ReStraV idea (DINOv2 trajectory curvature) | https://github.com/ChristianInterno/ReStraV @ c51ee520 (code read) | Apache-2.0 | 2026-11-21 | behaviour re-implemented in `hydra/genome/dfk.py` (`trajectory_stats`, curvature statistic only; their MLP and training data not used) |
| dinov2-small (already registered) | facebook/dinov2-small | Apache-2.0 | - | backbone of both training-free statistics |
| Calibration data (thresholds): OpenImages v7 validation images (1,200 random) | https://storage.googleapis.com/openimages/ | CC BY 2.0 per image list | server, 2026-11-21 | reals for the image threshold |
| Calibration data: Community Forensics train parquet `commercial/HFCF_Commercial_0`, `systematic/HFCF_Systematic_50`, `_150` | https://huggingface.co/datasets/OwensLab/CommunityForensics | CC BY 4.0 | server | fakes for the image threshold |
| Calibration data: Wikimedia Commons videos (CC0 / CC BY / CC BY-SA, per-file manifest with authors) | https://commons.wikimedia.org | per file | server | reals for the video threshold |
| Calibration data: Rapidata text-2-video-human-preferences-veo3 (1,000 videos) | https://huggingface.co/datasets/Rapidata/text-2-video-human-preferences-veo3 | Apache-2.0 | server | fakes for the video threshold |
| Evaluation only (numbers only): Community Forensics Eval (59 of 413 shards) | https://huggingface.co/datasets/OwensLab/CommunityForensics-Eval | CC BY-NC-SA 4.0 | server | AUROC of the image statistic |
| Evaluation only: GenVideo re-upload (5 shards) | https://huggingface.co/datasets/sshaiton/GenVideo-50k-shard | tagged MIT; original reals from YouTube/MSR-VTT/Youku | server | AUROC of the video statistic |
| Evaluation only: In-the-Wild audio | https://huggingface.co/datasets/mueller91/In-The-Wild | CC BY-SA 4.0 | server | negatives for the watermark FPR; AASIST-L measurement |
| Evaluation only: VoxPopuli PL test | https://huggingface.co/datasets/facebook/voxpopuli | CC0 | server | speech carrier for watermarked / clean test audio |
| Evaluation only: AASIST-L (clovaai/aasist @ a04c986, in-repo weights) | https://github.com/clovaai/aasist | MIT code; weights ASVspoof 2019 LA (ODC-By) | server clone | measured on In-the-Wild for the survey verdict; not shipped |

No organiser data was sent anywhere; all evaluation media stayed on the server under `~/HydraNexus/research2/deepfake/`
and only aggregates were copied into the docs. No deepfake-detector weights trained on non-commercial data are used.
Survey: internal notes (not published); genes and numbers: internal notes (not published).

## RESEARCH-50 prior-art study (2026-11-21 ~18:50-20:00, documentation only)

| What | Source | Licence | How used |
|---|---|---|---|
| Code-level study of 89 public GitHub projects in 6 clusters (list with URLs, stars, licences and commit dates in internal notes (not published)) | shallow clones in `/tmp/research50/` on the laptop, outside the repository, read only (not built, not run, not committed) | mixed, per row | findings and the ranked plan internal notes (not published); no code, data or weights copied into the repository |
| Clean-room behaviour specs: kit_exfil_indicators, iok_kit_rules, parking_page_detector, kit_filetree_fingerprint, dns_wildcard_consensus, misp_correlation, addownloader, google_ads_transparency, osome_coordination_tfidf, disparity_backbone, fastlink, er_evaluation, thehive_similar_cases, cortex_taxonomy, phone_disposable (`docs/cleanroom/*.md`) | kitphishr (GPL-2.0), IOK (ODbL-1.0), Lookyloo parking-page-detection (no licence), PhishingKit-Yara-Rules (AGPL-3.0), theHarvester (GPL-2.0), MISP (AGPL-3.0), AdDownloader (GPL-3.0), GoogleAdsTransparencyScraper (MIT-style text), osome-iu coordination-detection (no licence), CoordGalaxy (no licence), fastLink (GPL-3.0+), er-evaluation (AGPL-3.0), TheHive 4 (AGPL-3.0), Cortex-Analyzers (AGPL-3.0), PhoneInfoga (GPL-3.0) | as listed | behaviour studied from these projects by research agents. Any implementation will be independent, from the spec only, with no code from them, and its similarity-check result will be recorded in the spec. No implementation existed when these specs were committed. |

Research was done with Claude Code research agents (one lead and six cluster subagents). That work produced documentation only.

## Operator, payment and kit-exfiltration genes (2026-11-21 from 19:20)

| What | Source | Licence | When | How used |
|---|---|---|---|---|
| schwifty 2026.11.3 (pip dependency) | https://github.com/mdomke/schwifty | MIT | 2026-11-21 | IBAN validation and bank registry (bank code and name) in `hydra/genome/operator_ids.py`; not copied |
| phonenumbers 9.0.44 (pip dependency) | https://github.com/daviddrysdale/python-phonenumbers | Apache-2.0 | 2026-11-21 | E.164 normalisation, number type, short and emergency numbers for phone tiering; not copied |
| pycountry 26.2.16, rstr 4.0.0 (transitive dependencies of schwifty) | PyPI | LGPL-2.1-only, BSD-3-Clause | 2026-11-21 | installed unmodified as libraries, never copied |
| Phone tiering behaviour | PhoneInfoga, via `docs/cleanroom/phone_disposable.md` | GPL-3.0 | 2026-11-21 | behaviour studied from PhoneInfoga (GPL-3.0); independent implementation, no PhoneInfoga code; similarity check recorded in the spec |
| Wallet / account checksums | public specifications: ISO 13616 IBAN, BIP-173/BIP-350, EIP-55, FIPS 202 Keccak, Base58Check, Monero Base58 | public specifications | 2026-11-21 | own implementation in `hydra/genome/operator_ids.py`; test vectors from BIP-173/BIP-350/EIP-55 |
| Kit exfiltration ids (`hydra/genome/kit_exfil.py`, `hydra/redact.py` token patterns) | kitphishr, via `docs/cleanroom/kit_exfil_indicators.md`; public Telegram Bot API, Discord webhook URL, reCAPTCHA/hCaptcha/Turnstile docs (test sitekeys) | GPL-2.0 (studied only) | 2026-11-21 | behaviour studied from kitphishr (GPL-2.0); independent implementation, no kitphishr code, identifiers or regex literals; similarity check recorded in the spec |
| Kit-rule engine (`hydra/genome/kit_rules.py`) and rules (`hydra/genome/lexicons/kit_rules/*.yml`) | IOK, via `docs/cleanroom/iok_kit_rules.md` | ODbL-1.0 (studied only) | 2026-11-21 | behaviour studied from IOK (ODbL-1.0); independent implementation, no IOK code; the two shipped rules are HYDRA's own (synthetic test kit, generic parked template), no IOK rule text used |
| Kit file-name-set gene (`hydra/genome/kit_names.py`) | PhishingKit-Yara-Rules, via `docs/cleanroom/kit_filetree_fingerprint.md` | AGPL-3.0 (studied only) | 2026-11-21 | behaviour studied from PhishingKit-Yara-Rules (AGPL-3.0); independent implementation, no rule text, names or lists from it; similarity check recorded in the spec |
| MISP warninglists, full mapping (206 of 231 lists used, every list mapped with an effect or a reason) | https://github.com/MISP/misp-warninglists @ 40e2b570fda2e5443a2d6fa1d1db16c37ab0a956 (2026-10-29) | CC0-1.0 (README); X4BNet datacenter/VPN lists MIT | retrieved 2026-11-21 (43 lists 15:21, 206 lists 19:50, verified against the GitHub tree 20:16) | offline snapshot outside git (16 MB), lowers INF and phone gene tiers, no runtime calls; four synthetic test fixtures under `tests/genes_inf/wl_fixture/lists/` contain a few copied CC0 patterns (phone_numbers, common-contact-emails) |
| CERT Polska, "Fałszywe reklamy i złośliwe aplikacje — analiza operacji toll fraud" (2026-09-23) | https://cert.pl/posts/2026/09/analiza-tollfraud/ (public post; read 2026-11-21 from the saved copy `~/HydraNexus/data/external/certpl-posts/2026-09-analiza-tollfraud.html`, outside git) | CERT Polska publication (cited, not copied) | 2026-11-21 | reference link chain for `hydra/genome/tollfraud.py` (app package ids, identical creative SHA-256, Ad Library ids, cloud load-balancer names, reused TLS certificate, premium SMS short codes); no text, data or code copied; tests use synthetic values only |
| Internal gap audit and paper review (items B7, B11, B18, G02, G07) | internal notes (not published) | own documents | 2026-11-21 | OCR/ASR hosts as INF landing genes, jury reference-base step, LICENSES regeneration, text template key, chat-widget/payment/ad-publisher ids; own code |
| PyYAML 6.0.3 (pip dependency, previously transitive) | https://pyyaml.org | MIT | 2026-11-21 | loads the kit-rule YAML files |
| Brand hotline list (10 numbers) | public contact pages of the brands, prior knowledge, not re-verified online on 2026-11-21 | facts | 2026-11-21 | `hydra/genome/assets/phone_lists.json`, marks hotlines as boilerplate (0 bits) |

## REVIEW-CONF stream (2026-11-21)

| What | Source | Licence | How used |
|---|---|---|---|
| Similar-cases card (`hydra/similar_cases.py`, `GET /v1/incidents/{id}/similar`) | behaviour studied from TheHive 4 (TheHive-Project/TheHive @d390a03) by a research agent, spec `docs/cleanroom/thehive_similar_cases.md` | AGPL-3.0 | behaviour studied from TheHive (AGPL-3.0); independent implementation, no TheHive code. Similarity check recorded in the spec (longest common token run 3, 0 shared 8-grams) |
| Member / campaign support score idea (1 - RMS of missing support), bridges and articulation points | dedupe (MIT) and Splink (MIT) ideas via the internal adoption plan; `networkx.bridges`, `articulation_points`, `maximum_spanning_tree` (BSD, already a dependency) | MIT / BSD | own code in `hydra/campaign_support.py`; no code copied |
| Review loop patterns (unsure state, suggestion queue, cannot-link bridge abstain) | nomenklatura (MIT) ideas via the internal adoption plan | MIT | own code in `hydra/review.py`, `hydra/review_queue.py`, `hydra/assign.py`; no code copied |

Work done with Claude Code (one builder session and one UI subagent). Synthetic test data only; the organiser
sample was used only through `HYDRA_SAMPLE_TEST=1 uv run pytest tests/core/test_sample_regression.py` (6 passed).

## PL-SCAM stream: Polish public-source scam knowledge (2026-11-21 from 19:20)

Research notes with every fact, URL and date: internal notes (not published). All downloads ran on the
server under `~/HydraNexus/research2/pl/` (raw files with SHA-256 and UTC retrieval time); no organiser data was
used to build any asset below. Assets in git contain short quoted phrases, our own regex patterns, brand names,
three documented person names and aggregate counts only.

| What | Source | Licence / terms | How used |
|---|---|---|---|
| CERT Polska annual reports 2021-2025, monthly summaries 12/2025-10/2026, blog posts (2024-11-25 ad fraud, 2025-09-17 URL analysis, 2026-09-23 toll fraud), knowledge-base pages | https://cert.pl/publikacje/, https://cert.pl/posts/ (retrieved 2026-11-21 18:23-18:26 UTC) | reports CC BY 4.0 (Raport 2025 imprint); web pages public | facts, scheme taxonomy, quoted lure phrases (attributed per term in `hydra/genome/lexicons/pl_scam_schemes.json`) |
| CSIRT KNF Raport roczny 2025, Krajobraz cyberzagrożeń 2026 | https://www.knf.gov.pl/knf/pl/komponenty/img/Raport_Roczny_CSIRT_KNF_97516.pdf (retrieved 2026-11-21 18:29 UTC) | public-sector document [U exact terms] | facts and quoted lure phrases |
| KNF Lista ostrzeżeń publicznych (669 warnings, 386 domains parsed) | https://www.knf.gov.pl/dla_konsumenta/ostrzezenia_publiczne (sha256 619d2d31..., 2026-11-21 18:43 UTC) | public-sector register [U exact re-use terms] | offline snapshot `$HYDRA_DATA_DIR/external/knf/` (not in git; entries may name natural persons) -> label genes `knf_warning_listed` |
| MF Rejestr domen służących do oferowania gier hazardowych niezgodnie z ustawą (60 231 domains) | https://hazard.mf.gov.pl/api/Register (sha256 854b94c7..., 2026-11-21 18:43 UTC) | statutory public register [U exact re-use terms] | offline snapshot `$HYDRA_DATA_DIR/external/mf_hazard/` (not in git) -> label genes `mf_hazard_listed` |
| CERT Polska warning list snapshot 2026-11-20 (already in E5) | https://hole.cert.pl/domains/v2/domains.json | any use incl. commercial, no endorsement | aggregate domain-naming statistics (TLD shares, brand and service tokens) in `pl_domain_patterns.json` |
| Tranco top-1M list | https://tranco-list.eu/ (daily list downloaded 2026-11-21 18:28 UTC, zip sha256 5d30cb86...) | free research list, citation requested [U exact terms] | only per-TLD counts, as the reference distribution for the TLD ratio |
| UOKiK ostrzeżenia konsumenckie | https://uokik.gov.pl/bip/ostrzezenia-konsumenckie | public | read only (no domains); not used by genes |
| PhishPLENS dataset metadata | https://zenodo.org/records/23579138 | CC BY 4.0 | listed as a candidate, not downloaded or used |

Code written in this stream (our own): `hydra/genome/lexicons/public.py` and the four `pl_*.json` assets,
`hydra/genome/domain_pl.py` (extractor `inf_domain_pl`), `hydra/enrich/public_warnings.py`,
`scripts/fetch_public_warnings.py`, `scripts/plscam_measure.py`, tests in `tests/genes_pl/`; small edits in
`hydra/genome/lexicons/__init__.py`, `persons.py`, `nar.py`, `hydra/genome/base.py` (module list). No third-party
code was copied. Research used no subagents (the session's subagent limit was reached) and no web search engine.

## TRAIN stream: background document-frequency prior (2026-11-21 17:30-)

- Written during the event: one-sided background prior in
  `hydra/entropy.py` (`Rarity.u_estimate`, `CompositePrior`), hashed prior loader `hydra/priors/`, build script
  `scripts/train_prior.py` (materialise / extract / build), eval options (`eval.run --fusion`,
  `HYDRA_BACKGROUND_PRIOR_FILE`), tests `tests/core/test_background_prior.py` (synthetic data only).
- Shipped artefact: `hydra/priors/background_df.json.gz` (510 KB): per (kind, value) only `sha256(kind + 0x1f + value)`
  truncated to 16 hex characters and two unit counts (legit, fraud); corpus sizes and the source/licence list.
  Personal and identity kinds are never counted. Low-entropy values (for example a common brand name) could be
  recovered by hashing guesses; that reveals only that the value is common on public pages, which is the purpose.
- Sources (all external, public, SHIP-SAFE in `datasets.lock.json`; organiser data never used; every incident of the
  evaluation corpora `corpus-demo` and `corpus-putra` excluded): web-legit-tranco 3,149 homepages (row above),
  PhreshPhish 1,127 Polish pages (CC BY 4.0), purino-scam-ads 3,408 ads (lock: aggregates only, creatives never
  redistributed), phishing-websites-putra 2 pages (CC BY 4.0). Attribution: docs/LICENSES.md.
- Computation: server only (`the Linux server`), extraction with the product's own extractors into separate state
  dirs (`~/HydraNexus/state-train*`, never the product state); raw pages stay on the server.
- Default `mixed` since 2026-11-22 (architect decision; `HYDRA_BACKGROUND_PRIOR=0` restores the old behaviour):
  neutral on the NASK sample, fewer links through legitimate pages on corpus-demo (docs/METHODOLOGY.md §5,
  internal errata notes). A pretrained fusion model fitted on purino advertiser pages was evaluated and is
  not shipped. Measurement script: `scripts/prior_demo_compare.py`.

## AI assistance

Code is written with Claude Code (Anthropic) as a development tool. It is not part of the product
runtime and receives no organiser data unless the task rules allow it.

## Independent ASTRA review (2026-11-21)

Codex (OpenAI) was used as a development and review assistant for
synthetic adversarial tests, contract requests, the audit report and Polish Settings/Data UI strings.
No organiser data, stored HYDRA state, model weights or persisted application caches were read or copied.
No product Python implementation was changed.

The tests reuse existing dependencies, including Hypothesis 6.170.17 (already in the development group;
MPL-2.0, test-only), NumPy, Pillow and the existing pHash/PDQ implementations. No new dependency, model,
dataset or copied third-party implementation was added. Images, text, identifiers and vectors are generated
in the tests. Synthetic image embeddings are explicitly labelled contract fixtures, not learned model
measurements. Report: internal notes (not published).

## Independent ASTRA security review (2026-11-21)

Codex (OpenAI) was used as a development/review assistant for
synthetic API/import security tests, export validation, contract requests and the audit report.
No organiser material, persisted HYDRA cache/state or model weights were read or copied. No Python
product code, UI implementation or migration was changed.

The tests reuse installed project dependencies, including stix2 3.0.2 (BSD, already a runtime dependency),
pytest-timeout 2.4.0 (MIT, already a development dependency), FastAPI/httpx, Pillow, pandas and PyArrow.
One tiny corrupt-video test invokes the existing ffprobe binary; no new media/tool/model was installed.
The Cypher lexer, archives, image header, HTML/HAR, credentials and model responses are original synthetic
test fixtures. No copied third-party implementation or new dependency was added.

Published XARF schema acquisition was blocked by the execution environment's network restrictions.
No unavailable schema or licence was substituted or claimed as verified. Two formal XARF tests explicitly
skip pending the schema and validator. Execution and measurements were on Saturday 2026-11-21.

## ENRICH-PLUS stream (2026-11-21 19:20-)

| What | Source | Licence / terms | When | Use |
|---|---|---|---|---|
| Wildcard-aware DNS consensus (`hydra/enrich/dns_consensus.py`) | https://github.com/laramies/theHarvester via `docs/cleanroom/dns_wildcard_consensus.md` | GPL-2.0 | 2026-11-21 | behaviour studied from theHarvester (GPL-2.0); independent implementation, no theHarvester code |
| Parked / for-sale page detector (`hydra/genome/parked.py`) | https://github.com/Lookyloo/parking-page-detection and webpage-feature-extraction via `docs/cleanroom/parking_page_detector.md` | no licence (all rights reserved) | 2026-11-21 | behaviour studied from Lookyloo parking-page-detection (no licence); independent implementation, no code, keyword lists, data or models from it; own phrase and provider lists |
| Ad-library scan hardening (`hydra/enrich/adlibrary.py` `page_through`, `snapshot_status`; `hydra/scheduler.py` `job_adlibrary_pull`) | https://github.com/Paularossi/AdDownloader (commit a682599) via `docs/cleanroom/addownloader.md` | GPL-3.0 | 2026-11-21 | behaviour studied from AdDownloader (GPL-3.0); independent implementation, no AdDownloader code; built from the spec and the public Meta Ad Library API documentation, tested with a mocked HTTP layer |
| HHHash-style header-order hash (`hydra/genome/http_fp.py`) | public HHHash description by A. Dulaunoy / CIRCL (https://github.com/adulau/HHHash; URL *unverified*, not opened in this session) | idea only | 2026-11-21 | own implementation from the description; names lower-cased, so values are not byte-compatible with HHHash |
| JARM-like TLS probe (`hydra/enrich/tlsprobe.py`) | idea from the public JARM description (Salesforce, https://github.com/salesforce/jarm, BSD-3-Clause; not opened in this session) | idea only | 2026-11-21 | own lower-resolution variant with the Python `ssl` module; no JARM code; values not comparable with JARM |
| Quad9 public resolver 9.9.9.9 | https://quad9.net | public resolver terms (*unverified* for limits) | 2026-11-21 | third resolver of the DNS consensus (live mode only) |

Builders worked only from the clean-room specs and general knowledge; the studied GPL and unlicensed sources were not
opened. The similarity check against the studied repositories is still to be run by a research agent (recorded as
pending in each spec). No new dependency, model or dataset. Tests use synthetic zones under `.test`, fictional
domains and a fake resolver, handshake and HTTP layer.


## ASTRA pipeline audit milestone (2026-11-21)

OpenAI Codex/Astra and three delegated agents reviewed frozen product source `951af8f235916334ffdbce740266e7630fb8b01c`. The audit commit following that baseline contains original synthetic tests, an aggregate-only Python function-entry probe and documentation; no product source, model, dependency or organiser material is added. The existing environment-test helper was corrected to source `scripts/env.sh` under an explicit temporary root.

Tests use the installed project tools. The TypeScript request-serialization probe runs the real local client code with transport intercepted, then replays requests against the real in-process ASGI app. It is not a browser or live-service test. Only the previously authorised sample regression executes organiser input; its deliverable retains aggregate outcomes, not originals, genomes, identifiers or model output.

Anastasios N. Angelopoulos and Stephen Bates, *A Gentle Introduction to Conformal Prediction and Distribution-Free Uncertainty Quantification*, arXiv:2107.07511, was consulted by the parent on 2026-11-21 for the distinction between held-out exchangeable calibration and reuse of fitted data. URL: https://arxiv.org/abs/2107.07511 . Bibliographic reference and paraphrase only; no text/code/weights vendored. No new external code, model or dataset was downloaded.

Validation and limitations: internal notes (not published).


## ASTRA research and upgrade plan, 2026-11-21

OpenAI Codex/Astra and three delegated research/review agents inspected product snapshot
`ec2b50cec8c204ea7470489821f91b7bd16e0b2d`. Its notes are internal and not published.
The two contest PDFs were read locally through macOS PDFKit; no cloud OCR
or document upload was used. Only PDF hashes/page counts are retained, not the originals or full text.

Previously recorded public research in the internal research notes was reconciled with
current source. External retrieval failed; primary URLs, stars and licence leads are explicitly not
freshly verified. No external implementation, model, corpus or dependency was downloaded or vendored.
No organiser incident data, runtime state, replay cache or model weights were inspected.

Original synthetic probes use installed NumPy/torch and public project functions, with a private temporary
root and offline configuration. They test card/parser semantics, an idealised watermark-output stub and
numeric AV masking; they do not test trained detector accuracy. The report distinguishes prior aggregate
measurements from new probe outputs. Code and UI remain unchanged. Commit range: snapshot `ec2b50c`
through the documentation commit containing this section; no push or merge.

## UI redesign 2027 (2026-11-21)

Design trends were read from public pages on 2026-11-21:
- shadcn/ui sidebar docs, Vercel Geist, the Linear UI redesign post, the Tailwind CSS v4 blog, the Evil Martians
  OKLCH article, the Apple HIG materials page, WCAG 2.2, MDN prefers-reduced-motion, cmdk and Inter;
- the URLs are listed in `docs/ui/DESIGN_2027.md`.

The pages were used as design guidance only. No code, assets or fonts were copied from them; Inter Variable was
already bundled via `@fontsource-variable/inter`. All components are our own Tailwind/React code, and no
dependency was added. No project or organiser data was sent in those requests.

## JURY-READY stream (2026-11-22)

| What | Source | Licence / terms | When | Use |
|---|---|---|---|---|
| Offline reference-data bundle `hydra-reference-data-<date>.tar.zst` (built by `scripts/reference_data.py build`, 4.3 MB on 2026-11-22), shipped next to the image, never in git | `misp-warninglists/` = https://github.com/MISP/misp-warninglists @ 40e2b570fda2e5443a2d6fa1d1db16c37ab0a956; `psl/` = https://github.com/publicsuffix/list @ d7d7f1fd55166aaf73ad2f526e72634490acd9c8; `cert/` = https://hole.cert.pl/domains/v2/domains.json snapshot 2026-11-20 18:58 UTC (sha256 `bd904e8d…`) | MISP: CC0-1.0 (X4BNet lists inside: MIT); PSL: MPL-2.0, file unmodified, full licence text in `psl/LICENSE-MPL-2.0.txt` (copied from the lightningcss package's MPL-2.0 LICENSE); CERT Polska list: any use, no attribution required, no endorsement implied | bundle built 2026-11-22 | the same snapshots as rows above, copied unmodified with `SOURCE.md` and `SHA256SUMS` per set; `install` verifies every checksum. Not included: `knf/` (may name natural persons, re-use terms unverified) and `mf_hazard/` (re-use terms unverified); those stay behind the optional one-time online step |
| Synthetic jury-import corpus (`tests/fuzz/jury_corpus.py`) | written during the event | project code | 2026-11-22 | generated at test time only: NASK-like layouts, hostile files (ZIP bomb, traversal, encrypted ZIP, symlinks, corrupt media, cp1250/ISO-8859-2 text); no organiser material |

## INTEGRATIONS stream: connectors, exports, webhooks, API console (2026-11-22 from ~02:00)

Provider documentation was read on public pages on 2026-11-22 02:20–03:00 (Europe/Warsaw); no project, organiser
or NASK data was sent in those requests (one crt.sh probe used `example.com`). No provider SDK, client library or
sample code was copied or vendored; every client and format builder is our own code from the public
specification, tested with hand-written fixtures and an injected transport (no network in tests). No new
dependency. Everything is off by default; live clients need a key, `HYDRA_OFFLINE=0` and an explicit test/enable.
Details and the full connector table: `docs/INTEGRATIONS.md`; verified facts: internal notes (not published)

| What | Source / URL | Licence / terms (as read) | Use in HYDRA |
|---|---|---|---|
| STIX 2.1 (OASIS) | https://docs.oasis-open.org/cti/stix/v2.1/stix-v2.1.html | OASIS Non-Assertion Mode IPR policy | format produced locally (existing `hydra.export_stix`, `stix2` BSD-3-Clause) |
| TAXII 2.1 (OASIS) | https://docs.oasis-open.org/cti/taxii/v2.1/taxii-v2.1.html | OASIS standard | described only; push is planned |
| OpenCTI import | https://docs.opencti.io/latest/deployment/connectors/ | licence not stated on the page read | STIX file for manual import; no OpenCTI code |
| MISP event JSON and REST `/events/add` | https://www.misp-project.org/openapi/ , https://www.misp-project.org/license/ | MISP core AGPL-3.0-or-later (applies to MISP core, not to software using its API); objects/taxonomies CC0 or BSD-2-Clause | format produced locally; optional push to the operator's own instance, off by default; no MISP or PyMISP code |
| XARF v4 | https://github.com/xarf/xarf-spec , https://xarf.org | MIT | format produced locally (existing `hydra.export_abuse`); sending is manual |
| Splunk HTTP Event Collector | https://help.splunk.com/en/splunk-enterprise/get-started/get-data-in/9.4/get-data-with-http-event-collector/format-events-for-http-event-collector | Splunk documentation | event JSON produced locally; optional push, off by default |
| Elasticsearch / OpenSearch `_bulk` | https://www.elastic.co/docs/api/doc/elasticsearch/operation/operation-bulk | Elastic documentation | NDJSON produced locally; optional push, off by default |
| Neo4j Cypher | https://neo4j.com/docs/http-api/current/ | Neo4j Community GPL-3.0 (no Neo4j code used) | Cypher script produced locally (existing `hydra.export_neo4j`) |
| Meta Ad Library API | https://developers.facebook.com/docs/graph-api/reference/ads_archive/ | Meta Platform terms (not read in full) | optional live client (existing `hydra.enrich.adlibrary`), off by default |
| urlscan.io search API | https://docs.urlscan.io/pages/api-intro | provider terms (not verified) | optional live client, off by default; search only, no new scans |
| VirusTotal API v3 | https://docs.virustotal.com/reference/public-vs-premium-api | public API: not for commercial products/services; 4/min, 500/day | optional live client, off by default |
| URLhaus API (abuse.ch) | https://urlhaus-api.abuse.ch/ | Auth-Key required since 2025-06-30; terms on a separate page (not read) | optional live client, off by default |
| OpenPhish community feed | https://openphish.com/phishing_feeds.html | Terms of Use required; commercial use not stated as permitted | optional download of the public feed, matched locally |
| Google Safe Browsing v4 | https://developers.google.com/safe-browsing/v4 | non-commercial use only; commercial use: Web Risk API | optional live client, off by default |
| AbuseIPDB API v2 | https://docs.abuseipdb.com/ | provider terms (not verified); free plan 1000 checks/day | optional live client, off by default |
| CERT Polska warning list | https://cert.pl/lista-ostrzezen/ | free for any use incl. commercial, no warranty, no implied endorsement | local snapshot lookup (existing `hydra.enrich.cert_pl`), no network at lookup |
| crt.sh, RDAP (rdap.org), public DNS | https://crt.sh/ , https://about.rdap.org/ | public data; rdap.org 10 requests / 10 s | existing enrichment modules (ADR-0007), exposed as connectors |
| Slack incoming webhooks | https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks | Slack terms | optional push of a short campaign summary, off by default |
| TikTok, Google Ads Transparency, LinkedIn, X, Shodan, Censys, PhishTank, Teams, SMTP | URLs in `docs/INTEGRATIONS.md` | see the table there | descriptors only (`planned`), no client |

## WATCH-RT stream: realtime public-feed watch (2026-11-22 from 03:00)

Own code only (`hydra/watch/rt/`, `hydra/api_watch_rt.py`, `hydra/cli_watch.py`, UI `/watch`); no new dependency,
no code, lists or data copied from any provider or tool. No live request was made while building it: every test
uses fixtures and an injected transport. All sources are off by default; a live poll needs `HYDRA_OFFLINE=0` and
an explicit enable, and goes through the audited `hydra.integrations.http.request` path. Campaign-derived
indicators (organiser data) are matched locally and never sent out; crt.sh and Meta receive manual operator
keywords only. Replay fixture `hydra/watch/rt/fixtures/demo` is synthetic (generator `scripts/watch_rt_fixture.py`,
only reserved `.example` / `.test` domains) and is labelled SYNTETYCZNE / ODTWORZENIE wherever it is shown.

| Source | Endpoint | Licence / terms (as read earlier, see the INTEGRATIONS table) | Use in WATCH-RT |
|---|---|---|---|
| CERT Polska warning-list action log | https://hole.cert.pl/domains/v2/actions_<year>.log | any use incl. commercial; no implied endorsement | HTTP Range tail every 60 s, block actions become items |
| URLhaus recent URLs (abuse.ch) | https://urlhaus-api.abuse.ch/v1/urls/recent/limit/1000/ | Auth-Key required since 2025-06-30; terms page not read | optional, needs `URLHAUS_AUTH_KEY`; endpoint shape from public docs, not called during the build (unverified live) |
| OpenPhish community feed | https://raw.githubusercontent.com/openphish/public_feed/refs/heads/main/feed.txt | Terms of Use required; commercial use not stated as permitted; DISCLOSURE public-corpus section excludes OpenPhish from shown derived results | optional, off by default, at most hourly, matched locally; do not enable for a commercial deployment without reading the terms |
| crt.sh JSON search | https://crt.sh/?q=%25<keyword>%25&output=json&exclude=expired | public CT data (RFC 6962); service terms unverified | optional, manual keywords only, max 3 queries per poll |
| Meta Ad Library | existing `hydra.scheduler.job_adlibrary_pull` | Meta Platform terms (not read in full) | optional, token + live enrichment only |

## ADV-VIS stream: pair geometry, crop rule, colour feature (2026-11-22)

- Written during the event: `hydra/vis_geom.py`, `hydra/vis_colour.py`,
  `hydra/api_geom.py`, `ui/src/components/GeometryOverlay.tsx`, `ui/src/api/geometry.ts`, `tests/core/test_adv_vis.py`,
  one hook each in `hydra/synapse.py`, `hydra/api_task.py` and `ui/src/pages/task/VariantDiffPage.tsx`.
- No new dependency, model, dataset or downloaded file. OpenCV (`opencv-python`, already locked) provides ORB, SIFT and
  `cv2.USAC_MAGSAC`; RootSIFT is the published descriptor normalisation (Arandjelovic & Zisserman, CVPR 2012),
  written from the paper's description; no code copied.
- SigLIP2 (`google/siglip2-base-patch16-224`, Apache-2.0) was NOT added: the weights exist offline only in the lab
  dir and are not pinned in `models.lock.json` (internal errata notes).
- Measurements used the NASK sample and the hidden-set simulator on the server only; only aggregate numbers are in
  the docs.

## META-COLLECT: Meta Ad Library collection (2026-11-22)

- Written during the event: `hydra/meta/*`, `hydra/api_meta.py`, ,
  `tests/meta_collection/*`, `ui/src/api/metaAds.ts`, `ui/src/components/watch/meta/*`,
  `ui/src/pages/task/WatchMeta*.tsx`, `ui/e2e/meta.spec.ts`, fixture `hydra/watch/rt/fixtures/meta-demo` (synthetic,
  locally generated images); migration v8 (architect-approved); changes in `hydra/watch/rt/{sources,engine}.py`,
  `hydra/scheduler.py`, `hydra/enrich/adlibrary.py`, `hydra/integrations/{secrets.py,clients/meta_adlibrary.py}`,
  `hydra/api_auth.py`, `hydra/api.py`.
- External resource: Meta Graph API `ads_archive` (Meta Platform terms; access with the operator's own token, stored
  in `~/.config/hydra/meta.env`, never in git). Public reference pages read 2026-11-22: S1 ads_archive, S2
  ArchivedAd, S3 rate limiting (`docs/integrations/META_API_CONTRACT.md`). No new library, model or dataset.
- Data sent to Meta: only the operator's manual Polish search terms (public words, no organiser data), country PL,
  and page ids taken from public ads (P1 expansion, off by default).
- Operator consent (human operator, main session, 2026-11-22 ~05:40 CET, relayed by the architect): "pobieraj podglądy
  reklamy - mam zgodę" — fetch ad snapshot pages (`ad_snapshot_url`) and the creative media they reference, for
  analysis. This records the operator's decision for our use; it is not a statement of Meta's permission. Meta's
  documentation (S2) allows downloading individual ad creative for analysis subject to its data storage terms, which
  were not re-read. Implemented as `HYDRA_META_SNAPSHOTS=1` / admin toggle `media_fetch`, with robots.txt respected:
  on 2026-11-22 `https://www.facebook.com/robots.txt` (sha256 739d9608…e62d) ends with `User-agent: *` /
  `Disallow: /`, so HYDRA fetched no snapshot page (20 attempts recorded as blocked, 1 robots.txt request).
- Live data stays in an isolated state dir (`~/HydraNexus/state-meta-live`, laptop, outside git); only aggregate
  counts appear in the docs.
