"""Analyst queue by expected information gain (ADV-QUEUE, 2026-11-22). Display/order only, never scoring.

For each queue item (`hydra.review_queue`) a cheap counterfactual over the current link graph answers: how many
decisions would change if the analyst confirms or rejects it?

- reject a pair inside one campaign: the link edge goes away and, as `nexus.cluster` does for a cannot-link, the
  minimum weighted edge cut of the campaign subgraph separates the two incidents (analyst must-links uncuttable).
  Changed decisions = |side A| x |side B| pairwise co-membership decisions; incidents moved = the smaller side.
- confirm a pair across two campaigns (or with an unassigned incident): the campaigns merge. Changed decisions =
  |A| x |B|; incidents moved = the smaller campaign.
- confirm a pair inside one campaign, reject a pair across campaigns: nothing changes (0).
- an abstained assignment: confirming the best candidate adds the incident to it: 1 + |C| decisions (the
  incident's own membership and its pairs with the members); the second candidate likewise.
Open-set assignments to an affected campaign are counted too (each may be re-pointed by the revision).

Expected decision change (heuristic, not a probability model): E = p x changes(confirm) + (1 - p) x changes(reject),
where p = 1 / (1 + 2^(-(evidence - link) / P_SCALE_BITS)) is an uncalibrated chance that the analyst confirms,
pulled halfway to 0.5 for a pair with a contradiction or a link that rests on one family. Evidence = the pair's
bits, plus for campaign mates the minimum-cut bits of every other path (what a cannot-link would have to cut;
without it the first version was worse than `ev`, docs/eval/QUEUE_CURVE.md).
Effort (assumed analyst cost units): 1 for a
pair, +0.5 when a contradiction must be read, 1.5 for an assignment abstain (three-column card). The queue is ranked
by E / effort, ties by the older `ev` heuristic.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any

import networkx as nx

from hydra.labels_pl import num_pl

METHOD = "queue_info_gain@1"
METHOD_PL = ("Kolejność = oczekiwana liczba zmienionych decyzji na jednostkę wysiłku analityka (heurystyka):"
             " dla każdej pozycji liczymy kontrfaktycznie na grafie powiązań, ile decyzji o parach incydentów zmieni"
             " potwierdzenie, a ile odrzucenie (rozdzielenie kampanii najmniejszym cięciem, połączenie dwóch kampanii),"
             " i ważymy to nieskalibrowaną szansą potwierdzenia z bitów. To nie jest prawdopodobieństwo.")
MUST_CAPACITY = 1e7
MAX_EXACT_CAMPAIGN = 400  # larger campaigns: split size from the stored bridge or 1 (cost bound)
P_SCALE_BITS = 1.0  # bits read as log2 odds around the link threshold (chosen on dev seeds 900-905, QUEUE_CURVE.md)
EFFORT_PAIR, EFFORT_CONTRA, EFFORT_ASSIGN = 1.0, 0.5, 1.5
ORDERS = ("gain", "ev")


def p_confirm(distance_bits: float | None, shaky: bool) -> float:
    if distance_bits is None:
        return 0.5
    p = 1.0 / (1.0 + 2.0 ** (-float(distance_bits) / P_SCALE_BITS))
    return 0.5 + (p - 0.5) / 2.0 if shaky else p


class LinkGraph:
    """Current link graph of a run: same-campaign pair edges + analyst must-links, restricted to campaign mates."""

    def __init__(self, conn: sqlite3.Connection, run: str, member_of: dict[str, str]) -> None:
        params = {}
        row = conn.execute("SELECT params FROM runs WHERE id=?", (run,)).fetchone()
        if row is not None and row["params"]:
            params = json.loads(row["params"]) or {}
        cons = params.get("constraints") or []
        cannot = {tuple(sorted((c["a"], c["b"]))) for c in cons if c.get("kind") == "cannot"}
        self.member_of = member_of
        self.members: dict[str, list[str]] = defaultdict(list)
        for inc, cid in member_of.items():
            self.members[cid].append(inc)
        self.g = nx.Graph()
        for r in conn.execute("SELECT a, b, total_bits FROM pairs WHERE run_id=? AND hypothesis='same_campaign'",
                              (run,)):
            a, b = r["a"], r["b"]
            if (a, b) in cannot or member_of.get(a) is None or member_of.get(a) != member_of.get(b):
                continue
            self.g.add_edge(a, b, capacity=max(0.1, float(r["total_bits"] or 0.0)))
        for c in cons:
            a, b = c.get("a"), c.get("b")
            if c.get("kind") == "must" and a != b and member_of.get(a) and member_of.get(a) == member_of.get(b):
                self.g.add_edge(a, b, capacity=MUST_CAPACITY, must=True)
        self._cache: dict[tuple[str, str], tuple[int, int, bool, float]] = {}

    def size(self, inc: str | None) -> int:
        cid = self.member_of.get(inc or "")
        return len(self.members[cid]) if cid else 1

    def split(self, a: str, b: str, bridge: dict | None) -> tuple[int, int, bool, float]:
        """(side of a, side of b, blocked, other_bits) after a cannot-link between campaign mates a and b;
        `other_bits` = link bits of the minimum cut once the direct edge is gone (evidence through other paths)."""
        key = (a, b) if a < b else (b, a)
        if key in self._cache:
            return self._cache[key]
        cid = self.member_of[a]
        ms = self.members[cid]
        if len(ms) > MAX_EXACT_CAMPAIGN:
            sizes = (bridge or {}).get("split_sizes") or [1, len(ms) - 1]
            out = (int(sizes[0]), int(sizes[1]), False, 0.0)
        else:
            sub = self.g.subgraph(ms).copy()
            sub.add_nodes_from(ms)
            if sub.has_edge(a, b) and sub.edges[a, b].get("must"):
                out = (len(ms), 0, True, MUST_CAPACITY)
            else:
                if sub.has_edge(a, b):
                    sub.remove_edge(a, b)
                if not nx.has_path(sub, a, b):
                    ca = nx.node_connected_component(sub, a)
                    out = (len(ca), len(ms) - len(ca), False, 0.0)
                else:
                    comp = sub.subgraph(nx.node_connected_component(sub, a))
                    cut, (side_a, side_b) = nx.minimum_cut(comp, a, b, capacity="capacity")
                    if cut >= MUST_CAPACITY:
                        out = (len(ms), 0, True, float(cut))
                    else:  # nodes outside a's component stay with the larger part: count them on b's side
                        out = (len(side_a), len(ms) - len(side_a), False, float(cut))
        self._cache[key] = out
        return out


def _assigned_to(assignment_rows: list[Any]) -> dict[str, int]:
    n: dict[str, int] = defaultdict(int)
    for r in assignment_rows:
        cid = r["campaign_id"] if "campaign_id" in r.keys() else None
        if r["decision"] == "assign" and cid:
            n[cid] += 1
    return n


def annotate(conn: sqlite3.Connection, run: str, items: list[dict[str, Any]], assignment_rows: list[Any],
             member_of: dict[str, str], bridges: dict[tuple[str, str], dict]) -> None:
    """Adds `gain` (expected changed decisions), `effort`, `gain_per_effort`, `p_confirm` and `counterfactual`."""
    lg = LinkGraph(conn, run, member_of)
    assigned = _assigned_to(assignment_rows)
    for it in items:
        if it.get("incident_id"):
            _annotate_assignment(it, lg, assigned)
        else:
            _annotate_pair(it, lg, assigned, bridges)


def _annotate_pair(it: dict, lg: LinkGraph, assigned: dict[str, int], bridges: dict) -> None:
    a, b = it["a"], it["b"]
    ca, cb = lg.member_of.get(a), lg.member_of.get(b)
    shaky = "conflict" in it["kinds"] or it.get("robust_link") is False
    p = p_confirm(it.get("distance_bits"), shaky)
    if ca is not None and ca == cb:
        sa, sb, blocked, other = lg.split(a, b, bridges.get((a, b) if a < b else (b, a)))
        # campaign mates: the evidence that they belong together is the direct pair plus every other path (the
        # bits a cannot-link would have to cut), not the direct pair alone
        if other > 0 and it.get("distance_bits") is not None:
            p = p_confirm(min(float(it["distance_bits"]) + other, 64.0), shaky)
        it["other_path_bits"] = round(min(other, 1e6), 3)
        conf = {"decisions": 0, "incidents": 0, "campaigns": [], "effect": "none"}
        rej = {"decisions": sa * sb, "incidents": min(sa, sb), "campaigns": [ca], "sides": [sa, sb],
               "effect": "blocked" if blocked else "split", "assignments": assigned.get(ca, 0) if not blocked else 0}
    else:
        na, nb = lg.size(a), lg.size(b)
        cs = sorted(c for c in (ca, cb) if c)
        conf = {"decisions": na * nb, "incidents": min(na, nb), "campaigns": cs, "sizes": [na, nb], "effect": "merge",
                "assignments": sum(assigned.get(c, 0) for c in cs)}
        rej = {"decisions": 0, "incidents": 0, "campaigns": [], "effect": "none"}
    _finish(it, p, conf, rej, EFFORT_PAIR + (EFFORT_CONTRA if "conflict" in it["kinds"] else 0.0))


def _annotate_assignment(it: dict, lg: LinkGraph, assigned: dict[str, int]) -> None:
    cands = it.get("candidates") or []
    sizes = [len(lg.members.get(c.get("campaign_id") or "", [])) or 1 for c in cands[:2]]
    p = p_confirm(it.get("distance_bits"), "bridge" in it["kinds"])
    first = 1 + sizes[0] if sizes else 1
    second = 1 + sizes[1] if len(sizes) > 1 else 0
    conf = {"decisions": first, "incidents": 1, "campaigns": [c.get("campaign_id") for c in cands[:1]],
            "effect": "assign"}
    rej = {"decisions": second, "incidents": 1 if second else 0,
           "campaigns": [c.get("campaign_id") for c in cands[1:2]], "effect": "assign_other" if second else "none"}
    _finish(it, p, conf, rej, EFFORT_ASSIGN)


def _finish(it: dict, p: float, conf: dict, rej: dict, effort: float) -> None:
    gain = (p * (conf["decisions"] + conf.get("assignments", 0))
            + (1 - p) * (rej["decisions"] + rej.get("assignments", 0)))
    it["p_confirm"] = round(p, 4)
    it["gain"] = round(gain, 4)
    it["effort"] = effort
    it["gain_per_effort"] = round(gain / effort, 4)
    it["counterfactual"] = {"confirm": conf, "reject": rej}


def _effect_pl(cf: dict, verb: str) -> str:
    e = cf["effect"]
    if e == "split":
        sa, sb = cf["sides"]
        s = (f"{verb} rozdzieli kampanię {cf['campaigns'][0]} na {sa} i {sb} incydentów"
             f" ({cf['decisions']} decyzji o parach)")
        return s + (f", dotyczy też {cf['assignments']} przypisań" if cf.get("assignments") else "")
    if e == "merge":
        na, nb = cf["sizes"]
        what = " i ".join(cf["campaigns"]) if len(cf["campaigns"]) == 2 else "incydent z kampanią"
        s = f"{verb} połączy {what} ({na} + {nb} incydentów, {cf['decisions']} decyzji o parach)"
        return s + (f", dotyczy też {cf['assignments']} przypisań" if cf.get("assignments") else "")
    if e == "assign":
        return f"{verb} dołączy incydent do kampanii {cf['campaigns'][0]} ({cf['decisions']} decyzji)"
    if e == "assign_other":
        return f"{verb} wskaże drugą kampanię {cf['campaigns'][0]} ({cf['decisions']} decyzji)"
    if e == "blocked":
        return f"{verb} nie rozdzieli kampanii bez naruszenia wcześniejszej decyzji analityka"
    return f"{verb} niczego nie zmieni w podziale"


def why_first(items: list[dict[str, Any]], order: str) -> None:
    """`why_first_pl`: one Polish sentence per item explaining its rank (first item compared with the second)."""
    for i, it in enumerate(items):
        cf = it.get("counterfactual") or {}
        if not cf:
            continue
        parts = [_effect_pl(cf["confirm"], "potwierdzenie"), _effect_pl(cf["reject"], "odrzucenie")]
        head = (f"Szansa potwierdzenia z bitów ≈ {num_pl(it['p_confirm'], '.2f')} (nieskalibrowana); oczekiwana zmiana"
                f" {num_pl(it['gain'], '.2f')} decyzji przy wysiłku {num_pl(it['effort'], '.1f')}"
                f" → {num_pl(it['gain_per_effort'], '.2f')} na jednostkę")
        if order == "gain":
            if i == 0:
                nxt = items[1]["gain_per_effort"] if len(items) > 1 and "gain_per_effort" in items[1] else None
                rank = ("najwyższa w kolejce" + (f" (następna pozycja: {num_pl(nxt, '.2f')})" if nxt is not None
                                                  else ""))
            else:
                rank = f"pozycja {i + 1}, pierwsza ma {num_pl(items[0].get('gain_per_effort', 0.0), '.2f')}"
        else:
            rank = f"kolejność wg wartości decyzji (heurystyka niepewność × wpływ), pozycja {i + 1}"
        it["why_first_pl"] = f"{parts[0][0].upper()}{parts[0][1:]}; {parts[1]}. {head}, {rank}."


def sort_key(order: str):
    from hydra.review_queue import KIND_ORDER

    if order == "gain":
        return lambda it: (-it.get("gain_per_effort", 0.0), -it["ev"], KIND_ORDER.index(it["kinds"][0]), it["key"])
    return lambda it: (-it["ev"], KIND_ORDER.index(it["kinds"][0]), it["key"])


__all__ = ["METHOD", "METHOD_PL", "ORDERS", "LinkGraph", "annotate", "p_confirm", "sort_key", "why_first"]
