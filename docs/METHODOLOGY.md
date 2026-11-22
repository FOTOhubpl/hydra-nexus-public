# HYDRA NEXUS methodology: linking scam-ad incidents into campaigns

Task: NASK "Kolekcjoner fejków — FOLLOW THE FAKE" (own-words summary: `docs/REQUIREMENTS_SUMMARY.md`). Version:
2026-11-21 22:30, checked against the 1.0 code. Numbers on NASK data come from the evaluation harness `eval/run.py`
(run `eval-20261121T204751`, later runs where stated); the result tables are not published because they describe
organiser material. Every number on the 10-incident sample at the default thresholds is in-sample: the thresholds
were set while studying these incidents. Every number from the
`sample+syn` corpus is marked SYNTHETIC because it mixes the 10 real incidents with 45 generated ones. Lab numbers quoted for design
rationale come from throwaway pre-start experiments on synthetic or public data (`docs/DISCLOSURE.md`) and are
never results on NASK data.

## 1. Problem statement and unit of analysis

The unit is an **incident**: one reported suspicious ad with everything saved about it (ad screenshot or video,
feed screenshot, screen recording, saved landing page with its assets, HAR network log, platform metadata,
pasted text, starting URL). The contract is `hydra/contracts.py` (`Incident`, `IncidentArtifact`,
`ArtifactRole`).

The tool answers four questions the task asks (§2, §3, §6):

1. Is this incident a **duplicate** of one already seen (same file, same creative re-encoded)?
2. Is it a **variant** of a known campaign (edited image, new headline, rotated domain)?
3. Which incidents form a **campaign**, and which campaigns are **similar to each other** (shared template,
   shared asset, shared infrastructure provider)?
4. For **new material**: which known campaign does it belong to, or is it a **new pattern**?

Four relation types are kept apart, because they mean different things to an analyst (`Hypothesis`):
`same_campaign`, `shared_template` (same kit or agency, not proven same operator), `shared_asset` (the same image,
video or person reused, nothing else), `independent`, plus `abstain` when the evidence is weak or contradictory.
HYDRA links artefacts, not actors: a link never states who is behind a campaign.

## 2. Pipeline overview

```
input (dataset folder | URL | image | video | text/log | ZIP)
  -> M0 import: safe ingest, sha256 content store, per-file status, exact duplicates by hash
  -> M1 genes: per-family extractors (VIS, TXT, INF, ID, NAR, META) -> Gene rows with status + evidence pointer
  -> enrichment (optional, cached): RDAP, DNS, crt.sh, ASN, CERT Polska list -> more INF genes
  -> M2 rarity: document frequency per gene value -> evidence bits; background similarity for embeddings
  -> M3 retrieval: union of candidate indexes -> candidate pairs with the index that proposed them
  -> M5 scoring: per-family bits (dependency-grouped) -> total bits (an uncalibrated logistic of bits is stored
     with calibrated=false; an LR+isotonic model exists but is used only when enough labelled pairs exist)
  -> M6 clustering: connected components on accepted links + size guard + Leiden split -> campaigns
  -> M9 assignment: new incident -> known campaign | new pattern | abstain
  -> explanations: bit waterfall per pair, core genome per campaign, export
```

Modules: `hydra/adapters`, `hydra/genome`, `hydra/enrich`, `hydra/entropy`, `hydra/retrieval`, `hydra/synapse`,
`hydra/nexus`, `hydra/assign`, `hydra/pipeline`, CLI `hydra/cli_task.py`, API `hydra/api_task.py`.

## 3. Feature acquisition (genes)

A **gene** is one observed feature value with: family, kind, value or vector, quality in [0, 1], status
(`observed`, `unavailable`, `failed`, `not_applicable`), provenance (`OBS` observed, `INF` inferred, `HYP`
hypothesis), an evidence pointer (the artifact sha256, plus a locator where the extractor records one: bbox,
character range, timecode, HAR entry index, DOM path or enrichment key), the extractor name and version, and an
optional dependency group.

| Family | Source artefacts | Genes (planned set; the implemented extractors are listed below and in §13) | Why it links |
|---|---|---|---|
| VIS visual | ad creative, feed/landing screenshots, video frames (1 fps + scene cuts) | PDQ (8 dihedral variants), pHash, 4x4 tile pHash, colour histogram, DINOv2-small embedding, optional SigLIP2, ORB keypoints + MAGSAC geometric check, logo matches | the same creative is reused with crops, recolouring, mirroring and overlays; PDQ/pHash alone collapse on geometric edits (lab), so dihedral variants and a learned embedding are combined |
| TXT text | OCR of images (RapidOCR PP-OCRv6, Tesseract `pol` fallback), landing page text, platform text, pasted logs | normalised text (NFKC, zero-width removal, homoglyph folding), MinHash 5-gram shingles, char 3-5 TF-IDF, multilingual-e5 embedding, entities (phone, IBAN, e-mail, messenger handles, promo/ref codes, amounts), obfuscation gene (homoglyph or zero-width use) | campaigns reuse phrases and contact entities; rare entities (one IBAN) are strong evidence, boilerplate (call-to-action) is not |
| INF infrastructure | URLs in the ad and page, HAR, saved HTML | registrable domain (PSL with private suffixes), URL label skeleton, path skeleton, redirect chain, hosts and IPs from the HAR, shortener / free-host flags, tracker and pixel IDs, TDS parameters, DOM skeleton hash, asset hashes, favicon hash | operators rotate domains but keep kits, trackers, paths and hosting; lab: URL label skeleton was the most precise infrastructure gene (same-target precision 0.95 on 144,613 public phishing URLs) |
| ENRICH (INF) | public lookups on hosts/IPs found above (ADR-007) | registrar, registration date, nameservers (RDAP); A/AAAA/NS/MX/TXT (DNS); certificate issuers and SAN names (crt.sh); ASN and hosting provider (Team Cymru); CERT Polska warning-list hit by host | shared registrar + date window, nameserver set or certificate SAN set across rotated domains |
| ID identity | faces in creatives and frames | YuNet detection + SFace 128-d embedding; optional small reference gallery of the most frequent persons built from the sample (NASK §5 allows it) | impersonated public figures recur across campaigns; on its own this is `shared_asset`, not `same_campaign` |
| NAR narrative | text genes | scheme type (investment, fake news bait, health, giveaway, funds recovery...), persona, promise, call-to-action motif, named social-engineering technique (urgency, scarcity, authority, social proof, reciprocity, liking, fear; G12) | groups similar campaigns; weak alone, used for the "similar campaigns" view; 0 campaign bits |
| META platform | `.info.json`, platform metadata | ad/page id, uploader/advertiser, dates | exact ids are strong evidence; dates give first/last seen |

Kit file set (G06, `hydra/genome/kitset.py`): per saved page the normalised first-party file set (content hashes
after masking ids/URLs/e-mails/IPs/phones, distinctive file names; libraries, CMS core, fonts and IP blocklists
skipped) is emitted as 16 MinHash LSH band genes in one kit group, so pairs sharing more of the set share more
bands. Fewer than 4 files: `not_applicable`. Thresholds borrowed from P01/P10, not validated internal notes, not published.

Lure-domain facts (G13, `hydra/genome/lure.py`): `brand_offdomain` (a media/bank mark on a landing outside the brand's
official domains; explanation fact, 0 bits) and `registrar_window` (same registrar, registration within ±2 days;
operator tier for small registrars, boilerplate for large retail ones). Assumptions, see internal notes, not published

Rules applied by every extractor:

1. Only stored material is processed; the analysis core is offline and deterministic.
2. An extractor that cannot run on an input writes a gene with status `unavailable` or `failed`, never nothing.
3. Every observed gene points to the artefact it came from (sha256). Where a locator exists (pixel box, character
   range, timecode, HAR entry, DOM path or lookup record) it is stored too; corpus-level and per-incident summary
   genes (for example file-set or check genes) point to the artefact only.

**Implemented extractors** (registry after `hydra.genome.base.load_family_modules` plus the CORE extractor
`artifact_sha256` that `hydra.pipeline` registers; 29 on `a213ae8`, listed with
`python -c "from hydra.genome.base import load_family_modules, REGISTRY; load_family_modules; print(list(REGISTRY))"`):
INF `inf_url`, `inf_har`, `inf_http`, `inf_dom`, `inf_kit`, `inf_kit_names`, `inf_enrich`, `inf_domain_pl`,
`inf_text_hosts`, `inf_tollfraud`, `artifact_sha256`; VIS `vis`, `logo`, `aud_fp` (audio fingerprint, filed under
VIS), `vid_avsync`, `dfk_image`, `dfk_video`; ID `ident`; TXT `txt_lines`, `txt_phrases`, `txt_entities`,
`txt_embed`, `txt_chat`, `aud_asr` (speech transcript); NAR `nar`, `nar_funnel`; META `meta`, `prov`,
`dfk_audio_wm`. The audio, provenance and manipulation-suspicion extractors have their own gene docs
(internal notes, not published, `PROV.md`, `DFK.md`); the DFK genes are label tier (0 linking bits), and `dfk_image` /
`dfk_video` are off by default. The table above lists the planned set per family; the registry is the
implemented set.

**Infrastructure gene tiers.** The analysts' review of the sample (internal notes, not published)
showed that infrastructure genes mean different things, so each INF gene kind carries a tier that bounds the
hypothesis it can support:

| Tier | Meaning of a shared value | Examples | Effect |
|---|---|---|---|
| operator | same buyer, team or account | tracker/pixel ids, ad-account ids, landing UUIDs, CDN account path, postback/CRM hosts, affiliate/team ids, operator domains | full rarity bits; can support `same_campaign` |
| platform | same cloaker, tracker or landing-builder product used by many customers | TDS family signature, lead-endpoint path skeleton, URL parameter set and value shapes, POST field signature, anti-analysis script set | 0 bits towards a campaign link; drives campaign-group links (`shared_template`) |
| kit | copied template or brand-impersonation assets | copied favicon (mmh3), FB-reaction sprite, comment-name packs, DOM tag-path shingles, class/id vocabulary | 0 bits towards a campaign link; drives campaign-group links; fuzzy style genes at most 3 bits even there |
| boilerplate | shared by unrelated pages | CDN anycast IPs, ASN and server headers of CDN-fronted hosts, public libraries, web fonts, platform ad-URL macros, single anti-analysis flags, large retail registrars | 0 bits, coverage only |

