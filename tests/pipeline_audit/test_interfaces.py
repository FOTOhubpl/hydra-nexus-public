"""Cross-revision, API/client, optional-feature and disclosure boundaries, using only synthetic state."""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.export_dataset import export_dataset
from hydra.jobs import create_job
from tests.pipeline_audit.interface_helpers import (
    CAMPAIGN,
    NEW_CAMPAIGN,
    NEW_IDS,
    NEW_RUN,
    OLD_IDS,
    OLD_RUN,
    SECRET,
    TOKEN,
    frontend_requests,
    seed_two_runs,
)


@pytest.fixture
def interface_client(settings, monkeypatch):
    for name in ("HYDRA_WATCH_REC_DIR", "HYDRA_WATCH_STATE_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HYDRA_WATCH_POLL", "0")
    monkeypatch.setenv("HYDRA_LLM", "0")
    seed_two_runs(settings)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        try:
            yield client
        finally:
            watch = getattr(client.app.state, "watch", None)
            if watch is not None:
                watch.stop()


@pytest.fixture(scope="module")
def browser_requests(tmp_path_factory):
    repo = Path(__file__).resolve().parents[2]
    return frontend_requests(repo, tmp_path_factory.mktemp("frontend-interface"))


@pytest.fixture
def dataset_files(interface_client, settings, tmp_path):
    output = tmp_path / "dataset"
    export_dataset(settings, OLD_RUN, output)
    return output


@pytest.mark.parametrize("name", ["incidents", "genes"])
def test_dataset_tables_contain_only_selected_run_incidents(dataset_files, name):
    import pandas as pd

    table = pd.read_parquet(dataset_files / f"{name}.parquet")
    assert set(table["incident_id"]) == OLD_IDS


def test_dataset_enrichment_excludes_unrelated_run_entities(dataset_files):
    import pandas as pd

    table = pd.read_parquet(dataset_files / "enrichment.parquet")
    assert set(table["entity"]) == {"campaign.example"}


def test_neo4j_nodes_are_scoped_to_the_selected_run(interface_client):
    response = interface_client.get("/api/v1/export/neo4j",
                                    params={"run_id": OLD_RUN, "format": "nodes_csv"})
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.text)))
    incidents = [row for row in rows if row[":LABEL"] == "Incident"]
    # merge-batch3 admin CSV: internal `:ID` is `<Label>:<key>`, the incident id stays the `id` property.
    assert {row["id"] for row in incidents} == OLD_IDS
    assert {row[":ID"] for row in incidents} == {f"Incident:{i}" for i in OLD_IDS}


def test_explicit_run_scopes_campaign_downloads_and_audits(interface_client):
    old = interface_client.get(f"/api/v1/campaigns/{CAMPAIGN}/export", params={"run_id": OLD_RUN})
    new = interface_client.get(f"/api/v1/campaigns/{NEW_CAMPAIGN}/export", params={"run_id": NEW_RUN})
    assert old.status_code == new.status_code == 200
    assert set(old.json()["members"]) == OLD_IDS and set(new.json()["members"]) == NEW_IDS
    assert old.json()["run_id"] == OLD_RUN and new.json()["run_id"] == NEW_RUN
    assert interface_client.get("/api/v1/audit/verify").json()["ok"]


@pytest.mark.parametrize("path", [
    "/api/v1/export/neo4j?format=nodes_csv&run_id=nonexistent-run",
    "/api/v1/runs/nonexistent-run/stix",
    "/api/v1/runs/nonexistent-run/lead-times",
])
def test_export_of_unknown_run_is_not_a_successful_evidence_package(interface_client, path):
    response = interface_client.get(path)
    assert response.status_code == 404, response.text[:400]


@pytest.mark.parametrize("name", ["campaign", "abuse", "stix", "neo4j"])
def test_actual_frontend_download_urls_preserve_selected_run(browser_requests, name):
    assert parse_qs(urlsplit(browser_requests[name]).query).get("run_id") == [OLD_RUN]


def test_frontend_download_matches_the_campaign_currently_displayed(interface_client, browser_requests):
    card = interface_client.get(f"/api/v1/campaigns/{CAMPAIGN}", params={"run_id": OLD_RUN})
    download = interface_client.get(browser_requests["campaign"])
    assert card.status_code == 200
    assert download.status_code == 200, download.text
    assert (download.json()["run_id"], download.json()["members"]) == (
        card.json()["run_id"], card.json()["members"])


