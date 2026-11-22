# Clean-room spec: content-based parked / for-sale page detector

## Source studied
- URLs: https://github.com/Lookyloo/parking-page-detection (last commit 2024-09-06) and https://github.com/Lookyloo/webpage-feature-extraction (last commit 2024-09-03). SHAs [U]. Neither repo has a LICENSE file, so treat them as all rights reserved.
- Files read: `feature_extractor/feature_extractor.py`, `feature_extractor/blacklist_approach.py`, `parking_page_detection/feature_selection_evaluation.py` (skim), `results/evalutaion_results.csv`. Studied 2026-11-21.

## Purpose
Recognise landing captures that are parked, for-sale, registrar-default or hosting-default pages. Their shared provider templates (DOM, title shape, assets) must not produce same-campaign links. Detection is also an explainable "why not linked" reason.

## Inputs
Rendered or saved HTML of the final document, landing URL and final URL (from the redirect chain), HAR request list (optional), and resolved A/NS from HYDRA enrichment (optional). It also uses the existing MISP parking-NS / parking-IP warninglist hits.

## Behaviour
1. Extract the visible text (strip script/style). Count alphanumeric characters.
2. Vocabulary features: count hits of a curated multilingual (PL, EN, DE, plus a few more) list of parking and for-sale phrases (domain for sale, this domain may be for sale, buy this domain, parked free, related searches, sponsored listings, and Polish equivalents such as "domena na sprzedaż" or "ta domena jest zaparkowana"). Also note whether such a phrase appears in the title, and whether two or more phrases appear on the same text line.
3. Structure features: presence of a nav element, iframe count, image count, whether the body is mostly a single iframe, whether the landing hostname appears in the title and in the text.
4. Link features: number of anchors, anchors pointing to the same host, distinct link hosts, average and maximum href length, link-text to total-text ratio, non-link text length.
5. Redirect feature: the final registrable domain differs from the landing one.
6. Infrastructure feature: the warninglist hit counts only if the resolved A records intersect the IPs actually seen in the capture (so a later DNS change does not mislabel an old capture).
7. Decision rule (no ML needed at HYDRA scale; a calibrated logistic model can replace it later):
   - parked if (infra hit AND text < 1500 chars), OR
   - parked if (vocabulary title hit), OR
   - parked if (vocabulary hits >= 2 AND link-to-text ratio >= 0.5), OR
   - parked if (single-iframe body AND iframe host is on the parking list).
   - Otherwise not parked. Also record a 0-1 score = fraction of satisfied indicators, for the explanation.
8. If parked: tag the incident `parked`. Downgrade DOM skeleton, DOM shingles, title, assets and favicon genes of that incident to `boilerplate:parked` (0 bits). Keep operator-tier genes (e.g. registrant, a tracker id on the page), which can still be meaningful.

## Parameters and thresholds
- Text cap: 1500 alphanumeric chars.
- Link-to-text ratio: 0.5.
- Vocabulary hits: >= 2.
- All are to be tuned on the NASK sample.
- Reference point from the studied repo: RF classifier F1 about 0.81 (precision 0.91, recall 0.73). Our rule set should aim for precision >= 0.95, because a false "parked" verdict removes evidence.

## Outputs
Per incident: (parked: bool, score, satisfied indicators list). Genes downgraded with note "parked page template". The explanation line looks like "not linked: both pages are parked-domain templates (vocabulary + parking NS)".

## Edge cases
- Scam landers that are deliberately thin (cloaked white pages): their text is short, but they lack parking vocabulary and parking infrastructure. They must not be flagged by rule 1 without an infra hit.
- Pages in other languages: vocabulary misses, but infra and link rules still apply.
- Hosting default pages ("It works!", cPanel default): treat as a separate `hosting_default` class with its own small phrase list. Same downgrade.
- Missing HTML: undecided. Do not downgrade.

## What NOT to replicate
- Do not copy the repos' keyword lists, feature names, datasets (CSV), or trained models. Build our own phrase list.
- Do not do live DNS during scoring. Use cached enrichment.

## Suggested acceptance tests
1. Three synthetic pages from one fictional parking provider template on unrelated domains: all flagged, and no same_campaign pair between them.
2. A short scam lander with a phone form and no parking vocabulary: not flagged.
3. A parking-NS warninglist hit, but the capture IP differs from the resolved A: the infra rule does not fire.
4. The explanation lists the satisfied indicators.

## Similarity-check reminder
Compare the final phrase list and code with the studied repos before merging. There must be no copied keyword lists, identifiers or comments.

## Implementation record
- 2026-11-21, ENRICH-PLUS builder : `hydra/genome/parked.py`, wired into `inf_dom`, `inf_http`, `inf_har`, `inf_enrich`; tests `tests/genes_inf/test_parked.py` (acceptance tests 1-4 plus a control test without the detector). Built from this spec only; the repositories were not opened. Own phrase lists (PL, EN, DE, FR, ES) and provider/ad-feed marker list; added rule R5 (provider marker plus a phrase or thin text). Details: internal notes, not published section 9.
- Similarity check: pending (to be run by a research agent; the builder may not open the source).
