"""Task API (hydra/api_task.py) against synthetic v2 rows written through the Writer in tmp_path."""

from __future__ import annotations

import csv
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import Settings
from hydra.contracts import Assignment, PairScore, Review
from ui_api.synthetic import RUN_ID, seed


@pytest.fixture
def empty_client(settings: Settings):
    with TestClient(create_app(settings)) as c:
        yield c


@pytest.fixture
def client(settings: Settings):
    seed(settings.db_path, settings.cas_dir)
    with TestClient(create_app(settings)) as c:
        yield c


def test_empty_db_returns_empty_states_not_errors(empty_client):
    c = empty_client
    assert c.get("/v1/runs/latest").json()["run"] is None
    for path in ("/v1/incidents", "/v1/campaigns", "/v1/assignments", "/v1/pairs"):
        body = c.get(path).json()
        assert body["items"] == [] and body["total"] == 0, path
    g = c.get("/v1/graph").json()
    assert g == {"run_id": None, "nodes": [], "edges": [], "stats": {"nodes": 0, "edges": 0, "edges_total": 0}}
    assert c.get("/v1/pairs/a/b").status_code == 404
    assert c.get("/v1/export/neo4j").status_code == 404
    st = c.get("/v1/status").json()
    assert st["run_id"] is None and st["enrichment_mode"] == "replay" and st["audit"]["ok"]


def test_latest_run_and_counts(client):
    body = client.get("/v1/runs/latest").json()
    assert body["run"]["id"] == RUN_ID
    assert body["counts"] == {"incidents": 6, "pairs": 6, "campaigns": 2, "assignments": 1, "members": 5}
    ev = client.get("/v1/runs/latest", params={"kind": "eval"}).json()
    assert ev["run"]["metrics"]["system"]["sample"]["loo"]["summary"]["accuracy"] == 0.75


def test_incidents_never_leak_ground_truth(client):
    body = client.get("/v1/incidents").json()
    assert body["total"] == 6
    assert "TRUTH" not in str(body)
    one = client.get("/v1/incidents/syn-1").json()
    assert "label" not in one and "TRUTH" not in str(one)


def test_incident_detail_groups_genes_and_enrichment(client):
    inc = client.get("/v1/incidents/syn-1").json()
    assert inc["campaign_id"] == "syn-c1"
    assert inc["artifacts"][0]["role"] == "ad_creative" and inc["artifacts"][0]["mime"] == "image/png"
    fams = {f["family"]: f for f in inc["families"]}
    assert list(fams) == ["VIS", "TXT", "INF", "ID", "NAR", "META"]
    assert fams["NAR"]["status"] == "unknown" and fams["NAR"]["genes"] == []
    assert fams["INF"]["counts"] == {"observed": 2}
    assert fams["ID"]["counts"] == {"not_applicable": 1, "observed": 1}
    enr = inc["enrichment"]
    assert enr[0]["source"] == "rdap" and enr[0]["fetched_at"] and enr[0]["summary"]["registrar"]
    assert inc["neighbours"][0]["other"] == "syn-2"
    assert client.get("/v1/incidents/nope").status_code == 404
    # search + campaign filter
    assert client.get("/v1/incidents", params={"campaign_id": "syn-c2"}).json()["total"] == 2
    assert client.get("/v1/incidents", params={"q": "stawy"}).json()["total"] == 2


def test_campaign_list_and_card(client):
    lst = client.get("/v1/campaigns").json()
    assert [c["id"] for c in lst["items"]] == ["syn-c1", "syn-c2"]
    assert lst["items"][0]["size"] == 3
    card = client.get("/v1/campaigns/syn-c1").json()
    assert card["members"] == ["syn-1", "syn-2", "syn-3"]
    assert card["core_genome"][0]["kind"] == "tracker_id"
    assert card["infrastructure"]["domain"][0]["value"] == "energia-zysk.example"
    assert card["infrastructure"]["domain"][0]["share"] == 1.0
    assert card["persons"]["person_named"][0]["incidents"] == ["syn-1", "syn-2", "syn-3"]
    assert len(card["internal_pairs"]) == 3
    assert card["cross_links"][0]["other_campaign"] == "syn-c2"
    assert [t["incident_id"] for t in card["timeline"]] == ["syn-1", "syn-2", "syn-3"]
    assert client.get("/v1/campaigns/zzz").status_code == 404


def test_campaign_export_json_and_csv(client):
    r = client.get("/v1/campaigns/syn-c2/export", params={"format": "json"})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.json()["id"] == "syn-c2"
    r = client.get("/v1/campaigns/syn-c2/export", params={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {row["incident_id"] for row in rows} == {"syn-4", "syn-5"}
    assert "TRUTH" not in r.text


def test_graph_filters(client):
    g = client.get("/v1/graph").json()
    assert g["stats"]["edges"] == 6 and len(g["nodes"]) == 6
    e = next(x for x in g["edges"] if x["id"] == "syn-1|syn-2")
    assert e["dominant_family"] == "INF" and e["probability"] == 0.991
    assert {n["id"]: n["group"] for n in g["nodes"]}["syn-6"] is None
    hi = client.get("/v1/graph", params={"min_p": 0.9}).json()
    assert {x["id"] for x in hi["edges"]} == {"syn-1|syn-2", "syn-1|syn-3"}
    vis = client.get("/v1/graph", params={"family": "VIS"}).json()
    assert {x["id"] for x in vis["edges"]} == {"syn-4|syn-5", "syn-3|syn-4"}


def test_pair_detail_is_a_valid_pairscore(client):
    body = client.get("/v1/pairs/syn-5/syn-4").json()  # order-independent
    score = body["score"]
    PairScore.model_validate({k: v for k, v in score.items() if k != "unknown_families"})
    assert score["a"] == "syn-4" and score["contradictions"][0]["bits"] < 0
    assert "ID" in score["missing_families"] and "NAR" in score["unknown_families"]
    assert body["incident_a"]["visuals"][0]["role"] == "ad_creative"
    assert body["same_campaign_in_run"] is True
    assert client.get("/v1/pairs/syn-1/syn-6").status_code == 404


def test_pairs_list_and_uncertainty_order(client):
    body = client.get("/v1/pairs", params={"order": "uncertainty", "limit": 2}).json()
    assert body["total"] == 6 and body["items"][0]["a"] == "syn-2" and body["items"][0]["b"] == "syn-3"


def test_assignments(client):
    body = client.get("/v1/assignments").json()
    Assignment.model_validate({k: v for k, v in body["items"][0].items() if k not in ("created_at", "assign_run_id")})
    assert body["items"][0]["decision"] == "new_campaign"


def test_review_accept_then_undo_writes_audit_events(client):
    r = client.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "syn-3", "b": "syn-2"}})
    assert r.status_code == 201, r.text
    rv = Review.model_validate(r.json()["review"])
    assert rv.target == {"a": "syn-2", "b": "syn-3"}
    assert client.get("/v1/pairs/syn-2/syn-3").json()["review"] == "accepted"
    edge = next(e for e in client.get("/v1/graph").json()["edges"] if e["id"] == "syn-2|syn-3")
    assert edge["review"] == "accepted"
    u = client.post("/v1/reviews", json={"action": "undo", "undoes": rv.id})
    assert u.status_code == 201
    assert client.get("/v1/pairs/syn-2/syn-3").json()["review"] is None
    assert client.post("/v1/reviews", json={"action": "undo", "undoes": rv.id}).status_code == 409
    assert client.post("/v1/reviews", json={"action": "undo", "undoes": "missing"}).status_code == 404
    assert client.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "x"}}).status_code == 400
    hist = client.get("/v1/pairs/syn-2/syn-3").json()["reviews"]
    assert [h["action"] for h in hist] == ["accept_link"] and hist[0]["undone"] is True
    audit = client.get("/v1/audit/verify").json()
    assert audit["ok"] and client.get("/v1/status").json()["reviews"] == 2
    assert client.get("/v1/reviews").json()["total"] == 2


