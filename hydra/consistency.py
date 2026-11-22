"""M6 consistency gate (spec 3.0, "Algorytmy" §6) and the per-campaign consistency report (ADV-CAMP).

Spec: a merge of clusters A and B passes only when
1. at least 2 distinct pairs between A and B are `same_campaign`, or one pair has S >= 2 x T_link;
2. the median S over (a sample of up to 5 x 5) pairs between A and B is >= T_low;
3. no pair between A and B has a hard contradiction (veto).
This blocks chains A~B~C where A and C have nothing in common.

Implementation
- S of a pair = `PairEval.support` (campaign-level support bits, contradiction bits included): the number the link
  threshold is compared with. Only scored pairs count; an unscored pair is unknown and is left out of the median
  (never a 0). The median runs over every scored pair between the two clusters, not a 5 x 5 sample (deterministic).
- Two singletons joined by one `same_campaign` link pass condition 1: a single possible pair is the pair decision
  itself (the same rule `campaign_support` applies to two-member campaigns). Analyst must-links merge without a gate.
- Hard contradiction = a contradiction reason with |bits| >= HARD_CONTRADICTION_BITS (today only the scheme mismatch,
  -4 bits).
- `gate_split` replays the merges of one connected component in descending link support (ties: ids), repeated until
  no merge passes. Then the member rule (ADV brief): inside a part of >= 3 members every member needs >= 2 link
  pairs to the rest of its part or one link of >= 2 x T_link (a strong single bridge, reported with its reason);
  the weakest failing member is detached until the rule holds. The resulting parts replace the component.
  Components of <= 2 members are not touched.

Flags
- `HYDRA_ADV_GATE=1` turns the splitting on inside `nexus.cluster` (default off: partitions unchanged).
- `consistency_report` is read-only telemetry (API `GET /v1/campaigns/{id}/consistency`): computed on demand from the
  stored run, it reports cohesion, the weakest link, members held by a single pair, strong single bridges with their
  reason, contradiction pairs and what the gate would do, whether or not the flag is on. Bits are not probabilities.
"""

from __future__ import annotations

import os
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import networkx as nx

METHOD = "consistency_gate@1"
HARD_CONTRADICTION_BITS = 4.0
STRONG_FACTOR = 2.0
SAME = "same_campaign"


def gate_enabled() -> bool:
    return os.environ.get("HYDRA_ADV_GATE", "0") == "1"


@dataclass
class PairView:
    """What the gate needs of one scored pair (from a live `PairEval` or a stored `pairs.detail`)."""

    support: float
    linked: bool
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    reasons: list[dict[str, Any]] = field(default_factory=list)
    campaign_by_family: dict[str, float] = field(default_factory=dict)
    robustness: dict | None = None

    @property
    def hard(self) -> list[dict[str, Any]]:
        return [c for c in self.contradictions if abs(float(c.get("bits") or 0.0)) >= HARD_CONTRADICTION_BITS]


def view_of(pe: Any) -> PairView:
    """PairView of a live PairEval or a stored pair detail dict (`pipeline._write_run`)."""
    if isinstance(pe, PairView):
        return pe
    if isinstance(pe, Mapping):
        return PairView(support=float(pe.get("support") or 0.0), linked=str(pe.get("hypothesis")) == SAME,
                        contradictions=list(pe.get("contradictions") or []), reasons=list(pe.get("reasons") or []),
                        campaign_by_family=dict(pe.get("campaign_by_family") or {}), robustness=pe.get("robustness"))
    s = pe.score
    return PairView(support=float(pe.support), linked=str(getattr(s.hypothesis, "value", s.hypothesis)) == SAME,
                    contradictions=[c.model_dump(mode="json") for c in s.contradictions],
                    reasons=[r.model_dump(mode="json") for r in s.reasons],
                    campaign_by_family=dict(pe.campaign_by_family or {}), robustness=pe.robustness)


def _key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _between(xs: Iterable[str], ys: Iterable[str], pairs: Mapping[tuple[str, str], PairView]) -> list[PairView]:
    out = []
    ys = list(ys)
    for x in xs:
        for y in ys:
            p = pairs.get(_key(x, y))
            if p is not None:
                out.append(p)
    return out