def test_actual_frontend_subgraph_request_preserves_selected_revision(interface_client, browser_requests):
    request = browser_requests["subgraph"]
    response = interface_client.request(request["method"], request["url"], json=request["body"])
    assert response.status_code == 200
    assert response.json()["run_id"] == OLD_RUN
    assert {node["id"] for node in response.json()["nodes"]} == OLD_IDS


@pytest.mark.parametrize("path", [
    "/v1/system/flow",  # run-aware since API-PERF (merge-batch3)
    "/v1/review/queue", "/v1/genes/explain?kind=operator_id&value=operator-old", "/v1/analytics/indicators",
])
def test_run_sensitive_frontend_panels_preserve_selected_run(browser_requests, path):
    assert parse_qs(urlsplit(browser_requests["scoped"][path]).query).get("run_id") == [OLD_RUN]


def test_indicator_backend_honours_the_selected_run(interface_client):
    """CR-ASTRA-PIPE-304 backend half: the indicator pivot resolved the latest run whatever the client sent."""
    old = interface_client.get("/api/v1/analytics/indicators", params={"run_id": OLD_RUN})
    new = interface_client.get("/api/v1/analytics/indicators", params={"run_id": NEW_RUN})
    assert old.status_code == new.status_code == 200
    assert (old.json()["run_id"], new.json()["run_id"]) == (OLD_RUN, NEW_RUN)
    incidents = lambda body: {i for row in body["indicators"] for i in row["incidents"]}  # noqa: E731
    assert incidents(old.json()) == OLD_IDS and incidents(new.json()) == NEW_IDS
    assert interface_client.get("/api/v1/analytics/indicators", params={"run_id": "nonexistent-run"}
                                ).status_code == 404


def test_frontend_core_queries_preserve_selected_run(browser_requests):
    for path in ("/v1/campaigns", "/v1/graph"):
        assert parse_qs(urlsplit(browser_requests["scoped"][path]).query)["run_id"] == [OLD_RUN]


@pytest.mark.parametrize("path,params", [
    (f"/api/v1/campaigns/{CAMPAIGN}/lifecycle", {"run_id": OLD_RUN}),
    ("/api/v1/pairs/astra-a/astra-b/diff", {"run_id": OLD_RUN}),
    ("/api/v1/pairs/astra-a/astra-b/provenance", {"run_id": OLD_RUN}),
    ("/api/v1/genes/explain", {"run_id": OLD_RUN, "kind": "operator_id", "value": SECRET}),
])
def test_visual_evidence_api_redacts_recognized_secret_forms(settings, path, params):
    seed_two_runs(settings, secret=True)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    assert TOKEN not in response.text


def test_job_list_is_as_redacted_as_individual_job_status(interface_client):
    job = create_job(interface_client.app.state.hydra.writer, "import", {"source": SECRET})
    detail = interface_client.get(f"/api/v1/jobs/{job}")
    assert detail.status_code == 200 and TOKEN not in detail.text
    listing = interface_client.get("/api/v1/jobs")
    assert listing.status_code == 200 and TOKEN not in listing.text


def test_unsure_review_note_is_redacted_when_it_reaches_the_ui(interface_client):
    response = interface_client.post("/api/v1/review/unsure", json={
        "target": {"a": "astra-a", "b": "astra-b"}, "note": SECRET})
    assert response.status_code == 201
    listing = interface_client.get("/api/v1/review/unsure")
    assert listing.status_code == 200
    assert TOKEN not in response.text + listing.text


@pytest.mark.parametrize("suffix", ["json", "csv", "stix", "abuse-package"])
def test_unknown_campaign_export_returns_not_found(interface_client, suffix):
    path = f"/api/v1/campaigns/not-a-campaign/{suffix}"
    if suffix in {"json", "csv"}:
        path = f"/api/v1/campaigns/not-a-campaign/export?format={suffix}"
    assert interface_client.get(path).status_code == 404


def test_optional_llm_absence_does_not_break_evidence_pages(interface_client):
    for path in (f"/api/v1/campaigns/{CAMPAIGN}/brief", "/api/v1/pairs/astra-a/astra-b/explain"):
        response = interface_client.get(path, params={"run_id": OLD_RUN})
        assert response.status_code == 200
        assert response.json()["status"] == "unavailable"
        assert "wyłączona" in response.json()["reason_pl"]
    assert interface_client.get(f"/api/v1/campaigns/{CAMPAIGN}", params={"run_id": OLD_RUN}).status_code == 200