def test_review_merge_campaigns_validated(client):
    assert client.post("/v1/reviews", json={"action": "merge_campaigns", "target": {}}).status_code == 400
    ok = client.post("/v1/reviews", json={"action": "merge_campaigns",
                                          "target": {"campaign_ids": ["syn-c1", "syn-c2"]}, "note": "ten sam kit"})
    assert ok.status_code == 201


def test_search(client):
    body = client.get("/v1/search", params={"q": "energia"}).json()
    assert body["campaigns"][0]["id"] == "syn-c1"
    dom = next(g for g in body["genes"] if g["kind"] == "domain")
    assert dom["incidents"] == ["syn-1", "syn-2", "syn-3"] and dom["doc_freq"] == 3


def _wait_job(c: TestClient, job_id: str) -> dict:
    for _ in range(200):
        body = c.get(f"/v1/analyse/{job_id}").json()
        if body["job"]["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_analyse_without_pipeline_fails_visibly(client, monkeypatch):
    import hydra.api_task as at

    def missing():
        raise at.PipelineUnavailable("hydra.pipeline.analyse_new is not available in this build")

    monkeypatch.setattr(at, "_pipeline_entry", missing)
    r = client.post("/v1/analyse", json={"url": "https://example.invalid/x"})
    assert r.status_code == 202
    body = _wait_job(client, r.json()["job_id"])
    assert body["job"]["status"] == "failed" and body["job"]["error_code"] == "PipelineUnavailable"
    assert body["assignment"] is None


def test_analyse_upload_hands_folder_to_pipeline_and_returns_assignment(client, monkeypatch, make_png):
    import hydra.api_task as at

    seen: dict = {}

    def fake(kind, payload: Path, settings, *, writer):
        seen["kind"] = kind
        seen["root"] = payload.name
        seen["files"] = sorted(str(p.relative_to(payload)) for p in payload.rglob("*") if p.is_file())
        a = Assignment(incident_id="new-1", decision="assign", campaign_id="syn-c1",
                       reason_pl="Wspólny piksel i domena", model="test@0")
        return {"kind": kind, "incident_id": "new-1", "assignment": a.model_dump(), "notes": []}

    monkeypatch.setattr(at, "_pipeline_entry", lambda: fake)
    files = [("files", ("inc/ad.png", make_png(), "image/png")),
             ("files", ("inc/../../evil/page.html", b"<html></html>", "text/html")),
             ("files", ("inc/.DS_Store", b"x", "application/octet-stream"))]
    r = client.post("/v1/analyse", files=files)
    assert r.status_code == 202, r.text
    body = _wait_job(client, r.json()["job_id"])
    assert body["job"]["status"] == "done", body
    # one dropped folder → the folder itself; traversal stripped, hidden files dropped
    assert seen == {"kind": "file", "root": "inc", "files": ["ad.png", "evil/page.html"]}
    assert body["result"]["incident_id"] == "new-1"
    assert body["assignment"]["campaign_id"] == "syn-c1" and body["campaign"]["label"]
    assert client.get("/v1/audit/verify").json()["ok"]


def test_analyse_rejects_bad_input(client, tmp_path, settings):
    assert client.post("/v1/analyse", json={}).status_code == 400
    assert client.post("/v1/analyse", json={"url": "ftp://x"}).status_code == 400
    assert client.post("/v1/analyse", json={"path": str(settings.data_dir / "nope")}).status_code == 400
    # review #6: JSON only with application/json; paths only under data_dir / tmp_dir
    r = client.post("/v1/analyse", content=b'{"hint": "x y"}', headers={"content-type": "text/plain"})
    assert r.status_code == 415
    outside = tmp_path / "outside"
    outside.mkdir()
    assert client.post("/v1/analyse", json={"path": str(outside)}).status_code == 400
    assert client.post("/v1/analyse", json={"path": str(settings.data_dir / ".." / "outside")}).status_code == 400
    assert client.get("/v1/analyse/nope").status_code == 404


def test_analyse_hint_and_screenshot_kinds(client, monkeypatch, make_png):
    import hydra.api_task as at

    seen: list = []

    def fake(kind, payload, settings, *, writer):
        seen.append((kind, payload if isinstance(payload, str) else payload.name))
        if kind == "hint":
            return {"kind": "hint", "search": {"incidents": [{"id": "syn-4"}]}}
        return {"assignment": Assignment(incident_id="new-2", decision="abstain", reason_pl="za mało dowodów",
                                         model="test@0").model_dump(), "urls_in_image": [], "notes": ["brak URL"]}

    monkeypatch.setattr(at, "_pipeline_entry", lambda: fake)
    r = client.post("/v1/analyse", json={"hint": "Lek na stawy"})
    hint = _wait_job(client, r.json()["job_id"])
    assert hint["assignment"] is None and hint["result"]["search"]["incidents"][0]["id"] == "syn-4"
    shot = [("files", ("shot.png", make_png(), "image/png"))]
    r = client.post("/v1/analyse", files=shot, data={"kind": "screenshot"})
    shot_body = _wait_job(client, r.json()["job_id"])
    assert shot_body["assignment"]["decision"] == "abstain" and shot_body["result"]["notes"] == ["brak URL"]
    assert seen == [("hint", "Lek na stawy"), ("screenshot", "shot.png")]
    assert client.post("/v1/analyse", json={"hint": "x", "url": "https://a.b"}).status_code == 400


def test_watchlist_and_scheduler(client, monkeypatch):
    import hydra.api_task as at

    monkeypatch.setattr(at, "_scheduler", lambda: None)  # fallback path; CORE's module is tested in feat/core
    assert client.get("/v1/watchlist").json()["items"] == []
    r = client.post("/v1/watchlist", json={"add": [{"kind": "keyword", "value": "lek na stawy"},
                                                   {"kind": "domain", "value": "Zdrowe-Stawy.example"}]})
    assert len(r.json()["items"]) == 2
    r = client.post("/v1/watchlist", json={"remove": [{"kind": "keyword", "value": "lek na stawy"}]})
    assert [i["value"] for i in r.json()["items"]] == ["zdrowe-stawy.example"]
    assert client.post("/v1/watchlist", json={"add": [{"kind": "bogus", "value": "x"}]}).status_code == 422
    assert client.get("/v1/scheduler/status").json()["available"] is False
    assert client.post("/v1/scheduler/run").json() == {"available": False, "started": False}
    assert client.get("/v1/audit/verify").json()["ok"]


def test_scheduler_run_now(client, monkeypatch):
    """'Uruchom teraz': one forced background run; a second request while it runs is refused (GAP-AUDIT B16)."""
    import threading

    import hydra.scheduler as sch

    gate, seen = threading.Event(), []

    def slow(settings, writer, wl):
        seen.append("ran")
        gate.wait(5)
        return {"status": "ok"}

    monkeypatch.setattr(sch, "JOBS", {"inbox": slow})
    first = client.post("/v1/scheduler/run").json()
    for _ in range(100):
        if seen:
            break
        threading.Event().wait(0.02)
    second = client.post("/v1/scheduler/run").json()
    assert client.get("/v1/scheduler/status").json()["running"] is True
    gate.set()
    for _ in range(100):
        if not sch._RUN_LOCK.locked():
            break
        threading.Event().wait(0.02)
    status = client.get("/v1/scheduler/status").json()
    assert first["started"] is True and second["started"] is False and seen == ["ran"]
    assert status["running"] is False and status["jobs"]["inbox"]["status"] == "ok"


def test_neo4j_export(client):
    cy = client.get("/v1/export/neo4j").text  # same graph as `hydra export-neo4j` (hydra/export_neo4j.py)
    assert '{id: "syn-1", ' in cy and "MERGE (n:Incident {id: r.id})" in cy and "[x:MEMBER_OF]" in cy
    assert "[x:LINKED_TO]" in cy and "[x:HAS_GENE]" in cy and "MERGE (n:Gene {key: r.key})" in cy
    assert "TRUTH" not in cy
    edges = list(csv.DictReader(io.StringIO(client.get("/v1/export/neo4j", params={"format": "edges_csv"}).text)))
    assert sum(e[":TYPE"] == "LINKED_TO" for e in edges) == 6
    nodes = client.get("/v1/export/neo4j", params={"format": "nodes_csv", "min_p": 0.9}).text
    assert "Campaign:syn-c1" in nodes and "syn-6" not in nodes  # min_p 0.9 drops the only link of syn-6


def test_campaign_groups_derived_from_group_id(client):
    import json

    def tx(conn):
        for cid in ("syn-c1", "syn-c2"):
            d = json.loads(conn.execute("SELECT detail FROM campaigns WHERE id=?", (cid,)).fetchone()[0])
            d["group_id"] = "syn-g1"
            conn.execute("UPDATE campaigns SET detail=? WHERE id=?", (json.dumps(d), cid))

    client.app.state.hydra.writer.run(tx)  # the app's single writer
    body = client.get("/v1/campaign-groups").json()
    assert body["items"][0]["id"] == "syn-g1" and body["items"][0]["campaign_ids"] == ["syn-c1", "syn-c2"]
    assert client.get("/v1/campaigns/syn-c1").json()["group_id"] == "syn-g1"


def test_campaign_groups_table_v3(client):
    import json

    def tx(conn):
        conn.execute("INSERT INTO campaign_groups (run_id, id, label, confidence, detail) VALUES (?,?,?,?,?)",
                     ("syn-run-1", "G1", "Wspólny kit: piksel fbq", 0.7,
                      json.dumps({"id": "G1", "label": "Wspólny kit: piksel fbq", "campaign_ids": ["syn-c1", "syn-c2"],
                                  "shared_genes": [], "confidence": 0.7})))

    client.app.state.hydra.writer.run(tx)
    g = client.get("/v1/campaign-groups").json()["items"][0]
    assert g["id"] == "G1" and g["campaign_ids"] == ["syn-c1", "syn-c2"]
    lst = client.get("/v1/campaigns").json()["items"]
    assert {c["group_label"] for c in lst} == {"Wspólny kit: piksel fbq"}
    assert client.get("/v1/campaigns/syn-c2").json()["group"]["label"] == "Wspólny kit: piksel fbq"


def test_enrichment_summarised_from_genes_without_record(client):
    import json

    def tx(conn):
        conn.execute(
            "INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance, evidence, extractor,"
            " created_at) VALUES ('syn-6','INF','registrar','NameSilo, LLC',1,'observed','OBS',?,'inf_enrich@1','x')",
            (json.dumps({"enrichment_key": "k-no-record", "note": "kasyno.example"}),))

    client.app.state.hydra.writer.run(tx)
    enr = client.get("/v1/incidents/syn-6").json()["enrichment"]
    rec = next(e for e in enr if e["key"] == "k-no-record")
    assert rec["derived_from_genes"] and rec["fetched_at"] is None and rec["query"] == "kasyno.example"
    assert rec["summary"] == {"registrar": "NameSilo, LLC"}


def test_assignments_come_from_assign_runs_on_top_of_the_analyse_run(client):
    """Review #3: CORE writes assignments under `assign-*` runs (params.base_run = analyse run)."""
    assert client.get("/v1/runs/latest").json()["counts"]["assignments"] == 1
    items = client.get("/v1/assignments").json()["items"]
    assert [(i["incident_id"], i["assign_run_id"]) for i in items] == [("syn-6", "syn-assign-1")]
    assert client.get("/v1/incidents/syn-6").json()["assignment"]["decision"] == "new_campaign"
    inc = {i["id"]: i for i in client.get("/v1/incidents").json()["items"]}
    assert inc["syn-6"]["assignment"] == "new_campaign"
    from hydra import events

    def tx(conn):  # newer assign run for syn-6 wins; an assign run on another base run is ignored
        for rid, base, dec, ts in (("syn-assign-2", "syn-run-1", "abstain", "2099-01-01T00:00:00+00:00"),
                                   ("other-assign", "other-run", "assign", "2099-02-01T00:00:00+00:00")):
            conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at) VALUES (?, 'assign', ?, '{}', ?)",
                         (rid, events.canonical_json({"base_run": base}), ts))
            conn.execute("INSERT INTO assignments (incident_id, run_id, decision, campaign_id, detail, created_at)"
                         " VALUES ('syn-6', ?, ?, NULL, ?, ?)",
                         (rid, dec, events.canonical_json({"incident_id": "syn-6", "decision": dec, "candidates": [],
                                                           "reason_pl": "x", "model": "t"}), ts))
        conn.execute("INSERT INTO pairs (a, b, run_id, total_bits, probability, calibrated, hypothesis, model, detail)"
                     " VALUES ('syn-1','syn-6','syn-assign-2',1.0,0.1,0,'independent','t',"
                     "'{\"family_bits\":{\"TXT\":1.0}}')")

    client.app.state.hydra.writer.run(tx)
    items = client.get("/v1/assignments").json()["items"]
    assert [(i["incident_id"], i["decision"]) for i in items] == [("syn-6", "abstain")]
    pair = client.get("/v1/pairs/syn-6/syn-1").json()  # pair scored in the assign run is reachable
    assert pair["run_id"] == "syn-assign-2"
    assert any(n["other"] == "syn-1" for n in client.get("/v1/incidents/syn-6").json()["neighbours"])


