"""ADOPT #7 (member / campaign support, bridges, stability) and #14 (unsure, queue, cannot-link bridge abstain),
plus the similar-cases card. Synthetic data only."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import dataclass, field

import networkx as nx
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.assign import analyse_input
from hydra.campaign_support import member_support, rank_campaigns
from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.events import verify_chain
from hydra.pipeline import analyse, latest_run, load_clustering
from hydra.review import apply_review, mark_unsure, new_review, undo_unsure, unsure_marks
from tests.core.test_pipeline import PX, corpus, page, test_extractors  # noqa: F401 — fixture
from tests.core.test_review import campaigns_of, review


@dataclass
class PE:
    support: float
    campaign_by_family: dict[str, float] = field(default_factory=dict)
    robustness: dict | None = None


def graph(edges: dict[tuple[str, str], PE], must: tuple[tuple[str, str], ...] = ()) -> nx.Graph:
    g = nx.Graph()
    for (a, b), pe in edges.items():
        g.add_edge(a, b, bits=pe.support)
    for a, b in must:
        g.add_edge(a, b, must=True)
    return g


# --------------------------------------------------------------------------------------- ADOPT #7 acceptance


def test_chain_has_two_bridges_and_the_middle_member_is_strongest():
    edges = {("A", "B"): PE(14.0, {"INF": 14.0}), ("B", "C"): PE(12.0, {"TXT": 12.0})}
    s = member_support(["A", "B", "C"], graph(edges), edges, 10.0)
    assert s["calibrated"] is False and s["kind"] == "heuristic_evidence_bits"
    assert {(b["a"], b["b"]) for b in s["bridges"]} == {("A", "B"), ("B", "C")}
    assert all(b["split_sizes"] == [1, 2] for b in s["bridges"])
    assert s["articulation_points"] == ["B"]
    idx = {m["id"]: m for m in s["members"]}
    assert idx["B"]["index"] > idx["A"]["index"] and idx["B"]["index"] > idx["C"]["index"]
    assert idx["B"]["bits_to_rest"] == 26.0 and idx["B"]["n_families"] == 2
    assert idx["C"]["anchor_bits"] == 12.0 and s["weakest_member"] == "C"
    assert s["density"] == round(2 / 3, 4) and s["weakest_link_bits"] == 12.0 and s["median_link_bits"] == 13.0
    # without INF the A-B link falls (no robustness stored: support - INF bits < link), A detaches
    assert s["stability"]["families"]["INF"] == {"parts": 2, "largest_share": round(2 / 3, 4), "detached": ["A"]}
    assert s["stability"]["fragile_families"] == ["INF", "TXT"] and s["stability"]["stable"] is False


def test_four_clique_has_density_one_and_no_bridges():
    ids = ["A", "B", "C", "D"]
    edges = {(a, b): PE(20.0, {"INF": 10.0, "TXT": 10.0},
                        {"families": {"INF": {"still_linked": True}, "TXT": {"still_linked": True}}})
             for i, a in enumerate(ids) for b in ids[i + 1:]}
    s = member_support(ids, graph(edges), edges, 10.0)
    assert s["density"] == 1.0 and s["bridges"] == [] and s["articulation_points"] == []
    assert s["cohesion"] == 1.0 and s["stability"]["stable"] is True


def test_missing_pair_counts_as_zero_support_and_must_link_as_analyst():
    edges = {("A", "B"): PE(10.0, {"INF": 10.0})}
    s = member_support(["A", "B", "C"], graph(edges, must=(("B", "C"),)), edges, 10.0)
    c = next(m for m in s["members"] if m["id"] == "C")
    assert c["analyst_link"] and c["anchor_bits"] is None and c["bits_to_rest"] == 0.0
    a = next(m for m in s["members"] if m["id"] == "A")
    assert a["index"] < 0.5  # one half-strength link, one missing pair
    assert next(b for b in s["bridges"] if b["a"] == "B")["bits"] is None  # analyst link: no bits


def test_rank_orders_multi_member_campaigns_and_skips_singletons():
    e1 = {("A", "B"): PE(30.0)}
    e2 = {("C", "D"): PE(12.0)}
    sup = {"c1": member_support(["A", "B"], graph(e1), e1, 10.0), "c2": member_support(["C", "D"], graph(e2), e2, 10.0),
           "c3": member_support(["E"], nx.Graph(), {}, 10.0)}
    rank_campaigns(sup)
    assert (sup["c1"]["rank"], sup["c2"]["rank"], sup["c3"]["rank"]) == (1, 2, None)
    assert sup["c3"]["singleton"] and sup["c1"]["rank_of"] == 2
    assert sup["c1"]["bridges"] == []  # a two-member campaign is one link, judged by its bits


def test_analyse_stores_support_in_campaign_meta(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    camps = {tuple(c.members): c for c in res.clustering.campaigns}
    s = camps[("nask-1", "nask-2", "nask-3")].meta["support"]
    assert s["density"] == 1.0 and s["bridges"] == [] and s["calibrated"] is False
    assert s["median_link_bits"] >= 10 and s["rank"] in (1, 2) and s["rank_of"] == 2
    assert camps[("nask-6",)].meta["support"]["singleton"]
    group = res.clustering.group_meta[camps[("nask-1", "nask-2", "nask-3")].group_id]
    assert group["evidence_bits"] > 0


# --------------------------------------------------------------------------------------- ADOPT #14


def _split_3(settings: Settings) -> str:
    a = campaigns_of(settings)[frozenset({"nask-1", "nask-2", "nask-3"})]
    return review(settings, "split_campaign", {"campaign_id": a.id, "incident_ids": ["nask-3"]})


def test_new_incident_bridging_a_cannot_link_abstains(settings: Settings, make_png, tmp_path,
                                                       test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    rid = _split_3(settings)
    cs = campaigns_of(settings, rid)
    assert frozenset({"nask-3"}) in cs and frozenset({"nask-1", "nask-2"}) in cs
    page(tmp_path / "C", "evil-a.example", "+48 700 100 100" + PX)
    out = analyse_input("file", tmp_path / "C", settings, only=test_extractors)
    a = out["assignment"]
    assert a["decision"] == "abstain", a["reason_pl"]
    bridge = a["meta"]["bridge_cannot_link"]
    assert bridge["reason"] == "bridges cannot-link"
    assert set(bridge["campaigns"]) == {cs[frozenset({"nask-3"})].id, cs[frozenset({"nask-1", "nask-2"})].id}
    assert "rozdzielił" in a["reason_pl"]


def test_cannot_link_makes_dependent_assignment_abstain_and_undo_restores(settings: Settings, make_png, tmp_path,
                                                                          test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    page(tmp_path / "C", "evil-a.example", "+48 700 100 100" + PX)
    out = analyse_input("file", tmp_path / "C", settings, only=test_extractors)
    inc = out["incident_id"]
    assert out["assignment"]["decision"] == "assign"
    a_id = campaigns_of(settings)[frozenset({"nask-1", "nask-2", "nask-3"})].id
    w = Writer(settings.db_path)
    try:
        split = new_review("split_campaign", {"campaign_id": a_id, "incident_ids": ["nask-3"]})
        r1 = apply_review(split, settings, w)
        with closing(connect(settings.db_path, readonly=True)) as conn:
            row = conn.execute("SELECT decision, detail FROM assignments WHERE run_id=? AND incident_id=?",
                               (r1, inc)).fetchone()
            assert row["decision"] == "abstain"
            assert json.loads(row["detail"])["meta"]["bridge_cannot_link"]["previous_decision"] == "assign"
            assert conn.execute("SELECT COUNT(*) FROM events WHERE type='ASSIGNMENT_BRIDGE_ABSTAIN'").fetchone()[0] == 1
            assert json.loads(conn.execute("SELECT metrics FROM runs WHERE id=?", (r1,)).fetchone()[0]
                              )["bridge_abstains"] == 1
        r2 = apply_review(new_review("undo", {}, undoes=split.id), settings, w)
        with closing(connect(settings.db_path, readonly=True)) as conn:
            row = conn.execute("SELECT decision, campaign_id FROM assignments WHERE run_id=? AND incident_id=?",
                               (r2, inc)).fetchone()
            assert row["decision"] == "assign" and row["campaign_id"] == a_id
            assert verify_chain(conn)[0]
    finally:
        w.close()


def test_unsure_mark_leaves_campaigns_and_leaves_the_queue(settings: Settings, make_png, tmp_path,
                                                           test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    before = set(campaigns_of(settings))
    with closing(connect(settings.db_path, readonly=True)) as conn:
        run_before = latest_run(conn)
    w = Writer(settings.db_path)
    try:
        mark, h = mark_unsure(w, {"a": "nask-2", "b": "nask-1"}, note="nie wiem")
        assert mark["target"] == {"a": "nask-1", "b": "nask-2"} and h
        with closing(connect(settings.db_path, readonly=True)) as conn:
            assert [m["id"] for m in unsure_marks(conn)] == [mark["id"]]
            assert latest_run(conn) == run_before  # no revision
        assert set(campaigns_of(settings)) == before
        undo_unsure(w, mark["id"])
        with closing(connect(settings.db_path, readonly=True)) as conn:
            assert unsure_marks(conn) == []
            assert verify_chain(conn)[0]
    finally:
        w.close()


def test_queue_api_orders_by_value_and_hides_unsure_and_decided(settings: Settings, make_png, tmp_path,
                                                                test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    rid = _split_3(settings)
    page(tmp_path / "C", "evil-a.example", "+48 700 100 100" + PX)
    inc = analyse_input("file", tmp_path / "C", settings, only=test_extractors)["incident_id"]
    with TestClient(create_app(settings)) as c:
        q = c.get("/v1/review/queue").json()
        assert q["run_id"] == rid and q["threshold_bits"] == 10.0
        evs = [it["ev"] for it in q["items"]]
        assert evs == sorted(evs, reverse=True)
        item = next(it for it in q["items"] if it["incident_id"] == inc)
        assert item["kinds"] == ["abstain", "bridge"] and len(item["candidates"]) == 2
        assert all(cand["top_reasons"] for cand in item["candidates"])  # three-column card: both sides
        # the split's cannot-link pairs are decided between the two campaigns: never re-suggested
        assert not any(it["key"] in ("nask-1|nask-3", "nask-2|nask-3") for it in q["items"])
        r = c.post("/v1/review/unsure", json={"target": {"incident_id": inc}, "note": "sprawdzić"})
        assert r.status_code == 201 and r.json()["effect"] == "queue_only"
        assert c.post("/v1/review/unsure", json={"target": {"incident_id": inc}}).status_code == 409
        q2 = c.get("/v1/review/queue").json()
        assert not any(it["incident_id"] == inc for it in q2["items"]) and q2["skipped"]["unsure"] >= 1
        assert q2["unsure"][0]["target"] == {"incident_id": inc}
        assert c.post("/v1/review/unsure/undo", json={"id": r.json()["mark"]["id"]}).status_code == 200
        assert c.post("/v1/review/unsure/undo", json={"id": r.json()["mark"]["id"]}).status_code == 404
        assert any(it["incident_id"] == inc for it in c.get("/v1/review/queue").json()["items"])
        assert c.post("/v1/review/unsure", json={"target": {}}).status_code == 400
        mark = c.post("/v1/review/unsure", json={"target": {"a": "nask-1", "b": "nask-4"}}).json()["mark"]
        pair = c.get("/v1/pairs/nask-1/nask-4").json()
        assert pair["review"] == "unsure"
        assert mark["key"] == "nask-1|nask-4"
    with closing(connect(settings.db_path, readonly=True)) as conn:
        types = [r[0] for r in conn.execute("SELECT type FROM events WHERE type LIKE 'REVIEW_UNSURE%'")]
    assert types == ["REVIEW_UNSURE", "REVIEW_UNSURE_UNDONE", "REVIEW_UNSURE"]


def test_queue_lists_a_near_threshold_bridge_pair(settings: Settings, make_png, tmp_path,
                                                  test_extractors):  # noqa: F811
    from hydra.api_task import _pair_reviews
    from hydra.review_queue import build_queue

    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        run = latest_run(conn)
        camps, _ = load_clustering(conn, run)
        q = build_queue(conn, run, [], _pair_reviews(conn, unsure=False), set())
    b = next(c for c in camps if set(c.members) == {"nask-4", "nask-5"})
    item = next((it for it in q["items"] if it["key"] == "nask-4|nask-5"), None)
    s = b.meta["support"]
    if item is not None:  # 2-member campaign: a single link, listed only when near the threshold
        assert "near_threshold" in item["kinds"] and abs(item["distance_bits"]) <= 3
    assert s["links"] == 1 and s["bridges"] == []


# --------------------------------------------------------------------------------------- similar cases


def test_similar_cases_counts_shared_rare_values(settings: Settings, make_png, tmp_path,
                                                 test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with TestClient(create_app(settings)) as c:
        r = c.get("/v1/incidents/nask-1/similar", params={"k": 5}).json()
        assert r["total_values"] >= 4 and r["calibrated"] is False
        top = r["items"][:2]
        assert {t["incident_id"] for t in top} == {"nask-2", "nask-3"}
        t2 = next(t for t in r["items"] if t["incident_id"] == "nask-2")
        kinds = set(t2["per_kind"])
        assert {"host", "entity_phone", "tracker_id", "operator_id"} <= kinds
        assert t2["shared"] == sum(t2["per_kind"].values()) and t2["total"] == r["total_values"]
        assert t2["shared_operator"] >= 2 and t2["same_campaign"] and t2["reasons_pl"]
        assert all(v["df"] <= r["max_df"] for v in t2["values"])
        lone = c.get("/v1/incidents/nask-6/similar").json()
        assert lone["items"] == [] and lone["empty_reason_pl"]
        assert c.get("/v1/incidents/nope/similar").status_code == 404
