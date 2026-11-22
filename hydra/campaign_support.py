"""Member and campaign support (ADOPT_PLAN #7): evidence bits, cohesion, bridges, leave-one-family-out stability.

Replaces "median link probability" as the thing the analyst reads. Every number here is a heuristic built from
the rarity bits of the pairs inside one campaign; none is a calibrated probability (`calibrated: false`).

Inputs per campaign: its members, the link edges that hold it together in the final NEXUS graph (same-campaign
pairs that survived analyst constraints, plus analyst must-links), and every scored pair between two members.

Per member
- `bits_to_rest`: sum of campaign-level support bits (`PairEval.support`, the number compared with the link
  threshold) of its scored pairs to the other members; a missing pair contributes 0 (unknown, not negative).
- `anchor_bits`: bits of its strongest direct link: the weakest point that still keeps it in the campaign.
  `None` when it is held only by an analyst must-link.
- `families`: independent feature families with positive campaign bits on its links (`campaign_by_family`).
- `index` (0..1, heuristic): 1 - RMS(1 - s) over all other members, s = min(1, bits / (2 x link threshold))
  for a link, 1 for an analyst must-link, 0 for a missing or non-link pair (dedupe-style member score).
- `articulation`: removing the member splits the campaign graph.

Per campaign
- `density` = links / possible links; `median_link_bits`; `weakest_link_bits` = weakest edge of the maximum
  spanning tree (the bottleneck the campaign hangs on); `cohesion` = mean member index; `weakest_member`.
- `bridges`: link edges whose removal splits the campaign (campaigns with >= 3 members; a two-member campaign is
  one link and is judged by that link's bits), with the sizes of the two parts.
- `stability`: for each family present on the links, drop that family and keep an edge only if it still links
  (the pair's stored leave-one-family-out result, else support minus that family's bits >= link threshold).
  Reports the parts, the share of the largest part and the detached members; `fragile_families` split it.
- `rank` / `rank_of`: order of multi-member campaigns by median link bits (1 = strongest). A rank, not a score.
"""

from __future__ import annotations

import math
import statistics
from typing import Any

import networkx as nx

METHOD = "campaign_support@1"


def _link_bits(d: dict) -> float | None:
    return None if d.get("must") else float(d.get("bits", 0.0))


def _still_links(pe: Any, fam: str, link: float) -> bool:
    rob = getattr(pe, "robustness", None) or {}
    per = (rob.get("families") or {}).get(fam)
    if per is not None:
        return bool(per.get("still_linked"))
    by_fam = getattr(pe, "campaign_by_family", None) or {}
    return float(getattr(pe, "support", 0.0)) - float(by_fam.get(fam, 0.0)) >= link


