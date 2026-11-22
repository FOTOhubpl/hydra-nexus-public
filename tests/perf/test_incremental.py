"""Incremental analyse (pipeline.StageCache): every reused stage gives exactly the result of a full run."""

from __future__ import annotations

import json
import shutil

from hydra import pipeline
from hydra.config import Settings
from hydra.db import Writer
from hydra.review import new_review

ONLY = ["inf_url", "inf_dom", "nar", "meta", "artifact_sha256"]


def _settings(root):
    s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                 state_dir=root / "state", workers=1, offline=True, enrich=False)
    s.ensure_dirs()
    return s


def _snap(res):
    m = res.metrics
    camps = sorted((c.id, tuple(c.members), c.group_id, c.label, c.confidence) for c in res.clustering.campaigns)
    groups = sorted((g.id, tuple(g.campaign_ids), g.confidence) for g in res.clustering.groups)
    assign = {k: v for k, v in m["assignment"].items() if k != "seconds"}
    return res.pairs, camps, groups, m["conformal"], assign, m["campaign_groups"], m["alerts"]


def _reject(s, a, b):
    r = new_review("reject_link", {"a": a, "b": b})
    w = Writer(s.db_path)
    try:
        w.run(lambda c: c.execute("INSERT INTO reviews (id, action, target, analyst, note, undoes, created_at)"
                                  " VALUES (?,?,?,?,?,?,?)", (r.id, r.action, json.dumps(r.target), r.analyst,
                                                              r.note, r.undoes, r.created_at)))
    finally:
        w.close()


def test_incremental_analyse_equals_full_run(tmp_path, make_png, monkeypatch):
    from core.test_pipeline import corpus

    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    ds = corpus(tmp_path, make_png)
    s = _settings(tmp_path / "st")

    full = pipeline.analyse([ds], s, only=ONLY)
    first = pipeline.analyse([ds], s, only=ONLY, incremental=True)
    assert first.metrics["incremental"]["stages_reused"] == []
    again = pipeline.analyse([ds], s, only=ONLY, incremental=True)
    assert again.metrics["incremental"]["stages_reused"] == ["score", "cluster", "conformal", "self_assign"]
    assert _snap(first) == _snap(again) == _snap(full)
    from contextlib import closing

    from hydra.db import connect

    with closing(connect(s.db_path, readonly=True)) as c:
        metas = [json.loads(r[0])["meta"] for r in c.execute("SELECT detail FROM assignments WHERE run_id=?",
                                                              (again.run_id,))]
    assert metas and all(m["base_run"] == again.run_id for m in metas)  # reused rows carry the new run id

    # analyst decision: scoring is reused, clustering and everything after it recomputed
    _reject(s, "nask-1", "nask-2")
    inc = pipeline.analyse([ds], s, only=ONLY, incremental=True)
    assert inc.metrics["incremental"]["stages_reused"] == ["score"]
    ref = pipeline.analyse([ds], s, only=ONLY)
    assert _snap(inc) == _snap(ref)

    # a new incident changes every document frequency: nothing is reused, still equal to a full run
    shutil.copytree(ds / "4", ds / "4bis")
    inc = pipeline.analyse([ds], s, only=ONLY, incremental=True)
    assert inc.metrics["incremental"]["stages_reused"] == []
    ref = pipeline.analyse([ds], s, only=ONLY)
    assert _snap(inc) == _snap(ref)


def test_stage_key_covers_code_and_thresholds(tmp_path, make_png, monkeypatch):
    """A changed threshold or analysis code never reuses a stage."""
    from core.test_pipeline import corpus

    from hydra.synapse import Thresholds

    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    ds = corpus(tmp_path, make_png)
    s = _settings(tmp_path / "st")
    pipeline.analyse([ds], s, only=ONLY, incremental=True)
    th = Thresholds()
    th.link = th.link + 1.0
    r = pipeline.analyse([ds], s, only=ONLY, incremental=True, thresholds=th)
    assert r.metrics["incremental"]["stages_reused"] == []
    monkeypatch.setattr(pipeline, "analysis_code_digest", lambda: "edited")
    r = pipeline.analyse([ds], s, only=ONLY, incremental=True)
    assert r.metrics["incremental"]["stages_reused"] == []