def test_path_mode_corpus_vs_single_incident(client, monkeypatch, settings, make_png):
    """Review #4: a dataset folder → corpus run; one incident folder → analyse_input(file)."""
    import hydra.api_task as at

    corpus = settings.data_dir / "sample"
    for name in ("1", "2"):
        (corpus / name).mkdir(parents=True)
        (corpus / name / "ad.png").write_bytes(make_png())
    seen = []
    monkeypatch.setattr(at, "_corpus_entry", lambda: lambda path, s, writer, **kw: (
        seen.append(("corpus", path.name)) or {"kind": "corpus", "run_id": "r-new", "metrics": {"incidents": 2}}))
    monkeypatch.setattr(at, "_pipeline_entry", lambda: lambda kind, payload, s, writer: (
        seen.append((kind, payload.name)) or {"assignment": None, "notes": []}))
    r = client.post("/v1/analyse", json={"path": str(corpus)})
    assert r.json()["mode"] == "corpus"
    assert _wait_job(client, r.json()["job_id"])["result"]["run_id"] == "r-new"
    r = client.post("/v1/analyse", json={"path": str(corpus / "1")})
    assert r.json()["mode"] == "path"
    _wait_job(client, r.json()["job_id"])
    assert seen == [("corpus", "sample"), ("file", "1")]


