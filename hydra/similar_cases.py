"""Similar cases card: top-k earlier incidents that share rare discrete evidence with a target incident.

Independent implementation from the behaviour spec `docs/cleanroom/thehive_similar_cases.md` (behaviour studied
from TheHive, AGPL-3.0, by a research agent; no TheHive code was read or used by the builder).

It is an explanation card next to the bit waterfall, not a link score and not a decision:
1. Target values = observed discrete genes of the incident (no vectors), without genes that never count
   (`synapse.level == "none"`: boilerplate, labels, app UI) and without stop values (`entropy.is_stop`).
2. Every other incident in scope (members of the run, else all incidents) holding the same (kind, value) shares it.
3. A value is *rare* when at most `max_df` incidents in scope hold it (default max(3, 5 % of the scope)); common
   shared values are counted in `excluded_common` and never listed.
4. Rarity of a value = log2(N / df) bits (self-information among the N incidents in scope; heuristic, not a
   probability). `rare_bits` of a row sums the rarest value per dependency group, so correlated views of one
   observation (domain and host of the same URL) are not counted twice.
5. Rows are ordered by shared operator-tier values, then rare bits, then shared count, then shared ratio.
6. Two incidents sharing the same original file (`artifact_sha256`) are flagged as duplicates.
"""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from typing import Any

from hydra.entropy import is_stop
from hydra.labels_pl import num_pl
from hydra.synapse import SUM_FAMILIES, _short, gene_tier, kind_label_pl, level

METHOD = "similar_cases@1"
METHOD_PL = ("Wspólne rzadkie wartości dyskretne (dokładna równość: telefon, IBAN, domena, identyfikator"
             " śledzący, plik…). Rzadkość = log2(N / liczba incydentów z wartością), heurystyka; to karta"
             " wyjaśniająca, nie wynik powiązania.")


def _is_operator(g: Any) -> bool:
    return gene_tier(g) == "operator" or (level(g) == "campaign" and str(g.family) in SUM_FAMILIES)


def similar_cases(conn: sqlite3.Connection, incident_id: str, scope: set[str] | None = None,
                  campaign_of: dict[str, str] | None = None, k: int = 10, max_df: int | None = None
                  ) -> dict[str, Any]:
    from hydra.pipeline import _row_gene

    campaign_of = campaign_of or {}
    if not scope:
        scope = {r[0] for r in conn.execute("SELECT id FROM incidents")}
    scope = set(scope) | {incident_id}
    n = len(scope)
    max_df = max_df if max_df is not None else max(3, math.ceil(0.05 * n))
    rows = conn.execute("SELECT * FROM genes WHERE incident_id=? AND status='observed' AND value IS NOT NULL"
                        " AND vector_ref IS NULL", (incident_id,)).fetchall()
    target: dict[tuple[str, str], Any] = {}
    for r in rows:
        g = _row_gene(r)
        if level(g) == "none" or is_stop(g.kind, g.value):
            continue
        target.setdefault((g.kind, str(g.value)), g)
    total = len(target)
    total_op = sum(1 for g in target.values() if _is_operator(g))
    out: dict[str, Any] = {"incident_id": incident_id, "method": METHOD, "method_pl": METHOD_PL, "scope": n,
                           "max_df": max_df, "total_values": total, "operator_values": total_op,
                           "excluded_common": 0, "calibrated": False, "items": [], "empty_reason_pl": None}
    if not target:
        out["empty_reason_pl"] = "Brak zaobserwowanych wartości dyskretnych w tym incydencie."
        return out
    shared: dict[str, list[dict[str, Any]]] = defaultdict(list)
    common = 0
    for (kind, value), g in target.items():
        holders = {h[0] for h in conn.execute(
            "SELECT DISTINCT incident_id FROM genes WHERE kind=? AND value=? AND status='observed'", (kind, value))
                   } & scope
        others = holders - {incident_id}
        if not others:
            continue
        df = len(holders)
        if df > max_df:
            common += 1
            continue
        bits = round(math.log2(n / df), 3)
        entry = {"kind": kind, "family": str(g.family), "value": value, "df": df, "bits": bits,
                 "tier": gene_tier(g) or level(g), "operator": _is_operator(g),
                 "group": g.dependency_group or f"{g.family}:{kind}:{value}",
                 "evidence": g.evidence.model_dump(exclude_none=True),
                 "explanation_pl": f"{kind_label_pl(kind)}: „{_short(value)}” — w {df} z {n} incydentów"
                                   f" ({num_pl(bits)} bit rzadkości)"}
        for o in others:
            shared[o].append(entry)
    out["excluded_common"] = common
    titles = {r["id"]: r["title"] for r in conn.execute(
        f"SELECT id, title FROM incidents WHERE id IN ({','.join('?' * len(shared)) or 'NULL'})", list(shared))}
    items = []
    for other, vals in shared.items():
        per_group: dict[str, float] = {}
        per_kind: dict[str, int] = defaultdict(int)
        for v in vals:
            per_group[v["group"]] = max(per_group.get(v["group"], 0.0), v["bits"])
            per_kind[v["kind"]] += 1
        vals.sort(key=lambda v: (not v["operator"], -v["bits"], v["kind"], v["value"]))
        n_op = sum(1 for v in vals if v["operator"])
        items.append({
            "incident_id": other, "campaign_id": campaign_of.get(other), "title": titles.get(other),
            "same_campaign": campaign_of.get(other) is not None and campaign_of.get(other) == campaign_of.get(
                incident_id),
            "shared": len(vals), "total": total, "shared_operator": n_op, "total_operator": total_op,
            "ratio": round(len(vals) / total, 4), "rare_bits": round(sum(per_group.values()), 3),
            "per_kind": dict(sorted(per_kind.items())), "duplicate": any(v["kind"] == "artifact_sha256" for v in vals),
            "values": [{k: v[k] for k in ("kind", "family", "value", "df", "bits", "tier", "operator", "evidence",
                                          "explanation_pl")} for v in vals[:20]],
            "reasons_pl": [v["explanation_pl"] for v in vals[:5]]})
    items.sort(key=lambda r: (-r["shared_operator"], -r["rare_bits"], -r["shared"], -r["ratio"], r["incident_id"]))
    out["items"] = items[:k]
    if not items:
        out["empty_reason_pl"] = ("Żaden inny incydent nie dzieli z tym rzadkiej wartości dyskretnej"
                                  + (f" ({common} wspólnych wartości jest pospolitych)." if common else "."))
    return out


__all__ = ["METHOD", "similar_cases"]
