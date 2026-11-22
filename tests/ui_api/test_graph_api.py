"""Graph explorer API (/v1/graph/*) on the synthetic v2 rows."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import Settings
from ui_api.synthetic import RUN_ID, seed


@pytest.fixture
def client(settings: Settings):
    seed(settings.db_path, settings.cas_dir)
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def empty_client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


def test_explore_empty_state(empty_client):
    r = empty_client.get("/v1/graph/explore")
    assert r.status_code == 200
    assert r.json()["run_id"] is None and r.json()["nodes"] == []


def test_explore_levels_and_independent_excluded(client):
    d = client.get("/v1/graph/explore").json()
    assert d["run_id"] == RUN_ID
    ids = {e["id"] for e in d["edges"]}
    assert "syn-5|syn-6" not in ids  # independent: not a link
    lv = {e["id"]: e["level"] for e in d["edges"]}
    assert lv["syn-1|syn-2"] == "campaign"
    assert lv["syn-3|syn-4"] == "kit"
    assert lv["syn-2|syn-3"] == "weak"
    e = next(e for e in d["edges"] if e["id"] == "syn-1|syn-2")
    assert e["family"] == "INF" and len(e["reasons"]) <= 3
    assert {n["id"] for n in d["nodes"]} >= {"syn-1", "syn-6"}
    n1 = next(n for n in d["nodes"] if n["id"] == "syn-1")
    assert n1["campaign_id"] == "syn-c1" and n1["corpus"] == "synthetic" and n1["thumbnail"]
    assert n1["observed_at"] == "2026-10-01"
    assert "label" not in n1  # ground truth never leaves the API
    st = d["stats"]
    assert st["nodes"] == len(d["nodes"]) and st["edges"] == len(d["edges"])
    assert st["largest_campaign"]["id"] == "syn-c1"
    assert st["components"] >= 2


def test_explore_review_badges(client):
    r = client.post("/v1/reviews", json={"action": "reject_link", "target": {"a": "syn-2", "b": "syn-1"}})
    assert r.status_code == 201
    r = client.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "syn-5", "b": "syn-6"}})
    assert r.status_code == 201
    edges = {e["id"]: e for e in client.get("/v1/graph/explore").json()["edges"]}
    assert edges["syn-1|syn-2"]["review"] == "cannot_link"
    # an accepted pair the scorer called independent is still drawn (must-link)
    if "syn-5|syn-6" in edges:
        assert edges["syn-5|syn-6"]["review"] == "must_link"


def test_path_strongest_and_respects_cannot_link(client):
    d = client.get("/v1/graph/path", params={"a": "syn-2", "b": "syn-4"}).json()
    assert d["found"] is True
    assert [n["id"] for n in d["nodes"]][0] == "syn-2" and d["nodes"][-1]["id"] == "syn-4"
    assert all(s["level"] in ("campaign", "kit", "review") for s in d["steps"])
    assert d["steps"][0]["genes"] is not None
    # syn-6 is only reachable by an independent pair
    d = client.get("/v1/graph/path", params={"a": "syn-1", "b": "syn-6"}).json()
    assert d["found"] is False and d["reason_pl"]
    client.post("/v1/reviews", json={"action": "reject_link", "target": {"a": "syn-1", "b": "syn-2"}})
    d = client.get("/v1/graph/path", params={"a": "syn-1", "b": "syn-2"}).json()
    assert all(s["edge_id"] != "syn-1|syn-2" for s in d["steps"])
    assert client.get("/v1/graph/path", params={"a": "syn-1", "b": "syn-1"}).status_code == 400


def test_indicators_flags(client):
    d = client.get("/v1/graph/indicators").json()
    assert d["run_id"] == RUN_ID
    for it in d["indicators"]:
        assert it["df"] >= 2 and len(it["incidents"]) == it["df"]
        assert isinstance(it["over_correlating"], bool)


def test_export_json_and_cypher(client):
    r = client.post("/v1/graph/export", json={"ids": ["syn-1", "syn-2", "syn-3"], "format": "json"})
    assert r.status_code == 200
    d = json.loads(r.text)
    assert {n["id"] for n in d["nodes"]} == {"syn-1", "syn-2", "syn-3"}
    assert all(e["source"] in {"syn-1", "syn-2", "syn-3"} for e in d["edges"])
    r = client.post("/v1/graph/export", json={"ids": ["syn-1", "syn-2"], "format": "cypher"})
    assert r.status_code == 200
    # same graph model as the full Neo4j export (B19): Incident nodes, LINKED_TO, Gene nodes when shared
    assert "MERGE (n:Incident" in r.text and "LINKED_TO" in r.text and "[x:LINKED]" not in r.text
    assert "self-contained, incidents 2," in r.text and "syn-1" in r.text
    assert client.post("/v1/graph/export", json={"ids": []}).status_code == 422


def test_campaign_summary_rule_based(client):
    d = client.get("/v1/graph/campaigns/syn-c1/summary").json()
    assert d["status"] == "rule_based" and "syn-c1" in d["text_pl"]
    assert d["levels"].get("campaign", 0) >= 1
    assert client.get("/v1/graph/campaigns/nope/summary").status_code == 404


def test_legacy_graph_route_still_served(client):
    assert client.get("/v1/graph").json()["run_id"] == RUN_ID
