"""KONTRA K1: real re-scoring scenarios, flip set, next observation, package, receiver, API (synthetic only)."""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from hydra.db import connect
from hydra.kontra import canonical_view, freeze, rescore, run_experiment
from hydra.kontra_package import build_package, is_loopback_url, save_package, send_package
from hydra.kontra_receiver import create_app as receiver_app
from hydra.pipeline import assign_stored


def _db_snapshot(settings):
    with connect(settings.db_path, readonly=True) as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("genes", "campaigns", "assignments", "runs", "pairs")}


def test_baseline_rescore_equals_product_path(kontra_state):
    s = kontra_state["settings"]
    for key in ("assign", "kit", "none"):
        x = kontra_state[key]
        ctx = freeze(x.incident_id, s)
        mine = rescore(ctx).assignment
        prod = assign_stored(x.incident_id, s, base_run=ctx.base_run).assignment
        assert (mine.decision, mine.campaign_id, mine.group_id) == (prod.decision, prod.campaign_id, prod.group_id)
        assert (mine.decision, mine.campaign_id, mine.group_id) == (x.decision, x.campaign_id, x.group_id)
        assert [(c.campaign_id, c.best_pair.total_bits) for c in mine.candidates] == \
            [(c.campaign_id, c.best_pair.total_bits) for c in prod.candidates]
        assert mine.meta["margin_bits"] == prod.meta["margin_bits"]


def test_experiment_is_shadow_and_reproducible(kontra_state):
    s = kontra_state["settings"]
    inc = kontra_state["assign"].incident_id
    before = _db_snapshot(s)
    e1 = run_experiment(inc, s)
    e2 = run_experiment(inc, s)
    assert _db_snapshot(s) == before  # nothing written to the analysis
    assert e1["experiment_id"] == e2["experiment_id"]
    assert canonical_view(e1) == canonical_view(e2)
    assert e1["mode"] == "shadow" and e1["baseline"]["matches_stored"] is True
    assert e1["stats"]["recomputations"] == len(e1["scenarios"])  # one real re-score per scenario row


def test_removing_the_decisive_group_flips_and_other_groups_do_not(kontra_state):
    s = kontra_state["settings"]
    exp = run_experiment(kontra_state["assign"].incident_id, s)
    assert exp["baseline"]["decision"] == "assign"
    rows = {r["scenario_id"]: r for r in exp["scenarios"]}
    tracker = rows["drop-dependency_group:INF:operator:tracker"]
    assert tracker["class"] == "observation_uncertainty" and tracker["decision_changed"]
    assert not rows["drop-dependency_group:TXT:entity_phone"]["decision_changed"]
    fam = rows["drop-family:INF"]
    assert fam["class"] == "diagnostic_stress" and fam["decision"] != "assign"
    # the flip set is a group whose removal (re-scored) changes the decision
    fs = exp["flip_set"]
    assert fs["found"] and fs["groups"] == ["dependency_group:INF:operator:tracker"]
    assert "nie globalnie najmniejszy" in fs["note_pl"]
    # removal re-scores: the support after removal is not "baseline minus the reason's bits" by construction,
    # it is whatever the product path returns
    ctx = freeze(kontra_state["assign"].incident_id, s)
    idx = frozenset(i for i, g in enumerate(ctx.genome.genes) if g.dependency_group == "INF:operator:tracker")
    assert str(rescore(ctx, drop=idx).assignment.decision) == tracker["decision"]
    comp = rows[f"without-campaign:{exp['baseline']['campaign_id']}"]
    assert comp["class"] == "competitor_check" and comp["campaign_id"] != exp["baseline"]["campaign_id"]
    assert exp["limits"]["retrieval_complete"] is None
    assert exp["retrieval_scope"] is not None


def test_hypotheses_and_next_observation_are_labelled(kontra_state):
    exp = run_experiment(kontra_state["kit"].incident_id, kontra_state["settings"])
    kinds = {h["kind"] for h in exp["hypotheses"]}
    assert {"campaign", "shared_kit", "new_pattern"} <= kinds
    assert exp["baseline"]["winner"].startswith("shared_kit")
    nxt = exp["next_observations"]
    assert nxt["heuristic"] is True and len(nxt["pair"]) == 2 and nxt["ranking"]
    ranked = {r["action"] for r in nxt["ranking"]}
    assert ranked >= {"read_dom_fragment", "ask_analyst_pair"}
    # the synthetic item has a saved page but no HAR and no image: those actions are listed, never ranked (§7)
    missing = {r["action"]: r for r in nxt["unavailable"]}
    assert set(missing) == {"reread_har", "ocr_region", "same_asset_geometry"} and not ranked & set(missing)
    assert all(r["status"] == "artefact_missing" for r in missing.values())
    assert all(r["status"] == "requires_input" for r in nxt["ranking"])
    assert nxt["label_pl"] == "rekomendacja heurystyczna, bez wykazanej przewagi"