def test_url_and_hint_redacted_in_job_params(client, monkeypatch):
    """Review #9: secrets in the URL never reach job params or the job dict."""
    import hydra.api_task as at

    monkeypatch.setattr(at, "_pipeline_entry", lambda: lambda kind, payload, s, writer: {"assignment": None})
    r = client.post("/v1/analyse", json={"url": "https://x.example/p?token=abc123secret&utm_source=fb"})
    body = _wait_job(client, r.json()["job_id"])
    assert "abc123secret" not in json_dumps(body)
    assert body["job"]["params"]["url"] == "https://x.example/p?token=[REDACTED]&utm_source=fb"
    with client.app.state.hydra.reader() as conn:
        raw = conn.execute("SELECT params FROM jobs WHERE id=?", (r.json()["job_id"],)).fetchone()[0]
    assert "abc123secret" not in raw


def json_dumps(obj) -> str:
    import json

    return json.dumps(obj)


def test_source_ref_hides_os_username(client):
    """Review #20."""
    def tx(conn):
        conn.execute("UPDATE incidents SET source_ref='/Users/jan.kowalski/HydraNexus/data/nask/sample/14'"
                     " WHERE id='syn-1'")

    client.app.state.hydra.writer.run(tx)
    assert client.get("/v1/incidents/syn-1").json()["source_ref"] == "~/HydraNexus/data/nask/sample/14"
    assert "jan.kowalski" not in json_dumps(client.get("/v1/incidents").json())


def test_status_audit_verified_incrementally_and_tamper_still_caught(client, monkeypatch):
    """Review #21."""
    import hydra.api_task as at

    first = client.get("/v1/status").json()["audit"]
    assert first["ok"]
    client.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "syn-1", "b": "syn-2"}})
    calls = []
    real = at.events.verify_chain
    monkeypatch.setattr(at.events, "verify_chain", lambda conn: calls.append(1) or real(conn))
    second = client.get("/v1/status").json()["audit"]
    # the review event plus whatever the applied revision logs; verified incrementally, no full scan
    assert second["ok"] and second["events"] >= first["events"] + 1 and calls == []

    def tamper(conn):
        conn.execute("UPDATE events SET payload='{}' WHERE seq=1")

    client.app.state.hydra.writer.run(tamper)
    monkeypatch.setattr(at, "AUDIT_FULL_RECHECK_S", -1.0)  # force the periodic full re-verify
    third = client.get("/v1/status").json()["audit"]
    assert third["ok"] is False and third["first_bad_seq"] == 1


def test_corpus_entry_passes_a_list_of_paths(monkeypatch, tmp_path):
    """Regression: analyse_corpus(paths: list[Path]) got a bare Path ("'PosixPath' object is not iterable")."""
    from hydra import api_task, pipeline

    seen = {}

    def fake(paths, settings, writer=None, **kw):
        seen["paths"] = paths
        return {}

    monkeypatch.setattr(pipeline, "analyse_corpus", fake, raising=False)
    api_task._corpus_entry()(tmp_path, None, None)
    assert seen["paths"] == [tmp_path]