Why platform and kit genes do not count towards campaigns (the methodological point). Rarity alone cannot tell
"rare because one operator" from "rare because one product in a small sample": a tracker or landing platform used by
many customers looks rare in a 10-incident sample. In the analyst's rarity-bit scoring of the sample (sum over
families of the maximum bits, script `infra_link.py`, outside git), pairs that shared only the landing platform
scored 6.7-9.0 bits, while the true same-campaign and duplicate pairs scored 19.2-25.2 bits. Connected components
at 6 bits chained all five news-bait incidents (at least two different platform accounts) into one group; at
10 bits they split into {1, 2, 3} and {48, 61}. The first rule capped platform and kit bits at 4 towards a campaign
link; with all extractors merged that still linked umbrella pairs through template text plus capped kit bits, so
since 2026-11-21 14:20 platform and kit bits count **0** towards campaign support (`PLATFORM_KIT_CAP = 0`) and are
used only for campaign-group links (kit level >= 6 bits). The explanation stays typed ("same landing platform,
different account"). Incident 3 joins {1, 2} because incident 2's folder holds incident 3's landing material; the
evaluation labels follow that reading, with the alternative "3 separate" label set reported too (§12).

**Secrets and personal data in the material** (leaked access tokens, API keys in hidden fields and URLs, user
names in local file paths) are redacted in everything persisted for display or export (`hydra/redact.py`:
`redact_text`, `redact_url`). Matching on such a value still works through `secret_fingerprint`, a salted,
truncated sha256 (salt per installation, `HYDRA_REDACT_SALT`). Originals in the content store stay
byte-identical because they are evidence.

### 3a. Extraction-accuracy options (ADV-F01, `HYDRA_ADV_F01=1`, default off)

The F01 synthetic harness (internal notes, not published) found six extractor defects (CR-F01-1..6).
`hydra/genome/adv_f01.py` fixes them behind one flag; without the flag the extractors, their gene values and their
version strings are unchanged. With the flag the versions of `txt_entities` (7), `txt_lines` (2), `inf_dom` (6) and
`inf_har` (4) change, so stored genes of those extractors are recomputed.

1. Foreign phone numbers: a Polish-looking 9-digit run is dropped when it directly follows a non-+48 country
   code or lies inside a `+CC`/`00CC` number that libphonenumber accepts as a possible number. The foreign number
   itself is still extracted (`find_intl_phones`, unchanged).
2. Redirect chains: a 2xx HTML/JS response whose meta refresh, `location=` / `location.replace|assign` or
   `Refresh` header points to a URL that the browser then requested (same URL, or same host and path) becomes a
   hop; `window.open` and targets never requested (cloaking branches not taken) do not. The chain follows hop
   targets from the first hop, uses each hop once and stops at 32 hops, so loops terminate and unrelated
   sub-resource 3xx hops do not join it.
3. IBAN: boundaries are Unicode-aware (`... 1332 Złoty` no longer adds `Z`). A match that fails mod-97 or the
   country length (schwifty IBAN registry) is cut back to the longest group-boundary prefix that is a complete
   valid IBAN; OCR letter-for-digit confusions are mapped only in positions the country's BBAN format declares
   numeric and only if the result passes mod-97 (one deterministic mapping, no search). An account with a wrong
   check digit stays a quality-0.4 observation.
4. Person names: gazetteer surnames accept Polish case endings longer than three letters (-iego, -iemu, -iej,
   -ich, -imi, -owie); the heuristic 'Given Surname' value is lemmatised to the nominative for -ski/-cki/-dzki
   surnames and listed given names (`Krzysztofa Stanowskiego` -> `Krzysztof Stanowski`), so inflected and
   nominative mentions share one value.
5. Brands: four-letter title-case tokens (`Onet`, `Lidl`) count; 26 public brands often impersonated in Polish
   phishing and absent from the shipped lexicon (telecoms, banks, couriers, shops, state agencies; origin `prior`)
   are added to `brand_mention` only (boilerplate tier, no linking bits). A test asserts that none of them fires on
   the name of a brand the lexicon already has.
6. Telegram: a bare `t.me/<handle>` (also `m.me`, `t.co`, `x.com`) yields domain/URL genes; a four-character
   `@handle` next to the word Telegram is accepted (Telegram usernames are 5-32 characters, collectible ones 4).
   `t.me` stays a boilerplate host for INF host genes.

Measured effect (synthetic F01 harness and NASK sample / hidden_sim, flag on vs off) and the decision: internal notes, not published.

## 4. Enrichment layer (ADR-007)

Optional, opt-in (`HYDRA_ENRICH=1`, `HYDRA_OFFLINE=0`). Queries only hostnames and IPs extracted from the material;
no organiser file, image or text leaves the machine. Every response is stored as an `EnrichmentRecord` (source,
query, timestamp, sha256, cache path). **Replay mode** (default) reads only the cache, so the demo and evaluation
run without network and give the same result. An unreachable page or failed lookup becomes `unavailable`, never an
error and never evidence against a link. Facts carry their lookup date because registration and DNS data change.
Sources, terms and limits: `docs/DATA_SOURCES.md`, `docs/LICENSES.md`.

## 5. Rarity weighting: evidence bits (Fellegi–Sunter style)

Implemented in `hydra/entropy.py` (M2). The score is a Fellegi–Sunter agreement weight in bits. For a discrete gene
value v shared by two incidents:

```
bits(v) = clip( log2( m / u(v) ), 0, 14 )
u(v)    = (df_others(v) + A * u0(kind)) / (N_others + A)
m = P(agree | same campaign) = 0.5 (conservative; no labels to fit it)      A = 20 pseudo-incidents
```

- `df_others` counts the incidents (or campaign units, see below) carrying v, **excluding the pair itself**:
  term-frequency adjustment as in Splink.
- `u0(kind)` is a per-kind background prior (for example a tracker or pixel id 2^-14, a registrable domain 2^-11,
  a URL skeleton 2^-7, an ASN 2^-3). These priors are **assumptions**, written in `KIND_U0`. With 10 incidents
  the prior dominates, so a value seen twice in a tiny corpus is not treated as common; at 50k incidents the
  corpus counts dominate.
- **Units**: campaign-level df counts **incidents** (byte-identical incidents as one unit), in `analyse` and in
  open-set assignment, except operator-tier values, which use the operator units described in the next two items
  (campaign units of a first pass exist behind an option and stay off, because on the sample they chained a false
  merge into a giant component). Assignment rarity:
  (`assign.assignment_rarity`). Kit-level df counts **campaign groups** as one unit during assignment, batch
  cross-comparison and leave-one-out, so a kit shared by related campaigns still ties a new incident to their group.
- **Operator values in grouping** (SCORE-C1, 2026-11-22): in assignment an operator-tier value (tracker, pixel,
  phone, account, landing host) counts the known campaigns holding it, not their incidents. `analyse` and batch
  cross-comparison have no known campaigns yet, so they now use pass-0 operator units
  (`entropy.operator_seed_units`): incidents sharing one account-kind operator-tier value form one unit, unless the
  value is held by more than max(HUB_MIN, ceil(HUB_FRAC x n)) incidents (a hub keeps the incident count). Only
  operator-tier bits read these units; other genes still count incidents. Without it, k members of one unknown
  campaign were k holders of their shared value and could stay singletons (CR-E2E-2). Measured on `sample` and
  `sample+syn`: identical campaign partitions, 3 of 1,485 pairs move from independent to abstain (internal notes, not published).
  Leave-one-out self-assignment also drops the rarity caches per held-out incident (SCORE-C2).
  The hard holder limit above is the `legacy` mode (`HYDRA_OPERATOR_UNITS=legacy`); it is no longer the default.
- **Smooth operator units (default since commit 1c12321, operator decision 2026-11-22; SCORE-GROWTH, internal notes, not published
  E-SH-24)** (`entropy.operator_units_mode`, `entropy.operator_seed_soft`, `Rarity._soft_operator_bits`). With n
  incidents and h the number of incidents holding an operator-tier value:
  - L = max(HUB_MIN, ceil(HUB_FRAC x n)) = max(5, ceil(0.2 n)); U = max(2 x HUB_MIN, ceil(HUB_FRAC_INCIDENTS x n))
    = max(10, ceil(0.5 n));
  - spread rule: a value held by 6 to U incidents whose holders fall into >= 3 (`SPREAD_MIN_CAMPAIGNS`) distinct
    multi-incident units formed by the *other* operator values (each such unit is one operation evidenced by its
    own identifier) is a cross-operator value (vendor SDK, shared platform account): it joins no unit and gets no
    weight;
  - values held by 2 <= h <= L incidents (not spread) are unioned into units, as in legacy mode;
  - values with L < h <= U (not spread) join no unit and score
    bits = w x bits(holders as one unit) + (1 - w) x bits(holders as incidents), with w = (U + 1 - h) / (U + 1 - L),
    so the weight falls linearly with h instead of flipping at L;
  - values above the incident hub limit stay at 0 bits, as before.
  `HYDRA_OPERATOR_UNITS=legacy` restores the C1 hard limit. On `sample` and `sample+syn` the smooth mode is a no-op
  (0 soft and 0 spread values), so no gain on real data is claimed (E-SH-24).
- **Repeated values.** Because df counts incidents, every further incident holding a value lowers its bits, also
  when that incident is a copy. Arithmetic example (not a measurement) for a registrable domain (u0 = 2^-11) in a
  10-incident corpus, pair excluded (N_others = 8): held only by the pair, df_others = 0, u = 20 * 2^-11 / 28,
  10.5 bits; held by one more incident, df_others = 1, 3.8 bits. Byte-identical incidents are not merged into one
  rarity unit yet (contract request CR-VP-1, status open), so N copies of one incident add N to the df of every
  value they share.
- **External background** (on by default since 2026-11-22, `HYDRA_BACKGROUND_PRIOR=0` restores the old behaviour; TRAIN): document frequencies of external
  corpora say how common a value is on the open web. Local estimate `u_loc = (df_o + A·u0) / (n_o + A)`; when the
  value is seen in the background (`p_df` of `p_n` background units observing the kind),
  `u = max(u_loc, (df_o + p_df + A·u0) / (n_o + p_n + A))`. One-sided on purpose: the background population (web
  pages, global phishing) differs from the analysed one, so it may only say "this value is common" (cookie banner,
  CMS template, shop price), never make a value rarer than the local corpus says; values unseen in the background
  keep their local rarity. Kinds observed by fewer than 200 background units are ignored. Two sources:
  - the **shipped hashed prior** `hydra/priors/background_df.json.gz` (switch `HYDRA_BACKGROUND_PRIOR=mixed|legit|0`,
    default `mixed`; `legit` uses only legitimate pages, `mixed` adds fraud pages and ads). It stores per value only a
    truncated sha256 of (kind, value) and two unit counts (legit, fraud), never the value. Units are distinct
    registrable domains for pages and advertiser pages for ads. Personal and identity kinds (e-mail, phone,
    person, author, capture fields) are never counted. Built by `scripts/train_prior.py` from SHIP-SAFE sets only
    (`datasets.lock.json`), with every incident of the evaluation corpora (`corpus-demo`, `corpus-putra`) left out.
    v1 (2026-11-21): 7,686 incidents, 3,178 legit + 1,114 fraud units, 47,091 keys (min df 3), 72 usable kinds:
    all 3,149 Tranco-listed homepages (2,082 `.pl`), all 1,127 Polish PhreshPhish pages, 3,408 purino-scam-ads ads,
    2 putra pages. Licences and attribution: docs/LICENSES.md, docs/DISCLOSURE.md;
  - an explicit `--prior ext:...` corpus in the state (`entropy.BackgroundPrior`), combined with the shipped one
    when both are on (`entropy.CompositePrior`).

  **Measured** (server, `eval.run --quick`, same code for all runs, 2026-11-21): neutral on the NASK sample.
  Campaign pairwise F1, B-cubed, false merges (0), leave-one-out, open-set rejection and group-level metrics are
  identical with the prior off, `legit` and `mixed`, on `sample` and `sample+syn`; `test_sample_regression`
  passes with `mixed`. The prior is active: of the 1,867 values shared by two or more sample incidents, 124 are in
  the background and 52 lose bits (about 2 bits each, 105 bits in total; mostly `dom_token`, `dom_shingle`,
  `entity_price`, `brand_mention`), none gains bits; the sample's campaign margins are large, so no decision
  moved.

  **Measured on corpus-demo** (825 external labelled incidents: purino-scam-ads 297, phreshphish-2k 289, Polish
  phreshphish 239, all excluded from the prior; same genes, `score_and_cluster` with default thresholds, commit
  fa026b8; `scripts/prior_demo_compare.py`):

  | pairs linked into one campaign | off | mixed |
  |---|---|---|
  | total | 2,369 | 2,171 |
  | touching a legitimate page | 426 | 270 |
  | cross-label (different brand / advertiser page) | 232 | 207 |
  | same-label | 1,611 | 1,611 |
  | same-label recall (5,967 labelled fraud pairs) | 27.0% | 27.0% |
  | new pairs vs the other setting | 198 | 0 |

  3,574 of 104,106 scored pairs lose bits (mean -0.08 bits over all pairs), 1 gains; 19 of 2,749 pairs at or above
  the link threshold drop below it, which removes the chains through legitimate pages.

  **Decision** (architect, 2026-11-22): default `mixed`. Rationale: one-sided by design (can only lower bits of values common on the web, never adds a link), on corpus-demo it removed 37% of links touching legitimate pages (426→270) and 10.8% of cross-label links (232→207) with unchanged same-label recall (27.0%) and 0 new links, and on the NASK sample it is neutral (identical metrics, sample regression green). Caveats: brand and advertiser-page
  labels are weak and group-level, so cross-label links are only a proxy for false merges; the prior contains
  other items of the same source datasets (same distribution as corpus-demo), which may flatter the result; one
  corpus, one seed, no confidence interval. `HYDRA_BACKGROUND_PRIOR=0` restores the old behaviour. A pretrained LR + isotonic fusion fitted
  on purino advertiser-page groups (weak labels; held-out AUC 0.651 vs 0.648 for the rarity-bits logistic)
  lowered campaign pairwise F1 to 0.0 on both corpora and is not used.
- **Stop-genes**: platform and boilerplate values (CDN, web fonts, public libraries, pixel loader, generic icons)
  score 0 bits whatever their frequency. Redacted values never match. Also stop genes (SCORING-HARDEN,
  internal notes, not published): SDK placeholders in identifier kinds (`G-XXXXXXXXXX`, `UA-XXXXX-Y`, `YOUR_PIXEL_ID`,
  template syntax) and form field sets made only of standard contact/consent fields (`name,phone`); whole ad
  lines or headlines of fewer than 5 word tokens ("Zarejestruj się teraz") score 0 bits but stay observations.
- **Hub cap** (ADOPT #2): a value held by more than max(HUB_MIN = 5, ceil(f x units)) units scores 0 bits, with
  f = `HUB_FRAC` = 0.2 for kit genes and for campaign or operator units, and f = `HUB_FRAC_INCIDENTS` = 0.5 for other
  genes counted in incident units (`Rarity.discrete_bits`; with incident units the k members of one campaign are k
  holders) (MISP-style
  over-correlation; the same bound as the retrieval index). An analyst can move one value to the boilerplate tier
  with the `ignore_value` review (§11a).
- **Observation quality**: every match is weighted by `min(1, q / 0.5)` with q the lower prior quality of the two
  observations (OCR confidence, detector score, warninglist demotion); q < 0.25 gives 0 bits and q >= 0.5 counts
  fully (E-SH-3, E-SH-17).
- **Units for account identities** (E-SH-15): during open-set assignment the document frequency of account
  identities (tracker/pixel, ad and page ids, ad account, phone, IBAN, e-mail) counts known campaigns, not their
  members, so a tracker shared with a 10-member campaign is as rare as one shared with a 1-member campaign; a value
  held by members of three or more known campaigns is a platform value (0 bits). Infrastructure values (hosts,
  endpoints, asset hashes) keep incident counts, because sibling campaigns of one kit share them. Byte-identical
  incidents are one unit everywhere (E-SH-18).
- **One domain, one group** (E-SH-18): host genes of one registrable domain (landing, contacted, linked) pool in
  one dependency group.
- **Conflicting annotations**: when observations of one (kind, value) carry different tiers (for example operator
  and boilerplate from two extractors), the most conservative level wins on both sides and the explanation lists the
  conflict; input or extractor order never matters (E-SH-4).
- **Vector genes** (DINOv2, e5): the background is the empirical cosine distribution between vectors of different
  incidents for the same model; a best-of-k x l match is corrected for multiple comparisons,
  `u_pair = min(1, k * l * P_bg(sim >= s))`, capped at 10 bits. Raw cosine is never thresholded.
- **Near-duplicate hashes** match by Hamming distance (PDQ <= 31 of 256 bits, pHash <= 10 of 64, tile pHash <= 6).
  PDQ is compared as the minimum over the 8 dihedral variants of one image against the original orientation of the
  other (ThreatExchange recommendation; the lexicographic-minimum canonical hash missed 25.3 % of synthetic flipped,
  rotated or re-encoded pairs, the dihedral minimum 0 %, E-SH-9). The frequency of a hash is the number of incidents
  holding a hash inside the match radius, not the exact value count, so re-encoded stock variants share one
  frequency (E-SH-8).
  `MIN_BITS = 0.5`: weaker agreements are not listed as reasons.

Worked example (arithmetic, not a measurement, m = 1): a value seen in 1 of 4,000 incidents gives log2(4000) = 12
bits; a call-to-action seen in 30% of incidents gives 1.7 bits.

**Dependency groups** (`hydra/synapse.py`). Fellegi–Sunter assumes independent fields; creatives violate that (one
stock photo gives a PDQ, pHash, tile and DINOv2 match at once). Bits are pooled per `Gene.dependency_group`
(default `FAMILY:kind`): the strongest gene counts fully, the others at 0.25, the group is capped at 14 bits; groups
sum per family, capped at 24 bits per family. Lab rationale (synthetic data): summing bits per token gave 7.0%
hard-negative false merges vs 0.7-1.3% for group-joint bits.

**Tiers and levels.** `dependency_group = "<family>:<tier>:<name>"`. Operator and phrase genes count at **campaign
level**; platform and kit genes at **kit level**: 0 bits towards a campaign link (`PLATFORM_KIT_CAP = 0`), full
bits for campaign-group links; fuzzy style genes (CSS class tokens, DOM shingles, comment-name packs,
parameter sets) add at most 3 bits even at kit level; asset and persona genes (images, faces, logos) at **asset
level**; boilerplate and labels (CERT Polska hits) 0 bits. Rationale: §3, "Why the cap".

**Per-family caps and the campaign rule** (`hydra/synapse.py`). Campaign-level bits are capped per
family: TXT 8, VIS 8, ID 6, NAR 0, INF and META 24 (their campaign-level genes are operator tier); an identical file
(same sha256) is exempt. Non-INF families pool their groups as max + 0.5 x second. A same-campaign link needs
operator-tier INF/META (or identical-file) evidence of at least 8 bits, or at least 8 bits in two families, and
support at or above `link`. Image and text embeddings count as near-duplicates only at raw cosine >= 0.95 (images)
and otherwise give at most 2 bits; faces below SFace cosine 0.60 are ignored. Consequence: one text or visual family
alone cannot create a campaign link, except through the near-duplicate rules below (§11, limitations).

**Near-duplicate rules** . `text_near_duplicate` (page-text MinHash Jaccard >= 0.9 over >= 50
word 5-shingles, navigation, footer, comments and legal text excluded) and `visual_near_duplicate` (the same image
unit with pHash <= 6 or PDQ <= 31 and DINOv2 >= 0.95; alone for a creative, a desktop crop or region, >= 2 video
frames or >= 5 page images) give 12 bits each and may link alone, like an identical file. 3-4 shared images give
`visual_multi_match` at campaign level with the raised cap of 12 bits. A page text that is near-identical in more than two incidents (known campaigns during assignment) is a template body and gives 0 bits (E-SH-20). Images are counted as distinct contents:
near-identical copies inside one incident are one image, and the count is a maximum one-to-one matching between the
two incidents, so repetition or incident naming cannot change it (E-SH-2). A crop variant matches when the pHash of
one of 22 sub-windows (each >= 64 % of the image) of one unit is within 6 bits of the other unit, with DINOv2 >= 0.95
(E-SH-6). An image whose near-identical hashes are held by more than 2 incidents is stock content and never counts.

**Cross-modal page text** (E-XM-1). A full-page screenshot of a landing page is compared with the saved page text of
another incident: the screenshot OCR (platform chrome, feed zones and cookie / privacy / consent lines removed) is
shingled like the page text (word 5-shingles, same MinHash permutations), and the share of its shingles contained
in the page is estimated from the MinHash Jaccard and both set sizes. At containment >= 0.5 over >= 30 OCR shingles
(>= 4 of 128 agreeing positions) this `cross_modal_text` match gives 12 bits in its own group and may link alone,
like a near-duplicate page. It does not fire when the screenshot's incident has a saved page of its own (the
page-vs-page rule applies) or when the page body is near-identical in more than 2 incidents (template text).

**Rare ad image** (E-SH-14). The same rare ad image (a creative, desktop screenshot or region on at least one side;
pHash <= 6 or PDQ <= 31; held by at most two incidents within that radius) is campaign-level VIS evidence inside the
VIS cap of 8 bits: with another family it can link, alone the pair stays `shared_asset`. It also overrides a scheme
contradiction guessed from little text (image-hash bits >= 5).

**Corroborated contact identity** (E-SH-7). A rare normalised phone, IBAN or e-mail shared by both incidents counts
as operator evidence for the campaign rule when at least two other rare text items from another artefact share it;
it adds no bits. One generic phrase plus a contact, or a contact held by more than two incidents, does not qualify.

**Audio reuse** (E-SH-12). A reused track is a near-complete copy at landmark containment >= 0.4 and MinHash Jaccard
>= 0.08; a weaker overlap counts as kit evidence only when a non-audio identity-kit gene corroborates it, otherwise
it is shown with 0 bits.

**Contradictions.** Incompatible scheme types give -4 bits when the pair's campaign-level bits are below `link`
(no shared rare campaign gene outweighs them); they are listed separately in the explanation. Only detected schemes count: `other` (no scheme reached the lexicon threshold) and schemes observed below quality 0.5 never contradict (E-SH-11, E-SH-17).

**Size guard** (E-SH-18): a campaign component is split only when it has more than max(60, n / 3) incidents and density below 0.3.

The approach is an application of Fellegi–Sunter record linkage with term-frequency adjustment (as in Splink) to
perceptual, embedding and web-infrastructure genes, not a new theory.

## 6. Candidate retrieval

`hydra/retrieval.py` (M3). Scoring all pairs does not scale (lab: candidate pairs grow about N^1.4). Candidates are
the **union** of:

- `exact:<kind>`: inverted index on discrete gene values (stop-genes and values held by too many incidents are
  skipped: they cannot be rare evidence);
- `near:<kind>`: vectorised Hamming search on PDQ/pHash hex hashes;
- `knn:<kind>`: exact cosine top-k per embedding model (`cosine_topk`, torch; exact top-k over 100k vectors took
  about 10 s in the lab, so no approximate index).

Up to 60 incidents every pair is a candidate (`all_pairs`, `all_pairs_below = 60`), which is the case for the
sample. Each pair records the indexes that proposed it (`PairScore.candidate_sources`). On the sample and
`sample+syn` every pair is scored, so candidate recall is 1 by construction. Above the cut-off, internal notes, not published
§B6 measured a SYNTHETIC-heavy 231-incident corpus: the indexes proposed 11,678 of 26,565 pairs with candidate
recall 0.941 of 3,500 labelled same-campaign pairs and the same campaign F1 (0.785) as scoring all pairs. The
pre-start lab measured union recall 0.61 of true pairs at 50k synthetic ads (`mvp-lab/scale/RESULTS.md`). Neither
is a measurement on NASK data at scale.

**Recall against an all-pairs oracle (ADV-RETR, 2026-11-22).** The oracle scores every pair with the same scorer;
the measurement is `scripts/bench_adv_retr.py` on a read-only copy of the 837-incident integration state
(`apiperf-state`, run of 2026-11-21) and a deterministic 231-incident subset of it. Of the pairs the oracle links,
the retrieval union proposed 294/294 at 231 incidents (6,582 of 26,565 pairs scored) and 3,298/3,298 at 837
(89,423 of 349,866), and the campaign partitions equal the oracle's. Recall of same-label pairs (incident labels of
that state, not NASK ground truth) is 0.882 at 231 and 0.831 at 837. The scorer links none of the label pairs
that were not proposed. The 0.941 of §B6 is this labelled recall on a different synthetic corpus, not oracle recall.