def member_support(members: list[str], g: nx.Graph, pairs: dict[tuple[str, str], Any], link: float
                   ) -> dict[str, Any]:
    """Support block for one campaign. `g` holds the final link edges (attributes: `bits`, `must`)."""
    ms = sorted(members)
    n = len(ms)
    base = {"calibrated": False, "kind": "heuristic_evidence_bits", "method": METHOD, "link_threshold_bits": link}
    if n < 2:
        return {**base, "singleton": True, "links": 0, "possible_links": 0, "density": None,
                "median_link_bits": None, "weakest_link_bits": None, "cohesion": None, "min_member_index": None,
                "weakest_member": None, "bridges": [], "articulation_points": [],
                "members": [{"id": m, "bits_to_rest": 0.0, "anchor_bits": None, "links": 0, "families": [],
                             "n_families": 0, "index": None, "articulation": False, "analyst_link": False}
                            for m in ms],
                "stability": {"families": {}, "stable": True, "fragile_families": []}}
    sub = nx.Graph()
    sub.add_nodes_from(ms)
    sub.add_edges_from((u, v, d) for u, v, d in g.subgraph(ms).edges(data=True))
    possible = n * (n - 1) // 2
    edge_bits = [b for _u, _v, d in sub.edges(data=True) if (b := _link_bits(d)) is not None]
    norm = 2.0 * link if link > 0 else 20.0

    def pe_of(a: str, b: str) -> Any:
        return pairs.get((a, b) if a < b else (b, a))

    rows = []
    for m in ms:
        bits_to_rest, sq, fams = 0.0, 0.0, set()
        anchors: list[float] = []
        analyst = False
        for x in ms:
            if x == m:
                continue
            pe = pe_of(m, x)
            if pe is not None and pe.support > 0:
                bits_to_rest += float(pe.support)
            if sub.has_edge(m, x):
                d = sub.edges[m, x]
                if d.get("must"):
                    analyst, s = True, 1.0
                else:
                    b = float(d.get("bits", 0.0))
                    anchors.append(b)
                    s = min(1.0, max(0.0, b) / norm)
                if pe is not None:
                    fams |= {f for f, v in (pe.campaign_by_family or {}).items() if v > 0}
            else:
                s = 0.0
            sq += (1.0 - s) ** 2
        index = 1.0 - math.sqrt(sq / (n - 1))
        rows.append({"id": m, "bits_to_rest": round(bits_to_rest, 3),
                     "anchor_bits": round(max(anchors), 3) if anchors else None, "links": sub.degree(m),
                     "families": sorted(fams), "n_families": len(fams), "index": round(index, 4),
                     "articulation": False, "analyst_link": analyst})

    art = sorted(nx.articulation_points(sub)) if nx.is_connected(sub) else []
    for r in rows:
        r["articulation"] = r["id"] in art
    bridges = []
    if n >= 3 and nx.is_connected(sub):
        for u, v in sorted(tuple(sorted(e)) for e in nx.bridges(sub)):
            cut = sub.copy()
            cut.remove_edge(u, v)
            sizes = sorted((len(nx.node_connected_component(cut, u)), len(nx.node_connected_component(cut, v))))
            bridges.append({"a": u, "b": v, "bits": _link_bits(sub.edges[u, v]), "split_sizes": sizes})
        bridges.sort(key=lambda e: (-e["split_sizes"][0], e["bits"] if e["bits"] is not None else math.inf,
                                    e["a"], e["b"]))
    weakest_link = None
    if sub.number_of_edges():
        for _u, _v, d in sub.edges(data=True):
            d["_w"] = math.inf if d.get("must") else float(d.get("bits", 0.0))
        tree = nx.maximum_spanning_tree(sub, weight="_w")
        finite = [d["_w"] for _u, _v, d in tree.edges(data=True) if math.isfinite(d["_w"])]
        weakest_link = round(min(finite), 3) if finite else None
    weakest = min(rows, key=lambda r: (r["index"], r["id"]))

    fam_set = sorted({f for u, v in sub.edges if (pe := pe_of(u, v)) is not None
                      for f, val in (pe.campaign_by_family or {}).items() if val > 0})
    stability: dict[str, dict] = {}
    for fam in fam_set:
        kept = nx.Graph()
        kept.add_nodes_from(ms)
        for u, v, d in sub.edges(data=True):
            pe = pe_of(u, v)
            if d.get("must") or pe is None or _still_links(pe, fam, link):
                kept.add_edge(u, v)
        parts = sorted(nx.connected_components(kept), key=lambda c: (-len(c), sorted(c)[0]))
        stability[fam] = {"parts": len(parts), "largest_share": round(len(parts[0]) / n, 4),
                          "detached": sorted(x for p in parts[1:] for x in p)}
    fragile = sorted(f for f, v in stability.items() if v["parts"] > 1)
    return {**base, "singleton": False, "links": sub.number_of_edges(), "possible_links": possible,
            "density": round(sub.number_of_edges() / possible, 4),
            "median_link_bits": round(statistics.median(edge_bits), 3) if edge_bits else None,
            "weakest_link_bits": weakest_link,
            "cohesion": round(statistics.fmean(r["index"] for r in rows), 4),
            "min_member_index": weakest["index"], "weakest_member": weakest["id"],
            "bridges": bridges, "articulation_points": art,
            "members": sorted(rows, key=lambda r: (r["index"], r["id"])),
            "stability": {"families": stability, "stable": not fragile, "fragile_families": fragile}}


def rank_campaigns(supports: dict[str, dict]) -> None:
    """Heuristic rank of multi-member campaigns by median link bits (then size of evidence); in place."""
    multi = [(cid, s) for cid, s in supports.items() if not s.get("singleton")]
    multi.sort(key=lambda t: (-(t[1].get("median_link_bits") or 0.0), -(t[1].get("weakest_link_bits") or 0.0),
                              -t[1].get("links", 0), t[0]))
    for i, (_cid, s) in enumerate(multi, 1):
        s["rank"], s["rank_of"] = i, len(multi)
    for s in supports.values():
        if s.get("singleton"):
            s["rank"], s["rank_of"] = None, len(multi)


__all__ = ["METHOD", "member_support", "rank_campaigns"]