def test_dashboard_aggregates_and_no_truth(client):
    d = client.get("/v1/dashboard").json()
    k = d["kpis"]
    assert k["incidents"] == 6 and k["campaigns"] == 2 and k["assignments"] == 1
    assert k["artifacts_by_role"] == {"ad_creative": 6}
    assert k["genes_by_family"]["ID"] == {"not_applicable": 6, "observed": 3}
    assert k["enriched_incidents"] == 6
    prev = {p["id"]: p for p in d["previews"]}
    assert prev["syn-1"]["thumbnail"] and prev["syn-1"]["campaign_id"] == "syn-c1"
    assert prev["syn-6"]["campaign_id"] is None and prev["syn-6"]["video"] is None
    c1 = d["campaigns"][0]
    assert c1["id"] == "syn-c1" and [m["id"] for m in c1["members"]] == ["syn-1", "syn-2", "syn-3"]
    assert c1["pivots"][0]["kind"] == "tracker_id"
    cov = {r["incident_id"]: r["families"] for r in d["coverage"]}
    assert cov["syn-6"]["INF"] == {"observed": 1, "unavailable": 1} and cov["syn-1"]["NAR"] == {}
    assert d["jobs"] == [] or "params" in d["jobs"][0]
    assert "TRUTH" not in json_dumps(d)


def test_simulation_stages_from_tables(client):
    s = client.get("/v1/simulation").json()
    assert s["run_id"] == RUN_ID
    st = s["stages"]
    assert st["import"]["incidents"] == 6 and st["import"]["artifacts_by_role"] == {"ad_creative": 6}
    assert st["genes"]["incident_id"] in ("syn-1", "syn-2", "syn-3")  # member of the largest campaign
    assert sum(st["genes"]["by_extractor"].values()) >= 5
    assert sum(sum(v for k, v in h.items() if k != "df") for h in st["rarity"]["histogram"]) == \
        st["rarity"]["distinct_values"]
    assert st["candidates"]["all_pairs"] == 15
    hyps = {p["why"] for p in st["evidence"]["presets"]}
    assert "ta sama kampania" in hyps and "pułapka: niezależne mimo bitów" in hyps
    assert sum(st["decision"]["probability_bins"]) == 6 and st["decision"]["hypotheses"]["same_campaign"] == 3
    assert st["grouping"]["edges"][0]["p"] >= st["grouping"]["edges"][-1]["p"]
    assert st["grouping"]["truth"] is None  # incidents.label is never exposed
    assert st["result"]["eval_run_id"] == "syn-eval-1" and st["result"]["eval_headline"]["crashes"] == 0
    custom = client.get("/v1/simulation", params={"presets": "syn-2:syn-1", "incident": "syn-6"}).json()
    assert custom["stages"]["evidence"]["presets"] == [{"a": "syn-1", "b": "syn-2", "why": "wybrane"}]
    assert custom["stages"]["genes"]["incident_id"] == "syn-6"
    assert "TRUTH" not in json_dumps(s)


def test_live_progress_derived_from_rows(client, monkeypatch, make_png):
    """The live view reads the new incident's genes and the assign run that appear after the job started."""
    import hydra.api_task as at
    from hydra import events

    def fake(kind, payload, s, writer):
        def tx(conn):
            now = events.utcnow()
            conn.execute("INSERT INTO incidents (id, source, source_ref, title, meta, created_at)"
                         " VALUES ('new-live','upload','x','Nowy materiał','{}',?)", (now,))
            conn.execute("INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance, evidence,"
                         " extractor, created_at) VALUES ('new-live','INF','domain','zdrowe-stawy.example',1,"
                         "'observed','OBS','{}','t@1',?)", (now,))
            conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at) VALUES ('assign-live','assign',?,"
                         "'{}',?)", (events.canonical_json({"base_run": RUN_ID}), now))
            conn.execute("INSERT INTO pairs (a, b, run_id, total_bits, probability, calibrated, hypothesis, model,"
                         " detail) VALUES ('new-live','syn-4','assign-live',12.0,0.9,0,'same_campaign','t','{}')")
        writer.run(tx)
        a = Assignment(incident_id="new-live", decision="assign", campaign_id="syn-c2", reason_pl="r", model="t",
                       group_id=None)
        return {"incident_id": "new-live", "run_id": "assign-live", "assignment": a.model_dump()}

    monkeypatch.setattr(at, "_pipeline_entry", lambda: fake)
    r = client.post("/v1/analyse", files=[("files", ("ad.png", make_png(), "image/png"))])
    live = _wait_job(client, r.json()["job_id"])["live"]
    assert live["incident_id"] == "new-live" and live["genes_by_family"] == {"INF": {"observed": 1}}
    assert live["assign_run_id"] == "assign-live" and live["pairs_scored"] == 1
    assert live["top_pairs"][0]["other"] == "syn-4"
    assert [m["id"] for m in live["campaign_members"]] == ["syn-4", "syn-5"]


def test_image_entropy_prefers_detail(tmp_path):
    from PIL import Image, ImageDraw

    from hydra.api_task import image_entropy

    blank = tmp_path / "blank.png"
    Image.new("RGB", (200, 200), (255, 255, 255)).save(blank)
    busy = tmp_path / "busy.png"
    im = Image.new("RGB", (200, 200))
    d = ImageDraw.Draw(im)
    for i in range(0, 200, 4):
        d.line((0, i, 200, 200 - i), fill=(i, 255 - i, (i * 7) % 255))
    im.save(busy)
    assert image_entropy(blank) < 0.5 < 3 < image_entropy(busy)


def test_ai_endpoints_unavailable_without_llm(client, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "hydra.llm", None)  # import fails
    b = client.get("/v1/campaigns/syn-c1/brief").json()
    assert b["status"] == "unavailable" and "Warstwa AI wyłączona" in b["reason_pl"]
    e = client.get("/v1/pairs/syn-1/syn-2/explain").json()
    assert e["status"] == "unavailable"
    assert client.get("/v1/pairs/syn-1/syn-6/explain").status_code == 404


def test_ai_endpoints_call_llm_when_enabled(client, monkeypatch):
    import sys
    import types

    seen = {}
    fake = types.ModuleType("hydra.llm")
    fake.is_enabled = lambda settings: True

    def brief(card, *, settings):
        seen["brief"] = card["id"]
        return {"text_pl": "Kampania łączy piksel [1].", "citations": [{"n": 1, "section": "core_genome", "index": 0,
                                                                         "label": "tracker_id"}], "model": "m"}

    def explain(pair, *, settings):
        raise RuntimeError("timeout")

    fake.brief_for_campaign, fake.explain_pair = brief, explain
    monkeypatch.setitem(sys.modules, "hydra.llm", fake)
    import hydra

    monkeypatch.setattr(hydra, "llm", fake, raising=False)
    b = client.get("/v1/campaigns/syn-c1/brief").json()
    assert b["status"] == "ok" and b["citations"][0]["section"] == "core_genome" and seen["brief"] == "syn-c1"
    e = client.get("/v1/pairs/syn-1/syn-2/explain").json()
    assert e["status"] == "error" and "dowody" in e["reason_pl"]
    fake.is_enabled = lambda settings: False
    assert client.get("/v1/campaigns/syn-c1/brief").json()["status"] == "unavailable"