Optional extra sources (`hydra/retrieval_adv.py`, switch `HYDRA_ADV_RETR=1`, default off; a no-op at or below
the all-pairs cut-off):
- `lsh:page`, `lsh:ocr_page`: MinHash LSH over the `minhash_sig` signatures, with 128 bands of one row. The
  shared band keys of all signature pairs come from one sparse product; keys held by more than `max_df`
  signatures are skipped. Each proposed pair is then verified on the full signatures against the scorer's own
  preconditions, with a margin: page near-duplicate J >= 0.8 (scorer 0.9), and screenshot-OCR-in-page
  containment >= 0.4 (scorer 0.5, `crossmodal`). The containment case has J of a few percent, which the 32x4
  `minhash_band` genes of the exact index cannot reach;
- `tfidf:text`: char 3-5-gram TF-IDF (scikit-learn) over the incident's own text gene values, cosine top-k >= 0.5.
On both corpora these sources proposed no pair outside today's union (0 new pairs). They added 2.0 s of
retrieval at 837 incidents. At 22,715 incidents (`state-ext-all` copy, where no oracle is possible)
`tfidf:text` proposed 334 new pairs (of which the scorer links 29: 7 `same_campaign` and 22 `shared_template`) and `lsh:page` none, at +86 s per corpus pass and
1.6 s -> 45 s per single assignment. The sources stay off. `query_scope` reports how many embedding neighbours above the
k-NN similarity floor fell outside the top-k (`knn_above_floor`, `knn_beyond_k`, CR-UI-FINAL-5). This is
telemetry only, and is added only in indexed mode (above the all-pairs cut-off).

