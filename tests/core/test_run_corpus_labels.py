"""AUDIT B13 / B16: corpus and source labels in the API layer.

B13: a review revision run (params root_run / revision_of, no corpus, no paths) showed "korpus: unknown"; it now
resolves to the corpus of its root run. B16: a dataset uploaded in the panel was labelled as the NASK sample
(corpus `nask` → "Tylko dane NASK", incidents `nask-sample`); it is now `upload`. Read-time mapping only."""

from __future__ import annotations

import json
from contextlib import closing

from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.api_task import display_source, run_corpus
from hydra.db import Writer, connect
from hydra.jobs import create_job


def _run(writer: Writer, rid: str, params: dict, at: str) -> None:
    writer.run(lambda c: c.execute(
        "INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES (?,?,?,?,?,?)",
        (rid, "analyse", json.dumps(params), "{}", at, at)))


def _world(settings):
    w = Writer(settings.db_path)
    local = create_job(w, "analyse", {"paths": ["/data/nask/sample"], "corpus": "nask"})
    upload = create_job(w, "analyse", {"source": "upload", "input_kind": "corpus", "files": 1})
    _run(w, "run-nask", {"paths": ["/home/x/HydraNexus/data/nask/sample"], "corpus": "nask", "job_id": local},
         "2026-11-22T01:00:00Z")
    _run(w, "run-rev", {"revision_of": "run-nask", "root_run": "run-nask", "review_id": "r1"}, "2026-11-22T02:00:00Z")
    _run(w, "run-rev2", {"revision_of": "run-rev", "root_run": "run-nask", "review_id": "r2"}, "2026-11-22T03:00:00Z")
    _run(w, "run-up", {"paths": ["/state/tmp/analyse-k2x9/incident/jury.zip"], "corpus": "nask", "job_id": upload},
         "2026-11-22T04:00:00Z")
    _run(w, "run-up-rev", {"revision_of": "run-up", "root_run": "run-up"}, "2026-11-22T05:00:00Z")
    _run(w, "run-orphan", {"revision_of": "missing"}, "2026-11-22T00:00:00Z")
    return w


def test_revision_runs_take_the_root_corpus(settings):
    w = _world(settings)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        params = lambda rid: json.loads(conn.execute("SELECT params FROM runs WHERE id=?", (rid,)).fetchone()[0])  # noqa: E731
        assert run_corpus(params("run-rev")) == "unknown"  # without a connection: old behaviour
        assert run_corpus(params("run-rev"), conn) == "nask"
        assert run_corpus(params("run-rev2"), conn) == "nask"
        assert run_corpus(params("run-nask"), conn) == "nask"
        assert run_corpus(params("run-orphan"), conn) == "unknown"
    w.close()


def test_uploaded_dataset_is_labelled_upload(settings):
    w = _world(settings)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        params = lambda rid: json.loads(conn.execute("SELECT params FROM runs WHERE id=?", (rid,)).fetchone()[0])  # noqa: E731
        assert run_corpus(params("run-up"), conn) == "upload"
        assert run_corpus(params("run-up-rev"), conn) == "upload"
    w.close()


def test_api_runs_and_corpora_use_the_resolved_labels(settings):
    _world(settings).close()
    with TestClient(create_app(settings)) as c:
        runs = {r["id"]: r["corpus"] for r in c.get("/v1/runs").json()["items"]}
        assert runs["run-rev"] == runs["run-rev2"] == "nask"
        assert runs["run-up"] == runs["run-up-rev"] == "upload"
        assert c.get("/v1/runs/run-rev").json()["corpus"] == "nask"
        corpora = {i["corpus"]: i for i in c.get("/v1/corpora").json()["items"]}
        assert corpora["nask"]["latest_run"] == "run-rev2"
        assert corpora["upload"]["latest_run"] == "run-up-rev"
        assert "unknown" in corpora and [r["id"] for r in corpora["unknown"]["runs"]] == ["run-orphan"]


def test_display_source():
    assert display_source("nask-sample", "zip-c0c7f26e/incydent_00040") == "upload"
    assert display_source("nask-sample", "analyse-k2x9/a1") == "upload"
    assert display_source("nask-sample", "/Users/x/HydraNexus/data/nask/sample/incydent_1") == "nask-sample"
    assert display_source("nask-sample", "C:\\dane\\incydent_1") == "nask-sample"
    assert display_source("ext:foo", "zip-1/a") == "ext:foo"
    assert display_source("upload", None) == "upload"


def test_graph_overview_corpus_of_a_review_run(settings):
    from hydra.graph_lod import build_base

    _world(settings).close()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        assert build_base(conn, "run-rev2")["corpus"] == "nask"
        assert build_base(conn, "run-up-rev")["corpus"] == "upload"
