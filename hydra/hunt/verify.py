"""Hypothesis verification (INNOWACJE §3, §12).

`check(h, obs)` compares one later observation with the hypothesis:

| verdict | rule |
|---|---|
| `self` / `known` | derived from this hypothesis, or one of the members/evidence at issue: never confirming (rule 2) |
| `conflict` | hard conflict: an operator-id kind of the invariants present with ANOTHER value and none of the same
  kind matching (a different operator; rule 5: weak agreements cannot cancel it), or an analyst cannot-link |
| `supports` | every required evidence group has >= 1 matched invariant and no conflict |
| `unknown` | some groups matched, the missing groups were simply not observed (rule 3: missing = unknown) |
| `partial` | some groups matched, a missing group was observed with other values |
| `decoration_only` | only decoration / kit / brand matched: a brand alone never confirms (rule 4) |
| `unrelated` | nothing matched |

`resolve(h, checks, now, coverage_complete)`:
confirmed if a `supports` observation arrived with issued_at < available_at <= horizon_end; pending while
now < horizon_end; unverifiable if the source did not cover the window or any candidate stayed `unknown`;
otherwise refuted (within the named sources only).
"""

from __future__ import annotations

from typing import Any

from hydra.hunt.hypotheses import parse_ts
from hydra.hunt.records import Hypothesis, Observation, Status


def check(h: Hypothesis, obs: Observation, *, cannot_link: set[str] | None = None) -> dict[str, Any]:
    base = {"observation_id": obs.observation_id, "ref": obs.ref, "unit": obs.meta.get("unit") or obs.ref,
            "available_at": obs.available_at}
    if obs.derived_from_hypothesis == h.hypothesis_id:
        return {**base, "verdict": "self", "reason_pl": "Obserwacja wytworzona przez tę hipotezę - nie potwierdza jej."}
    known = set(h.evidence_ids_at_issue) | set(h.known_refs)
    if obs.ref in known or obs.meta.get("unit") in known:
        return {**base, "verdict": "known", "reason_pl": "Materiał znany przed wydaniem hipotezy."}
    if obs.status != "observed":
        return {**base, "verdict": "unknown", "reason_pl": "Źródło nie zwróciło danych - brak danych, nie dowód."}
    traits = [t for t in obs.traits if t.provenance != "HYP"]
    keys = {t.key for t in traits}
    inv = set(h.invariants)
    matched = sorted(keys & inv)
    groups = h.invariant_groups or {k: "operator" for k in inv}
    matched_groups = sorted({groups[k] for k in matched})
    missing = [g for g in h.required_evidence_groups if g not in matched_groups]
    observed_groups = {t.group for t in traits}
    conflicts: list[str] = []
    for kind in h.conflict_kinds:
        have = {t.value for t in traits if t.kind == kind}
        want = {k.split("=", 1)[1] for k in inv if k.split("=", 1)[0] == kind}
        if have and not (have & want):
            conflicts.append(f"different_operator_id:{kind}")
    if cannot_link and obs.ref in cannot_link:
        conflicts.append("cannot_link_with_member")
    out = {**base, "matched_invariants": matched, "matched_groups": matched_groups, "missing_groups": missing,
           "conflicts": conflicts}
    if conflicts:
        return {**out, "verdict": "conflict",
                "reason_pl": "Twarda sprzeczność: " + ", ".join(conflicts) + ". Zgodności dekoracji jej nie znoszą."}
    if matched and not missing:
        return {**out, "verdict": "supports", "reason_pl": "Zachowane niezmienniki ze wszystkich wymaganych grup: "
                + ", ".join(matched)}
    if matched:
        unseen = [g for g in missing if g not in observed_groups]
        if len(unseen) == len(missing):
            return {**out, "verdict": "unknown", "reason_pl": "Brakuje obserwacji z grup: " + ", ".join(missing)
                    + " - nieznane, nie przeciw. Wskazany następny dowód."}
        return {**out, "verdict": "partial", "reason_pl": "Część niezmienników zgodna, grupa "
                + ", ".join(missing) + " zaobserwowana z innymi wartościami."}
    deco = keys & set(h.decoration_keys)
    if deco:
        return {**out, "verdict": "decoration_only",
                "decoration": sorted(deco),
                "reason_pl": "Zgodna tylko dekoracja (" + ", ".join(sorted(deco)[:3])
                + ") - marka nie jest dowodem kampanii."}
    return {**out, "verdict": "unrelated", "reason_pl": "Brak zgodnych niezmienników."}


def in_window(h: Hypothesis, obs: Observation) -> bool:
    t = parse_ts(obs.available_at)
    return parse_ts(h.issued_at) < t <= parse_ts(h.horizon_end)


def resolve(h: Hypothesis, checks: list[dict[str, Any]], now: str, *, coverage_complete: bool) -> dict[str, Any]:
    win = [c for c in checks if parse_ts(h.issued_at) < parse_ts(c["available_at"]) <= parse_ts(h.horizon_end)]
    sup = sorted((c for c in win if c["verdict"] == "supports"), key=lambda c: c["available_at"])
    n = {v: sum(1 for c in win if c["verdict"] == v) for v in
         ("supports", "conflict", "unknown", "partial", "decoration_only", "unrelated", "self", "known")}
    if sup:
        status, why = Status.CONFIRMED, f"Potwierdzona przez {sup[0]['ref']} ({sup[0]['available_at']})."
    elif parse_ts(now) < parse_ts(h.horizon_end):
        status, why = Status.PENDING, "Horyzont jeszcze trwa."
    elif not coverage_complete:
        status, why = Status.UNVERIFIABLE, "Źródło nie pokrywało całego okna - brak danych, nie obalenie."
    elif n["unknown"]:
        status, why = Status.UNVERIFIABLE, f"{n['unknown']} kandydatów bez wymaganego dowodu - nierozstrzygnięte."
    else:
        status, why = Status.REFUTED, "Brak wariantu z zachowanymi niezmiennikami w oknie (tylko w nazwanych źródłach)."
    return {"hypothesis_id": h.hypothesis_id, "status": status.value, "reason_pl": why, "evaluated_at": now,
            "counts": n, "first_confirming": sup[0] if sup else None,
            "confirming": sorted({c.get("unit") or c["ref"] for c in sup}), "coverage_complete": coverage_complete}