## 7. Pair scoring and hypotheses

Per pair: campaign-level bits, kit-level bits and asset-level bits (§5); total = families + contradictions.
Missing families (no observed gene on one side) are listed and never penalised. Thresholds (`synapse.Thresholds`,
bits):

| Threshold | Default | Meaning |
|---|---|---|
| `link` | 10 | campaign-level bits at or above: `same_campaign` |
| `kit` | 6 | kit-level bits at or above, without a campaign link: `shared_template` (campaign-group link) |
| `asset` | 6 | asset-level bits only (images, faces, logos): `shared_asset` |
| `low` | 4 | below: `independent`; between `low` and `link` with mixed evidence: `abstain` |

The defaults come from the analyst's scoring of the sample (platform-only pairs 6.7-9.0 bits, linked pairs
19.2-25.2 bits) and are design constants, not fitted values. They are re-checked on the corpus at its real size;
the lab showed the optimum scales with corpus size (about 16 bits at 400 ads, 22 at 2k, 36 at 50k on synthetic
data), so thresholds are never transferred between corpus sizes. Sensitivity (internal notes, not published, SYNTHETIC
`syn-dev`): campaign F1 0.842 for every link threshold 6-24 bits; kit thresholds 3 and 4 merged unrelated groups
(group F1 0.784, 66 group false merges), 6 and 8 gave group F1 1.000 with 0 group false merges; the defaults
(10, 6) are applied, the most stable grid point (10, 8) is reported only. A link threshold chosen on `syn-dev`
alone (24 bits) gives sample campaign F1 1.000 and SYNTHETIC `sample+syn` 0.836.

- **Default scorer `rarity_bits@1`** (baseline B3): the sample has too few labels to fit a model.
  `PairScore.probability` is then an uncalibrated logistic of bits and `calibrated=false`; the UI shows bits.
- **Supervised path `FusionModel`**: logistic regression + isotonic calibration on per-family bits plus
  missingness indicators, trained on a campaign-level split, used only when enough labelled pairs exist; marked
  calibrated only with a reliability plot on held-out pairs. No such plot exists for the sample (too few labelled
  pairs), so every stored probability is `calibrated=false` and no screen shows it as a probability. In the
  baseline ladder an LR on the CORE features reached campaign pairwise F1 0.667 (sample) and 0.669 (SYNTHETIC
  `sample+syn`, 77 false merges), below the rarity-bits system (§12).

Unsupervised EM alone is not used: in the lab it failed on boilerplate-heavy data (B-cubed F1 0.17).

### 7a. Pair geometry for visual near-misses (ADV-VIS; crop flag default on, geometry and colour flags default off)

Hashes (PDQ, pHash, the 22 pHash sub-windows) decide near-identical images cheaply, but a creative cropped by 6-12 %
on random sides and resized moves pHash by 12-26 bits and PDQ by about 105, so the hash rules miss it (internal notes, not published
E-ADV-VIS-2). `hydra/vis_geom.py` adds local-feature geometry only where it can change a decision:

1. **Which pairs.** Inside `synapse._visual_near_duplicate`, the unit pairs that passed the class and stock gates,
   have DINOv2 cos >= 0.80 and were NOT matched by a hash rule. At most 4 per incident pair (highest cosine first)
   and at most `HYDRA_GEOM_BUDGET` verifications per process (default 100,000 since 2026-11-22, raised from 4000, commit
   a370d4d). A verification refused by the budget gives no geometry bits for that unit pair, is counted and logged once,
   and marks the incident pair as incomplete (CR-ASTRA-SW-01, commit 89903f7): `synapse.score_pair` adds a 0-bit
   `geom_skipped` note ("wynik niepełny") and `meta.geometry_incomplete` (number of skipped checks), and a hypothesis
   that would otherwise be `independent` becomes `abstain`; other hypotheses are unchanged. A verification that ran
   and found nothing stays an ordinary negative, and a missing artefact stays no observation. Separately,
   `GET /v1/geometry/stats` reports `complete: false`; that is a process-wide counter, not the result of a given pair. Images are reopened from the content store through the gene's
   evidence pointer (artefact sha256 + bbox); video frames are skipped.
2. **Matching.** ORB (2000 keypoints, Lowe ratio 0.75) with a mirrored retry, homography by `cv2.USAC_MAGSAC`
   (MAGSAC++, 5 px). When ORB has fewer than 120 inliers there is one RootSIFT retry (SIFT descriptors L1-normalised
   and square-rooted, Arandjelovic & Zisserman 2012).
3. **Verdicts.** `same_asset`: >= 120 inliers and inlier convex-hull coverage >= 15 % of BOTH images (internal notes, not published
   the coverage floor cuts the false matches of app UI and faces). `crop_match`: `same_asset` and the transform is
   a crop/resize of the same picture. That means a near-similarity transform (singular-value ratio of the affine part
   <= 1.25, projective term <= 0.15 across the image, not mirrored), with coverage >= 0.25 and 8 x 8 grid occupancy
   >= 0.30 on both sides. Grid occupancy rejects hulls spanned only by platform chrome.
4. **Evidence.** `HYDRA_ADV_GEOM=1`: one ASSET-tier reason `geom_same_asset` (4 asset bits, never campaign bits).
   `HYDRA_ADV_CROP` (default on since 2026-11-22, operator decision, `vis_geom.crop_on`; `HYDRA_ADV_CROP=0` turns it
   off): `crop_match` pairs join the crop matrix of the near-duplicate rule. They then count exactly
   like a sub-window crop match: `visual_near_duplicate` for a creative / desktop screenshot / region, or >= 3 matched
   page images. Stock, boilerplate, app-UI, avatar, icon, logo and phone-screenshot units stay excluded.
5. **Colour.** `HYDRA_ADV_HSV=1` (`hydra/vis_colour.py`): a joint 8 x 4 x 4 HSV histogram per unit, computed at
   scoring time from the same reopened images. Intersection >= 0.90 with cos >= 0.85 adds one ASSET-tier reason
   `hsv_colour` (1 bit). A recolour breaks it and same-palette creatives collide, so it is never campaign evidence.
6. **Cost and reproducibility.** Results are memoised per process and cached as JSON under
   `<state>/work/vis_geom/` (keyed by version + both units), so leave-one-out, robustness and calibration re-scoring
   reuse them. ORB and MAGSAC++ are deterministic for a given input (`test_verify_is_deterministic`). With every flag
   off (including `HYDRA_ADV_CROP=0`) the hook is not entered: genes, gene-cache keys, the extraction code digest, pair scores and partitions are
   byte-identical to integration c7f4817 (measured, E-ADV-VIS-2).

The pair view (`/v1/pairs/{a}/{b}/geometry`, "Co się zmieniło" page) always shows the verification of the three most
similar image pairs. It draws the inlier correspondences as lines between the two thumbnails, with inlier count,
coverage, anisotropy and the verdict. This view is display only and is computed on demand from the same cache.

## 8. Campaigns and campaign groups

`hydra/nexus.py` (M6). Two levels, because NASK asks for both campaigns and "groups of similar campaigns" (§2):

1. **Campaigns** = connected components over `same_campaign` pairs (keeps chained campaigns whose first and last
   variants share nothing directly; lab chained recall 0.80 vs 0.31 for Louvain). Every incident belongs to exactly
   one campaign; a singleton is its own pattern.
2. **Size guard**: a component larger than `max_size` = max(60, n / 3) and sparser than density 0.3 (E-SH-18) is split by Leiden (graspologic-native, MIT; no GPL
   igraph/leidenalg), falling back to networkx Louvain, weighted by bits; the split is recorded in
   `Campaign.meta.split` and raised as an alert. Reason: near the threshold a single bridge merges everything (lab:
   below about 28 bits at 50k synthetic ads one giant component swallowed the corpus).
3. **Campaign groups** (E-SH-22: kit bits count identity-kit genes only; style/NAR evidence is shown but never links groups; kit values held by more than 20 % of the corpus score 0) = connected components over campaigns linked by `shared_template` pairs or by kit-level bits
   >= `kit` between members of different campaigns (same platform, kit or impersonation template).
4. Each campaign gets a **core genome** (gene values shared by at least half of its members and at least two),
   first/last seen, a label from titles, and a heuristic confidence (median link probability; 0 for singletons).
   `Campaign.confidence` is kept for API compatibility only; no screen shows it (CR-ASTRA-9).
5. **Member and campaign support** (`hydra/campaign_support.py`, stored in
   `Campaign.meta.support`, `calibrated: false`). Built from the link edges that hold the campaign together and
   every scored pair between two members; "bits" below are `PairEval.support`, the campaign-level number that is
   compared with the link threshold (10 bits).
   - Per member: bits to the rest (sum over its scored pairs inside the campaign; a missing pair adds 0, it is
     unknown, not negative), anchor bits (its strongest direct link: the weakest point that still keeps it in),
     the independent families behind its links, and a heuristic index 1 - RMS(1 - s) with
     s = min(1, bits / 20) for a link, 1 for an analyst must-link, 0 for a missing or non-link pair (dedupe-style).
   - Per campaign: density = links / possible links, median link bits, weakest link bits (weakest edge of the
     maximum spanning tree), cohesion = mean member index, the weakest member, **bridges** (links whose removal
     splits a campaign of three or more members, with the sizes of both parts) and **articulation members**.
   - **Stability under leave-one-family-out**: for each family on the links, an edge survives if the pair still
     links without that family (the stored `synapse.robustness` result, else support minus that family's bits
     >= link); the campaign reports parts, largest share and detached members, and lists fragile families.
   - A **heuristic rank** of multi-member campaigns by median link bits (1 = strongest). The UI shows bits and this
     rank ("Dowody (bit)", "Najsłabsze ogniwo", "ranking heurystyczny #k z n"), never a percentage. Groups show
     "siła dowodów" = the bits of their top three shared genes (`meta.evidence_bits`).
   Acceptance (synthetic, `tests/core/test_review_conf.py`): in a chain A-B-C both edges are bridges, B is the
   articulation member with the highest index; a 4-clique has density 1 and no bridges.

## 8a. Variant lineage per campaign