def test_analytics_overview_and_indicators(client):
    o = client.get("/v1/analytics/overview").json()
    assert o["run_id"] == RUN_ID
    links = {(x["family"], x["campaign_id"]): x["bits"] for x in o["sankey"]["links"]}
    assert links[("INF", "syn-c1")] == pytest.approx(12.4 + 9.5 + 4.0)  # internal syn-c1 pairs only
    assert ("VIS", "syn-c2") in links and all(c in ("syn-c1", "syn-c2") for _, c in links)
    hyps = {t["hypothesis"] for t in o["tiers"]["links"]}
    assert hyps <= {"same_campaign", "shared_template", "abstain", "shared_asset"}
    assert [t["id"] for t in o["timeline"]][:2] == ["syn-6", "syn-1"]  # null dates sort first in SQLite
    assert o["clusters"][0]["id"] == "syn-c1" and o["clusters"][0]["size"] == 3
    ind = client.get("/v1/analytics/indicators").json()
    dom = next(i for i in ind["indicators"] if i["kind"] == "tracker_id")
    assert dom["incidents"] == ["syn-1", "syn-2", "syn-3", "syn-4", "syn-5"] and dom["df"] == 5
    assert all(i["df"] >= 2 for i in ind["indicators"])
    assert "TRUTH" not in json_dumps(o) + json_dumps(ind)


def test_demo_corpora_run_and_progress(client, settings, monkeypatch, make_png):
    import hydra.api_task as at

    assert client.get("/v1/demo/corpora").json() == {"items": []}
    sample = settings.data_dir / "nask" / "sample"
    for n in ("1", "2"):
        (sample / n).mkdir(parents=True)
        (sample / n / "ad.png").write_bytes(make_png())
    items = client.get("/v1/demo/corpora").json()["items"]
    assert items[0]["id"] == "nask" and items[0]["incidents_on_disk"] == 2 and "_path" not in items[0]
    seen = []
    monkeypatch.setattr(at, "_corpus_entry",
                        lambda: lambda path, s, writer, **kw: seen.append((path.name, kw)) or {"kind": "corpus"})
    r = client.post("/v1/demo/run", json={"corpus": "nask"})
    assert r.status_code == 202
    _wait_job(client, r.json()["job_id"])
    assert seen[0][0] == "sample" and seen[0][1]["corpus"] == "nask" and seen[0][1]["use_cache"] is True
    r2 = client.post("/v1/demo/run", json={"corpus": "nask", "fresh": True})
    _wait_job(client, r2.json()["job_id"])
    assert seen[1][1]["use_cache"] is False and seen[1][1]["job_id"] == r2.json()["job_id"]
    pub = settings.data_dir / "public-corpus" / "certpl"
    (pub / "inc-1").mkdir(parents=True)
    ids = {c["id"]: c for c in client.get("/v1/demo/corpora").json()["items"]}
    assert ids["public"]["spec"] == "public" and ids["public"]["incidents_on_disk"] == 1
    assert ids["public"]["incidents_in_db"] == 0 and "public:certpl" not in ids  # nothing stored yet
    prog = client.get(f"/v1/demo/progress/{r.json()['job_id']}").json()
    assert prog["job"]["params"]["corpus"] == "nask" and len(prog["incidents"]) == 6
    assert client.post("/v1/demo/run", json={"corpus": "certpl"}).status_code == 404


def test_corpus_of_runs():
    from hydra.api_task import run_corpus

    assert run_corpus({"paths": ["/x/data/nask/sample"]}) == "nask"
    assert run_corpus({"paths": ["/x/data/corpora/extended"]}) == "ext:extended"
    assert run_corpus({"paths": ["/x/data/nask/sample", "/x/data/corpora/extended"]}) == "mixed"
    assert run_corpus({"corpus": "certpl"}) == "ext:certpl"
    for native in ("nask", "extended", "public", "all", "public:certpl", "ext:foo", "nask,ext:foo", "mixed"):
        assert run_corpus({"corpus": native}) == native  # native corpus names stay unprefixed
    assert run_corpus({}) == "unknown"


def test_demo_public_corpus_is_offered_by_stored_source_not_folder(client, settings, monkeypatch):
    """A `pl-feeds` folder holds incidents with sources public:certpl / public:phishtank / public:phiusiil: the
    folder-named spec `public:pl-feeds` analysed 0 incidents (server run analyse-20261121T210246-cd0255)."""
    import hydra.api_task as at
    from hydra import events

    feeds = settings.data_dir / "public-corpus" / "pl-feeds"
    for n in range(4):
        (feeds / f"inc-{n}").mkdir(parents=True)
    now = events.utcnow()
    rows = [("pub-c1", "public:certpl"), ("pub-c2", "public:certpl"), ("pub-p1", "public:phishtank"),
            ("pub-u1", "public:phiusiil")]
    client.app.state.hydra.writer.run(lambda conn: conn.executemany(
        "INSERT INTO incidents (id, source, source_ref, title, meta, created_at) VALUES (?, ?, 'x', 't', '{}', ?)",
        [(i, src, now) for i, src in rows]))
    ids = {c["id"]: c for c in client.get("/v1/demo/corpora").json()["items"]}
    assert "public:pl-feeds" not in ids
    assert ids["public"]["spec"] == "public" and ids["public"]["incidents_in_db"] == 4
    assert ids["public"]["incidents_on_disk"] == 4
    assert {k: (v["spec"], v["incidents_in_db"]) for k, v in ids.items() if k.startswith("public:")} == {
        "public:certpl": ("public:certpl", 2), "public:phishtank": ("public:phishtank", 1),
        "public:phiusiil": ("public:phiusiil", 1)}
    assert ids["public:certpl"]["label"] == "Publiczny korpus CERT PL"
    from hydra.corpus import corpus_of, matches

    for spec in ("public", "public:certpl"):  # every offered spec selects stored incidents
        assert any(matches(corpus_of(src), spec) for _i, src in rows)
    seen = []
    monkeypatch.setattr(at, "_corpus_entry",
                        lambda: lambda path, s, writer, **kw: seen.append((path.name, kw)) or {"kind": "corpus"})
    r = client.post("/v1/demo/run", json={"corpus": "public"})
    assert r.status_code == 202
    _wait_job(client, r.json()["job_id"])
    assert seen[-1] == ("public-corpus", {**seen[-1][1], "corpus": "public"})
    r = client.post("/v1/demo/run", json={"corpus": "public:phishtank"})
    _wait_job(client, r.json()["job_id"])
    assert seen[-1][0] == "public-corpus" and seen[-1][1]["corpus"] == "public:phishtank"
    prog = client.get(f"/v1/demo/progress/{r.json()['job_id']}").json()
    assert prog["job"]["params"]["corpus"] == "public:phishtank"
    assert client.post("/v1/demo/run", json={"corpus": "public:pl-feeds"}).status_code == 404


