"""Variant tree by mutation distance (spec 3.0 M7 / "Algorytmy" §7, ADV-CAMP; API output behind HYDRA_ADV_LINEAGE=1).

The default lineage (`hydra/lineage.py`) attaches every member to the earlier member with the strongest link. This
module builds the spec's tree instead: a minimum spanning arborescence (Edmonds, `networkx`) over directed edges
weighted by how much the genome changed between two members, rooted at the earliest observation.

Edge cost u -> v
    cost = mutation(u, v) + BETA x dir(u, v) + GAMMA x (S_max - S(u, v)) / S_max
- mutation(u, v): sum over gene kinds of CATEGORY_WEIGHT[category] x per-kind distance, where
  - near-hash image kinds (pdq, phash, dhash, ahash): minimum Hamming distance / hash bits (0 = same image; a small
    distance is an image transform such as crop / recompression / overlay, a large one a new creative);
  - any other kind observed on both sides: Jaccard distance of the value sets (text edit, domain rotation, new
    contact ...);
  - kind observed on one side only, while the same extractor produced observations on the other side: ADDED_COST
    (gene added / removed); otherwise unknown on the other side and 0 (a missing observation is never a mutation);
  - kinds that only restate others (file sha256, MinHash, SimHash, TLSH, embeddings) and lineage noise kinds are
    not counted.
- dir(u, v): 1 when u was observed after v (both dated), 0.5 when either date is unknown, else 0 (spec: earlier date
  first; resolution and recompression hints are not extracted as genes, so they are not used).
- S(u, v): campaign-level support bits of the scored pair (0 when not scored); S_max: the campaign's maximum. A small
  tie-breaker that prefers strongly linked parents among equally distant ones.
Root: the first member in observation order (`lineage.observation_order`); its incoming edges are removed, so any
spanning arborescence starts there. Candidates: every ordered member pair up to MAX_COMPLETE members, else scored
pairs only (then the result may be a forest: minimum branching, `spanning: false`).
Every edge carries its mutations (category, kind, type, distance, before / after values) and a Polish summary. The
direction is a hypothesis from observation order and gene distance, not a proven edit order.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

import networkx as nx

from hydra.lineage import (
    CATEGORY_ORDER,
    CATEGORY_PL,
    NEAR_HASH,
    GeneRow,
    PairLink,
    _hex_bits,
    _parse_time,
    _short,
    _values,
    category,
    observation_order,
)

METHOD = "mutation_distance_arborescence@1"
CATEGORY_WEIGHT = {"text": 1.0, "creative": 1.0, "domain": 1.0, "contact": 1.0, "operator": 1.0, "person": 1.0,
                   "kit": 0.5, "narrative": 0.5, "meta": 0.25, "other": 0.25}
ADDED_COST = 0.5
BETA = 2.0
GAMMA = 0.5
MAX_COMPLETE = 80
MAX_MUTATIONS = 12
TRANSFORM_MAX = 0.25  # near-hash distance up to this share of the hash bits = image transform, above = new creative
SKIP_PREFIX = ("artifact_sha256", "minhash", "simhash", "tlsh", "dinov2", "siglip", "tvec", "e5", "embedding")
TYPE_PL = {
    "text_edit": "edycja tekstu", "image_transform": "transformacja obrazu", "new_creative": "nowa kreacja",
    "domain_rotation": "rotacja domeny", "changed": "zmiana", "added": "dodano", "removed": "usunięto",
}


def enabled() -> bool:
    return os.environ.get("HYDRA_ADV_LINEAGE", "0") == "1"


def _near_spec(kind: str) -> tuple[int, int] | None:
    k = kind.lower()
    return next((v for p, v in sorted(NEAR_HASH.items(), key=lambda x: -len(x[0])) if k.startswith(p)), None)


def _hamming_share(kind: str, a: set[str], b: set[str]) -> float | None:
    spec = _near_spec(kind)
    if spec is None:
        return None
    ha = [h for h in (_hex_bits(v) for v in a) if h is not None]
    hb = [h for h in (_hex_bits(v) for v in b) if h is not None]
    if not ha or not hb:
        return None
    return min(bin(x ^ y).count("1") for x in ha for y in hb) / float(spec[0])


def mutations(parent: list[GeneRow], child: list[GeneRow]) -> tuple[float, list[dict[str, Any]]]:
    """Mutation distance parent -> child and the per-kind mutations behind it (distance > 0 only)."""
    pv, prep, punk, pext = _values(parent)
    cv, crep, cunk, cext = _values(child)
    total, out = 0.0, []
    for kind in sorted(set(pv) | set(cv)):
        if kind.lower().startswith(SKIP_PREFIX):
            continue
        a, b = pv.get(kind), cv.get(kind)
        rep = prep.get(kind) or crep.get(kind)
        if rep is None:
            continue
        cat = category(rep.family, kind, rep.dependency_group)
        w = CATEGORY_WEIGHT.get(cat, 0.25)
        if a and b:
            share = _hamming_share(kind, a, b)
            if share is not None:
                d = share
                typ = "image_transform" if share <= TRANSFORM_MAX else "new_creative"
            else:
                inter = len(a & b)
                d = 1.0 - inter / len(a | b)
                typ = {"text": "text_edit", "domain": "domain_rotation",
                       "creative": "new_creative"}.get(cat, "changed")
            if d <= 0:
                continue
            item = {"type": typ, "distance": round(d, 4), "before": _short(a - b or a), "after": _short(b - a or b)}
            if share is not None:
                item["hamming_bits"] = round(share * _near_spec(kind)[0])  # type: ignore[index]
        elif b and not a and kind not in punk and rep.extractor.split("@")[0] in pext:
            d, item = ADDED_COST, {"type": "added", "distance": ADDED_COST, "after": _short(b)}
        elif a and not b and kind not in cunk and rep.extractor.split("@")[0] in cext:
            d, item = ADDED_COST, {"type": "removed", "distance": ADDED_COST, "before": _short(a)}
        else:
            continue  # unknown on one side: not a mutation
        cost = round(w * d, 4)
        total += cost
        out.append({"kind": kind, "family": rep.family, "category": cat, "category_pl": CATEGORY_PL[cat],
                    "type_pl": TYPE_PL[item["type"]], "cost": cost, **item})
    out.sort(key=lambda m: (-m["cost"], CATEGORY_ORDER.index(m["category"]), m["kind"]))
    return round(total, 4), out


def summary_pl(muts: list[dict[str, Any]]) -> str:
    if not muts:
        return "brak mutacji w porównywalnych genach"
    parts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for m in muts:
        key = (m["type"], m["category"])
        if key in seen:
            continue
        seen.add(key)
        if m["type"] == "domain_rotation":
            parts.append(f"rotacja domeny ({', '.join(m['before'][:1])} → {', '.join(m['after'][:1])})")
        elif m["type"] == "image_transform":
            parts.append(f"transformacja obrazu ({m['kind']}: {m.get('hamming_bits')} bit Hamminga)")
        elif m["type"] == "text_edit":
            parts.append(f"edycja tekstu ({m['kind']}, odległość {m['distance']:.2f})")
        else:
            parts.append(f"{m['type_pl']}: {m['category_pl']} ({m['kind']})")
        if len(parts) >= 4:
            break
    rest = len({(m["type"], m["category"]) for m in muts}) - len(parts)
    return "; ".join(parts) + (f"; +{rest} innych" if rest > 0 else "")


def _dir(tu: Any, tv: Any) -> float:
    if tu is None or tv is None:
        return 0.5
    return 1.0 if tu > tv else 0.0


def mutation_tree(members: Iterable[str], observed_at: Mapping[str, str | None], pairs: Iterable[PairLink],
                  genes: Mapping[str, list[GeneRow]], import_rank: Mapping[str, int] | None = None,
                  base_parents: Mapping[str, str | None] | None = None) -> dict[str, Any]:
    """Minimum-cost variant arborescence of one campaign (see module docstring). Deterministic for the same input."""
    order, direction = observation_order(members, observed_at, import_rank)
    n = len(order)
    pos = {iid: i for i, iid in enumerate(order)}
    times = {i: _parse_time(observed_at.get(i)) for i in order}
    support: dict[tuple[str, str], float] = {}
    for p in pairs:
        if p.a in pos and p.b in pos and p.a != p.b:
            k = (p.a, p.b) if p.a < p.b else (p.b, p.a)
            support[k] = max(support.get(k, 0.0), float(p.support or 0.0))
    s_max = max([v for v in support.values() if v > 0] or [1.0])
    root = order[0] if order else None
    base = {"method": METHOD, "direction": direction, "root": root, "calibrated": False,
            "params": {"beta": BETA, "gamma": GAMMA, "added_cost": ADDED_COST, "category_weight": CATEGORY_WEIGHT,
                       "transform_max": TRANSFORM_MAX}}
    if n < 2:
        return {**base, "spanning": True, "candidates": "none", "nodes": [], "edges": [], "total_cost": 0.0}
    if n <= MAX_COMPLETE:
        cand = [(a, b) for i, a in enumerate(order) for b in order[i + 1:]]
        mode = "all_member_pairs"
    else:
        cand = [k for k, v in sorted(support.items()) if v > 0]
        mode = "scored_pairs"
    g = nx.DiGraph()
    g.add_nodes_from(order)
    cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
    for a, b in cand:
        s = support.get((a, b) if a < b else (b, a), 0.0)
        tie = GAMMA * max(0.0, s_max - max(0.0, s)) / s_max
        for u, v in ((a, b), (b, a)):
            if v == root:
                continue
            if (u, v) not in cache:
                cache[(u, v)] = mutations(genes.get(u, []), genes.get(v, []))
            mut = cache[(u, v)][0]
            dpen = _dir(times[u], times[v]) if direction != "undirected" else 0.0
            if direction in ("import_order", "mixed") and (times[u] is None or times[v] is None):
                dpen = 0.5 if pos[u] > pos[v] else 0.0
            g.add_edge(u, v, weight=round(mut + BETA * dpen + tie, 6), mutation=mut, dir=dpen, support=s)
    try:
        tree = nx.minimum_spanning_arborescence(g, attr="weight", preserve_attrs=True)
        spanning = True
    except nx.NetworkXException:
        tree = nx.minimum_branching(g, attr="weight", preserve_attrs=True)
        spanning = False
    parent = {v: u for u, v in tree.edges()}
    depth: dict[str, int] = {}

    def d(i: str) -> int:
        if i not in depth:
            depth[i] = 0 if i not in parent else d(parent[i]) + 1
        return depth[i]

    nodes = [{"incident_id": i, "observed_at": observed_at.get(i), "order": pos[i] + 1, "parent": parent.get(i),
              "root": i not in parent, "depth": d(i)} for i in order]
    edges, total = [], 0.0
    for child in sorted(parent, key=lambda c: pos[c]):
        par = parent[child]
        e = g.edges[par, child]
        mut, muts = cache[(par, child)]
        total += e["weight"]
        edges.append({"parent": par, "child": child, "cost": round(e["weight"], 4), "mutation_distance": mut,
                      "direction_penalty": e["dir"], "support_bits": round(e["support"], 3),
                      "against_observation_order": e["dir"] >= 1.0, "mutations": muts[:MAX_MUTATIONS],
                      "mutation_count": len(muts), "summary_pl": summary_pl(muts)})
    out = {**base, "spanning": spanning, "candidates": mode, "nodes": nodes, "edges": edges,
           "roots": [x["incident_id"] for x in nodes if x["root"]], "total_cost": round(total, 4),
           "note_pl": ("Drzewo wariantów (hipoteza): minimalna arborescencja Edmondsa po odległości mutacji genów "
                       "(edycje tekstu, transformacje obrazu, rotacja domeny), z korzeniem w najwcześniejszej "
                       "obserwacji. Brak obserwacji po jednej stronie nie jest mutacją.")}
    if base_parents is not None:
        same = sum(1 for x in order[1:] if base_parents.get(x) == parent.get(x))
        out["agreement_with_link_tree"] = round(same / (n - 1), 4)
    return out


__all__ = ["METHOD", "enabled", "mutation_tree", "mutations", "summary_pl"]