`hydra/lineage.py`, `GET /v1/campaigns/{campaign_id}/lineage?run_id=` (`hydra/api_lineage.py`), panel
"Linia wariantów" on the campaign card. Computed on demand from the stored run (members, scored pairs, genes), so
runs written before 2026-11-21 20:00 need no re-analysis; nothing is written to `Campaign.meta`.

1. **Order.** Members are sorted by `incidents.observed_at` (parsed to UTC, date-only = midnight UTC), ties by
   incident id. Members without a date follow the dated ones in import order (`incidents.rowid`), then id
   (`direction: "mixed"`). With no date at all the order is the import order (`"import_order"`); with neither,
   an undirected maximum spanning tree (`"undirected"`). The direction is **the observation order, not a proven
   edit order**; the API and the UI say so ("kolejność obserwacji").
2. **Tree.** Only pairs scored in the run count. Each is oriented from the earlier to the later member, so the
   candidate graph is a DAG, and a maximum spanning arborescence / branching (Edmonds) reduces to "each member keeps
   its strongest incoming edge". Edge weight = `pairs.detail.support` (else `campaign_bits`, else `total_bits`),
   only > 0; ties go to the earlier parent. A member with no earlier scored partner is a root (a forest is
   possible). The total weight equals `networkx.maximum_branching` on the same DAG (test).
3. **Edge diff.** Gene kinds are grouped into categories: domain, creative (VIS), text (TXT), contact (phone, IBAN,
   e-mail), operator ids (tracker / pixel / operator_id / ad ids, INF operator tier), kit (DOM / JS / CSS / asset
   files, kit and platform tiers), person (ID), narrative (NAR), platform metadata. Kept = the pair's stored
   positive reasons (bits > 0) plus exact or near-hash (PDQ / pHash Hamming) value overlaps (bits shown as "—");
   changed = kind observed on both sides with disjoint values (a different file or perceptual hash is not reported
   as a change when its category kept evidence); added / removed = kind observed on one side while the same
   extractor produced observations on the other side; otherwise the kind is "nieznane" on that side, never a
   change. Classifier, language, date-of-capture/registration kinds and boilerplate / label tiers are ignored.
   Each edge has a Polish one-line summary; synthetic test output: "wariant v3 (po v2): nowy nagłówek; bez zmian:
   domena, piksel, pliki zestawu, osoba / wizerunek".
4. **Limits.** Observation dates come from the material (file names, captures) and can be missing or late; the
   parent is the strongest earlier link, not necessarily the source copy; the stored reasons are the scorer's top
   reasons only, so a kept kind without bits may still have contributed nothing; categories are a name/family
   heuristic. Acceptance (SYNTHETIC, `tests/core/test_lineage.py`): a 4-member chain (new domain, then new
   headline, then new person photo, rare tracker and kit files kept) reproduces the order and names the changed
   category on every edge; output is identical across runs and input orders; every edge has a kept kind with
   bits > 0; singletons have no edges; no dates gives `import_order`. Not yet measured on the NASK sample.
5. **Tree by mutation distance (ADV-CAMP, 2026-11-22; API output behind `HYDRA_ADV_LINEAGE=1`).**
   `hydra/lineage_mutation.py` adds `mutation_tree` to the lineage response: the spec M7 minimum spanning
   arborescence (Edmonds, `networkx.minimum_spanning_arborescence`) over directed member pairs, rooted at the earliest
   observation (its incoming edges are removed). Edge cost u -> v = mutation(u, v) + 2.0 x dir(u, v) +
   0.5 x (S_max - S(u, v)) / S_max. mutation = sum over gene kinds of a category weight (text, creative, domain,
   contact, operator, person 1.0; kit, narrative 0.5; meta, other 0.25) x a per-kind distance: near-hash image kinds
   (PDQ, pHash, dHash, aHash) use minimum Hamming / hash bits (<= 0.25 = "transformacja obrazu", above = "nowa
   kreacja"); other kinds observed on both sides use the Jaccard distance of their value sets ("edycja tekstu",
   "rotacja domeny", "zmiana"); a kind observed on one side while the same extractor ran on the other costs 0.5
   (added / removed); a kind not observed on one side costs 0 (unknown, not a mutation). Hashes that restate other
   genes (file sha256, MinHash, SimHash, TLSH, embeddings) are not counted. dir = 1 when the parent was observed after
   the child, 0.5 when a date is missing, else 0; resolution and recompression hints of the spec are not extracted
   as genes and are not used. Every edge lists its mutations (category, type, distance, before / after values) and a
   Polish summary; the UI lineage panel shows them under "Drzewo mutacji (Edmonds)". Up to 80 members every ordered
   pair is a candidate (always spanning); above, scored pairs only (minimum branching, possibly a forest). The tree is
   a hypothesis, not a proven edit order. Acceptance (SYNTHETIC, `tests/core/test_adv_camp.py`): the 4-member chain
   gives v1 -> v2 -> v3 -> v4 with domain rotation, text edit and person change on the edges; the total cost equals a
   brute-force minimum over all arborescences; output is independent of input order.

## 8b. Consistency gate (spec M6, ADV-CAMP 2026-11-22)

`hydra/consistency.py`. Splitting behind `HYDRA_ADV_GATE=1` (default off); the report is on by default and read-only:
`GET /v1/campaigns/{campaign_id}/consistency?run_id=` (`hydra/api_consistency.py`), panel "Bramka spójności (M6)" on
the campaign card.

1. **Merge conditions** (spec "Algorytmy" §6) for clusters A and B, with S = `PairEval.support`: (1) at least two
   `same_campaign` links between A and B, or one with S >= 2 x T_link (two singletons joined by one link pass: the
   only possible pair is the pair decision); (2) median S over every scored pair between A and B >= T_low (unscored
   pairs are unknown and left out; the spec samples 5 x 5, the product uses all scored pairs, deterministic);
   (3) no pair between A and B with a hard contradiction (|bits| >= 4; today the scheme mismatch).
2. **Replay.** Inside each connected component of >= 3 members the link edges are replayed in descending S (analyst
   must-links first, without a gate) until no merge passes. Then the member rule: inside a part of >= 3 members every
   member needs >= 2 links to the rest of its part or one link >= 2 x T_link ("strong single bridge", reported with
   its reason); the weakest failing member is detached until the rule holds. Cut edges are removed from the NEXUS
   graph before the size guard; split campaigns carry `meta.consistency_gate` and an alert.
3. **Report** per multi-member campaign: verdict (consistent / weak members / gate would split / pair campaign /
   contradiction), cohesion and weakest link (from `campaign_support`), median S of scored pairs, members held by a
   single pair, strong single bridges with the reason, contradiction pairs, the parts the gate would produce, and per
   member the supporting pairs, median S and the strongest pair with its top reasons. Bits, not probabilities.

## 9. Open-set assignment of new material

`hydra/assign.py` (M9), CLI `hydra assign <file|folder|url> [--kind url|screenshot|hint|file]`, API
`POST /v1/analyse`. Both call one dispatcher, `hydra.pipeline.analyse_input(kind, payload)`:

| Input | Processing | Offline result |
|---|---|---|
| URL | isolated no-JS fetch (`hydra.enrich.fetch`: GET only, no cookies kept, 15 s per request, 5 MB HTML cap, SSRF guard, redirect chain recorded); landing HTML and the HAR-like log are stored in the CAS as evidence | "strona niedostępna offline": URL genes only (host, registrable domain, path/label/digit skeletons, parameter set, IDN/homoglyph folding, brand look-alike vs the public brand list of `hydra.watch.lexicon`) |
| Screenshot with a link | OCR → URLs / domains / phones; the best URL (scheme + path first) is followed as a URL input into the same incident | OCR genes + URL genes |
| Hint (product name, keyword) | case- and diacritic-insensitive search over corpus genes (texts, IDs, brands, domains, titles) and stored landing HTML, ranked by rarity bits | ranked incidents and campaigns with the matching genes |
| File | image, video, HTML/MHTML, HAR, PDF (text + link annotations, stdlib parser), SVG (text nodes only), ZIP; chat exports (WhatsApp Android/iOS text, Telegram JSON, Messenger JSON, generic logs) are parsed into messages: sender personas (ID `chat_persona`), first-message date, URLs in messages as INF URL genes; phones, IBANs, amounts and phrases come from message bodies | same |

Every result carries `input_report`: a status per file (`ok`, `duplicate` of incident X, `unreadable`,
`unsupported`, `no_features`, `skipped` with code such as `ZIP_PATH_TRAVERSAL`, `ZIP_SYMLINK`, `FILE_TOO_LARGE`,
`FILE_EMPTY`; a rejected archive such as `ZIP_BOMB_SUSPECTED` is not unpacked). Material with no observed gene
besides the file hash (empty folder, corrupt or unsupported file) is `abstain` with that reason, never a "new
pattern". A dataset ZIP or multi-incident folder sent to `POST /v1/analyse` (upload or `{path}`) runs as a
corpus analysis (adapter rule `classify_input`). The same file re-uploaded under another name reuses its
content-addressed incident (`duplicate_of`) instead of appending a copy. HEIC/HEIF images get a PNG rendition
(ffmpeg >= 7.1 or macOS `sips`; otherwise `HEIC_UNREADABLE`). A URL typed without a scheme gets `https://`.
Brand look-alike genes are boilerplate: the impersonated brand is the target, shared by unrelated
campaigns, so it is shown but never links. Tests: `tests/inputs/test_inputs_e2e.py` (synthetic inputs only).

Measured 2026-11-21 20:00–21:15 CET on the Linux server (`scripts/measure_inputs.py`, commits f58f608 and 2feb807, every registered
extractor with the real weights, 12 extraction workers; synthetic corpus of 65 incidents: campaign A on the
non-resolving domain pko-bonus24.com, campaign B, 60 fillers). Offline = `HYDRA_OFFLINE=1`; live =
`HYDRA_ENRICH=1 HYDRA_OFFLINE=0` (RDAP, DNS, crt.sh, ASN, CERT Polska list and the page fetch, all cached);
replay = offline on a fresh state with the live cache. Genes = observed genes of the new incident.

| Input | Offline: genes → decision, s | Live: genes → decision, s | Replay |
|---|---|---|---|
| URL on campaign A's domain | 11 INF → new_campaign naming A as best candidate (9.8 bits, 0.2 short of the link; kit 12.8 bits with A), 2.9 | 13 INF → same, 2.4 (fetch: "nazwa domeny nie rozwiązuje się w DNS") | same (2feb807: 3.4 s) |
| URL, homoglyph of that domain | 8 INF → new_campaign, 0.5 | 9 INF → new_campaign, 0.8 | same |
| URL, fetchable public page (example.com) | 6 INF → new_campaign, 0.4 ("niedostępna offline") | 105 INF/NAR/TXT → new_campaign, 20.4 (fetched, HTML + HAR in CAS) | 105 → new_campaign, 9.4 (from cache) |
| Screenshot with a link (real RapidOCR) | 68 → assign A, 27.3 (cold model load) | 70 → assign A, 8.7 | same |
| Hint "PKO bonus" | 3 ranked hits (campaign A members), 0.1 | 3 hits, 0.6 | same |
| WhatsApp chat .txt | 88 ID/INF/META/NAR/TXT → assign A, 5.5 | 88 → assign A, 4.5 | same |
| Telegram chat .json | 72 → assign A, 4.8 | 72 → assign A, 4.3 | same |
| PDF ad | 54 INF/NAR/TXT → assign A, 4.6 | 54 → assign A, 3.7 | same |
| HTML ad | 57 → assign A, 4.5 | 58 → assign A, 4.3 | same |
| Incident ZIP | 57 → assign A, 5.3 | 58 → assign A, 5.0 | same |
| MP4 (synthetic test pattern, unrelated) | 17 META/VIS → new_campaign, 4.7 | 17 → new_campaign, 2.5 | same |
| Corrupt PNG | 0 → abstain (no features), 0.3 | same | same |
| Empty folder | 0 → abstain, "pusty folder", 0.3 | same | same |

