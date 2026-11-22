# Known limitations of the 1.0 release

Status 2026-11-22 (Europe/Warsaw). Every row names the test, file or measurement it comes from. Quality numbers on
the organiser sample come from the evaluation run `eval-20261122T060608`; the sample, its labels and the full result
tables are organiser material and are not part of this repository, so only the summary numbers are quoted here.
The scoring and genome code were frozen before the final evaluation; none of the open items below was fixed for 1.0.

## Evaluation and claims

| # | Limitation | Consequence | Source |
|---|---|---|---|
| L1 | Labels of the 10-incident sample are the team's hypothesis. There is no second independent reviewer | Numbers on the sample measure agreement with the team's reading, not ground truth | evaluation protocol in `eval/` (`eval/labels.py`) |
| L2 | Default thresholds (link 10 bits, kit 6 bits) were set while studying the same 10 sample incidents | Every number at the defaults on the sample is in-sample; the synthetic and hidden-set-simulation material is derived from the sample and not independent | `hydra/assign.py`, `eval/hidden_sim.py` |
| L3 | Held-out-campaign rejection on the sample: 5 of the 10 held-out items are singletons and are rejected trivially; only 2 multi-member campaigns test pull towards a sibling | The rejection rate says little about sibling confusion | `docs/METHODOLOGY.md` §9 |
| L4 | The conformal set is inert below 20 calibration items; the sample has 5, so the bit threshold decides every sample assignment. Above 20 it can only confirm or demote, never promote | No number on the sample measures the conformal path | `hydra/conformal.py` (`MIN_CALIBRATION`), `tests/core/test_robust_conformal.py` |
| L5 | Bits are evidence weights, not probabilities. No reliability plot on held-out labelled pairs is part of this release | No calibration claim; every stored probability carries `calibrated=false` | `README.md`, `docs/METHODOLOGY.md` |
| L6 | The background prior is one-sided: it can only lower the bits of values common on the web, never add a link. It was neutral on the sample | Its effect is shown only as weaker evidence for common values | `docs/METHODOLOGY.md` §5, `hydra/priors/` |
| L7 | LR + isotonic fusion is opt-in (`hydra train`, `hydra analyse --fusion`) and scored below the rule-based path on the sample; the default path is tier bits with fixed thresholds | No learned-model claim | `hydra/cli_task.py` (`--fusion`) |
| L8 | Hidden-set simulator (synthetic variants of the sample incidents): 117/119 positives assigned, `creative_crop` 7/7, `landing_shot` 7/9; 2 positives came back `new_campaign` | Landing screenshots can still be flagged `new_campaign`; the crop result covers variants of the 10 sample incidents only | `eval/hidden_sim.py`, run `eval-20261122T060608` |
| L9 | `pdq_canon` (dihedral minimum) is unstable under small crops: a 2 % crop moved it to Hamming 122-132 on 10 of 12 frames | Crop variants depend on other visual genes | `scripts/measure_pdq_dihedral.py` |
| L10 | Infrastructure carries most campaign decisions; lure domains behind Cloudflare make IP and ASN uninformative | Material without infrastructure links groups mostly through near-duplicate rules only | ablation in run `eval-20261122T060608` |
| L11 | The hypothesis lab ("Laboratorium hipotez") is a tool for controlled robustness trials and for explaining decisions, not a better detection algorithm. Its next-observation planner tied with the fixed order (20 of 20 resolved by both, 0 fewer steps) on a synthetic pilot of 24 incidents | No accuracy or cost benefit is claimed; shadow mode only, default decisions unchanged | `tests/kontra/test_kontra.py` |
| L12 | HUNT ranking on the CERT Polska replay: 43 confirmed vs 42 and 42 for two baselines | No material gain of the ranking | `hydra/hunt/`, `tests/hunt/` |
| L13 | Past-only replay on the sample (rarity sees only earlier incidents): assignment 0.667 [0.35, 0.88], n = 9; of 3 known-campaign items 1 correct, 1 false assign | Standard leave-one-out (1.000) uses rarity from later incidents; in time order the result is much weaker | run `eval-20261122T060608` |
| L14 | Campaign groups over-merge at scale: on a 231-incident retrieval corpus (mostly synthetic) group F1 is 0.453 with 13,901 false group merges, while campaign F1 is 0.992 with 0 false merges (candidate recall 0.956 of 3,500 pairs) | Group links (shared kit/template) are not reliable above small corpora. Cause: misfiled variants (an incident carrying another incident's HAR) bridge two campaigns. Option `HYDRA_ADV_GROUPS=1` (confirmed bridges only, default off) measured group F1 0.848 with identical campaign partitions; a group is not attribution | run `eval-20261122T060608` |

## Open defects (not fixed in the release)

| # | Defect | Source |
|---|---|---|
| L15 | One rejected incident ZIP (bomb, nested, traversal) at a dataset root aborts the whole `hydra analyse` run; `hydra import` and `assign-batch` handle the same ZIPs per file. Workaround: unpack incident ZIPs of the known base | `scripts/hostile_smoke.py` |
| L16 | Foreign phone numbers written 3-3-3 are turned into Polish numbers (`+420 601 126 480` becomes `+48601126480`) | `scripts/measure_extraction.py` (synthetic ground truth) |
| L17 | A meta-refresh hop in a plain HAR body does not extend `redirect_chain` (20/20 synthetic chains truncated) | same |
| L18 | Person-name extraction misses genitive/dative -ski/-cki surnames | same |
| L19 | The IBAN pattern can glue the capital of a following Polish word to the IBAN | same |
| L20 | Brand extraction skips four-letter title-case brands | same |
| L21 | A bare `t.me/<handle>` gives no `domain_in_text` / `url_in_text` | same |
| L22 | Operator-host bits of a true pair fall as the campaign grows (8.4 bits at 4 members, 5.0 at 9); a large synthetic campaign splits into singletons at 9 members | `scripts/feature_economics.py` |
| L23 | `emb_headline` / `emb_body` put 41-49 % of their raw bits into false pairs (contained by the operator rule: 0 false links) | same |
| L24 | `chat_start_date` gives campaign-level bits to two chats that start on the same day (synthetic artefact; unchecked on real chats) | same |
| L25 | SMS lure incidents that share a sender number, a short-link domain and a recording stay singletons | strict xfail `tests/e2e/test_e2e_api.py::test_campaign_c_sms_lure_grouped` |
| L26 | Some `reason_pl` texts contain English fragments or family codes (TXT, VIS, INF); the UI maps them on display only | `ui/src/lib/displayPl.ts` |
| L27 | Whole-file evidence pointers (audio fingerprint, AV-sync, avatar geometry) are encoded as frame 0 / full-frame bbox; the UI shows "cały plik" | UI evidence view |
| L28 | Contradictions and missing families are not on `Assignment.meta`; assignments without candidates show no caveats | `hydra/contracts.py` |
| L29 | k-NN truncation is not counted in `retrieval_scope`; the UI shows the bound without a number | UI pair view |
| L30 | Category-nested datasets (`kategoria/incydent_N`) are imported per category folder; run `hydra analyse` per folder | [JURY_READINESS.md](JURY_READINESS.md) §6 |
| L31 | About 1000 incidents need about 32 GB RAM and more than 30 minutes; the progress counter can stand still for minutes | [JURY_READINESS.md](JURY_READINESS.md) §3 |

## Not shipped / not claimed

| # | Item |
|---|---|
| L32 | No face or logo reference gallery is shipped (biometric data, trademarks). Build one locally from your own material: GUI "Zbuduj galerię referencyjną", `hydra reference build`, `POST /v1/reference/build`; without it ID-gallery and ORB-logo genes are unknown, never a penalty | README "Galeria referencyjna", `hydra/reference_build.py` |
| L33 | No deepfake detection: DFK genes are manipulation-suspicion signals with 0 linking bits; image and video checks are off by default |
| L34 | No attribution: the tool links artefacts and does not name operators (`docs/METHODOLOGY.md`) |
| L35 | Not part of this release: SigLIP2 embeddings, a reliability plot, results on public benchmarks |
| L36 | Docker Desktop (macOS/Windows) was not tested; tested platforms are Linux x86_64 Docker and native macOS arm64 |
| L37 | Frozen held-out split (G1): the split and rule baselines exist, but the HYDRA run on that split was not verified; no held-out numbers are claimed | evaluation run `eval-20261122T065957` (product f450a70) for all other numbers |
