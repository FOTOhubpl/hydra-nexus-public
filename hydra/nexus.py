"""M6 NEXUS: campaigns from accepted links, campaign groups from kit-level links.

1. Campaigns = connected components over `same_campaign` pairs (chained-campaign recall beats community
   detection, PLAN_MAX §1.4). Every incident belongs to exactly one campaign; a singleton is its own pattern.
2. Size guard: a component larger than `max_size` is split by Leiden (graspologic-native) when installed,
   else by networkx Louvain, weighted by bits; the split is recorded in `Campaign.meta.split`.
3. Campaign groups (task §2 "grupy podobnych kampanii") = connected components over campaigns linked by
   `shared_template` pairs or kit-level bits ≥ `Thresholds.kit`, where the link rests on ≥ 2 independent
   identity-kit genes or one with df ≤ GROUP_RARE_DF; a group above MAX_GROUP_CAMPAIGNS campaigns or
   MAX_GROUP_MEMBERS incidents is split with Leiden (Louvain fallback) weighted by kit bits; the method is in
   `Clustering.group_meta` (stored as `meta` in the group JSON).
4. Each campaign gets a core genome (gene values shared by ≥ half of its members, ≥ 2; each entry carries the
   evidence pointers of the first two holders in member order as `evidence_a` / `evidence_b`), first/last seen, a
   label and a heuristic confidence (median link probability; 0 for singletons; kept for compatibility, the UI
   reads `meta.support` instead).
5. `Campaign.meta.support` (`hydra.campaign_support`, ADOPT #7): per-member evidence bits, weakest link,
   independent families, cohesion index, bridges, articulation members, leave-one-family-out stability and a
   heuristic rank; `calibrated: false` throughout (bits are not probabilities).
"""

from __future__ import annotations

import hashlib
import math
import os
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import networkx as nx

from hydra.campaign_support import member_support, rank_campaigns
from hydra.consistency import apply_gate, gate_enabled
from hydra.contracts import Campaign, CampaignGroup, GeneMatch, Hypothesis, Provenance
from hydra.entropy import Rarity
from hydra.synapse import IncidentGenome, PairEval, Thresholds, _short, is_kit, kind_label_pl, level

LABEL_KINDS = ("title", "og_title", "page_title", "ad_title")


@dataclass
class Clustering:
    campaigns: list[Campaign]
    groups: list[CampaignGroup]
    membership: dict[str, str]                 # incident → campaign id
    alerts: list[str] = field(default_factory=list)
    group_meta: dict[str, dict] = field(default_factory=dict)  # group id → method, sizes, limits (CR-CORE-6)


def _cid(members: list[str], prefix: str = "C") -> str:
    return f"{prefix}-" + hashlib.sha256("|".join(sorted(members)).encode()).hexdigest()[:8]


def split_component(g: nx.Graph, nodes: set[str], max_size: int, seed: int = 0) -> tuple[list[set[str]], str]:
    # A canonical subgraph (sorted nodes, sorted undirected edges): `g.subgraph(set)` iterates the set when it is
    # small against the graph, so the edge order followed string hashing and the seeded Leiden / Louvain split of
    # the same component differed between processes (6 partitions in 6 runs, SCALE 2026-11-21).
    edges = sorted((min(str(u), str(v)), max(str(u), str(v)), float(d.get("weight", 1.0)))
                   for u, v, d in g.subgraph(nodes).edges(data=True))
    sub = nx.Graph()
    sub.add_nodes_from(sorted(nodes))
    sub.add_weighted_edges_from(edges)
    try:
        import graspologic_native as gn  # type: ignore[import-not-found]

        _, parts = gn.leiden(edges=edges, seed=seed)
        comms: dict[int, set[str]] = defaultdict(set)
        for node, c in parts.items():
            comms[c].add(node)
        out, method = list(comms.values()), "leiden"
    except ImportError:
        out = [set(c) for c in nx.community.louvain_communities(sub, weight="weight", seed=seed)]
        method = "louvain"
    # recurse on parts still too large (bounded: Louvain may return the component unchanged)
    final: list[set[str]] = []
    for part in out:
        if len(part) > max_size and len(part) < len(nodes):
            final.extend(split_component(g, part, max_size, seed)[0])
        else:
            final.append(part)
    return final, method


