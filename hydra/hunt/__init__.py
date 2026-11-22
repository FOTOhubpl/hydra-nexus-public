"""HUNT: verifiable predictive layer (INNOWACJE 2026-11-21 §3-§5, §11-§16).

- `signatures`  campaign signature from APPROVED members of a published revision (stable rare genes vs decoration)
- `hypotheses`  Hypothesis / Observation records and the hash-chained, append-only ledger
- `replay`      offline temporal replay: a knowledge view that refuses data with available_at > cutoff
- `verify`      observation-vs-hypothesis check and the final status (pending / confirmed / refuted / unverifiable)
- `actions`     the finite list of allowed evidence actions over EXISTING artefacts
- `planner`     categorical next-evidence ranking (no fake VOI) and the hunt plan
- `metrics`     precision@K, confirmed per 100 queries, lead time, unresolved bounds vs a same-budget baseline

Nothing here opens a network connection. Rankings are heuristic priorities, never probabilities.
"""

HUNT_VERSION = "hunt@1"
