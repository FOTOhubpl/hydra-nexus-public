# Clean-room behaviour spec: performance estimation of a clustering from a sample of verified clusters

## Source studied
- URL: https://github.com/Valires/er-evaluation, commit d68c0bd (2023-12-03), licence AGPL-3.0.
- Files read: er_evaluation/estimators/_estimators.py, estimators/_utils.py, estimators/from_table.py (names only),
  error_analysis/_cluster_error.py, error_analysis/_subgroup_discovery.py (signature). Date studied: 2026-11-21.
- Method published: Binette et al. 2022, "Estimating the performance of entity resolution algorithms: lessons
  learned through PatentsView.org" (arXiv 2210.01230). This spec describes behaviour only.

## Purpose
On a corpus too large to label fully, an analyst verifies a random sample of TRUE campaigns (each fully: all its
incidents found, e.g. by reviewing a seed incident's neighbourhood). From that sample, estimate the pairwise and
B-cubed precision/recall of HYDRA's predicted campaigns with standard errors, and list per-campaign errors. Fits
`hydra/review.py` (analyst review) and the evaluation report.

## Inputs
- predicted: mapping incident -> predicted campaign id (all incidents).
- sample: mapping incident -> true campaign id, only for incidents of sampled true campaigns (each sampled campaign
  complete).
- weights per sampled true campaign: inverse inclusion probability; presets "uniform" (sampled uniformly among
  campaigns) or "by size" (sampled by picking a random incident, so probability proportional to size; weight =
  1/size).

## Behaviour
1. Restrict predicted to incidents that have a predicted id; inner-join with the sample.
2. For each sampled true campaign c:
   - split counts: how many of c's incidents fall in each predicted campaign;
   - TP_c = sum over predicted campaigns of C(k, 2) for those split counts (correct pairs inside c);
   - P_c = C(|c|, 2) (all true pairs of c);
   - FP_c = for each member r of c, the number of incidents in r's predicted campaign that are not in c, summed over
     members (each wrong pair touching c counted from c's side).
3. Pairwise recall: numerator TP_c, denominator P_c. Pairwise precision: numerator TP_c, denominator TP_c +
   FP_c / 2 (half because a wrong pair between two different true campaigns is shared by both).
4. Multiply numerators and denominators by the campaign weight.
5. Ratio-of-means estimate over sampled campaigns with first-order bias correction: R = mean(N)/mean(D) times
   (1 + correction), the correction being (1/((n-1) mean(D))) times mean(D * (N/mean(N) - D/mean(D))); n = number
   of sampled campaigns; no correction when n = 1; estimate 0 if mean(N) = 0.
6. Standard error by the delta method: R times the square root of the sum over campaigns of
   (D/mean(D))^2 + (N/mean(N))^2 - 2 N D /(mean(N) mean(D)), divided by n(n-1); undefined (NaN) when n = 1 or the
   quantity is negative.
7. F-score estimates from the precision and recall estimates.
8. B-cubed analogues: per member r of c, precision contribution = |pred(r) intersect c| / |pred(r)|, recall
   contribution = |pred(r) intersect c| / |c|; aggregate per campaign then ratio-of-means as above (exact
   weighting of the B-cubed precision estimator was not verified [U]; derive from the paper).
9. Error table per sampled true campaign: extra = total wrongly merged incidents seen from members; missing =
   members not in the member's predicted campaign, summed; expected size difference; relative versions divided by
   size; exact-recovery indicator (0/1).
10. Optional subgroup discovery: fit a shallow regression tree (depth <= 3) on campaign features (size, families
    present, scheme type, language) predicting the error indicator, weighted by campaign weights; report leaves with
    high error as "where HYDRA fails".

## Parameters and thresholds
- Weight scheme; minimum sampled campaigns (warn below 10, refuse SE below 2); tree depth 3 and min leaf size 5.

## Outputs
- Table: metric, estimate, standard error, n sampled campaigns, weight scheme.
- Per sampled campaign: TP, FP, P, extra, missing, exact flag.
- Optional subgroup table.

## Edge cases
- Sampled campaign of size 1: P_c = 0 contributes nothing to recall but can contribute FP to precision.
- Incidents in the sample but missing from predicted (failed ingest): count them as singletons predicted, and report
  their number.
- Overlapping or incomplete verified campaigns violate the design: the analyst UI must mark a campaign "complete".
- All weights equal and n small: SE can be NaN; show "not estimable".

## What NOT to replicate
- The library's pandas structures, MembershipVector class, plotting, dataset loaders, search utilities, docstrings
  and examples. Implement directly on HYDRA's dict/array types.

## Suggested acceptance tests
1. Full-sample identity: when every true campaign is sampled with uniform weights the estimates equal the exact
   pairwise precision/recall (within the bias correction; use n large) and the exact B-cubed values.
2. Hand example: predicted {1:a,2:a,3:b,4:c,5:b,6:d,7:d,8:d}, true sample {1,2,3 -> c1; 4,5 -> c2; 8 -> c4}: compute
   TP, FP, P by hand and compare.
3. Coverage: synthetic truth of 500 campaigns, prediction with known errors, 200 random samples of 40 campaigns;
   the true value lies within estimate +- 2 SE in >= 90% of samples, for uniform and by-size weighting.
4. Weight sanity: sampling by size with uniform weights (wrong) biases recall; with 1/size weights the bias goes.

## Similarity-check reminder
Implement from this spec and the published paper only. Do not open the er-evaluation sources while coding. Compare
identifiers and comments against the source before merging; none should match beyond standard metric names.