Corpus analyse: 48.6 s offline, 485 s live (rate-limited lookups for 63 domains), 39.8 s replay. These are
synthetic checks of the input paths, not accuracy metrics.

For a new incident X:

1. rarity is recomputed with X included (X's own values never make themselves look common: the pair is excluded);
2. candidates are the retrieval union restricted to pairs touching X;
3. each pair is scored (§7); per campaign: best pair, support (members with a same-campaign pair), campaign-level
   bits of the best pair;
4. decision (`AssignmentDecision`):
   (`assign._decide`; campaign score s_c = best support of a member pair, support plus contradiction bits)
   - `assign` when the best campaign's best pair is `same_campaign` with s_1 >= `link` (10), unless the second
     campaign also has s_2 >= `link` and s_1 - s_2 < 3 (`MARGIN`), which gives `abstain`;
   - `abstain` (grey band) when the best pair's hypothesis is `abstain`, its campaign-level bits are at least
     `ABSTAIN_MIN` = 6, it carries no contradiction, and it has operator evidence or at least two families with
     campaign bits; a pair with a contradiction is not abstained here and ends as `new_campaign`;
   - `new_campaign` otherwise: reported as a **new pattern**; `group_id` is still set when kit-level evidence ties
     X to a known campaign group ("new campaign, same toolchain").

5. **Bridges across an analyst cannot-link** : if X has link-strength evidence (same-campaign
   pair, support >= link) to two campaigns that an active cannot-link separates (resolved constraints of the base
   revision plus pair-level `reject_link` reviews), X abstains with the reason "bridges cannot-link" and
   `meta.bridge_cannot_link` (both campaigns, their bits and pairs, the cannot-link pairs). Assigning would silently
   re-join what the analyst split. The same check runs on earlier assignments when a review publishes a revision
   (§11a).

6. **Batch of new items** (`hydra/batch.py`, CLI `hydra assign-batch <dir> --out <dir>`, API
   `POST /v1/assign-batch`, GAP-AUDIT B3). Each item is first decided alone against the fixed base run (steps 1-5,
   unchanged). Then the items that came back `new_campaign` are scored against each other with the same pair
   scorer (candidates as in §6: every pair below 60 batch items, index candidates above; rarity over the base run plus the whole batch, kit units = base campaign groups, the base run's prior
   and fusion model) and clustered with the batch rule of §8 (connected components over `same_campaign` pairs,
   analyst cannot-links respected). Every such item gets a `new_pattern_id` (`N-<sha8 of the members>`); items of
   one unseen campaign share it, and the pattern carries its core genome and the linking pairs with their
   reasons. `assign` and `abstain` decisions are never changed: a new item that links to a batch item assigned to
   a known campaign is only listed (`batch_links`), it is not moved into that campaign, because its own evidence
   against the campaign's members stayed below the link threshold. The batch is stored as a run of kind
   `assign_batch` (pairs among the new items, the new patterns as campaigns of that run, the report in
   `runs.metrics`), and the members' assignment rows get `meta.new_pattern_id`, `meta.new_pattern`,
   `meta.duplicate_of`. Outputs: `assignments.json`, `assignments.csv` (columns `new_pattern_id`,
   `new_pattern_size`, `duplicate_of`, `duplicate_of_item`), `new_patterns.json`. Acceptance (synthetic,
   `tests/core/test_batch.py`): three items sharing a new host, tracker pixel and phone share one
   `new_pattern_id`; two unrelated items do not; a known-campaign variant is still assigned and joins no pattern.

Validated by leave-one-incident-out assignment and leave-one-campaign-out rejection (§12; in-sample, see the
header): on the sample 10 of 10 leave-one-out decisions were correct, and 10 of 10 held-out items were rejected as
`new_campaign`. Those 10 are 5 members of the 2 multi-member campaigns ({1, 2, 3} and {48, 61}) and the 5
singletons, so only 2 campaigns test "a held-out campaign is not pulled into a sibling".

**Who decides: threshold or conformal set** (`hydra/conformal.py`, `apply_conformal`). `analyse` calibrates a
split-conformal predictor on the corpus: one nonconformity score (minus the support) per member of a multi-member
campaign (leave-one-incident-out, capped at 120 items by a round-robin over campaigns), epsilon = 0.1. Every
assignment stores the prediction set, p-values and `decided_by`.
- Fewer than 20 calibration items (`MIN_CALIBRATION`): the set is reported only and the threshold rule above
  decides (`decided_by: threshold`). The sample has 5 calibration items (members of {1, 2, 3} and {48, 61}), so on
  the sample the threshold rule always decides.
- Usability is derived from the calibration array itself (finite alphas, `n`, `0 < eps < 1`, at least 20 items),
  never from a stored flag; problems are listed (internal notes, not published).
- 20 or more usable items: the calibrator can only confirm or demote, never promote (E-SH-16): a one-campaign set
  confirms a threshold `assign` to that campaign that meets the link rule, two or more give `abstain` (a threshold
  `new_campaign` stays new), an empty set gives `new_campaign`. A campaign can enter the set only through the
  minimum-evidence gate (`conformal.eligible`): campaign-level support >= `link` (10 bits), no contradiction, and
  operator-tier evidence or two families. The nonconformity score saturates at 2 x `link` (20 bits), so strong
  operator evidence is never "atypical". The output states only its guarantee: P(false new-pattern flag) <= eps for
  incidents of known campaigns under exchangeability, marginal, with the 5-95 % band of the conditional coverage
  (0.78-0.98 at 20 items). No number on NASK data measures the conformal path.

Cap of the leave-one-out pass (2026-11-21, EXT-PROCESS). Every self-assignment item scores one incident against
the whole run, so the pass grows faster than linearly with N (estimated 1-2.5 h per 10k incidents on the
128-core server; measured 453 s for 837 incidents in state-int run analyse-20261121T203607-6c2dae). The
environment variable `HYDRA_SELF_ASSIGN_MAX=<n>` (default unset = every incident, unchanged behaviour) runs the
pass on a deterministic stratified subset of n incidents: members of multi-member campaigns round-robin over the
campaigns (sorted ids), singletons in sha256 order, each stratum in proportion to its share of the run (at least
one item of each when both exist). Rates in `metrics.assignment` are then over the subset and
`metrics.assignment.capped` records cap, run size and subset size; incidents outside the subset have no
leave-one-out assignment row. The stage-cache key includes the cap. Used for the external-corpus demo runs
(state-ext-all); the NASK sample (10 incidents) is never capped. `HYDRA_DB_BUSY_MS` (default 5000) raises the
SQLite lock wait when two batch jobs share one state dir. Test: `tests/core/test_self_assign_cap.py`.

## 10. Missing data, duplicates, formats

- **Unknown is not negative.** A missing modality, an unreachable page or a failed lookup removes that family from
  the pair (listed in `missing_families`); it never lowers the score. Learned models get explicit missingness
  indicators instead of zeros.
- **Duplicates**: identical bytes are deduplicated at import by sha256 (one artefact, several references). A pair
  that shares an identical original file (`artifact_sha256`) or a near-identical creative or page text (§7
  near-duplicate rules) links on that evidence alone, with the reason "Identyczny plik materiału (duplikat)".
  In a batch of new items (§9 step 6) an item whose incident has exactly the artefact set (sha256) of an earlier
  batch item or of a base-run incident is reported with `duplicate_of` (incident id) and `duplicate_of_item`; a
  copy of a batch item follows its original's new pattern and is not counted as an extra pattern member.
  A single file (not a folder or archive) whose bytes were already imported through the single-item path reuses
  that incident (`pipeline._already_imported`): no second copy is appended, the decision is recomputed against the
  current base run, and the result carries `duplicate_of` (stored in `Assignment.meta`).
- **What duplicate marking does not do** (state on `a213ae8`): it only covers identical artefact sets (sha256);
  near-identical creatives or pages are linked by the near-duplicate rules but are not marked `duplicate_of`.
  There is no `duplicate` hypothesis value. Document frequency still counts every incident, so byte-identical and
  near-identical incidents each add one to the df of the values they share and lower those values' bits (§5,
  "Repeated values"); CR-VP-1 (count byte-identical incidents as one rarity unit) is open. The SYNTHETIC check
  "0 campaign inflation" (§12) measures the number of campaigns, not the bits of the shared values.
- **Formats**: images by magic bytes (not extension), video to frames, saved HTML and MHTML, HAR, JSON metadata,
  plain text logs, chat exports, PDF, SVG, ZIP archives with traversal, symlink and bomb guards; every file ends
  with a status. Text is decoded as UTF-8 (BOM, UTF-16 BOM) or cp1250, so the same chat exported in another
  encoding gives the same genes. Nothing is executed: HTML and SVG are parsed as data (stored artefacts are
  served with `Content-Security-Policy: sandbox` and `nosniff`), ffmpeg reads local files only
  (`-protocol_whitelist file,pipe`).

## 11. Explanations

Every link carries a `PairScore`: per-family bits, the top reasons (`GeneMatch`: both values, document frequency,
bits, evidence pointers on both sides, a one-line Polish explanation), contradictions, missing families, the
candidate indexes and the scorer version. The UI shows it as a **bit waterfall** next to both artefacts with the
matched regions highlighted. Campaigns show their core genome; every core-genome entry carries the evidence
pointers of the first two members holding the value (`evidence_a`, `evidence_b`, member order), so a campaign
reason opens the artefact like a pair reason (GAP-AUDIT B10). Assignments show the top candidate campaigns with
their best pair.

