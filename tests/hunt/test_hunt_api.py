"""HUNT API on a published revision: plan, issue, later material, verify, next action, metrics, audit chain."""

from __future__ import annotations

from contextlib import closing

from fastapi.testclient import TestClient

from hydra import events
from hydra.api import create_app
from hydra.assign import analyse_input
from hydra.config import Settings
from hydra.db import connect
from hydra.hunt.signatures import from_revision
from hydra.pipeline import analyse, latest_run, load_clustering
from tests.core.test_pipeline import PX, corpus, page, test_extractors  # noqa: F401 — fixture


def _campaign(settings: Settings) -> str:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        camps, _ = load_clustering(conn, latest_run(conn))
    return next(c.id for c in camps if set(c.members) == {"nask-1", "nask-2", "nask-3"})


def test_signature_from_published_revision(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    sig, meta = from_revision(settings.db_path, _campaign(settings))
    assert sig.members == ["nask-1", "nask-2", "nask-3"] and sig.approval_basis == "published_revision"
    assert "tracker_id=1234567890123456" in sig.invariants
    assert meta["run_id"]


def test_hunt_flow(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    cid = _campaign(settings)
    with TestClient(create_app(settings)) as client:
        plan = client.post(f"/v1/campaigns/{cid}/hunt-plan", json={"query_budget": 5}).json()
        assert plan["signature"]["invariants"] and plan["score_kind"] == "heuristic_priority"
        assert all(c["ref"] not in plan["signature"]["members"] for c in plan["candidates"])
        r = client.post("/v1/hypotheses", json={"campaign_id": cid, "horizon_hours": 48})
        assert r.status_code == 201, r.text
        h = r.json()["hypothesis"]
        assert h["status"] == "pending" and h["mode"] == "LIVE" and h["knowledge_cutoff"] <= h["issued_at"]
        # later material: a new-domain variant with the operator pixel, and a same-template other-operator page
        page(tmp_path / "V", "evil-new.example", "+48 700 100 100" + PX)
        analyse_input("file", tmp_path / "V", settings, only=test_extractors)
        page(tmp_path / "W", "other.example", '<i data-pixel="9999999999999999"></i><i data-acct="zzz"></i>')
        analyse_input("file", tmp_path / "W", settings, only=test_extractors)
        v = client.post(f"/v1/hypotheses/{h['hypothesis_id']}/verify").json()
        assert v["status"] == "confirmed", v
        verdicts = {c["verdict"] for c in v["checks"]}
        assert "conflict" in verdicts  # the other operator is not linked
        na = client.post(f"/v1/hypotheses/{h['hypothesis_id']}/next-action", json={}).json()
        assert "hypothesis_id" in na
        got = client.get(f"/v1/hypotheses/{h['hypothesis_id']}").json()
        assert got["hypothesis"]["status"] == "confirmed" and got["ledger"]["ok"]
        assert client.get("/v1/hypotheses").json()["total"] == 1
        m = client.get("/v1/hunt/metrics").json()
        assert m["LIVE"]["by_status"] == {"confirmed": 1}
        assert client.get("/v1/hunt/replay").json()["available"] is False
        assert client.post("/v1/campaigns/nope/hunt-plan", json={}).status_code == 404
    with closing(connect(settings.db_path, readonly=True)) as conn:
        assert events.verify_chain(conn)[0]
        types = {r["type"] for r in conn.execute("SELECT type FROM events")}
    assert {"HUNT_HYPOTHESIS_ISSUED", "HUNT_VERIFIED"} <= types


def test_replay_endpoint_without_recordings(settings: Settings):
    with TestClient(create_app(settings)) as client:
        r = client.post("/v1/hunt/replay")
        assert r.status_code == 200
        rep = client.get("/v1/hunt/replay").json()
        assert rep["REAL"]["status"] == "unavailable" and rep["SYNTHETIC"]["dataset_kind"] == "SYNTHETIC"
        assert client.get("/v1/hunt/metrics").json()["TEMPORAL_REPLAY"]["SYNTHETIC"]["hypotheses"]["issued"] == 2


def test_unsupported_hunt_source_is_rejected_and_never_refuted(settings: Settings, make_png, tmp_path,
                                                                test_extractors, monkeypatch):  # noqa: F811
    """CR-ASTRA-PIPE-308: a source HYDRA does not collect is rejected at issue time; a ledger entry that names one
    (issued before the check) verifies as unverifiable with coverage_complete=false, never refuted by absence."""
    from datetime import datetime, timedelta

    from hydra import api_hunt

    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    cid = _campaign(settings)
    with TestClient(create_app(settings)) as client:
        r = client.post("/v1/hypotheses", json={"campaign_id": cid, "sources": ["external-feed"]})
        assert r.status_code == 422 and "Nieobsługiwane źródło" in r.json()["detail"]
        assert client.post("/v1/hypotheses", json={"campaign_id": cid, "sources": []}).status_code == 422
        monkeypatch.setattr(api_hunt, "LIVE_SOURCES", frozenset({"local_store", "external-feed"}))
        r = client.post("/v1/hypotheses", json={"campaign_id": cid, "sources": ["local_store", "external-feed"],
                                                "horizon_hours": 1})
        assert r.status_code == 201, r.text
        h = r.json()["hypothesis"]
        monkeypatch.setattr(api_hunt, "LIVE_SOURCES", frozenset({"local_store"}))
        after = datetime.fromisoformat(h["horizon_end"].replace("Z", "+00:00")) + timedelta(seconds=1)
        monkeypatch.setattr("hydra.api_hunt.events.utcnow", lambda: after.isoformat())
        v = client.post(f"/v1/hypotheses/{h['hypothesis_id']}/verify").json()
        assert v["status"] == "unverifiable" and v["coverage_complete"] is False, v
        assert v["uncovered_sources"] == ["external-feed"]


def test_hunt_plan_without_operator_invariant_is_unavailable(settings: Settings, make_png, tmp_path,
                                                             test_extractors):  # noqa: F811
    """CR-ASTRA-PIPE-307: no operator-tier invariant → 200 with status unavailable and a Polish reason, no 500."""
    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    cid = _campaign(settings)
    with TestClient(create_app(settings)) as client:
        client.app.state.hydra.writer.run(lambda conn: conn.execute(
            "UPDATE genes SET kind='asn', value='AS13335', dependency_group='inf:boilerplate:asn'"))
        r = client.post(f"/v1/campaigns/{cid}/hunt-plan", json={})
        assert r.status_code == 200, r.text
        plan = r.json()
        assert plan["status"] == "unavailable" and plan["draft_hypothesis"] is None
        assert plan["candidates"] == [] and plan["next_evidence"] == [] and "operatora" in plan["reason_pl"]