def test_explain_data_panels_from_rows(client, monkeypatch, make_png):
    import hydra.api_task as at
    from hydra import events

    def fake(kind, payload, s, writer):
        def tx(conn):
            now = events.utcnow()
            conn.execute("INSERT INTO incidents (id, source, source_ref, title, meta, created_at)"
                         " VALUES ('new-x','upload','x','Nowy','{}',?)", (now,))
            for kind_, val in (("tracker_id", "fbq:123456789"), ("domain", "zupelnie-nowa.example")):
                conn.execute("INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance,"
                             " evidence, extractor, created_at) VALUES ('new-x','INF',?,?,1,'observed','OBS','{}',"
                             "'t@1',?)", (kind_, val, now))
            conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at) VALUES ('assign-x','assign',?,"
                         "'{}',?)", (events.canonical_json({"base_run": RUN_ID, "thresholds": {"link": 10}}), now))
            conn.execute("INSERT INTO pairs (a, b, run_id, total_bits, probability, calibrated, hypothesis, model,"
                         " detail) VALUES ('new-x','syn-4','assign-x',7.5,0.3,0,'abstain','t',?)",
                         (events.canonical_json({"family_bits": {"INF": 7.5},
                                                 "candidate_sources": ["exact:tracker_id"]}),))
        writer.run(tx)
        best = {"a": "new-x", "b": "syn-4", "support": 7.5, "total_bits": 7.5, "family_bits": {"INF": 7.5},
                "reasons": [{"family": "INF", "kind": "tracker_id", "value_a": "fbq:123456789", "bits": 7.5,
                             "explanation_pl": "Ten sam piksel"}]}
        return {"incident_id": "new-x", "run_id": "assign-x",
                "assignment": {"incident_id": "new-x", "decision": "new_campaign", "campaign_id": None,
                               "candidates": [{"campaign_id": "syn-c2", "probability": 0.3, "support": 1,
                                               "best_pair": best}, {"campaign_id": "syn-c1", "probability": 0.2,
                                                                     "support": 1, "best_pair": {"support": 2.0}}],
                               "reason_pl": "za mało", "model": "t"}}

    monkeypatch.setattr(at, "_pipeline_entry", lambda: fake)
    r = client.post("/v1/analyse", files=[("files", ("ad.png", make_png(), "image/png"))])
    _wait_job(client, r.json()["job_id"])
    d = client.get(f"/v1/analyse/{r.json()['job_id']}/explain-data").json()
    assert d["ready"] and d["incident_id"] == "new-x"
    feed = {g["kind"]: g for g in d["genes_feed"]}
    assert feed["tracker_id"]["df_other"] == 5 and feed["tracker_id"]["bits"] == 7.5
    assert feed["domain"]["df_other"] == 0 and "unikalny" in feed["domain"]["why"]
    assert d["genes_feed"][0]["kind"] == "tracker_id"  # bits first
    assert d["candidates"]["by_index"] == {"exact": 1} and d["candidates"]["funnel"]["candidates"] == 1
    assert d["why"]["margin"] == 5.5 and d["why"]["counterfactual"] == "brakuje 2,5 b do progu kampanii (10,0 b)"
    assert any("Ten sam piksel" in x for x in d["why"]["rationale_pl"])
    assert [s["key"] for s in d["stages"]] == ["import", "genes", "enrich", "assign"]
    assert d["metrics"]["genes_total"] == 2 and d["graph"]["edges"][0]["other"] == "syn-4"
    assert client.get("/v1/analyse/nope/explain-data").status_code == 404


def test_upload_lone_har_dotfile_is_data_not_hidden(client, monkeypatch):
    """Audit A03: `61/strona_1/.har` style uploads are data; only OS junk is dropped, and skips are reported."""
    import hydra.api_task as at

    seen = []
    monkeypatch.setattr(at, "_pipeline_entry", lambda: lambda kind, payload, s, writer: (
        seen.append(payload.name) or {"assignment": None}))
    har = b'{"log": {"version": "1.2", "entries": []}}'
    r = client.post("/v1/analyse", files=[("files", ("strona_1/.har", har, "application/json"))])
    assert r.status_code == 202, r.text
    _wait_job(client, r.json()["job_id"])
    assert seen == ["strona_1"] and r.json()["skipped"] == []
    r = client.post("/v1/analyse", files=[("files", (".DS_Store", b"x", "application/octet-stream"))])
    assert r.status_code == 400 and r.json()["detail"]["skipped"][0]["path"] == ".DS_Store"
    r = client.post("/v1/analyse", files=[("files", ("inc/ad.json", b"{}", "application/json")),
                                          ("files", ("inc/.env", b"SECRET=1", "text/plain")),
                                          ("files", ("__MACOSX/inc/._ad.json", b"x", "application/octet-stream"))])
    assert r.status_code == 202
    reasons = {s["path"]: s["reason"] for s in r.json()["skipped"]}
    assert "inc/.env" in reasons and "ukryty" in reasons["inc/.env"] and "__MACOSX/inc/._ad.json" in reasons
    # a hidden file recognised by its bytes (PNG magic) is data, not junk
    import io as _io

    from PIL import Image

    buf = _io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="PNG")
    r = client.post("/v1/analyse", files=[("files", ("inc/.preview", buf.getvalue(), "image/png"))])
    assert r.status_code == 202 and r.json()["skipped"] == []


