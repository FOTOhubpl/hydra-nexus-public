# Clean-room spec: "similar cases" card from shared observables (behaviour studied from TheHive)

## Source studied
- Repository: https://github.com/TheHive-Project/TheHive, commit d390a03 (shallow clone, branch head as seen 2026-11-21;
  the repository is archived). Licence: AGPL-3.0.
- Files read (research side only): `services/ObservableSrv.scala`, `services/AlertSrv.scala`, `services/CaseSrv.scala`,
  `models/Observable.scala`.
- Read on 2026-11-21 by research agent c6_osint. Builders must not open the source.

## Purpose
Give the analyst a short, countable explanation of why an incident resembles other incidents: which exact
indicator values they share. It is an explanation card, not the link score.

## Inputs
- Per incident: a list of discrete gene values (kind, value, tier) already stored in HYDRA.
- Per value: an optional analyst "ignore for similarity" review flag (logged, reversible).

## Behaviour
1. Each distinct (kind, value) is one shared object; an incident references it.
2. For a target incident, find every other incident holding at least one of the same (kind, value) pairs.
3. Values flagged "ignore" are excluded on both sides, whichever side the flag was set on.
4. Group the results by the other incident. For each, report: shared values / total values of the target,
   shared operator-tier values / operator-tier values of the target, and the shared count per kind.
5. Order the list by shared operator-tier count, then shared total, then shared ratio. The source has no numeric
   score; this ordering is HYDRA's own choice.
6. Setting "ignore" is an analyst review event in the audit log; it moves the value to the boilerplate tier for
   scoring, and undoing it restores the previous tier.

## Parameters and thresholds
None in the source beyond exact equality. HYDRA keeps its existing tiers.

## Outputs
A list of rows (incident, campaign, shared/total, shared operator/total operator, per-kind counts, list of shared
values with evidence pointers), shown next to the bit waterfall.

## Edge cases
- A target with no discrete values shows an empty card with the reason "no discrete values observed".
- Redacted values match only by their salted fingerprint and are displayed redacted.
- Duplicate incidents (same sha256) are listed once, with the duplicate flag.

## What NOT to replicate
Graph-database queries, Scala class or field names, the organisation-sharing model and the permission model.

## Suggested acceptance tests
1. Two incidents sharing a phone and an IBAN show "2 shared".
2. Ignoring the phone shows 1 shared; undo restores 2; both events are in the audit log.
3. A value ignored only on the other incident is still excluded.
4. A target with 10 values of which 2 are shared shows 2/10.

## Similarity-check reminder
Before merge, run the token similarity check (as in `docs/cleanroom/WATCH_SIMILARITY.md`) of the new files against
the TheHive repository and record the result here.

## Similarity-check result (2026-11-21, builder REVIEW-CONF)
- Implementation: `hydra/similar_cases.py`, `hydra/api_review.py` (`GET /v1/incidents/{id}/similar`), written from
  this spec only; the builder did not open the TheHive sources.
- Reference: TheHive-Project/TheHive @d390a03, `git clone --depth 1` into `/tmp/simcheck-rc/repos` on the server
  `the Linux server`, 383 `.scala` files (read only by the script, which prints aggregates).
- Method (cross-language, Python vs Scala): comments and docstrings removed, identifier/number tokens
  lower-cased, punctuation dropped; per our file the longest common contiguous token run over all reference files
  and the count of shared token 8-grams.
- Measured: `similar_cases.py` 629 tokens, longest run 3, shared 8-grams 0; `api_review.py` 450 tokens, longest run
  3, shared 8-grams 0. No long identical token sequences.
- Not implemented from this spec: the analyst "ignore for similarity" flag (behaviour 3 and 6) and fingerprint matching of redacted values beyond the stored (already redacted) gene values.