def merge_check(a: set[str], b: set[str], pairs: Mapping[tuple[str, str], PairView], link: float, low: float,
                links: set[tuple[str, str]] | None = None) -> dict[str, Any]:
    """The three spec conditions for merging cluster `a` with cluster `b`. `links`: the link edges (same-campaign
    pairs of the NEXUS graph); default = scored pairs with hypothesis same_campaign."""
    between = []
    supporting = 0
    strong = 0.0
    for x in a:
        for y in b:
            k = _key(x, y)
            p = pairs.get(k)
            if p is None:
                continue
            between.append(p)
            if (k in links) if links is not None else p.linked:
                supporting += 1
                strong = max(strong, p.support)
    single = len(a) == 1 and len(b) == 1
    c1 = supporting >= 2 or (supporting >= 1 and (single or strong >= STRONG_FACTOR * link))
    med = statistics.median(p.support for p in between) if between else None
    c2 = med is not None and med >= low
    hard = sum(1 for p in between if p.hard)
    c3 = hard == 0
    return {"pass": bool(c1 and c2 and c3), "supporting_pairs": supporting, "strongest_bits": round(strong, 3),
            "scored_pairs": len(between), "median_bits": round(med, 3) if med is not None else None,
            "contradictions": hard, "c1": c1, "c2": c2, "c3": c3}


def gate_split(nodes: Iterable[str], edges: Iterable[tuple[str, str, float, bool]],
               pairs: Mapping[tuple[str, str], PairView], link: float, low: float) -> list[set[str]]:
    """Parts of one component after replaying its merges through the gate. `edges`: (u, v, support bits, must)."""
    ns = sorted(set(nodes))
    parent = {n: n for n in ns}
    members = {n: {n} for n in ns}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx == ry:
            return
        if (len(members[rx]), ry) < (len(members[ry]), rx):
            rx, ry = ry, rx
        parent[ry] = rx
        members[rx] |= members.pop(ry)

    es = [(min(u, v), max(u, v), float(w), bool(m)) for u, v, w, m in edges]
    links = {(u, v) for u, v, _w, m in es if not m}
    for u, v, _w, m in sorted(es):
        if m:
            union(u, v)
    order = sorted((e for e in es if not e[3]), key=lambda e: (-e[2], e[0], e[1]))
    changed = True
    while changed:
        changed = False
        for u, v, _w, _m in order:
            ru, rv = find(u), find(v)
            if ru == rv:
                continue
            if merge_check(members[ru], members[rv], pairs, link, low, links)["pass"]:
                union(ru, rv)
                changed = True
    musts = {(u, v) for u, v, _w, m in es if m}
    bits = {(u, v): w for u, v, w, m in es if not m}
    out: list[set[str]] = []
    for part in members.values():
        out.extend(_prune(set(part), links, musts, bits, link))
    return sorted(out, key=lambda s: (-len(s), sorted(s)[0]))


def _prune(part: set[str], links: set[tuple[str, str]], musts: set[tuple[str, str]], bits: dict[tuple[str, str], float],
           link: float) -> list[set[str]]:
    """Member rule inside a part of >= 3 members: each member needs >= 2 link pairs to the rest of its part or one
    of >= 2 x T_link (analyst must-links exempt). The weakest failing member (lowest best link, then id) is
    detached as a singleton, then the rule is re-checked."""
    out: list[set[str]] = []
    while len(part) >= 3:
        failing = []
        for m in sorted(part):
            mine = [bits[_key(m, x)] for x in part if x != m and _key(m, x) in links]
            if any(_key(m, x) in musts for x in part if x != m):
                continue
            if len(mine) >= 2 or (mine and max(mine) >= STRONG_FACTOR * link):
                continue
            failing.append((max(mine) if mine else 0.0, m))
        if not failing:
            break
        _b, worst = min(failing)
        part.discard(worst)
        out.append({worst})
    if out:  # detaching a member may disconnect the rest
        h = nx.Graph()
        h.add_nodes_from(part)
        h.add_edges_from(e for e in links | musts if e[0] in part and e[1] in part)
        return [set(c) for c in nx.connected_components(h)] + out
    return [part, *out]


