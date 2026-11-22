"""Variant lineage per campaign (hydra/lineage.py, GAPS_PAPERS G01) on SYNTHETIC data only.

Chain: v1 -> v2 (new domain) -> v3 (new headline) -> v4 (new person photo); every member keeps a rare tracker and
the kit files. Pairs carry reasons built from the shared values, like the scorer's stored `pairs.detail`.
"""

from __future__ import annotations

import json
import random

import networkx as nx
import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import Settings
from hydra.db import Writer
from hydra.lineage import GeneRow, PairLink, build_lineage, category, edge_diff
from hydra.pipeline import analyse
from tests.core.test_pipeline import corpus, test_extractors  # noqa: F401 — fixture

BITS = {"tracker_id": 12.0, "asset_sha256": 4.0, "registrable_domain": 9.0, "ad_headline": 6.0, "face": 5.0}


def _member(domain: str, headline: str, face: str, *, tracker: str = "fbq:771234567") -> list[GeneRow]:
    return [
        GeneRow("INF", "registrable_domain", domain, extractor="har_hosts@1", dependency_group="inf:operator:domain"),
        GeneRow("INF", "tracker_id", tracker, extractor="dom_trackers@1", dependency_group="inf:operator:tracker"),
        GeneRow("INF", "asset_sha256", "a" * 64, extractor="dom_assets@1", dependency_group="inf:kit:assets"),
        GeneRow("TXT", "ad_headline", headline, extractor="txt@1"),
        GeneRow("ID", "face", face, extractor="sface@1"),
        GeneRow("VIS", "image_class", "creative", extractor="vis@1"),  # noise kind: never a change
    ]


CHAIN = {
    "v1": ("2026-10-01T09:00:00+00:00", _member("zysk-a.example", "Zarabiaj z energią", "face-A")),
    "v2": ("2026-10-03T09:00:00+00:00", _member("zysk-b.example", "Zarabiaj z energią", "face-A")),
    "v3": ("2026-10-05T09:00:00+02:00", _member("zysk-b.example", "Nowa platforma inwestycyjna", "face-A")),
    "v4": ("2026-10-09", _member("zysk-b.example", "Nowa platforma inwestycyjna", "face-B")),
}


def _pairs(genes: dict[str, list[GeneRow]]) -> list[PairLink]:
    out = []
    ids = sorted(genes)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            va = {(g.kind, g.value) for g in genes[a]}
            reasons = [{"family": g.family, "kind": g.kind, "value_a": g.value, "value_b": g.value,
                        "bits": BITS[g.kind], "explanation_pl": "wspólna wartość"}
                       for g in genes[b] if (g.kind, g.value) in va and g.kind in BITS]
            out.append(PairLink(a=a, b=b, support=sum(r["bits"] for r in reasons), reasons=reasons,
                                hypothesis="same_campaign", total_bits=sum(r["bits"] for r in reasons)))
    return out


def _chain_lineage(shuffle: int | None = None) -> dict:
    genes = {k: v[1] for k, v in CHAIN.items()}
    pairs = _pairs(genes)
    members = list(CHAIN)
    if shuffle is not None:
        rnd = random.Random(shuffle)
        rnd.shuffle(pairs)
        rnd.shuffle(members)
    return build_lineage(members, {k: v[0] for k, v in CHAIN.items()}, pairs, genes,
                         {k: i for i, k in enumerate(["v4", "v3", "v2", "v1"])})


def test_chain_reproduces_observation_order_and_names_the_change():
    lin = _chain_lineage()
    assert lin["direction"] == "observation_order"
    assert [n["incident_id"] for n in lin["nodes"]] == ["v1", "v2", "v3", "v4"]
    assert lin["roots"] == ["v1"]
    assert [(e["parent"], e["child"]) for e in lin["edges"]] == [("v1", "v2"), ("v2", "v3"), ("v3", "v4")]
    changed = [{c["category"] for c in e["changed"]} for e in lin["edges"]]
    assert changed == [{"domain"}, {"text"}, {"person"}]
    for e in lin["edges"]:
        kept = {k["kind"] for k in e["kept"]}
        assert {"tracker_id", "asset_sha256"} <= kept
        assert "image_class" not in {c["kind"] for c in e["changed"] + e["kept"]}
        assert "piksel" in e["summary_pl"] and "pliki zestawu" in e["summary_pl"]
    assert lin["edges"][0]["summary_pl"].startswith("wariant v2 (po v1): nowa domena")
    assert "nowy nagłówek" in lin["edges"][1]["summary_pl"]
    assert "nowa osoba / wizerunek" in lin["edges"][2]["summary_pl"]


def test_deterministic_for_same_input_and_input_order():
    first = json.dumps(_chain_lineage(), sort_keys=True)
    assert json.dumps(_chain_lineage(), sort_keys=True) == first
    for seed in (1, 2, 3):
        assert json.dumps(_chain_lineage(shuffle=seed), sort_keys=True) == first


def test_every_edge_has_kept_evidence_with_positive_bits():
    lin = _chain_lineage()
    assert lin["edges"]
    for e in lin["edges"]:
        assert any((k.get("bits") or 0) > 0 for k in e["kept"]), e
        assert e["bits"] > 0


def test_tree_weight_matches_networkx_maximum_branching():
    genes = {k: v[1] for k, v in CHAIN.items()}
    pairs = _pairs(genes)
    lin = _chain_lineage()
    order = {n["incident_id"]: n["order"] for n in lin["nodes"]}
    g = nx.DiGraph()
    for p in pairs:
        src, dst = sorted((p.a, p.b), key=order.get)
        g.add_edge(src, dst, weight=p.support)
    ref = nx.maximum_branching(g, attr="weight")
    assert sum(e["bits"] for e in lin["edges"]) == pytest.approx(sum(d["weight"] for *_, d in ref.edges(data=True)))