def core_genome(members: list[str], genomes: dict[str, IncidentGenome], rarity: Rarity, top: int = 15
                ) -> list[GeneMatch]:
    if len(members) < 2:
        return []
    cnt: Counter[tuple[str, str]] = Counter()
    fam: dict[tuple[str, str], str] = {}
    holders: dict[tuple[str, str], list] = defaultdict(list)  # first two holders' genes (sorted member order)
    for m in sorted(members):
        for key, genes in genomes[m].discrete.items():
            if level(genes[0]) == "none":
                continue
            cnt[key] += 1
            fam.setdefault(key, str(genes[0].family))
            if len(holders[key]) < 2:
                holders[key].append(genes[0])
    need = max(2, (len(members) + 1) // 2)
    out = []
    for (kind, value), c in cnt.items():
        if c < need:
            continue
        bits, df = rarity.discrete_bits(kind, value, members[:2])
        if bits <= 0:
            continue
        held = holders[(kind, value)]
        out.append(GeneMatch(
            family=fam[(kind, value)], kind=kind, value_a=_short(value, 200), doc_freq=df, bits=round(bits, 3),
            provenance=Provenance.INF,
            # B10: where the shared value was observed in the first two members (sorted), so the reason opens
            evidence_a=held[0].evidence.model_copy() if held else None,
            evidence_b=held[1].evidence.model_copy() if len(held) > 1 else None,
            explanation_pl=f"{kind_label_pl(kind)}: „{_short(value)}” — w {c} z {len(members)} incydentów kampanii"))
    out.sort(key=_gm_key)
    return out[:top]


def _top(counter: Counter) -> str:
    """Most frequent value, ties broken by the value itself (Counter.most_common keeps insertion order, which for
    values drawn from sets differs between processes)."""
    return min(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]


def _gm_key(m: GeneMatch) -> tuple:
    return (-m.bits, str(m.family), m.kind, m.value_a or "", m.value_b or "")


def _label(members: list[str], genomes: dict[str, IncidentGenome], core: list[GeneMatch]) -> str:
    for kind in LABEL_KINDS:
        vals = Counter(v for m in members for (k, v) in genomes[m].discrete if k == kind)
        if vals:
            return _short(_top(vals), 90)
    titles = Counter(genomes[m].title for m in members if genomes[m].title)
    if titles:
        return _short(_top(titles), 90)
    if core:
        return _short(f"{kind_label_pl(core[0].kind)}: {core[0].value_a}", 90)
    return f"Wzorzec {members[0]}"


def _scheme(members: list[str], genomes: dict[str, IncidentGenome]) -> str | None:
    c = Counter(s for m in members for s in genomes[m].schemes)
    return _top(c) if c else None


MAX_GROUP_CAMPAIGNS = 50
MAX_GROUP_MEMBERS = 200
GROUP_RARE_DF = 3


def group_link_supported(pe: PairEval) -> bool:
    return pe.kit_groups >= 2 or (pe.kit_groups >= 1 and pe.kit_min_df is not None and pe.kit_min_df <= GROUP_RARE_DF)


# Kit spread rule (EVAL 2026-11-21 group flooding: 14,076 group false merges at 231 incidents): a kit value held by
# members of more than max(KIT_SPREAD_MIN, KIT_SPREAD_FRAC x campaigns) campaigns is a widely sold or generic kit
# part, not evidence that two campaigns belong together; a group link then needs >= 2 independent kit genes of
# narrower spread, or one held by at most GROUP_RARE_CAMPAIGNS campaigns.
# Disabled 2026-11-21 (SPEC_ERRATA E-SH-22): on the eval harness it lowered sample+syn group F1 1.0 -> 0.846 and its
# target (the 231-incident retrieval test) could not be measured (harness crash), so it stays off until measured.
KIT_SPREAD_ENABLED = False
KIT_SPREAD_MIN = 4
KIT_SPREAD_FRAC = 0.03
GROUP_RARE_CAMPAIGNS = 2


def kit_spread_index(genomes: dict[str, IncidentGenome], membership: dict[str, str]) -> dict[tuple[str, str], int]:
    """(kind, value) of identity-kit genes -> number of campaigns whose members hold it."""
    from hydra.synapse import is_style_match_kind

    holders: dict[tuple[str, str], set[str]] = defaultdict(set)
    for iid, g in genomes.items():
        cid = membership.get(iid, iid)
        for key, gs in g.discrete.items():
            if level(gs[0]) == "kit" and not is_style_match_kind(key[0], str(gs[0].family)):
                holders[key].add(cid)
    return {k: len(v) for k, v in holders.items()}


def spread_supported(a: IncidentGenome, b: IncidentGenome, spread: dict[tuple[str, str], int], limit: int) -> bool:
    """Group link support recomputed over the kit genes both incidents share, by campaign spread."""
    narrow: dict[str, int] = {}
    for key in a.discrete.keys() & b.discrete.keys():
        k = spread.get(key)
        if k is None or k > limit:
            continue
        grp = a.discrete[key][0].dependency_group or key[0]
        narrow[grp] = min(k, narrow.get(grp, k))
    return len(narrow) >= 2 or any(v <= GROUP_RARE_CAMPAIGNS for v in narrow.values())


# G2 confirmed group bridges (HYDRA_ADV_GROUPS=1, default off; SPEC_ERRATA E-G2-1). Diagnosis on the 231-incident
# retrieval corpus: 237 of 239 group links that joined campaigns of different labelled groups had one end in a
# `noise_misfiled_har` incident (a sample incident carrying another incident's HAR): one incident with a foreign
# artefact bridged two otherwise separate campaigns. With the flag a group edge between two campaigns needs
# (1) links resting on >= 2 independent identity-kit genes (the single rare kit gene path is dropped: one trace is not
# a group) and (2) independent confirmation: such links from at least min(2, size) distinct members on EACH side. A
# rejected relation stays visible as `Campaign.meta.group_candidates` (an inter-campaign edge without a shared group).
# Campaign partitions are not touched: groups are a separate layer.
ADV_GROUPS_CONFIRM = 2
GROUP_CANDIDATES_MAX = 5


def adv_groups_enabled() -> bool:
    return os.environ.get("HYDRA_ADV_GROUPS", "0") == "1"


MUST_LINK_WEIGHT = 1e6
# Size guard (review M8): split a component only above max(MAX_SIZE_MIN, n // 3) incidents AND when it is sparse
# (density < SPLIT_MAX_DENSITY): a dense large component is one campaign, a sparse one is a chain through bridges.
MAX_SIZE_MIN = 60
SPLIT_MAX_DENSITY = 0.3


def cluster(genomes: dict[str, IncidentGenome], pairs: dict[tuple[str, str], PairEval], rarity: Rarity,
            th: Thresholds | None = None, max_size: int | None = None, max_group_campaigns: int | None = None,
            max_group_members: int | None = None, must_link: list[tuple[str, str]] | None = None,
            cannot_link: list[tuple[str, str]] | None = None) -> Clustering:
    """`must_link` / `cannot_link`: analyst constraints (hydra.review). Must-links are edges that are never cut;
    for every cannot-link pair still connected after removing its direct edge, the minimum weighted edge cut of
    the component (must-links uncuttable) separates them; a conflict between constraints raises an alert."""
    th = th or Thresholds()
    group_meta: dict[str, dict] = {}
    n = len(genomes)
    max_size = max_size or max(MAX_SIZE_MIN, n // 3)  # review M8: was max(25, n // 4), split legit campaigns
    g = nx.Graph()
    g.add_nodes_from(genomes)
    for (a, b), pe in pairs.items():
        if pe.score.hypothesis == Hypothesis.SAME_CAMPAIGN:
            g.add_edge(a, b, weight=max(0.1, pe.score.total_bits), prob=pe.score.probability, bits=pe.support)
    alerts: list[str] = []
    must = {tuple(sorted(p)) for p in must_link or [] if p[0] in genomes and p[1] in genomes and p[0] != p[1]}
    cannot = {tuple(sorted(p)) for p in cannot_link or [] if p[0] in genomes and p[1] in genomes and p[0] != p[1]}
    for a, b in must:
        g.add_edge(a, b, weight=MUST_LINK_WEIGHT, prob=1.0, must=True)
    for a, b in cannot:
        if (a, b) in must:
            alerts.append(f"Sprzeczne decyzje analityka dla pary {a}–{b} (musi i nie może być połączona);"
                          " obowiązuje nowsza: rozłączenie")
            g.remove_edge(a, b)
        elif g.has_edge(a, b):
            g.remove_edge(a, b)
    for a, b in sorted(cannot):
        if not nx.has_path(g, a, b):
            continue
        comp = g.subgraph(nx.node_connected_component(g, a)).copy()
        for _u, _v, d in comp.edges(data=True):
            d["capacity"] = MUST_LINK_WEIGHT * 10 if d.get("must") else d.get("weight", 1.0)
        cut_value, (side_a, _side_b) = nx.minimum_cut(comp, a, b, capacity="capacity")
        if cut_value >= MUST_LINK_WEIGHT * 10:
            alerts.append(f"Nie można rozdzielić {a} i {b} bez naruszenia powiązania zatwierdzonego przez analityka")
            continue
        for u, v in list(comp.edges()):
            if (u in side_a) != (v in side_a) and g.has_edge(u, v):
                g.remove_edge(u, v)
    gated: dict[str, dict] = {}
    if gate_enabled():  # ADV-CAMP: spec M6 consistency gate (HYDRA_ADV_GATE=1, default off), hydra/consistency.py
        for s in apply_gate(g, pairs, th.link, th.low):
            alerts.append(f"Bramka spójności: składowa {s['from_size']} incydentów rozdzielona na {len(s['parts'])}"
                          f" części ({s['removed_links']} powiązań bez wymaganego wsparcia)")
            for p in s["parts"]:
                for m in p:
                    gated[m] = {"from_size": s["from_size"], "parts": len(s["parts"])}
    comps: list[tuple[set[str], dict]] = []
    for comp in nx.connected_components(g):
        if len(comp) > max_size and nx.density(g.subgraph(comp)) < SPLIT_MAX_DENSITY:
            parts, method = split_component(g, set(comp), max_size)
            alerts.append(f"Składowa {len(comp)} incydentów przekroczyła limit {max_size}; podział metodą {method}"
                          f" na {len(parts)} części")
            for p in parts:
                comps.append((p, {"split": method, "from_size": len(comp)}))
        else:
            comps.append((set(comp), {}))

    campaigns: list[Campaign] = []
    membership: dict[str, str] = {}
    supports: dict[str, dict] = {}
    for comp, meta in sorted(comps, key=lambda t: (-len(t[0]), sorted(t[0])[0])):
        members = sorted(comp)
        cid = _cid(members)
        core = core_genome(members, genomes, rarity)
        probs = [g.edges[u, v]["prob"] for u, v in g.subgraph(members).edges if not g.edges[u, v].get("must")]
        dates = sorted(d for m in members if (d := genomes[m].observed_at))
        listed = sorted({v for m in members for (k, v) in genomes[m].discrete
                         if k.lower().startswith(("cert_pl", "certpl"))})
        if listed:
            meta = {**meta, "cert_pl_listed": listed}
        if gated.get(members[0]):
            meta = {**meta, "consistency_gate": gated[members[0]]}
        c = Campaign(id=cid, label=_label(members, genomes, core), members=members, core_genome=core,
                     scheme=_scheme(members, genomes), first_seen=dates[0] if dates else None,
                     last_seen=dates[-1] if dates else None,
                     confidence=round(statistics.median(probs), 4) if probs else 0.0,
                     meta={**meta, "size": len(members), "links": len(probs),
                           "confidence_kind": "median_link_score_uncalibrated"})
        supports[cid] = c.meta["support"] = member_support(members, g, pairs, th.link)
        campaigns.append(c)
        for m in members:
            membership[m] = cid

    rank_campaigns(supports)

    # groups: kit-level links across campaigns
    cg = nx.Graph()
    cg.add_nodes_from(c.id for c in campaigns)
    link_genes: dict[tuple[str, str], list[GeneMatch]] = defaultdict(list)
    rejected_weak = rejected_spread = rejected_bridge = 0
    adv = adv_groups_enabled()
    sides: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    strong: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    best_kit: dict[tuple[str, str], float] = {}
    spread = kit_spread_index(genomes, membership)
    spread_limit = max(KIT_SPREAD_MIN, math.ceil(KIT_SPREAD_FRAC * len(campaigns)))
    for (a, b), pe in pairs.items():
        ca, cb = membership[a], membership[b]
        if ca == cb:
            continue
        if not (pe.score.hypothesis == Hypothesis.SHARED_TEMPLATE or pe.kit_bits >= th.kit):
            continue
        # a group link needs >= 2 independent identity-kit genes, or one with df <= GROUP_RARE_DF
        # (public corpus: one shared platform value chained 1,431 campaigns into one group)
        if not group_link_supported(pe):
            rejected_weak += 1
            continue
        if (KIT_SPREAD_ENABLED and a in genomes and b in genomes
                and not spread_supported(genomes[a], genomes[b], spread, spread_limit)):
            rejected_spread += 1
            continue
        key = (ca, cb) if ca < cb else (cb, ca)
        if adv:
            sides[key][ca].add(a)
            sides[key][cb].add(b)
            best_kit[key] = max(best_kit.get(key, 0.0), pe.kit_bits)
            if pe.kit_groups < 2:
                continue
            strong[key][ca].add(a)
            strong[key][cb].add(b)
        w = cg.edges[key]["weight"] if cg.has_edge(*key) else 0.0
        cg.add_edge(*key, weight=max(w, pe.kit_bits))
        link_genes[key].extend(r for r in pe.score.reasons if r.bits > 0)
    groups: list[CampaignGroup] = []
    by_id = {c.id: c for c in campaigns}
    size_of = {c.id: len(c.members) for c in campaigns}
    if adv:
        candidates: dict[str, list[dict]] = defaultdict(list)
        for key in sorted(sides):
            x, y = key
            sx, sy = strong[key].get(x, set()), strong[key].get(y, set())
            if (len(sx) >= min(ADV_GROUPS_CONFIRM, size_of[x])
                    and len(sy) >= min(ADV_GROUPS_CONFIRM, size_of[y])):
                continue
            if cg.has_edge(*key):
                cg.remove_edge(*key)
            rejected_bridge += 1
            reason = "single_kit_trace" if not sx else "unconfirmed_bridge"
            for here, there in ((x, y), (y, x)):
                candidates[here].append({"campaign": there, "kit_bits": round(best_kit[key], 3), "reason": reason,
                                         "members_here": len(sides[key][here]),
                                         "members_there": len(sides[key][there]),
                                         "confirm_needed": ADV_GROUPS_CONFIRM})
            link_genes.pop(key, None)
        for cid, rows in candidates.items():
            rows.sort(key=lambda r: (-r["kit_bits"], r["campaign"]))
            by_id[cid].meta["group_candidates"] = rows[:GROUP_CANDIDATES_MAX]
    max_campaigns = max_group_campaigns or max(MAX_GROUP_CAMPAIGNS, len(campaigns) // 50)
    max_members = max_group_members or max(MAX_GROUP_MEMBERS, n // 25)
    comps: list[tuple[set[str], dict]] = []
    for comp in nx.connected_components(cg):
        if len(comp) < 2:
            continue
        members = sum(size_of[c] for c in comp)
        if len(comp) > max_campaigns or members > max_members:
            parts, method = split_component(cg, set(comp), max_campaigns)
            alerts.append(f"Grupa {len(comp)} kampanii ({members} incydentów) przekroczyła limit"
                          f" {max_campaigns} kampanii / {max_members} incydentów; podział metodą {method}"
                          f" na {len(parts)} części")
            for part in parts:
                comps.append((part, {"method": f"kit_links+{method}", "split_from_campaigns": len(comp),
                                     "split_from_members": members}))
        else:
            comps.append((set(comp), {"method": "kit_links_connected_components"}))
    for comp, gmeta in comps:
        if len(comp) < 2:
            continue
        cids = sorted(comp)
        shared: dict[tuple[str, str | None], GeneMatch] = {}
        for (x, y), ms in link_genes.items():
            if x in comp and y in comp:
                for m in ms:
                    k = (m.kind, m.value_a)
                    if k not in shared or _gm_key(m) < _gm_key(shared[k]):
                        shared[k] = m
        top = sorted(shared.values(), key=_gm_key)[:15]
        gid = _cid(cids, "G")
        kit_top = [m for m in top if m.kind in _kit_kind_names()] or top
        label = (f"Grupa {len(cids)} kampanii: {kind_label_pl(kit_top[0].kind).lower()}" if kit_top
                 else f"Grupa {len(cids)} kampanii")
        conf = min(1.0, sum(m.bits for m in top[:3]) / 30.0)
        groups.append(CampaignGroup(id=gid, label=label, campaign_ids=cids, shared_genes=top,
                                    confidence=round(conf, 4)))
        group_meta[gid] = {**gmeta, "evidence_bits": round(sum(m.bits for m in top[:3]), 3),
                           "confidence_kind": "top3_shared_bits_over_30_uncalibrated", "campaigns": len(cids),
                           "members": sum(size_of[c] for c in cids),
                           "rule": (f">=2 independent kit genes on links from >= min({ADV_GROUPS_CONFIRM}, size)"
                                    " members of each campaign" if adv
                                    else f">=2 independent kit genes or one with df <= {GROUP_RARE_DF}"),
                           "limits": {"campaigns": max_campaigns, "members": max_members}}
        for cid in cids:
            by_id[cid].group_id = gid
    if rejected_weak:
        alerts.append(f"Odrzucono {rejected_weak} powiązań grupowych opartych na jednym pospolitym genie zestawu"
                      " narzędzi")
    if rejected_bridge:
        alerts.append(f"Odrzucono {rejected_bridge} powiązań grupowych bez niezależnego potwierdzenia (most oparty na"
                      " jednym incydencie albo jednym śladzie zestawu narzędzi); pozostają jako kandydaci powiązań"
                      " między kampaniami")
    if rejected_spread:
        alerts.append(f"Odrzucono {rejected_spread} powiązań grupowych opartych na elementach zestawu narzędzi"
                      f" używanych przez więcej niż {spread_limit} kampanii")
    return Clustering(campaigns, groups, membership, alerts, group_meta)


def _kit_kind_names() -> set[str]:
    from hydra.synapse import KIT_KINDS

    return KIT_KINDS


__all__ = ["Clustering", "cluster", "core_genome", "split_component", "is_kit"]
