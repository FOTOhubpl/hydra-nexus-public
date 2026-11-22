"""ADV-CAMP on SYNTHETIC data only: spec M6 consistency gate (hydra/consistency.py) and the M7 variant tree by
mutation distance (hydra/lineage_mutation.py)."""

from __future__ import annotations

import itertools
import json

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.consistency import PairView, apply_gate, consistency_report, gate_split, merge_check
from hydra.lineage import GeneRow, PairLink
from hydra.lineage_mutation import mutation_tree, mutations
from hydra.pipeline import analyse
from tests.core.test_lineage import CHAIN, _pairs
from tests.core.test_pipeline import corpus, test_extractors  # noqa: F401 — fixture

LINK, LOW = 10.0, 4.0


def _pv(s: float, linked: bool | None = None, contra: bool = False) -> PairView:
    hard = [{"kind": "scheme", "bits": -4.0, "explanation_pl": "Sprzeczność"}] if contra else []
    return PairView(support=s, linked=s >= LINK if linked is None else linked, contradictions=hard,
                    reasons=[{"kind": "tracker_id", "bits": s, "explanation_pl": f"wspólny piksel {s}"}])


def _graph(pairs: dict[tuple[str, str], PairView]) -> nx.Graph:
    g = nx.Graph()
    for (a, b), p in pairs.items():
        g.add_node(a)
        g.add_node(b)
        if p.linked:
            g.add_edge(a, b, bits=p.support, weight=p.support)
    return g


# --------------------------------------------------------------------------------------- gate


def test_chain_a_b_c_is_split_by_the_gate():
    # A~B and B~C link at 12 bits, A and C share nothing (scored 0): spec "łańcuch A~B~C"
    pairs = {("a", "b"): _pv(13.0), ("b", "c"): _pv(12.0), ("a", "c"): _pv(0.0)}
    g = _graph(pairs)
    splits = apply_gate(g, pairs, LINK, LOW)
    assert len(splits) == 1 and splits[0]["parts"] == [["a", "b"], ["c"]]
    assert sorted(map(sorted, nx.connected_components(g))) == [["a", "b"], ["c"]]


def test_triangle_and_strong_single_bridge_stay_together():
    tri = {("a", "b"): _pv(12.0), ("b", "c"): _pv(11.0), ("a", "c"): _pv(10.5)}
    assert apply_gate(_graph(tri), tri, LINK, LOW) == []
    # d joins the triangle through one pair of 25 bits >= 2 x T_link: a strong single bridge, kept with its reason
    strong = {**tri, ("c", "d"): _pv(25.0), ("a", "d"): _pv(5.0), ("b", "d"): _pv(5.0)}
    assert apply_gate(_graph(strong), strong, LINK, LOW) == []
    rep = consistency_report(["a", "b", "c", "d"], strong, LINK, LOW)
    d_row = next(r for r in rep["members"] if r["id"] == "d")
    assert d_row["status"] == "strong_single" and "2 x próg" in d_row["reason_pl"]
    assert rep["strong_single_members"] == ["d"] and rep["verdict"] == "consistent"
    # the same bridge at 12 bits holds d by a single weak pair: the gate detaches it
    weak = {**strong, ("c", "d"): _pv(12.0)}
    rep = consistency_report(["a", "b", "c", "d"], weak, LINK, LOW)
    assert rep["single_pair_members"] == ["d"] and rep["gate_parts"] == [["a", "b", "c"], ["d"]]


def test_contradiction_vetoes_a_merge():
    # c is linked to a and b, but c-a carries a hard scheme contradiction
    pairs = {("a", "b"): _pv(15.0), ("b", "c"): _pv(12.0), ("a", "c"): _pv(11.0, linked=False, contra=True)}
    parts = gate_split(["a", "b", "c"], [(u, v, p.support, False) for (u, v), p in pairs.items() if p.linked],
                       pairs, LINK, LOW)
    assert [sorted(p) for p in parts] == [["a", "b"], ["c"]]
    rep = consistency_report(["a", "b", "c"], pairs, LINK, LOW)
    assert rep["contradiction_pairs"] == [["a", "c"]] and rep["verdict"] == "gate_would_split"