def apply_gate(g: nx.Graph, pairs: Mapping[tuple[str, str], Any], link: float, low: float) -> list[dict[str, Any]]:
    """Run the gate on every component of >= 3 members of the NEXUS graph `g` (in place: edges between the parts are
    removed). Returns one record per split component."""
    views: dict[tuple[str, str], PairView] = {}
    splits = []
    for comp in sorted((sorted(c) for c in nx.connected_components(g)), key=lambda c: c[0]):
        if len(comp) < 3:
            continue
        for x in comp:
            for y in comp:
                if x < y and (x, y) in pairs and (x, y) not in views:
                    views[(x, y)] = view_of(pairs[(x, y)])
        edges = [(u, v, float(d.get("bits", d.get("weight", 0.0))), bool(d.get("must")))
                 for u, v, d in g.subgraph(comp).edges(data=True)]
        parts = gate_split(comp, edges, views, link, low)
        if len(parts) < 2:
            continue
        where = {n: i for i, p in enumerate(parts) for n in p}
        cut = sorted(_key(u, v) for u, v, _w, _m in edges if where[u] != where[v])
        g.remove_edges_from(cut)
        splits.append({"from_size": len(comp), "parts": [sorted(p) for p in parts], "removed_links": len(cut)})
    return splits


# --------------------------------------------------------------------------------------- report


def _reason_pl(p: PairView, n: int = 2) -> list[str]:
    rs = sorted((r for r in p.reasons if float(r.get("bits") or 0.0) > 0),
                key=lambda r: (-float(r.get("bits") or 0.0), str(r.get("kind"))))
    return [str(r.get("explanation_pl") or r.get("kind")) for r in rs[:n]]


def consistency_report(members: Iterable[str], pairs: Mapping[tuple[str, str], Any], link: float, low: float,
                       must: Iterable[tuple[str, str]] = ()) -> dict[str, Any]:
    """Consistency of one campaign against the spec gate (read-only; see module docstring)."""
    from hydra.campaign_support import member_support

    ms = sorted(set(members))
    n = len(ms)
    views = {k: view_of(v) for k, v in pairs.items() if k[0] in ms and k[1] in ms and k[0] != k[1]}
    must_set = {_key(a, b) for a, b in must}
    base = {"method": METHOD, "calibrated": False, "kind": "heuristic_evidence_bits", "link_threshold_bits": link,
            "low_threshold_bits": low, "strong_bits": STRONG_FACTOR * link, "size": n, "gate_enabled": gate_enabled()}
    if n < 2:
        return {**base, "applicable": False, "verdict": "singleton", "members": []}
    g = nx.Graph()
    g.add_nodes_from(ms)
    for (a, b), p in sorted(views.items()):
        if p.linked:
            g.add_edge(a, b, bits=p.support, weight=max(0.1, p.support))
    for a, b in sorted(must_set):
        if a in ms and b in ms:
            g.add_edge(a, b, bits=1e6, weight=1e6, must=True)
    support = member_support(ms, g, views, link)
    rows = []
    for m in ms:
        scored, linked, hard = [], [], []
        for x in ms:
            if x == m:
                continue
            p = views.get(_key(m, x))
            if p is None:
                continue
            scored.append(p.support)
            if g.has_edge(m, x):
                linked.append((p.support, x, p))
            if p.hard:
                hard.append({"with": x, "explanation_pl": [str(c.get("explanation_pl") or c.get("kind"))
                                                           for c in p.hard]})
        linked.sort(key=lambda t: (-t[0], t[1]))
        best = linked[0] if linked else None
        must_held = any(_key(m, x) in must_set for x in ms if x != m)
        if n == 2:
            status = "pair_campaign"
        elif len(linked) >= 2:
            status = "ok"
        elif best is not None and best[0] >= STRONG_FACTOR * link:
            status = "strong_single"
        elif must_held:
            status = "analyst_link"
        elif best is not None:
            status = "single_pair"
        else:
            status = "no_direct_link"
        med = statistics.median(scored) if scored else None
        row = {"id": m, "supporting_pairs": len(linked), "scored_pairs": len(scored), "possible_pairs": n - 1,
               "median_bits": round(med, 3) if med is not None else None,
               "median_ok": med is not None and med >= low, "status": status, "contradictions": hard}
        if best is not None:
            row["best_pair"] = {"with": best[1], "bits": round(best[0], 3), "reasons_pl": _reason_pl(best[2])}
            if status == "strong_single":
                row["reason_pl"] = (f"jedno silne powiązanie z {best[1]}: {best[0]:.1f} bit"
                                    f" >= 2 x próg ({STRONG_FACTOR * link:.1f} bit)")
        rows.append(row)
    all_scored = [p.support for p in views.values()]
    med_all = statistics.median(all_scored) if all_scored else None
    contra_pairs = sorted({_key(a, b) for (a, b), p in views.items() if p.hard})
    parts = gate_split(ms, [(u, v, d["bits"], bool(d.get("must"))) for u, v, d in g.edges(data=True)], views,
                       link, low) if n >= 3 else [set(ms)]
    single = [r["id"] for r in rows if r["status"] == "single_pair"]
    strong_single = [r["id"] for r in rows if r["status"] == "strong_single"]
    failing = [r["id"] for r in rows if r["status"] in ("single_pair", "no_direct_link") or not r["median_ok"]
               or r["contradictions"]]
    if n == 2:
        verdict = "pair_campaign" if not contra_pairs else "contradiction"
    elif len(parts) > 1:
        verdict = "gate_would_split"
    elif failing:
        verdict = "weak_members"
    else:
        verdict = "consistent"
    verdict_pl = {
        "pair_campaign": "kampania z dwóch materiałów: ocenia ją jedno powiązanie",
        "contradiction": "para zawiera twardą sprzeczność",
        "gate_would_split": f"bramka spójności rozdzieliłaby kampanię na {len(parts)} części",
        "weak_members": f"{len(failing)} członków nie spełnia warunków bramki dla pojedynczego członka",
        "consistent": "wszyscy członkowie spełniają warunki bramki spójności",
    }[verdict]
    return {**base, "applicable": True, "verdict": verdict, "verdict_pl": verdict_pl,
            "cohesion": support.get("cohesion"), "weakest_member": support.get("weakest_member"),
            "weakest_link_bits": support.get("weakest_link_bits"), "median_link_bits": support.get("median_link_bits"),
            "median_scored_bits": round(med_all, 3) if med_all is not None else None,
            "median_ok": med_all is not None and med_all >= low, "scored_pairs": len(all_scored),
            "possible_pairs": n * (n - 1) // 2, "links": g.number_of_edges(), "bridges": support.get("bridges", []),
            "single_pair_members": single, "strong_single_members": strong_single, "failing_members": failing,
            "contradiction_pairs": [list(k) for k in contra_pairs],
            "gate_parts": [sorted(p) for p in parts] if len(parts) > 1 else [],
            "members": sorted(rows, key=lambda r: (r["status"] == "ok", r["supporting_pairs"], r["id"]))}


