"""Next-evidence ranking (INNOWACJE §5): categorical values and one explicit rule. No VOI estimate: the
outcome distributions it would need are not known.

    priority = resolving_power x availability / cost

- resolving_power: high = 3 when the action's source is where the hypothesis's invariants of a MISSING required
  group (or a hard-conflict kind) came from in the approved members; medium = 2 when that source carried other
  invariants; low = 1 otherwise; `ask_analyst_pair` is high (it decides the pair) but costs high.
- availability: 1 when the candidate has an unread artefact of that kind (or the cache holds it), else 0.
- cost: low = 1, medium = 2, high = 3.
Ties keep the catalogue order. Nothing is executed when the current verdict is already decisive (supports /
conflict) unless asked explicitly.
"""

from __future__ import annotations

from typing import Any

from hydra.hunt import actions as A
from hydra.hunt.records import Hypothesis, Observation
from hydra.hunt.verify import check

POWER = {"high": 3, "medium": 2, "low": 1}
DECISIVE = {"supports", "conflict", "self", "known"}


def rank(h: Hypothesis, obs: Observation, verdict: dict[str, Any]) -> list[dict[str, Any]]:
    missing = set(verdict.get("missing_groups") or [])
    srcs_missing = {h.invariant_sources.get(k) for k, g in h.invariant_groups.items() if g in missing}
    conflict_srcs = {h.invariant_sources.get(k) for k in h.invariants if k.split("=", 1)[0] in h.conflict_kinds}
    srcs_any = set(h.invariant_sources.values())
    unread = A.unread_sources(obs)
    out = []
    for a in A.ACTIONS:
        if a.id == "ask_analyst_pair":
            power, avail, why = "high", 1, "analityk rozstrzyga parę bezpośrednio"
        else:
            if a.source in srcs_missing:
                power, why = "high", "z tego źródła pochodziły niezmienniki brakującej grupy"
            elif a.source in conflict_srcs:
                power, why = "high", "może ujawnić identyfikator operatora (twarda sprzeczność)"
            elif a.source in srcs_any:
                power, why = "medium", "źródło innych niezmienników"
            else:
                power, why = "low", "źródło tylko dekoracji lub tła"
            avail = 1 if a.source in unread else 0
            if not avail:
                why += "; artefakt niedostępny lub już odczytany"
        score = POWER[power] * avail / A.COST[a.cost]
        out.append({"action": a.id, "label_pl": a.label_pl, "source": a.source, "resolving_power": power,
                    "availability": "available" if avail else "unavailable", "cost": a.cost,
                    "priority": round(score, 3), "rule": "priority = power x availability / cost",
                    "why_pl": why})
    order = {a.id: i for i, a in enumerate(A.ACTIONS)}
    return sorted(out, key=lambda r: (-r["priority"], order[r["action"]]))


def next_action(h: Hypothesis, obs: Observation, *, execute: bool = True, force: bool = False,
                cannot_link: set[str] | None = None) -> dict[str, Any]:
    """Rank actions, execute the top one, re-check and report whether the decision changed."""
    before = check(h, obs, cannot_link=cannot_link)
    ranking = rank(h, obs, before)
    out: dict[str, Any] = {"hypothesis_id": h.hypothesis_id, "observation_id": obs.observation_id, "ref": obs.ref,
                           "before": before, "ranking": ranking, "executed": None, "after": None,
                           "decision_changed": False, "observation": obs}
    if not execute or (before["verdict"] in DECISIVE and not force):
        out["note_pl"] = "Decyzja już rozstrzygnięta - nie wykonano czynności." if execute else "Tylko ranking."
        return out
    top = next((r for r in ranking if r["priority"] > 0), None)
    if top is None:
        out["note_pl"] = "Brak dostępnej czynności - przypadek pozostaje nierozstrzygnięty."
        return out
    new_obs, revealed = A.execute(A.BY_ID[top["action"]], obs)
    after = check(h, new_obs, cannot_link=cannot_link)
    out.update(executed={**top, "revealed": revealed}, after=after, observation=new_obs,
               decision_changed=after["verdict"] != before["verdict"])
    if top["action"] == "ask_analyst_pair":
        out["note_pl"] = "Utworzono pytanie do analityka; odpowiedź wraca jako review, nie jako gen."
    return out
