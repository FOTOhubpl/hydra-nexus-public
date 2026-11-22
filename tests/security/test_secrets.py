"""Synthetic canary credentials are scanned after decoding every generated export representation."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.db import connect
from hydra.export_abuse import abuse_package
from hydra.export_dataset import export_dataset
from hydra.export_neo4j import export_run
from hydra.export_stix import export_stix
from hydra.llm.client import LLMClient, LLMConfig, redact_content
from hydra.redact import redact_obj, redact_text, redact_url
from tests.security.helpers import SECRET_TEXT, TOKEN, decoded_package, run_import, seed


@pytest.mark.parametrize("kind", ["url", "bearer", "structured-password"])
def test_recognized_secret_forms_are_removed(kind):
    if kind == "url":
        result = redact_url(f"https://campaign.example/?access_token={TOKEN}&ordinary=keep")
        assert "ordinary=keep" in result
    elif kind == "bearer":
        result = redact_text(f"Authorization: Bearer {TOKEN}")
    else:
        result = json.dumps(redact_obj({"password": TOKEN}))
    assert TOKEN not in result


@pytest.mark.parametrize("kind", ["html-input", "har-header", "json-text", "hex-password", "url-userinfo",
                                 "llm-content"])
def test_structured_or_embedded_credentials_are_redacted(kind):
    if kind == "html-input":
        result = redact_text(f'<input name="password" value="{TOKEN}">')
    elif kind == "har-header":
        result = json.dumps(redact_obj({"request": {"headers": [{"name": "X-Api-Key", "value": TOKEN}]}}))
    elif kind == "json-text":
        result = redact_text(json.dumps({"password": TOKEN}))
    elif kind == "hex-password":
        secret = "ab12" * 8
        assert secret not in json.dumps(redact_obj({"password": secret}))
        return
    elif kind == "url-userinfo":
        result = redact_url(f"https://synthetic:{TOKEN}@campaign.example/")
    else:
        result = json.dumps(redact_content([{"type": "text", "text": f'<input name="api_key" value="{TOKEN}">'}]))
    assert TOKEN not in result


@pytest.mark.parametrize("target", [
    "neo4j-files", "dataset", "campaign-json", "campaign-csv", "neo4j-cypher", "neo4j-nodes",
    "subgraph-json", "subgraph-cypher", "neo4j-edges", "stix-campaign", "stix-run", "abuse",
])
def test_every_export_surface_redacts_secrets(settings, tmp_path, target):
    campaign, run, _ = seed(settings, title=SECRET_TEXT, value=SECRET_TEXT)
    if target == "neo4j-files":
        out = tmp_path / "neo4j"
        with closing(connect(settings.db_path, readonly=True)) as conn:
            export_run(conn, run, out)
        text = "\n".join(p.read_text() for p in out.iterdir())
    elif target == "dataset":
        import pandas as pd

        out = tmp_path / "dataset"
        export_dataset(settings, run, out)
        text = "\n".join(pd.read_parquet(p).to_json() if p.suffix == ".parquet" else p.read_text()
                         for p in out.iterdir())
    elif target.startswith("stix-"):
        text = json.dumps(export_stix(settings, campaign.id if target == "stix-campaign" else None, run))
    elif target == "abuse":
        text = decoded_package(abuse_package(settings, campaign.id, run)[0])
    else:
        with TestClient(create_app(settings)) as client:
            if target.startswith("subgraph-"):
                response = client.post("/v1/graph/export", json={
                    "run_id": run, "ids": ["astra-a", "astra-b"], "format": target.split("-")[1]})
            elif target.startswith("campaign-"):
                response = client.get(f"/v1/campaigns/{campaign.id}/export",
                                      params={"run_id": run, "format": target.split("-")[1]})
            else:
                fmt = {"neo4j-cypher": "cypher", "neo4j-nodes": "nodes_csv", "neo4j-edges": "edges_csv"}[target]
                response = client.get("/v1/export/neo4j", params={"run_id": run, "format": fmt})
        assert response.status_code == 200
        text = response.text
    assert TOKEN not in text, target


def test_abuse_html_evidence_removes_hidden_form_password(settings):
    campaign, run, _ = seed(settings, html=f'<html><input type="hidden" name="password" value="{TOKEN}"></html>')
    assert TOKEN not in decoded_package(abuse_package(settings, campaign.id, run)[0])


def test_abuse_har_excerpt_drops_headers_and_redacts_query(settings):
    har = {"log": {"entries": [{"request": {
        "url": f"https://campaign.example/?access_token={TOKEN}",
        "method": "GET", "headers": [{"name": "X-Api-Key", "value": TOKEN}]},
        "response": {"status": 200}, "serverIPAddress": "192.0.2.1"}]}}
    campaign, run, _ = seed(settings, har=json.dumps(har))
    assert TOKEN not in decoded_package(abuse_package(settings, campaign.id, run)[0])


def test_artifact_text_preview_does_not_disclose_credentials(settings, tmp_path):
    path = tmp_path / "page.html"
    path.write_text(f'<html><input name="password" value="{TOKEN}"></html>')
    imported = run_import(settings, [path])
    digest = imported["items"][0]["artifact_id"]
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/v1/artifacts/{digest}/content")
    assert response.status_code == 200
    assert TOKEN not in response.text


def test_import_problem_response_redacts_secret_in_filename(settings):
    with TestClient(create_app(settings)) as client:
        result = client.post("/v1/import", files={"files": (f"empty?access_token={TOKEN}.txt", b"")})
        assert result.status_code == 202
        # Joining the runner avoids sleeps and makes the background result deterministic.
        client.app.state.hydra.runner.shutdown()
        response = client.get(f"/v1/jobs/{result.json()['job_id']}")
    assert response.status_code == 200
    assert TOKEN not in response.text


@pytest.mark.parametrize("structured", [False, True])
def test_llm_response_and_replay_cache_are_redacted(settings, monkeypatch, structured):
    cfg = LLMConfig(enabled=True, model="astra-model", fast_model="astra-model", fallback_model=None,
                    region="local", timeout_s=1, max_retries=0, cache_dir=settings.cache_dir / "astra-llm",
                    backend="local")
    llm = LLMClient(cfg)
    response_text = json.dumps({"password": TOKEN}) if structured else SECRET_TEXT
    monkeypatch.setattr(llm, "_post_local", lambda *_a: {
        "choices": [{"message": {"content": response_text}, "finish_reason": "stop"}]})
    params = dict(task="security", model=cfg.model, system="Synthetic test only",
                  content=[{"type": "text", "text": "Return synthetic data"}], inputs_digest="synthetic",
                  schema={"type": "object", "properties": {"password": {"type": "string"}}} if structured else None)
    first = llm.complete(**params)
    replay = llm.complete(**params)
    assert first.ok and replay.ok and replay.cached
    assert TOKEN not in str(first.text) + json.dumps(first.data) + str(replay.text) + json.dumps(replay.data)
    assert all(TOKEN not in p.read_text() for p in cfg.cache_dir.rglob("*.json"))


def test_llm_operational_log_does_not_log_response_content(settings, monkeypatch, caplog):
    cfg = LLMConfig(enabled=True, model="astra-model", fast_model="astra-model", fallback_model=None,
                    region="local", timeout_s=1, max_retries=0, cache_dir=settings.cache_dir / "astra-llm",
                    backend="local")
    llm = LLMClient(cfg)
    monkeypatch.setattr(llm, "_post_local", lambda *_a: {
        "choices": [{"message": {"content": SECRET_TEXT}, "finish_reason": "stop"}]})
    assert llm.complete(task="security", model=cfg.model, system="synthetic",
                        content=[{"type": "text", "text": "synthetic"}], inputs_digest="synthetic").ok
    logs = list(cfg.cache_dir.rglob("*.jsonl"))
    assert logs
    assert TOKEN not in caplog.text + "\n".join(p.read_text() for p in logs)
