# Clean-room behaviour spec: actor-trace TF-IDF coordination (support-gated, top-quantile cut)

## Source studied
- URL: https://github.com/osome-iu/coordination-detection (paper: Pacheco et al., "Uncovering Coordinated Networks on
  Social Media", ICWSM 2021)
- Commit: a9ea318 (2024-12-20). No licence file (all rights reserved by default). Studied 2026-11-21 by research agent c3_coord.
- Files read: README, notebooks `CS1/Account_Handle_Sharing.ipynb`, `CS3/hashtag_sequence_coordination.ipynb`,
  `CS4/co_retweet.ipynb`, `CS5/activity_synchronization.ipynb`.

## Purpose
Find pairs of actors (in HYDRA: advertiser pages or incidents) that share an unusual set of traces (domains, phones,
image-cluster ids, time bins). Common traces are discounted automatically and low-activity actors are excluded.

## Inputs
- A table of (actor, trace, count). A trace can be any hashable value: a reposted item id, a time bin, a domain, an
  image-cluster id.
- Parameters: minimum support, minimum document frequency, neighbours per actor, edge quantile and/or absolute floor.

## Behaviour
1. Support = total trace count per actor. Drop actors whose support is at or below the minimum support.
2. Build one "document" per remaining actor that has more than one distinct trace, repeating each trace by its count.
3. Fit TF-IDF over the documents with sublinear term frequency (1 + log tf) and smoothed IDF. Ignore traces present in fewer
   than min_df actors (3). If that empties the vocabulary, retry with min_df 1.
4. L2-normalise the vectors and compute each actor's top-k most cosine-similar other actors (sparse top-k product, k = 100).
5. Emit each unordered pair once with its similarity and pair support = the smaller of the two actors' supports.
6. Keep pairs at or above the chosen quantile of all pair similarities (0.995 in the study). In HYDRA, also require an
   absolute floor, so random data does not always produce edges.
7. Build a graph from the kept pairs. Report components as candidate coordinated groups, each with its top shared traces
   (the traces that contribute most to the pair's dot product) as the explanation.
8. Time-sync variant: traces are time bins (30 minutes in the study) relative to the earliest record. Sequence variant:
   the trace is an actor's full ordered keyword/hashtag sequence (identical-sequence match), only for actors with at least
   5 items and at least 5 unique items.
9. Shared-identifier variant: an identifier (handle, phone) used by several actor ids makes them a clique, with edge weight =
   number of shared identifiers.

## Parameters and thresholds
- Minimum support: more than 10 actions (co-retweet study) or more than 8 (time-bin study). min_df 3. Top-k 100. Edge quantile 0.995.
  Time bin 1800 s. HYDRA additions: absolute cosine floor (start at 0.5), all values in config.

## Outputs
- Pair table (actor1, actor2, similarity, support), a graph file, and groups with explanation traces.

## Edge cases
- Very small corpora: quantile cuts become meaningless, so use the absolute floor.
- Hub traces (shorteners, platform domains): IDF shrinks them. Also remove them with the HYDRA platform/boilerplate lists.
- Actors with only one distinct trace never get edges (by design).
- Note that IDF here plays the same role as HYDRA's rarity bits, so in HYDRA the per-trace weight can be the existing
  rarity bits instead of IDF, with one shared rarity model.

## What NOT to replicate
- Twitter-specific JSON parsing, anonymised-id field names, notebook plotting, gzip-dump reading.

## Suggested acceptance tests
- Synthetic: 3 groups of 4 actors each sharing 5 private traces, plus 50 actors sharing one hub trace. The 3 groups are
  recovered and the hub creates no edges.
- Support gate: an actor with 2 actions never appears in the output.
- Every output group lists its top shared traces in descending contribution.

## Similarity-check reminder
Implement from this spec with your own identifiers. The source has no licence, so copy nothing. Run a similarity check
before merging.
