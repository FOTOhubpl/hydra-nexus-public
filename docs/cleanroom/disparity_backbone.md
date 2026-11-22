# Clean-room behaviour spec: disparity-filter backbone and overlapping-window network merge

## Source studied
- URL: https://github.com/lmannocci/CoordGalaxy
- Commit: 959fc70 (2026-07-14). No licence file. Studied 2026-11-21 by research agent c3_coord.
- Files read: `FilterGraphManager/FilterModels/BackboneNetwork/BackboneNetwork.py`, `configs/base.py`,
  `configs/russia1.py`, `SimilarityFunctionManager/SimilarityFunctionManager.py` (structure only), README.
- Public algorithm reference: M. A. Serrano, M. Boguna, A. Vespignani, "Extracting the multiscale backbone of complex
  weighted networks", PNAS 106(16), 2009.

## Purpose
Remove weak or incidental edges from a weighted similarity graph with a per-node significance test, not a single global
weight cut. Optionally build networks per overlapping time window and merge them. For HYDRA: cut single weak bridges
between dense groups inside oversized candidate campaigns, and prune advertiser-ring graphs.

## Inputs
- An undirected weighted graph (weights > 0; absolute value is used) and a significance threshold alpha_t.
- For the window merge: timestamped actions or edges, window length, slide, merge rule.

## Behaviour
1. For each node u with degree k > 1: s(u) = sum of incident weights. For each incident edge, p = w / s(u).
2. Edge significance from u's side: alpha_u = (1 - p)^(k - 1). This is the probability that a uniform random split of u's
   strength gives this edge a share >= p. Nodes with k = 1 give no test, so the edge counts as significant from that side.
3. Edge alpha = minimum of the two endpoint alphas. An edge is significant if it is significant for at least one endpoint.
   The studied code let the last visited endpoint overwrite the value (order dependent). Do not reproduce that.
4. Generic backbone: keep the edges with alpha < alpha_t. Report the removed edges and their alpha for the explanation view.
5. Directed variant: compute alpha_out from out-strength and out-degree, and alpha_in from in-strength and in-degree. Keep an edge by
   "or" (either side significant) or "and" (both sides significant).
6. Window merge (optional): split the timeline into windows of length L that start every S (S < L gives overlap). Build a
   similarity graph per window. Merge across windows by averaging the weight over the windows in which the pair appears
   (the studied default; sum or max are alternatives). Then apply steps 1-4.
7. HYDRA application (bridge guard, not a generic backbone, because HYDRA must keep chained campaigns): only inside
   components above the size guard, consider only graph bridges whose two endpoints both have degree >= 3. Remove such a
   bridge when its alpha > alpha_t. Recompute the components. Keep the removed edges as weak `shared_template`-style links so
   nothing is silently lost, and show "bridge removed: alpha 0.61" in the explanation.

## Parameters and thresholds
- The studied configs use alpha_t = 0.4 (lenient, applied as "keep below"). Literature values are 0.01-0.2. In HYDRA's bridge-guard
  form, start with "remove bridge if alpha > 0.3" and tune on SYNTHETIC data.
- The studied configs use a 7-day window with a 6-day slide (social media). For ads, use the delivery-date window learned in
  finding F1 of c3_coord.md.

## Outputs
- The pruned graph with `alpha` per edge, the list of removed edges with reason, and the components before and after.

## Edge cases
- Hubs with many equal weights: p = 1/k gives alpha = (1 - 1/k)^(k-1), about 0.37 for large k. A global alpha_t either keeps all
  or none of a hub's edges, so combine with HYDRA rarity bits rather than using the backbone alone.
- Chains: middle nodes of a path have degree 2 and p = 0.5, so alpha = 0.5. A generic backbone at alpha_t 0.05 would cut every
  chain. This is why step 7 limits removal to bridges between nodes of degree >= 3.
- Dyads (two degree-1 nodes): keep them (untestable on both sides). The studied code drops them implicitly.
- Zero or negative weights: drop them before the test.

## What NOT to replicate
- Numerical integration (the closed form is exact), LaTeX table generation, dataset-specific preprocessing classes,
  multiplex community-detection wrappers.

## Suggested acceptance tests
- Two 6-cliques with edge weight 20, joined by one edge of weight 10.5: endpoint degree 6, p ~ 0.095, alpha ~ 0.61.
  The bridge is removed and two campaigns remain.
- A path of 6 nodes with equal 12-bit edges stays one campaign (degree-2 nodes are never bridge-guard candidates).
- The result does not depend on node or edge insertion order.

## Similarity-check reminder
Implement from this spec and the PNAS paper only, with your own identifiers. The source has no licence. Run a
similarity check against the studied file before merging.