Zero-bit explanation rows (after the evidence, at most 3 per pair): `tier_conflict` (conflicting tier annotations
and the level used), `contact_operator` (a corroborated contact counted as operator evidence), `longest_shared_text`
(the longest shared normalised text with its document frequency, ADOPT #8) and uncorroborated partial audio.
Scheme ids are shown in Polish ("inwestycje", "hazard", ...).

Each same-campaign and group link also carries a leave-one-family-out check (`synapse.robustness`, stored in
`pairs.detail.robustness`): the pair is rescored with each contributing family removed, so the analyst sees whether
the link rests on one family only.

Optional Polish briefs and pair explanations (ADR-008, `hydra/llm/`, off by default; when enabled the default
backend is a local Bielik model behind `llama-server` on 127.0.0.1, `HYDRA_LLM_BACKEND=local`; Claude on Amazon
Bedrock or the Anthropic API only when `HYDRA_LLM_BACKEND=bedrock|anthropic` is set explicitly) are
written only from a numbered list of stored facts; a validator rejects answers that cite nothing, cite a missing fact
or contain a number not in the facts. The layer is cached and replayable (`HYDRA_LLM=replay`), text is redacted
before sending, and its narrative genes have a campaign cap of 0, so the LLM never creates or supports a link. It
is not needed for the demo or the evaluation.

## 11a. Analyst review loop

`hydra/review.py`, `hydra/review_queue.py`, API `hydra/api_review.py` .

- **Decisions** (accept / reject a link, move, merge, split, undo) are `reviews` rows plus a hash-chained audit
  event; `apply_review` publishes a new campaign revision computed from the root batch run under all active
  constraints, so undo restores the previous membership exactly.
- **Ignore value** (`ignore_value {kind, value}`, ADOPT #2): the value moves to the boilerplate tier (0 bits); the
  stored pairs whose two incidents hold it are rescored in the new revision, undo restores the stored scores. The
  `ReviewAction` contract has no member for it yet (CR-HARDEN-1), so the API cannot create it until the architect
  adds it; `hydra.review.new_review("ignore_value", ...)` works for the CLI and tests.
- **"Niepewne" (unsure)** marks a pair or an abstained assignment: an audit event (`REVIEW_UNSURE`, undo
  `REVIEW_UNSURE_UNDONE`) that removes the item from the queue and never changes campaigns. `ReviewAction` has
  no unsure value yet, so the marks live in the audit log (contract request CR-REVIEW-1).
- **Dependent assignments**: when a revision separates campaigns by cannot-link, every open-set assignment of the
  same root whose link-strength pairs reach two separated campaigns becomes `abstain` in the new revision
  (`ASSIGNMENT_BRIDGE_ABSTAIN` event); an undo that removes the separation restores it to the campaign of its
  strongest link (`ASSIGNMENT_BRIDGE_RESTORED`).
- **Queue** (`GET /v1/review/queue`): bridges, scorer abstains and abstained assignments, near-threshold pairs
  (|support - 10| <= 3 bits) and conflicts (a contradiction with some support, or inside one campaign), ordered
  by a heuristic expected value of the decision = uncertainty x impact. Uncertainty = 2^(-|bits - threshold| / 2),
  at least 0.5 when the link rests on one family or carries a contradiction; for an abstained assignment
  2^(-margin / 2), 1 for a cannot-link bridge. Impact = incidents whose membership flips: the smaller side of a
  bridge, the smaller of two campaigns a cross pair would merge, otherwise 1 (2 for a cannot-link bridge). Pairs
  already decided, marked unsure, or between two campaigns that already have an analyst decision are skipped.
  This ordering is a heuristic, not a probability and not a measured gain. It is kept as `order=ev`.
- **Queue by expected information gain** (default `order=gain`, `hydra/queue_gain.py`, ADV-QUEUE 2026-11-22). For
  each item, a counterfactual over the current link graph counts the pairwise co-membership decisions that a
  confirm and a reject would change:
  - reject inside a campaign: the minimum weighted edge cut that `nexus.cluster` would make (analyst must-links
    cannot be cut);
  - confirm across campaigns: the merge, |A| x |B|;
  - abstained assignment: 1 + |C|;
  - each open-set assignment to an affected campaign adds one.

  Expected change = p x confirm + (1 - p) x reject, with p = 1 / (1 + 2^(-(evidence - 10))). p is uncalibrated.
  Evidence = the pair bits, plus, for campaign mates, the min-cut bits of the other paths. Items are ranked by
  expected change / assumed effort: 1 per pair, +0.5 for a contradiction, 1.5 for an assignment card. Each item
  carries `why_first_pl`, shown as "Dlaczego to pierwsze".

  Measured on SYNTHETIC oracle curves (internal notes, not published), mean pairwise F1 over 1..30 answers:
  - sparse regime: 0.712, vs 0.612 for `ev` and 0.586 for random;
  - noisy regime: 0.798, vs 0.691 for `ev` and 0.659 for random.

  The new order is never below `ev` at any k. It changes only the order: campaigns and decisions are unchanged.

## 11b. Similar cases card

`hydra/similar_cases.py`, `GET /v1/incidents/{id}/similar` (TheHive behaviour, clean-room spec
`docs/cleanroom/thehive_similar_cases.md`; no TheHive code used). For a target incident: its observed discrete
genes (no vectors, no `level = none` genes, no stop values), the other incidents of the run holding the same exact
(kind, value), restricted to rare values (held by at most max(3, 5 % of the run) incidents; common shared values
are only counted). Per row: shared / total values, shared / total operator-tier values, counts per kind, the
shared values with document frequency and rarity log2(N / df) bits (the rarest value per dependency group is
summed, so correlated views of one observation count once), and a duplicate flag for a shared original file.
Order: shared operator values, rarity bits, shared count, shared ratio. It explains resemblance; it is not a link
score. The analyst "ignore value" flag of the spec is not built yet .

### Known limitations

- **CDN fronting.** Every lure domain in the sample sits behind Cloudflare, so the IP, ASN, server-header and
  InternetDB genes describe the CDN, not the operator. They are boilerplate (0 bits) for CDN prefixes, and the
  origin server stays hidden. Operator-level infrastructure evidence has to come from what the CDN does not hide:
  account-specific nameserver pairs, registration date and registrar, certificate history, tracker and backend
  identifiers in the HAR and page code, and backend (CRM, push, postback) hosts, which are often not CDN-fronted.
- **Infrastructure still carries most campaign decisions.** Run `eval-20261121T204751` (internal notes, not published,
  SYNTHETIC `sample+syn`, with the standalone near-duplicate rules of §5): removing INF lowered campaign F1 by 0.363
  (1.000 to 0.637) and LOO accuracy by 0.145; removing TXT by 0.046, META by 0.021, VIS, ID and NAR by 0.000. On
  their own, INF reached 0.926, TXT 0.347, VIS 0.254, META 0.135, ID and NAR 0. The near-duplicate rules
  (page-text MinHash Jaccard >= 0.9 over >= 50 shingles; the same image units with pHash <= 6 or PDQ <= 31 and
  DINOv2 >= 0.95, excluding app UI, logos, avatars and other boilerplate) give 12 bits and may link alone. Page
  images held by more than 2 incidents are excluded as stock (§5); other stock reuse below that count can still
  count. The multi-image count discounts stock images by near-hash frequency and counts distinct images one-to-one (internal notes, not published).
- **Crop recall is partial.** Sub-window hashes cover uniform crops of 10-20 % per side and 15 % strips; a crop that
  removes a 12.5 % banner measured 8 bits from the nearest window and is missed; crops below 64 % of the image,
  rotated crops and heavy overlays need the ORB/MAGSAC verifier. Since ADV-VIS it runs on visual near-miss pairs
  (§7a, `hydra/vis_geom.py`); crop matches count for scoring by default (`HYDRA_ADV_CROP`, on since 2026-11-22), the
  asset reason only with `HYDRA_ADV_GEOM=1` (default off).
  The matched dihedral transform is not named.
- **Short generic text** of fewer than 5 word tokens gives 0 bits even when it is rare in the corpus; a distinctive
  4-word slogan is therefore not evidence on its own (phrase shingles from longer texts still are).
- **Small sample.** With 10 incidents, document frequencies are 1-4, so rarity bits are coarse; tiers carry much
  of the judgement until the corpus grows.
- **Live page fetch is opt-in and limited.** In the default offline mode redirect chains come only from the saved
  HAR. With `HYDRA_ENRICH=1 HYDRA_OFFLINE=0` a URL input is fetched once, GET only, without JavaScript (§9), so
  pages that cloak, need JavaScript or have been taken down cannot be re-observed.
- **Time dependence.** DNS, registration and warning-list facts change; each carries its `fetched_at`.

### Specification 3.0 vs the product

The design specification (internal notes, not published) promised more than the competition build delivers.
To avoid over-claiming (external audit §12):

| Specification promise | State on integration (2026-11-21) |
|---|---|
| Multimodal genes with missing-data statuses | implemented |
| Rarity bits, hypotheses, abstention | implemented; formulas and thresholds differ from the first draft (this document is current) |
| Shared asset vs shared template vs campaign | implemented |
| Geometric verification (ORB + MAGSAC) in pair evidence | `same_asset` exists in `hydra/genome/vis.py`, **not wired** into scoring or the evidence view |
| Cluster consistency gate (three conditions) | implemented behind `HYDRA_ADV_GATE=1`, default off (§8b); the report is shown by default; size guard and group guard remain |

| Geometric verification (ORB + MAGSAC) in pair evidence | `hydra/vis_geom.py` (ORB/RootSIFT + MAGSAC++ on near-miss pairs): shown on the variant-diff page (`/v1/pairs/{a}/{b}/geometry`); counts in scoring only with `HYDRA_ADV_CROP=1` / `HYDRA_ADV_GEOM=1` (default off, §7a) |
| Cluster consistency gate (three conditions) | not as specified; size guard and group guard instead |
| Must-link, cannot-link, merge, split, undo | implemented: each review publishes a campaign revision (`hydra/review.py`); undo restores the previous membership; "niepewne" marks and the expected-value queue (§11a) |
| Incrementally updated, approved campaign core | partial: an approved new pattern does not yet join the corpus (audit A05) |
| Variant lineage, mutation direction, Edmonds arborescence | partial (2026-11-21, §8a): per-campaign tree in observation order with kept / changed genes per edge; direction is observation order, not a proven mutation direction; Edmonds arborescence by mutation distance behind `HYDRA_ADV_LINEAGE=1` (§8a.5) |
| Evidence ZIP with geometry, STIX 2.1, optional signature | partial: STIX 2.1 export and an XARF abuse-report ZIP with sha256-hashed evidence items exist (§14); no geometry in the evidence, no signature; other exports are JSON, CSV, Parquet with a manifest, and Neo4j |
| FAISS / BK-tree / LSH retrieval | replaced by exact kNN, vectorised Hamming and inverted indexes (measured choice, internal notes, not published) |
| No external inference | changed on purpose: optional ADR-008 LLM layer, off by default |
| Leak-free benchmark with proof of generalisation | protocol in internal notes, not published; gaps listed there |

### What HYDRA does not claim

- Rarity bits and per-feature waterfalls are Fellegi–Sunter / Splink ideas; HYDRA applies them to perceptual and
  embedding genes with dependency groups.
- Global retrieval plus local geometric verification is the standard copy-detection recipe.
- Landing-page template clustering and variant lineage trees have prior art.
- It does not identify who is behind a campaign and does not detect deepfakes.
- No calibration claim without a reliability plot on held-out labelled pairs; no robustness claim beyond the
  transformations tested; no synthetic or lab number presented as performance on NASK data.

## 12. Validation (mirrors NASK §6)

All values come from internal notes, not published, generated by `uv run python -m eval.run` (stream EVAL), run
`eval-20261121T204751` on commit `75f1917`. Sample numbers at the default thresholds are in-sample (header).
[a, b] are 95% intervals (Wilson for rates, campaign bootstrap for F1). Every result names its corpus:

- `sample`: the 10 organiser incidents (real), labels from the analysts' proposed ground truth at two levels
  (campaign and campaign group, see internal notes, not published);
- `sample+syn`: the sample plus synthetic variants derived from it (re-crop/re-encode, headline change, rotated
  domains with the URL skeleton kept, dropped HAR/landing/creatives, replaced portraits, language change, exact and
  near duplicates, format changes such as ZIP, MHTML, renamed extensions, nested bundles, misfiled HAR), plus
  distractor incidents from public phishing data (PhiUSIIL CC BY 4.0, PhishTank with attribution) and template
  clones (a sample page skeleton with all campaign-specific content removed: hard negatives);
- `syn-dev`: a separate synthetic set used only to choose thresholds, never for reported numbers.

Synthetic results are labelled synthetic and are not presented as performance on organiser material. With 10 real
incidents, real-sample numbers have very wide uncertainty; they are reported with counts, not only rates.

| NASK §6 check | How HYDRA measures it | Result (internal notes, not published) |
|---|---|---|
| Finding similar material and variants of the same campaign | pairwise and B-cubed P/R/F1 at campaign and group level against the development labels | sample: campaign pairwise F1 1.000 [1.00, 1.00], B-cubed 1.000, 0 false-merge pairs of 41; group F1 0.952 (1 group false merge of 35); SYNTHETIC `sample+syn`: campaign pairwise F1 1.000, B-cubed 1.000, group F1 1.000, 0 false merges of 1,361 |
| Correct assignment of new material to existing groups | leave-one-incident-out: remove an incident, rebuild, assign it back | sample: 1.000 [0.72, 1.00] (10 of 10; 5 known-campaign assigns, 5 correct new); SYNTHETIC `sample+syn`: 0.946 [0.85, 0.98] (n = 55; known-campaign 0.925 of 40, new-pattern rejection 1.000 of 15) |
| Recognising material that must not be linked to known campaigns | hold out every member of one campaign, rebuild, assign each back (must be `new_campaign`); analyst hard negatives (10 vs the investment group, 14 / 47 / 5) | sample: rejection 1.000 [0.72, 1.00] (n = 10: 5 members of 2 multi-member campaigns + 5 singletons), 0 of 8 hard-negative campaign violations, 1 group-level (14 and 5 share a group); SYNTHETIC `sample+syn`: rejection 1.000 [0.91, 1.00] (n = 40), 0 sent to a sibling campaign, 0 of 8 violations |
| Usefulness and diversity of features | per-family ablation; single-family runs; coverage per gene kind | SYNTHETIC `sample+syn`: -INF campaign F1 -0.363, LOO -0.145; -TXT F1 -0.046; -META F1 -0.021, LOO -0.055; -VIS, -ID, -NAR F1 0.000; coverage on the sample: VIS, TXT, ID, NAR, META 1.000, INF 0.900 |
| Robustness to duplicates, incomplete data, format differences | exact and near duplicates added; HAR, landing page or creatives removed; enrichment unreachable; ZIP, MHTML, renamed extensions, nested bundle, misfiled HAR | SYNTHETIC (n = 10 per case): exact and near duplicates 1.000 linked to the parent, 0 campaign inflation; assignment accuracy 1.000 without HAR, without landing page, with enrichment unreachable and for every format case; 0.900 without creatives; 0.800 with HAR only; 0.200 with a misfiled HAR; 0 crashes |
| Interpretability (reasons for links) | every pair and assignment carries `GeneMatch` reasons with document frequency, bits and a Polish one-liner; harness tests assert reasons exist | qualitative; shown in "Dowód pary" |
| End-to-end completeness | raw folders and ZIPs to campaigns and assignments, every exception counted | 0 crashes; sample cold run 98.2 s on 64 vCPU (internal notes, not published) |

Label caveat: the development labels put 3 in one campaign with 1 and 2 (the architect's decision on the analysts'
two readings). With the alternative set "3 separate", the sample gives campaign pairwise F1 0.667, B-cubed 0.929,
2 false merges, LOO 0.800 and rejection 0.800 (internal notes, not published). The hidden test set is the real measure.

Baseline ladder (internal notes, not published; every scorer uses the same CORE features and one threshold
chosen on SYNTHETIC `syn-dev`; the HYDRA row uses the CORE defaults):

| Scorer | sample campaign F1 | SYNTHETIC `sample+syn` campaign F1 | SYNTHETIC false merges | SYNTHETIC open-set rejection |
|---|---|---|---|---|
| B0 pHash only | 0.667 | 0.907 | 0 | 1.000 |
| B1 text only | 1.000 | 0.979 | 0 | 1.000 |
| B2 naive sum | 1.000 | 0.891 | 24 | 0.575 |
| B3 generic rarity bits (no tiers or caps) | 0.471 | 0.645 | 107 | 0.400 |
| LR fusion | 0.667 | 0.669 | 77 | 0.475 |
| HYDRA: rarity bits, tiers, caps (CORE defaults) | 1.000 | 1.000 | 0 | 1.000 |
| HYDRA with the link threshold chosen on `syn-dev` (24 bits) | 1.000 | 0.836 | 0 | 1.000 |

The naive sum is perfect on the 10 real incidents but merges 24 pairs and rejects 0.575 of the held-out
campaigns once template clones and distractors are added; generic rarity bits without tiers merge 107 pairs: that
is the boilerplate trap the tiers address. The HYDRA defaults were set on the sample (in-sample), the syn-dev row
is the out-of-sample threshold choice.

## 12a. Live view: new material over an immutable analysis (ADR-0009)

- An analyse run is a fixed snapshot: its campaigns, groups, pair scores and rarity are never edited. Material
  that arrives later (upload, `hydra assign-batch`, scheduler inbox) gets an open-set decision against that run
  (section 9) and is shown on top of it, marked **"nowe od analizy"**, with its own decision (`assign`,
  `new_campaign`, `abstain`). A new item assigned to a campaign is drawn inside that campaign's disc and counted in
  its size (`size`, of which `new`); it does not change the campaign's core genome, confidence or the scores of
  other pairs. A new full analyse makes it part of a new base.
- Every live number is a count over stored rows (incidents, genes, artifacts, decisions, pairs, exports), kept
  current by the single Writer in the same transaction as the event that changes it; a full recomputation gives
  the same numbers (property test, `tests/core/test_dataflow.py`). Nothing is extrapolated or estimated.
- Positions of new items on the campaign map are a deterministic placement (hash of the id on the campaign disc,
  or on a ring outside the base layout), not a layout result; distances there carry no meaning.
- HYDRA Watch alerts are streamed as candidates (fingerprint matches), never as confirmed members (section 9).

## 13. Implementation status

 Release 1.0; files present, not plans.

| Component | Module | Status |
|---|---|---|
| NASK incident adapter (roles by content, MHTML split), redaction | `hydra/adapters/nask_folder.py`, `hydra/redact.py` | in 1.0 |
| Genes: VIS (hashes, DINOv2, ORB/RootSIFT + MAGSAC++ pair geometry `hydra/vis_geom.py` and HSV colour `hydra/vis_colour.py` behind `HYDRA_ADV_*` flags, optional SigLIP2 whose weights are not in `models.lock.json`), INF (HAR, DOM, web parsing, kit file set, kit names and exfiltration ids, operator ids, lure facts, toll-fraud chain, HTTP/TLS fingerprints, parked pages), ID (faces), logos | `hydra/genome/{vis,vis_media,vis_siglip,inf,har,dom,webparse,kitset,kit_names,kit_exfil,kit_rules,operator_ids,lure,tollfraud,http_fp,parked,domain_pl,ident,logo,artifact}.py` | in 1.0 |
| Genes: audio (fingerprint, ASR), video AV-sync, provenance, manipulation-suspicion signals (0 bits), chat logs | `hydra/genome/{aud,vid_avsync,vid_seq,prov,dfk,chat}.py` | in 1.0 |
| Enrichment with cache and replay | `hydra/enrich/{rdap,dns,crtsh,asn,cert_pl,domains,fetch,client}.py` | in 1.0 |
| Rarity, retrieval, scoring, clustering, assignment, pipeline | `hydra/entropy.py`, `retrieval.py`, `synapse.py`, `nexus.py`, `assign.py`, `pipeline.py` | in 1.0 |
| CLI | `hydra analyse <dir>`, `hydra assign <path|url>`, `hydra assign-batch <dir> --out <dir>` (new patterns among new items, duplicates), `hydra export-neo4j <out>`, `hydra scheduler status|watch|run` | in 1.0 |
| API + UI | `hydra/api_task.py` (`/v1/...`), `ui/src/pages/task/*` | in 1.0 |
| TXT genes, NAR, META | `hydra/genome/txt.py` and related | in 1.0 |
| Robustness (leave-one-family-out), conformal assignment (decides from 20 calibration items, §9) | `hydra/synapse.py`, `hydra/conformal.py` | in 1.0 |
| Batch assignment with new patterns and duplicates, variant lineage, analyst review loop, campaign support, similar cases | `hydra/batch.py`, `hydra/lineage.py`, `hydra/review.py`, `hydra/review_queue.py`, `hydra/campaign_support.py`, `hydra/similar_cases.py` | in 1.0 |
| Evaluation harness and results | `eval/` (harness; result tables not published) | results of run `eval-20261121T204751` on `75f1917` |

## 14. Eksporty i zgłoszenia (exports and abuse reports)

Added 2026-11-21 (CORE). All exports are read-only, use the enrichment replay cache only (no network
call) and pass values through `hydra.redact`. Nothing is sent anywhere: the analyst downloads and acts by hand.

| Export | Module / endpoint | Content |
|---|---|---|
| Abuse / takedown package | `hydra/export_abuse.py`, `GET /v1/campaigns/{id}/abuse-package` (ZIP) | one XARF v4 report per reported host (category `content`, type `fraud` with `fraud_type` from the scheme, or `phishing`): evidence items (screenshot / ad creative bytes, redacted landing HTML, HAR excerpt for the host, RDAP + hosting + CERT PL snapshot) with `sha256:` hashes and size ≤ 5 MB; abuse contacts from RDAP `entities[abuse]` (domain and hosting network); Polish cover note per host; `manifest.json` (`sent: false`, abuse-contact coverage, per-report XARF validity) |
| STIX 2.1 | `hydra/export_stix.py`, `GET /v1/campaigns/{id}/stix`, `GET /v1/runs/{run_id}/stix` | campaign, indicators (domain, URL path pattern, core file sha256; `x_hydra_bits`, `x_hydra_doc_freq`), infrastructure (`consists-of` domain/IP observables), observed-data per incident, relationships `indicates` / `uses` / `related-to`; deterministic UUIDv5 ids; validated by `stix2.parse` |
| Lifecycle timeline | `hydra/lifecycle.py`, `GET /v1/campaigns/{id}/lifecycle`, `GET /v1/runs/{run_id}/lead-times` | per host: RDAP registration, first/last certificate (crt.sh), first observation / last seen (incident dates), CERT PL listing/removal; lead times in days; corpus medians with n and coverage |
| Pivot queries | `hydra/pivot_queries.py`, `GET /v1/campaigns/{id}/pivots` | copyable strings for crt.sh, urlscan, Shodan, Censys and a DNS/RDAP list; HYDRA never calls these services |
| Neo4j graph (added 2026-11-21) | `hydra/export_neo4j.py`; CLI `hydra export-neo4j <out>`, `GET /v1/export/neo4j?format=cypher\|zip\|nodes_csv\|edges_csv` | one builder, the same graph on both paths: Incident, Campaign, CampaignGroup, Gene (shared values, doc_freq ≥ 2) nodes; MEMBER_OF, IN_GROUP, HAS_GENE, LINKED_TO; CSV + `import.cypher` (LOAD CSV) and the self-contained `graph.cypher` (= API `format=cypher`); `docs/NEO4J.md` |
| Enriched dataset (added 2026-11-21) | `hydra/export_dataset.py`; CLI `hydra export-dataset`, `GET /v1/export/dataset?run_id=` (ZIP of the same folder) | incidents, genes, enrichment, links, assignments (with `duplicate_of`, `new_pattern_id`) as Parquet (incidents and assignments also CSV), campaigns and groups JSON, README, `manifest.json`; internal notes, not published |

XARF validity: `validate_xarf` checks the subset of the XARF v4.2.0 schemas emitted here (required core fields,
formats, category/type enums, `fraud_type`, contact info, evidence items); it accepts the official v4 samples
(`content-phishing.json`, `content-fraud.json`; test `test_validator_accepts_official_xarf_samples`, run
2026-11-21). A full JSON-Schema validation is not in the shipped path (`jsonschema` is not in the lock).
Reporter identity: `HYDRA_REPORTER_ORG/EMAIL/DOMAIN`; the defaults are placeholders to replace before sending.

Measured lead times on the public corpus (the Linux server, `state-public-core`, run analyse-20261121T152443-5258d2,
6,663 incidents, 8,111 hosts, replay cache `cache/enrich-public`; internal notes, not published, 2026-11-21):

| Lead (later − earlier) | n | coverage | median days | p25 / p75 |
|---|---|---|---|---|
| registration → first observation | 3,013 | 0.371 | 0.84 | 0.14 / 11.13 |
| registration → CERT PL listing | 2,130 | 0.263 | 0.49 | 0.08 / 9.35 |
| first observation → CERT PL listing | 5,344 | 0.659 | 0.00 | 0.00 / 0.00 |
| first certificate → first observation | 9 | 0.001 | 423.07 | – |

Caveats: in this feed-built corpus the "first observation" of a CERT PL entry is its feed date, so observation →
listing is 0 by construction (not a detection speed). crt.sh data covers almost no hosts of this corpus (n = 9),
so the certificate lead times are not interpretable. We do not claim takedown speed (we prepare evidence only).