def test_median_condition_and_unscored_pairs_are_unknown():
    a, b = {"a1", "a2"}, {"b1", "b2"}
    low = {("a1", "a2"): _pv(20.0), ("b1", "b2"): _pv(20.0), ("a1", "b1"): _pv(11.0), ("a2", "b2"): _pv(11.0),
           ("a1", "b2"): _pv(0.0), ("a2", "b1"): _pv(0.0)}
    chk = merge_check(a, b, low, LINK, LOW)
    assert chk["supporting_pairs"] == 2 and chk["median_bits"] == 5.5 and chk["pass"]
    low[("a1", "b2")] = _pv(-4.0, linked=False)
    low[("a2", "b1")] = _pv(-4.0, linked=False)
    assert not merge_check(a, b, low, LINK, LOW)["pass"]
    unscored = {k: v for k, v in low.items() if v.support > 0}
    assert merge_check(a, b, unscored, LINK, LOW)["median_bits"] == 11.0  # unscored: left out, not 0


def test_two_member_campaign_and_must_links_are_not_split():
    pair = {("a", "b"): _pv(10.5)}
    assert apply_gate(_graph(pair), pair, LINK, LOW) == []
    assert consistency_report(["a", "b"], pair, LINK, LOW)["verdict"] == "pair_campaign"
    pairs = {("a", "b"): _pv(13.0), ("b", "c"): _pv(12.0), ("a", "c"): _pv(0.0)}
    g = _graph(pairs)
    g.add_edge("a", "c", weight=1e6, must=True)
    assert apply_gate(g, pairs, LINK, LOW) == []


def test_report_is_deterministic_and_names_weakest_link():
    pairs = {("a", "b"): _pv(12.0), ("b", "c"): _pv(11.0), ("a", "c"): _pv(10.5), ("c", "d"): _pv(10.2),
             ("a", "d"): _pv(1.0)}
    r1 = consistency_report(["d", "c", "b", "a"], pairs, LINK, LOW)
    r2 = consistency_report(["a", "b", "c", "d"], dict(reversed(list(pairs.items()))), LINK, LOW)
    assert json.dumps(r1, sort_keys=True) == json.dumps(r2, sort_keys=True)
    assert r1["single_pair_members"] == ["d"] and r1["verdict"] == "gate_would_split"
    assert r1["gate_parts"] == [["a", "b", "c"], ["d"]]
    assert r1["weakest_link_bits"] == 10.2 and r1["cohesion"] is not None


# --------------------------------------------------------------------------------------- mutation tree


def test_mutation_tree_follows_the_chain_and_names_mutations():
    genes = {k: v[1] for k, v in CHAIN.items()}
    t = mutation_tree(list(CHAIN), {k: v[0] for k, v in CHAIN.items()}, _pairs(genes), genes)
    assert t["root"] == "v1" and t["spanning"] and t["roots"] == ["v1"]
    assert [(e["parent"], e["child"]) for e in t["edges"]] == [("v1", "v2"), ("v2", "v3"), ("v3", "v4")]
    types = [{m["type"] for m in e["mutations"]} for e in t["edges"]]
    assert types == [{"domain_rotation"}, {"text_edit"}, {"changed"}]
    assert "rotacja domeny (zysk-a.example → zysk-b.example)" in t["edges"][0]["summary_pl"]
    assert t["edges"][2]["mutations"][0]["category"] == "person"


def test_mutation_tree_is_the_minimum_arborescence_by_brute_force():
    genes = {k: v[1] for k, v in CHAIN.items()}
    obs = {k: v[0] for k, v in CHAIN.items()}
    pairs = _pairs(genes)
    t = mutation_tree(list(CHAIN), obs, pairs, genes)
    # recompute every edge cost from the tree's own params and brute-force all parent assignments
    s = {tuple(sorted((p.a, p.b))): p.support for p in pairs}
    s_max = max(s.values())
    order = ["v1", "v2", "v3", "v4"]

    def cost(u: str, v: str) -> float:
        dpen = 1.0 if order.index(u) > order.index(v) else 0.0
        return mutations(genes[u], genes[v])[0] + 2.0 * dpen + 0.5 * (s_max - s[tuple(sorted((u, v)))]) / s_max

    best = None
    for choice in itertools.product(order, repeat=3):
        par = dict(zip(order[1:], choice, strict=True))
        if any(par[c] == c for c in par):
            continue
        ok = True
        for c in par:  # acyclic: every node reaches the root
            seen, x = set(), c
            while x != "v1" and x not in seen:
                seen.add(x)
                x = par[x]
            ok &= x == "v1"
        if ok:
            total = sum(cost(par[c], c) for c in par)
            best = total if best is None else min(best, total)
    assert t["total_cost"] == pytest.approx(best, abs=1e-3)