def test_package_is_redacted_content_addressed_and_static(kontra_state):
    s = kontra_state["settings"]
    exp = run_experiment(kontra_state["assign"].incident_id, s)
    exp["baseline"]["reason_pl"] += " https://x.example/?token=SECRET123456789"
    p1, p2 = build_package(exp), build_package(exp)
    assert p1["package_id"] == p2["package_id"] and p1["content_sha256"] == p2["content_sha256"]
    assert "SECRET123456789" not in json.dumps(p1)
    assert "do weryfikacji" in p1["assessment_pl"]
    out = save_package(s, exp)
    html = (s.state_dir / "kontra" / "packages" / out["package_id"] / "report.html").read_text()
    assert "<script" not in html.lower() and "Content-Security-Policy" in html
    assert out["files"]["html"].endswith("/report.html")


def test_receiver_is_idempotent_and_validates(tmp_path):
    c = TestClient(receiver_app(tmp_path / "rcv"))
    pkg = build_package(_tiny_experiment())
    a1 = c.post("/receive", json=pkg).json()
    a2 = c.post("/receive", json=pkg).json()
    assert a1["duplicate"] is False and a2["duplicate"] is True
    assert a1["receipt_id"] == a2["receipt_id"] and a1["synthetic_case_id"].startswith("SYN-CASE-")
    assert c.get("/receipts").json()["count"] == 1
    bad = {**pkg, "assessment_pl": "zmieniona treść"}
    assert c.post("/receive", json=bad).status_code == 422
    assert c.get("/receipts").json()["count"] == 1


def test_sender_refuses_non_loopback_and_reports_unavailable():
    assert is_loopback_url("http://127.0.0.1:8765/receive") and is_loopback_url("http://[::1]:9/x")
    assert not is_loopback_url("http://10.0.0.1/receive") and not is_loopback_url("https://127.0.0.1/x")
    pkg = build_package(_tiny_experiment())
    assert send_package(pkg, "http://example.org/receive")["status"] == "rejected"
    assert send_package(pkg, "http://127.0.0.1:9/receive", timeout=1)["status"] == "unavailable"


def test_send_over_real_loopback_socket(tmp_path):
    import uvicorn

    cfg = uvicorn.Config(receiver_app(tmp_path / "rcv"), host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(cfg)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        pkg = build_package(_tiny_experiment())
        url = f"http://127.0.0.1:{port}/receive"
        r1, r2 = send_package(pkg, url), send_package(pkg, url)
        assert r1["status"] == "acknowledged" and r2["status"] == "duplicate"
        assert r1["ack"]["receipt_id"] == r2["ack"]["receipt_id"]
    finally:
        server.should_exit = True
        th.join(timeout=5)


def test_api_lab_flow(kontra_state, monkeypatch):
    from hydra.api import create_app

    s = kontra_state["settings"]
    inc = kontra_state["assign"].incident_id
    monkeypatch.setenv("HYDRA_KONTRA_RECEIVER", "http://127.0.0.1:9/receive")
    with TestClient(create_app(s)) as c:
        assert c.get(f"/v1/lab/{inc}").status_code == 404
        miss = c.get(f"/v1/lab/{inc}?missing=null")
        assert miss.status_code == 200 and miss.json() is None
        assert c.post(f"/v1/lab/{inc}/package", json={}).status_code == 404
        assert c.post("/v1/lab/nope-unknown/run", json={}).status_code == 404
        exp = c.post(f"/v1/lab/{inc}/run", json={}).json()
        assert exp["schema_version"] == "hydra.kontra.v1" and exp["baseline"]["decision"] == "assign"
        assert c.get(f"/v1/lab/{inc}").json()["experiment_id"] == exp["experiment_id"]
        pk = c.post(f"/v1/lab/{inc}/package", json={}).json()
        assert c.get(pk["files"]["json"]).json()["package_id"] == pk["package_id"]
        assert "Pakiet dowodowy" in c.get(pk["files"]["markdown"]).text
        h = c.get(pk["files"]["html"])
        assert h.status_code == 200 and "default-src 'none'" in h.headers["content-security-policy"]
        sent = c.post(f"/v1/lab/packages/{pk['package_id']}/send", json={}).json()
        assert sent["status"] == "unavailable" and sent["ack"] is None
        assert c.get("/v1/lab/packages/..%2Fx").status_code in (400, 404)
        assert c.get("/v1/lab/receiver").json()["loopback"] is True
    with connect(s.db_path, readonly=True) as conn:
        types = {r[0] for r in conn.execute("SELECT type FROM events WHERE type LIKE 'KONTRA_%'")}
    assert types == {"KONTRA_EXPERIMENT", "KONTRA_PACKAGE", "KONTRA_DELIVERY"}


def _tiny_experiment() -> dict:
    return {"experiment_id": "kx-test", "schema_version": "hydra.kontra.v1", "incident_id": "syn-x", "mode": "shadow",
            "context": {"base_run": "r", "fingerprint": "f", "corpus_signature": "c", "scorer": {},
                        "product_commit": None},
            "baseline": {"decision": "assign", "campaign_id": "C-1", "group_id": None, "reason_pl": "test",
                         "support_bits": 12.0, "link_threshold": 10.0, "margin_bits": 12.0, "link_rule_met": True,
                         "winner": "campaign:C-1"},
            "hypotheses": [], "scenarios": [], "flip_set": None, "next_observations": None,
            "retrieval_scope": None, "limits": None}


@pytest.mark.parametrize("bad", ["../etc", "a/b", ""])
def test_valid_id_rejects_paths(bad):
    from hydra.kontra import valid_id

    assert not valid_id(bad)