def test_singleton_has_no_edges():
    lin = build_lineage(["s1"], {"s1": None}, [], {"s1": CHAIN["v1"][1]}, {"s1": 1})
    assert lin["edges"] == [] and len(lin["nodes"]) == 1 and lin["nodes"][0]["root"] is True


def test_no_dates_falls_back_to_import_order_and_says_so():
    genes = {k: v[1] for k, v in CHAIN.items()}
    lin = build_lineage(list(genes), {}, _pairs(genes), genes, {"v1": 10, "v2": 11, "v3": 12, "v4": 13})
    assert lin["direction"] == "import_order" and "kolejność importu" in lin["direction_pl"]
    assert [n["incident_id"] for n in lin["nodes"]] == ["v1", "v2", "v3", "v4"]
    und = build_lineage(list(genes), {}, _pairs(genes), genes)
    assert und["direction"] == "undirected" and len(und["edges"]) == 3


def test_mixed_dates_and_isolated_member_is_a_root():
    genes = {k: v[1] for k, v in CHAIN.items()}
    genes["z"] = _member("other.example", "Inny", "face-Z", tracker="fbq:1")
    lin = build_lineage(list(genes), {"v1": "2026-10-01", "v2": "2026-10-02"}, _pairs(genes), genes,
                        {"v1": 1, "v2": 2, "v3": 3, "v4": 4, "z": 5})
    assert lin["direction"] == "mixed"
    # z shares only kit files (4 bits): still scored, so it attaches; with no pair at all it would be a root
    alone = build_lineage(["v1", "q"], {}, [], {"v1": genes["v1"], "q": []}, {"v1": 1, "q": 2})
    assert alone["roots"] == ["v1", "q"] and alone["edges"] == []
    assert lin["nodes"][-1]["incident_id"] == "z"


def test_unknown_is_not_a_change():
    parent = [GeneRow("INF", "tracker_id", "px", extractor="dom_trackers@1"),
              GeneRow("TXT", "entity_phone", None, status="unavailable", extractor="txt_entities@1")]
    child = [GeneRow("INF", "tracker_id", "px", extractor="dom_trackers@1"),
             GeneRow("TXT", "entity_phone", "+48700100100", extractor="txt_entities@1"),
             GeneRow("ID", "face", "f1", extractor="sface@1"),
             GeneRow("INF", "registrable_domain", "new.example", extractor="dom_trackers@1")]
    d = edge_diff(parent, child, [{"family": "INF", "kind": "tracker_id", "bits": 10.0}])
    assert {u["kind"] for u in d["unknown"]} == {"entity_phone", "face"}
    assert [a["kind"] for a in d["added"]] == ["registrable_domain"]  # same extractor observed the parent
    assert d["changed"] == [] and d["removed"] == []


def test_categories():
    assert category("INF", "tracker_id") == "operator"
    assert category("INF", "landing_host", "inf:operator:domain") == "domain"
    assert category("INF", "js_build_hash") == "kit"
    assert category("TXT", "entity_phone") == "contact"
    assert category("VIS", "pdq") == "creative"
    assert category("NAR", "scheme") == "narrative"


# --------------------------------------------------------------------------------------- API on an analysed run


@pytest.fixture
def client(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    w = Writer(settings.db_path)
    try:
        w.run(lambda c: c.execute("UPDATE incidents SET observed_at=CASE id WHEN 'nask-3' THEN '2026-10-01'"
                                  " WHEN 'nask-1' THEN '2026-10-02' WHEN 'nask-2' THEN '2026-10-04' END"
                                  " WHERE id IN ('nask-1','nask-2','nask-3')"))
    finally:
        w.close()
    with TestClient(create_app(settings)) as c:
        yield c, res


def test_api_lineage_route(client):
    c, res = client
    camp = next(x for x in res.clustering.campaigns if "nask-1" in x.members)
    r = c.get(f"/v1/campaigns/{camp.id}/lineage")
    assert r.status_code == 200
    d = r.json()
    assert d["campaign_id"] == camp.id and d["run_id"] == res.run_id
    assert d["direction"] == "observation_order"
    assert [n["incident_id"] for n in d["nodes"]] == ["nask-3", "nask-1", "nask-2"]
    assert d["nodes"][0]["root"] and len(d["edges"]) == 2
    for e in d["edges"]:
        assert e["bits"] > 0 and any((k.get("bits") or 0) > 0 for k in e["kept"])
        assert e["summary_pl"].startswith(f"wariant {e['child']} (po ")
        assert set(e) >= {"parent", "child", "bits", "kept", "changed", "added", "removed", "summary_pl"}
    assert "kolejność obserwacji" in d["note_pl"]
    again = c.get(f"/v1/campaigns/{camp.id}/lineage", params={"run_id": res.run_id}).json()
    assert again == d
    single = next(x for x in res.clustering.campaigns if x.members == ["nask-6"])
    s = c.get(f"/v1/campaigns/{single.id}/lineage").json()
    assert s["edges"] == [] and len(s["nodes"]) == 1 and s["nodes"][0]["root"]
    assert c.get("/v1/campaigns/nope/lineage").status_code == 404
    assert c.get(f"/v1/campaigns/{camp.id}/lineage", params={"run_id": "no-such-run"}).status_code == 404
