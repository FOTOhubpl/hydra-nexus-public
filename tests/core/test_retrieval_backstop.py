"""Bounded retrieval backstop for operator-tier values above max_df (CR-ASTRA-PIPE-201).

Synthetic contract inputs only (fabricated identifiers, no organiser data, no models). The campaign members share
the six production operator-tier identifiers of the audit fixture; every other incident holds one private value.
"""

from __future__ import annotations

from itertools import combinations

import pytest

from hydra.assign import assign
from hydra.pipeline import rarity_for, score_and_cluster
from hydra.retrieval import (
    BACKSTOP_FULL_DF,
    BACKSTOP_QUERY_CAP,
    BACKSTOP_RING_K,
    candidates,
    candidates_per_query,
    query_scope,
)
from hydra.synapse import Thresholds, build_genome, score_pair
from tests.pipeline_audit.test_effectiveness import IDENTIFIERS, gene, genomes, growth_identity

TH = Thresholds()


def iid(i: int) -> str:
    return f"i{i:04d}"


def population(n: int, m: int, extra=None):
    rows = {iid(i): [gene(iid(i), "private_identity", f"unique-{i}", "INF:operator:private")] for i in range(n)}
    for i in range(m):
        rows[iid(i)] += growth_identity(iid(i))
    for i, genes in (extra or {}).items():
        rows[iid(i)] += genes
    return genomes(rows)


