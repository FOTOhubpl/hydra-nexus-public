"""Export log (hydra/export_log.py): every export endpoint leaves an EXPORT event counted per run."""

from __future__ import annotations

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from hydra import events
from hydra.api import create_app
from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.export_log import export_counts, record_export
from ui_api.synthetic import RUN_ID, seed


@pytest.fixture
def client(settings: Settings):
    seed(settings.db_path, settings.cas_dir)
    with TestClient(create_app(settings)) as c:
        yield c


def _counts(settings: Settings, run: str | None = RUN_ID) -> dict:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        return export_counts(conn, run)


def test_export_endpoints_are_counted_per_run(client, settings):
    assert _counts(settings) == {"total": 0, "by_kind": {}, "last_at": None, "items": {}}
    assert client.get("/v1/campaigns/syn-c1/export").status_code == 200
    assert client.get("/v1/campaigns/syn-c1/export", params={"format": "csv"}).status_code == 200
    for fmt in ("cypher", "nodes_csv", "edges_csv"):
        assert client.get("/v1/export/neo4j", params={"format": fmt}).status_code == 200
    assert client.post("/v1/graph/export", json={"ids": ["syn-1", "syn-2"], "format": "json"}).status_code == 200
    c = _counts(settings)
    assert c["by_kind"] == {"campaign_json": 1, "campaign_csv": 1, "neo4j_cypher": 1, "neo4j_nodes_csv": 1,
                            "neo4j_edges_csv": 1, "subgraph_json": 1}
    assert c["total"] == 6 and c["last_at"] and c["items"]["incidents"] >= 2
    assert _counts(settings, "other-run")["total"] == 0
    with closing(connect(settings.db_path, readonly=True)) as conn:
        assert events.verify_chain(conn)[0]


def test_failed_export_is_not_counted(client, settings):
    assert client.get("/v1/campaigns/nope/export").status_code == 404
    assert _counts(settings)["total"] == 0


def test_record_export_never_raises_and_keeps_no_paths(settings):
    class Broken:
        def run(self, *_a, **_k):
            raise RuntimeError("db gone")

    record_export(Broken(), "dataset", RUN_ID, {"incidents": 3})  # no exception

    seed(settings.db_path, settings.cas_dir)
    w = Writer(settings.db_path)
    try:
        record_export(w, "dataset", RUN_ID, {"incidents": 3}, out_dir="/Users/someone/x", fmt="parquet")
    finally:
        w.close()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        row = conn.execute("SELECT payload FROM events WHERE type='EXPORT'").fetchone()
        assert "/Users" not in row["payload"] and "parquet" in row["payload"]
        assert events.verify_chain(conn)[0]
    assert _counts(settings)["by_kind"] == {"dataset": 1}


def test_stix_exports_are_counted(client, settings):
    r = client.get(f"/v1/runs/{RUN_ID}/stix")
    if r.status_code != 200:
        pytest.skip(f"run STIX not available on the synthetic run: {r.status_code}")
    assert client.get("/v1/campaigns/syn-c1/stix").status_code == 200
    by = _counts(settings)["by_kind"]
    assert by["stix_run"] == 1 and by["stix_campaign"] == 1


def test_system_flow_reports_exports_reviews_and_hints(client, settings):
    b = client.get("/v1/system/flow", params={"run_id": RUN_ID}).json()["boxes"]
    assert b["outputs"]["exports"]["total"] == 0 and b["outputs"]["hint_pl"]
    assert client.get("/v1/campaigns/syn-c1/export").status_code == 200
    b = client.get("/v1/system/flow", params={"run_id": RUN_ID}).json()["boxes"]
    assert b["outputs"]["exports"]["by_kind"] == {"campaign_json": 1} and b["outputs"]["hint_pl"] is None
    assert "hint_pl" in b["reviews"] and b["genes"]["extractors"]
    for name, st in b["genes"]["extractors"].items():  # run without metrics.extractors: from the genes table
        assert set(st) >= {"observed", "unavailable", "failed"}, name