def test_image_transform_vs_new_creative_and_unknown_is_free():
    near = "0" * 63 + "f"  # 4 bits from all-zero PDQ
    far = "f" * 64
    p = [GeneRow("VIS", "pdq", "0" * 64, extractor="pdq@1")]
    d1, m1 = mutations(p, [GeneRow("VIS", "pdq", near, extractor="pdq@1")])
    d2, m2 = mutations(p, [GeneRow("VIS", "pdq", far, extractor="pdq@1")])
    assert m1[0]["type"] == "image_transform" and m1[0]["hamming_bits"] == 4 and d1 < d2
    assert m2[0]["type"] == "new_creative"
    d3, m3 = mutations(p, p + [GeneRow("ID", "face", "f1", extractor="sface@1")])
    assert d3 == 0.0 and m3 == []  # face extractor never ran on the parent: unknown, not a mutation


def test_mutation_tree_root_is_earliest_even_when_shuffled():
    genes = {k: v[1] for k, v in CHAIN.items()}
    obs = {k: v[0] for k, v in CHAIN.items()}
    a = mutation_tree(["v4", "v2", "v1", "v3"], obs, list(reversed(_pairs(genes))), genes)
    b = mutation_tree(list(CHAIN), obs, _pairs(genes), genes)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    single = mutation_tree(["v1"], obs, [], genes)
    assert single["edges"] == []
    assert mutation_tree(["x", "y"], {}, [PairLink("x", "y", 0.0)], {})["edges"][0]["mutation_distance"] == 0.0


# --------------------------------------------------------------------------------------- API + nexus hook


@pytest.fixture
def analysed(settings, make_png, tmp_path, test_extractors):  # noqa: F811
    return analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)


def test_api_consistency_and_lineage_flag(settings, analysed, monkeypatch):
    res = analysed
    camp = next(x for x in res.clustering.campaigns if "nask-1" in x.members)
    with TestClient(create_app(settings)) as c:
        r = c.get(f"/v1/campaigns/{camp.id}/consistency")
        assert r.status_code == 200
        d = r.json()
        assert d["campaign_id"] == camp.id and d["applicable"] and d["method"] == "consistency_gate@1"
        assert {"cohesion", "weakest_link_bits", "single_pair_members", "members", "verdict"} <= set(d)
        assert c.get("/v1/campaigns/C-nope/consistency").status_code == 404
        monkeypatch.delenv("HYDRA_ADV_LINEAGE", raising=False)
        off = c.get(f"/v1/campaigns/{camp.id}/lineage").json()
        assert "mutation_tree" not in off
        monkeypatch.setenv("HYDRA_ADV_LINEAGE", "1")
        on = c.get(f"/v1/campaigns/{camp.id}/lineage").json()
        assert on["mutation_tree"]["root"] == on["nodes"][0]["incident_id"]
        assert len(on["mutation_tree"]["edges"]) == len(camp.members) - 1
        assert {k: v for k, v in on.items() if k != "mutation_tree"} == off


def test_gate_flag_off_leaves_partition_and_flag_on_runs(settings, make_png, tmp_path, test_extractors,  # noqa: F811
                                                         monkeypatch):
    monkeypatch.delenv("HYDRA_ADV_GATE", raising=False)
    off = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    monkeypatch.setenv("HYDRA_ADV_GATE", "1")
    on = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    assert sorted(c.members for c in off.clustering.campaigns) == sorted(c.members for c in on.clustering.campaigns)