def components(nodes, edges) -> int:
    parent = {x: x for x in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    return len({find(x) for x in nodes})


def backstop(cand):
    return {k for k, src in cand.items() if any(s.startswith("backstop:") for s in src)}


@pytest.mark.parametrize("n", [60, 61, 200, 1000])
@pytest.mark.parametrize("fraction", [0.1, 0.25, 0.5])
def test_campaign_candidates_stay_connected_and_bounded(n, fraction):
    m = max(13, int(fraction * n))
    pop = population(n, m)
    cand = candidates(pop, rarity_for(pop))
    members = [iid(i) for i in range(m)]
    assert components(members, cand) == 1, (n, m)
    extra = backstop(cand)
    bound = m * (m - 1) // 2 if m <= BACKSTOP_FULL_DF else m * BACKSTOP_RING_K * len(IDENTIFIERS)
    assert len(extra) <= bound
    assert all(a in members and b in members for a, b in extra)  # private values never reach the backstop
    if n <= 60:
        assert len(cand) == n * (n - 1) // 2  # exhaustive fallback unchanged


@pytest.mark.parametrize("n,m", [(61, 13), (64, 13), (3000, 300)])
def test_supported_campaign_is_one_component_across_the_cutoffs(n, m):
    """(61, 13): the audit's boundary case; (3000, 300): df above MAX_DF_ABS=200, sparse ring mode."""
    pop = population(n, m)
    assert score_pair(pop[iid(0)], pop[iid(1)], rarity_for(pop)).score.hypothesis == "same_campaign"
    _rarity, _pairs, clustering, _info = score_and_cluster(pop, TH)
    assert len({clustering.membership[iid(i)] for i in range(m)}) == 1


def test_new_variant_of_a_campaign_above_the_absolute_cap_is_assignable():
    pop = population(3000, 300)
    _rarity, _pairs, clustering, _info = score_and_cluster(pop, TH)
    query = build_genome("variant", growth_identity("variant"), {})
    result = assign(query, pop, clustering.campaigns, clustering.groups).assignment
    assert result.decision == "assign"
    assert result.campaign_id == clustering.membership[iid(0)]


def test_per_query_candidates_equal_query_retrieval_in_sparse_mode():
    """candidates_per_query stays exactly the per-incident query retrieval (leave-one-out contract)."""
    pop = population(400, 100)  # max_df 80 < df 100 > BACKSTOP_FULL_DF: round-robin sample per query
    rarity = rarity_for(pop)
    per_query = candidates_per_query(pop, rarity)
    for q in (iid(0), iid(37), iid(99), iid(250)):
        assert per_query[q] == candidates(pop, rarity, queries=[q])
    picked = [k for k, src in per_query[iid(0)].items() if any(s.startswith("backstop:") for s in src)]
    assert 0 < len(picked) <= BACKSTOP_QUERY_CAP * len(IDENTIFIERS)


def test_query_backstop_reaches_every_campaign_holding_the_value():
    """Two campaigns share one operator value held by 2 x 100 incidents; the 64-holder query sample is
    round-robin over campaign units, so both campaigns stay reachable for an assignment query."""
    shared = ("operator_id", "landing_cdn_account:shared-account")
    extra = {i: [gene(iid(i), shared[0], shared[1], "inf:operator:landing_cdn_account")] for i in range(200)}
    pop = population(1000, 0, extra)
    pop["query"] = build_genome("query", [gene("query", shared[0], shared[1], "inf:operator:landing_cdn_account")],
                                {})
    rarity = rarity_for(pop)
    rarity.kit_unit_of = {iid(i): ("C-a" if i < 100 else "C-b") for i in range(200)}
    picked = {b if a == "query" else a for a, b in backstop(candidates(pop, rarity, queries=["query"]))}
    assert len(picked) == BACKSTOP_QUERY_CAP
    assert {rarity.kit_unit_of[h] for h in picked} == {"C-a", "C-b"}


@pytest.mark.parametrize("group", ["inf:kit:css", "inf:boilerplate:cdn", "inf:platform:url_path",
                                   "TXT:operator:contact"])
def test_common_non_operator_values_get_no_backstop(group):
    """Kit, boilerplate and platform tiers, and operator tier outside INF/META, stay dropped above max_df."""
    extra = {i: [gene(iid(i), "shared_value", "common", group, family=group.split(":")[0].upper())]
             for i in range(30)}
    pop = population(61, 0, extra)
    cand = candidates(pop, rarity_for(pop))
    assert not backstop(cand)
    assert not any(pair in cand for pair in combinations(sorted(iid(i) for i in range(30)), 2))


def test_query_scope_reports_capped_backstop_without_values():
    """U03 telemetry: a 200-holder operator value is sampled to BACKSTOP_QUERY_CAP holders for the query; the scope
    reports the truncation by gene kind and holder count, never the value, and changes no candidate."""
    shared = ("operator_id", "landing_cdn_account:shared-account")
    extra = {i: [gene(iid(i), shared[0], shared[1], "inf:operator:landing_cdn_account")] for i in range(200)}
    pop = population(1000, 0, extra)
    pop["query"] = build_genome("query", [gene("query", shared[0], shared[1], "inf:operator:landing_cdn_account")],
                                {})
    rarity = rarity_for(pop)
    before = candidates(pop, rarity, queries=["query"])
    scope = query_scope(pop, rarity, "query")
    assert candidates(pop, rarity, queries=["query"]) == before
    assert scope["mode"] == "indexed" and scope["truncated"] is True
    assert scope["postings_backstop_capped"] == {"operator_id": 1}
    assert scope["holder_slots_not_reached"] == 200 - BACKSTOP_QUERY_CAP
    assert "backstop_query_cap" in scope["bounded_by"]
    assert shared[1] not in repr(scope)


@pytest.mark.parametrize("group,dropped", [("inf:kit:css", True), ("inf:operator:landing_cdn_account", False)])
def test_query_scope_dropped_postings_and_exhaustive_mode(group, dropped):
    extra = {i: [gene(iid(i), "shared_value", "common", group, family=group.split(":")[0].upper())]
             for i in range(30)}
    pop = population(61, 0, extra)
    scope = query_scope(pop, rarity_for(pop), iid(0))
    assert scope["postings_dropped"] == ({"shared_value": 1} if dropped else {})
    assert scope["truncated"] is dropped
    assert scope["postings_backstop_full"] == (0 if dropped else 1)
    small = population(40, 0, {i: extra[i] for i in range(20)})
    s2 = query_scope(small, rarity_for(small), iid(0))
    assert s2["mode"] == "all_pairs" and s2["truncated"] is False and s2["bounded_by"] == []


def test_assignment_explain_carries_retrieval_scope():
    pop = population(61, 13)
    _rarity, _pairs, clustering, _info = score_and_cluster(pop, TH)
    query = build_genome("variant", growth_identity("variant"), {})
    meta = assign(query, pop, clustering.campaigns, clustering.groups).assignment.meta
    assert meta["retrieval_scope"]["mode"] == "indexed"
    assert meta["retrieval_scope"]["corpus_incidents"] == 62