def report_for_campaign(conn: Any, run_id: str, campaign_id: str, link: float, low: float) -> dict[str, Any] | None:
    """Consistency report of a stored campaign, computed from the run's tables. None when it is not in the run."""
    import json

    row = conn.execute("SELECT detail FROM campaigns WHERE run_id=? AND id=?", (run_id, campaign_id)).fetchone()
    if row is None:
        return None
    members = [r[0] for r in conn.execute(
        "SELECT incident_id FROM campaign_members WHERE run_id=? AND campaign_id=? ORDER BY incident_id",
        (run_id, campaign_id))]
    detail: dict[str, Any] = {}
    try:
        detail = json.loads(row[0] or "{}") or {}
    except ValueError:
        pass
    if not members:
        members = list(detail.get("members") or [])
    pairs: dict[tuple[str, str], PairView] = {}
    if len(members) > 1:
        marks = ",".join("?" * len(members))
        for r in conn.execute(f"SELECT a, b, hypothesis, detail FROM pairs WHERE run_id=? AND a IN ({marks})"
                              f" AND b IN ({marks})", [run_id, *members, *members]):
            try:
                d = json.loads(r[3] or "{}") or {}
            except ValueError:
                d = {}
            pairs[_key(r[0], r[1])] = view_of({**d, "hypothesis": r[2]})
    out = consistency_report(members, pairs, link, low)
    gate_meta = (detail.get("meta") or {}).get("consistency_gate")
    if gate_meta:
        out["gate_applied"] = gate_meta
    return {"campaign_id": campaign_id, "run_id": run_id, **out}


__all__ = ["HARD_CONTRADICTION_BITS", "METHOD", "PairView", "apply_gate", "consistency_report", "gate_enabled",
           "gate_split", "merge_check", "report_for_campaign", "view_of"]