def test_aggregates_scoped_to_the_run_incidents(client):
    """Audit A13: an ext:/public incident stored in the DB but not part of the NASK run never shows up in that
    run's dashboard, analytics, graph, indicators or simulation."""
    from hydra import events

    def tx(conn):
        now = events.utcnow()
        conn.execute("INSERT INTO incidents (id, source, source_ref, title, observed_at, meta, created_at)"
                     " VALUES ('ext-1','ext:phishtank','x','Obcy','2026-09-01T00:00:00+00:00','{}',?)", (now,))
        for kind, val in (("registrar", "Evil Registrar"), ("tracker_id", "fbq:123456789"), ("domain", "obcy.example")):
            conn.execute("INSERT INTO genes (incident_id, family, kind, value, quality, status, provenance, evidence,"
                         " extractor, created_at) VALUES ('ext-1','INF',?,?,1,'observed','OBS','{}','t@1',?)",
                         (kind, val, now))

    client.app.state.hydra.writer.run(tx)
    blob = json_dumps
    o = client.get("/v1/analytics/overview").json()
    assert "ext-1" not in blob(o["timeline"]) and "Evil Registrar" not in blob(o["infrastructure"])
    d = client.get("/v1/dashboard").json()
    assert d["kpis"]["incidents"] == 6 and "ext-1" not in blob(d["previews"]) + blob(d["coverage"])
    assert "ext-1" not in {n["id"] for n in client.get("/v1/graph").json()["nodes"]}
    tracker = next(i for i in client.get("/v1/analytics/indicators").json()["indicators"] if i["kind"] == "tracker_id")
    assert "ext-1" not in tracker["incidents"] and tracker["df"] == 5
    sim = client.get("/v1/simulation").json()["stages"]
    assert sim["import"]["incidents"] == 6 and "ext-1" not in blob(sim["import"]["incidents_list"])


def test_review_is_an_annotation_until_core_applies_reviews(client, monkeypatch):
    """Audit A04: without CORE's apply_review the API says so; with it, the review is applied."""
    import hydra.api_task as at

    monkeypatch.setattr(at, "_apply_review_fn", lambda: None)
    r = client.post("/v1/reviews", json={"action": "move_incident", "target": {"incident_id": "syn-6",
                                                                               "campaign_id": "syn-c1"}}).json()
    assert r["effect"] == "annotation" and "nie zmienia grupowania" in r["effect_pl"]
    assert client.get("/v1/status").json()["reviews_change_grouping"] is False
    calls = []
    monkeypatch.setattr(at, "_apply_review_fn", lambda: lambda review_id, settings, writer: calls.append(review_id) or
                        {"revision": "rev-2"})
    r = client.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "syn-1", "b": "syn-6"}}).json()
    assert r["effect"] == "applied" and r["applied"] == {"revision": "rev-2"} and calls == [r["review"]["id"]]


def test_review_endpoint_applies_review_through_engine(monkeypatch, tmp_path):
    """Audit A04: /v1/reviews must call hydra.review.apply_review with the stored review id."""
    from hydra import api_task

    called = {}

    def fake_apply(review_id, settings, writer):
        called["id"] = review_id
        return "analyse-new-revision"

    monkeypatch.setattr(api_task, "_apply_review_fn", lambda: fake_apply)
    from fastapi.testclient import TestClient

    from hydra.api import create_app
    from hydra.config import Settings

    s = Settings(root=tmp_path, state_dir=tmp_path / "state", data_dir=tmp_path / "data",
                 cache_dir=tmp_path / "cache", models_dir=tmp_path / "models")
    s.ensure_dirs()
    with TestClient(create_app(s)) as client:
        r = client.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "i1", "b": "i2"}})
        assert r.status_code == 201
        body = r.json()
        assert body["effect"] == "applied" and called["id"] == body["review"]["id"]


def test_visual_endpoints_from_rows(client):
    f = client.get("/v1/system/flow").json()
    b = f["boxes"]
    assert f["run_id"] == RUN_ID and b["input"]["incidents"] == 6 and b["scoring"]["pairs_scored"] == 6
    assert b["candidates"]["by_index"] == {"exact": 6} and b["clustering"]["campaigns"] == 2
    assert b["genes"]["by_family"]["INF"] == 11 and b["assignment"]["by_decision"] == {"new_campaign": 1}
    d = client.get("/v1/pairs/syn-2/syn-1/diff").json()
    assert d["a"] == "syn-1" and d["same_campaign"] is True
    infra = {r["kind"]: r for r in d["infrastructure"]}
    assert infra["tracker_id"]["state"] == "same"
    assert client.get("/v1/pairs/syn-1/nope/diff").status_code == 404
    p = client.get("/v1/pairs/syn-1/syn-2/provenance").json()
    assert p["hypothesis"] == "same_campaign" and any(link["target"] == "hyp:same_campaign" for link in p["links"])
    lc = client.get("/v1/campaigns/syn-c1/lifecycle").json()
    assert [i["id"] for i in lc["incidents"]] == ["syn-1", "syn-2", "syn-3"]
    assert lc["first_seen"].startswith("2026-10-01")
    g = client.get("/v1/genes/explain", params={"kind": "tracker_id", "value": "fbq:123456789"}).json()
    assert g["df"] == 5 and g["holders"] == ["syn-1", "syn-2", "syn-3", "syn-4", "syn-5"]
    assert client.get("/v1/genes/explain", params={"kind": "x", "value": "y"}).status_code == 404
    cs = {c["corpus"]: c for c in client.get("/v1/corpora/stats").json()["items"]}
    assert cs["nask"]["incidents"] == 6 and cs["nask"]["campaigns_in_latest_run"] == 2
    assert client.get("/v1/revisions").json()["items"] == []


def test_membership_diff_ignores_renumbering():
    from hydra.api_visual import membership_diff

    before = {"1": "A", "2": "A", "3": "A", "48": "B", "61": "B"}
    after = {"1": "X", "2": "X", "3": "Y", "48": "Y", "61": "Y"}
    moved, renamed = membership_diff(before, after)
    assert [m["incident_id"] for m in moved] == ["3"] and moved[0]["to_successor_of"] == "B"
    assert renamed == {"A": "X", "B": "Y"}


def test_resolve_run_skips_stub_eval_runs():
    import sqlite3

    from hydra.api_task import resolve_run

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE runs (id TEXT, kind TEXT, params TEXT, created_at TEXT, finished_at TEXT)")
    conn.execute("INSERT INTO runs VALUES ('real','eval','{\"backend\":\"core\"}','2026-11-21T10:00','x')")
    conn.execute("INSERT INTO runs VALUES ('stub','eval','{\"backend\":\"EVAL STUB\"}','2026-11-21T11:00','x')")
    assert resolve_run(conn, kind="eval") == "real"
