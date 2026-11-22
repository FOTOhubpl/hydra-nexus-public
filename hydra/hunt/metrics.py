"""HUNT metrics (INNOWACJE §14). Same query budget and the same cutoff for every strategy.

- precision@K = confirmed / K. Unresolved hypotheses stay in the denominator: `lower` counts them as misses,
  `upper` as hits; `point` is reported only when nothing is unresolved.
- confirmed new items per 100 queries = 100 x distinct new confirming items / queries spent (one query = one
  hypothesis watched in one source over its horizon).
- lead time vs a NAMED reference source: hours from the hypothesis issue to the first confirming item becoming
  available in that source. It is "registered before the reference listed the variant", not "before the first
  victim" and not "first in the world".
"""

from __future__ import annotations

from statistics import median
from typing import Any

from hydra.hunt.hypotheses import parse_ts


def summarise(results: list[dict[str, Any]], *, k: int, reference_source: str) -> dict[str, Any]:
    """`results`: one dict per issued hypothesis: {"hypothesis": {...}, "result": resolve(...)}."""
    st = [r["result"]["status"] for r in results]
    n = {s: st.count(s) for s in ("confirmed", "refuted", "unverifiable", "pending")}
    unresolved = n["unverifiable"] + n["pending"]
    kk = max(k, 1)
    items: set[str] = set()
    leads: list[float] = []
    for r in results:
        res = r["result"]
        items.update(res.get("confirming") or [])
        if res.get("first_confirming"):
            leads.append((parse_ts(res["first_confirming"]["available_at"]) -
                          parse_ts(r["hypothesis"]["issued_at"])).total_seconds() / 3600)
    queries = len(results)
    return {
        "issued": len(results), "budget_k": k, **n, "unresolved": unresolved,
        "precision_at_k": {"lower": round(n["confirmed"] / kk, 3),
                           "upper": round((n["confirmed"] + unresolved) / kk, 3),
                           "point": round(n["confirmed"] / kk, 3) if unresolved == 0 else None},
        "confirmed_new_items": len(items),
        "confirmed_per_100_queries": round(100 * len(items) / queries, 1) if queries else None,
        "lead_time_h": {"reference_source": reference_source, "n": len(leads),
                        "median": round(median(leads), 2) if leads else None,
                        "min": round(min(leads), 2) if leads else None,
                        "max": round(max(leads), 2) if leads else None},
        "score_kind": "counts over a frozen replay; not probabilities",
    }


def candidate_precision(flagged: list[str], truth: set[str]) -> dict[str, Any]:
    """precision@K for a ranked candidate list (SYNTHETIC: truth = generator labels)."""
    hits = sum(1 for x in flagged if x in truth)
    return {"k": len(flagged), "hits": hits, "precision_at_k": round(hits / len(flagged), 3) if flagged else None,
            "recall": round(hits / len(truth), 3) if truth else None,
            "confirmed_per_100_queries": round(100 * hits / len(flagged), 1) if flagged else None}