def test_watchlist_domain_removal_normalizes_like_domain_addition(interface_client):
    body = {"kind": "domain", "value": "MiXeD.Example"}
    response = interface_client.post("/api/v1/watchlist", json={"add": [body]})
    assert response.status_code == 200
    assert response.json()["items"] == [{"kind": "domain", "value": "mixed.example"}]
    removed = interface_client.post("/api/v1/watchlist", json={"remove": [body]})
    assert removed.status_code == 200 and removed.json()["items"] == []


def test_hunt_plan_without_operator_evidence_degrades_without_500(interface_client):
    interface_client.app.state.hydra.writer.run(lambda conn: conn.execute(
        "UPDATE genes SET kind='asn',value='AS13335',dependency_group='inf:boilerplate:asn'"
        " WHERE incident_id IN ('astra-a','astra-b')"))
    response = interface_client.post(f"/api/v1/campaigns/{CAMPAIGN}/hunt-plan", json={"run_id": OLD_RUN})
    assert response.status_code in {200, 422}, response.text
    if response.status_code == 200:
        assert response.json()["draft_hypothesis"] is None
        assert response.json()["candidates"] == []


def test_hunt_never_claims_coverage_for_an_unimplemented_source(interface_client, monkeypatch):
    response = interface_client.post("/api/v1/hypotheses", json={
        "campaign_id": CAMPAIGN, "run_id": OLD_RUN, "sources": ["unimplemented-external-source"]})
    if response.status_code in {400, 422}:
        return  # Rejecting a source with no collection/coverage implementation is also safe.
    assert response.status_code == 201, response.text[:500]
    hypothesis = response.json()["hypothesis"]
    after = datetime.fromisoformat(hypothesis["horizon_end"].replace("Z", "+00:00")) + timedelta(seconds=1)
    monkeypatch.setattr("hydra.api_hunt.events.utcnow", lambda: after.isoformat())
    verified = interface_client.post(f"/api/v1/hypotheses/{hypothesis['hypothesis_id']}/verify")
    assert verified.status_code == 200
    assert verified.json()["status"] == "unverifiable", verified.text
    assert verified.json()["coverage_complete"] is False


def test_api_shutdown_stops_watch_background_workers(settings, monkeypatch):
    monkeypatch.delenv("HYDRA_WATCH_REC_DIR", raising=False)
    monkeypatch.delenv("HYDRA_WATCH_STATE_DIR", raising=False)
    monkeypatch.setenv("HYDRA_WATCH_POLL", "0")
    watch = None
    try:
        with TestClient(create_app(settings), raise_server_exceptions=False) as client:
            response = client.post("/api/v1/watch/live")
            assert response.status_code == 200 and response.json()["mode"] == "live"
            watch = client.app.state.watch
            threads = list(watch._threads)
            assert threads
        assert all(not thread.is_alive() for thread in threads)
    finally:
        if watch is not None:
            watch.stop()


@pytest.mark.parametrize("record", [[], {"source": "ct", "raw": [1]}, {"source": "ct", "raw": {"leaf_cert": [1]}}])
def test_malformed_watch_recording_is_a_controlled_input_error(interface_client, settings, record):
    folder = settings.data_dir / "watch" / "recordings"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "synthetic.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    response = interface_client.post("/api/v1/watch/replay", json={"file": "synthetic.jsonl"})
    assert response.status_code == 422, response.text
    assert "uszkodzonych rekordów" in response.json()["detail"]  # Polish, names the input problem
    assert interface_client.get("/api/v1/watch/status").status_code == 200  # the app stays usable


@pytest.mark.parametrize("path", [
    "/api/v1/system/flow", "/api/v1/review/queue", "/api/v1/revisions", "/api/v1/corpora/stats",
    "/api/v1/materials", "/api/v1/watch/status", "/api/v1/watch/events", "/api/v1/watch/alerts",
    "/api/v1/watch/recordings", "/api/v1/hunt/metrics", "/api/v1/hunt/replay",
    "/api/v1/hypotheses", "/api/v1/scheduler/status", "/api/v1/dashboard",
])
def test_frontend_supporting_routes_are_mounted_and_return_json(interface_client, path):
    response = interface_client.get(path)
    assert response.status_code == 200, response.text[:300]
    assert "application/json" in response.headers["content-type"]
    assert isinstance(response.json(), dict)
