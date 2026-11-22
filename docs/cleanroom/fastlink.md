# Clean-room behaviour spec: missing-at-random Fellegi-Sunter EM with label-free error estimates

## Source studied
- URL: https://github.com/kosukeimai/fastLink, commit da2e889 (2023-11-17), licence GPL (>= 3) per DESCRIPTION.
- Files read: R/emlinkMARmov.R (EM), R/confusion.R (error table), R/dedupeMatches.R, R/nameReweight.R,
  R/fastLink.R (defaults only). Date studied: 2026-11-21.
- Method is published (Enamorado, Fifield, Imai 2019, APSR); this spec describes behaviour only.

## Purpose
Estimate per-family m (P(agree level | same campaign)) and u (P(agree level | different)) and the match prior lambda
from unlabelled candidate pairs, treating absent evidence as unobserved (never as disagreement), and derive
expected error counts for the accepted links without labels. In HYDRA this is an optional estimator for
`hydra/entropy.py` m values and an extra report; the fixed m = 0.5 stays the default.

## Inputs
- A list of candidate pairs. For each pair and each family f, an ordinal agreement level g_f in {0 = disagree,
  1 = partial, ..., L_f = strong} or "unobserved" (gene missing on either side, lookup failed, redacted).
- Only operator-tier and asset-tier genes; stop-genes and boilerplate excluded before EM.
- Optional priors: Beta(a, b) on lambda; per-family Dirichlet pseudo-counts on m.
- Tolerance (default 1e-4) and iteration cap (default 5000 for EM; HYDRA may use 500).

## Behaviour
1. Collapse pairs into unique agreement patterns (vector of levels with "unobserved" as its own symbol) with counts.
   All later sums are count-weighted over patterns, so cost depends on the number of patterns, not pairs.
2. Initialise lambda small (e.g. 0.01 or from the prior mean), m levels increasing with agreement, u levels
   decreasing (e.g. random Dirichlet draws then sorted).
3. E-step: for each pattern, the match likelihood is the product over observed families of m_f(level), the
   non-match likelihood the product of u_f(level); unobserved families contribute factor 1 to both. Posterior
   zeta = lambda*Lm / (lambda*Lm + (1-lambda)*Lu), computed in log space with a stable log-sum.
4. M-step lambda: (sum of count*zeta + prior_a - 1) / (total count + prior_a + prior_b - 2).
5. M-step m_f(level): sum over patterns where f is observed and equals level of count*zeta (+ pseudo-count - 1),
   divided by the sum over patterns where f is observed of count*zeta (+ pseudo-counts). u_f analogously with
   count*(1 - zeta) and no prior. Patterns where f is unobserved are excluded from both numerator and denominator.
6. Monotonicity: after each M-step order each family's m values so they do not decrease with agreement strength and
   u values so they do not increase (a simple re-sort; a pool-adjacent-violators projection is an acceptable
   alternative and preferable).
7. Stop when the maximum absolute change of all parameters is below tolerance or the cap is reached; warn if lambda
   falls below 1e-13 (too little overlap among candidates).
8. Convert to bits for HYDRA: bits_f(level) = log2(m_f/u_f); TF adjustment is still applied on top for exact-value
   genes (u from corpus frequency), the EM m only replaces the constant 0.5. Clip m into a configured band
   (e.g. 0.3-0.95) and report both fitted and used values.
9. Label-free error table at threshold t on posterior: expected true links among declared = sum of zeta over
   declared; expected false links = declared count - that sum; expected missed links = sum of zeta over undeclared
   candidates (fastLink additionally assumes almost no matches among never-compared records; HYDRA should report
   that assumption explicitly). Derive expected precision, recall, F1.
10. Optional one-to-one cleanup (only for duplicate-free sources): keep, per item, its maximum-posterior link.

## Parameters and thresholds
- Tolerance 1e-4; iteration cap; lambda prior (default none); m clip band (HYDRA choice).
- fastLink's own string-comparison cut points (Jaro-Winkler 0.94 agree, 0.88 partial; numeric 1 and 2.5) are for
  names/numbers and are NOT relevant to HYDRA genes; HYDRA levels come from its own Hamming/Jaccard/cosine rules.
- Error table threshold default 0.85 in the source; HYDRA should use its calibrated link probability.

## Outputs
- Per family: m and u per level, bits per level, number of pairs where the family was observed.
- lambda, iterations, converged flag, log-likelihood trace.
- Per pair posterior (labelled uncalibrated unless validated) and the expected confusion table.

## Edge cases
- A family observed in fewer than ~20 pairs: do not estimate, keep prior m.
- Patterns with every family unobserved: posterior equals lambda; exclude from the error table and flag.
- Strong dependence between families (VIS hashes vs DINOv2) breaks conditional independence: run EM on HYDRA's
  dependency-group level (one variable per group, max level), never per raw gene.
- Boilerplate-dominated data (the lab failure, B-cubed 0.17): EM latches onto template similarity as "match".
  Guard: stop-genes removed, kit/platform-tier genes excluded, and the fitted lambda must be plausible (compare with
  the deterministic-rule estimate); otherwise reject the fit.
- Label switching: enforce that the class with higher average agreement is called "match".

## What NOT to replicate
- R code structure, variable names, the address/gender special fields, the voter-file-specific mover priors,
  the name re-weighting by first-name frequency (HYDRA already has corpus TF), parallel chunking code.

## Suggested acceptance tests
1. Synthetic 5 families, known m/u, 30% of family values missing completely at random: fitted m within 0.05 of
   truth; same data with missing coded as disagreement gives m biased down by > 0.1 (negative control).
2. Monotonicity: fitted m never decreases with level on 100 random seeds.
3. Pattern collapse: results identical (1e-9) to running on the uncollapsed pair list.
4. Error table: on synthetic data with calibrated posteriors, expected false-link count within 2 sqrt(n) of the
   realised count in >= 90% of repeats.
5. Boilerplate trap: inject 40% pairs sharing only a kit-tier template; the guard rejects the fit or lambda stays
   below 2x the true value.

## Similarity-check reminder
Write the implementation from this spec only, in Python/NumPy, with HYDRA's own names. Do not open the fastLink R
sources while implementing. Before merging, diff identifiers and comments against the source to confirm no copying.
