"""Open-set assignment keeps the base corpus between items (pipeline._base_genomes): same assignments as loading it
per item; a changed base incident is reloaded."""

from __future__ import annotations

import shutil

import pytest

from hydra import pipeline
from hydra.config import Settings

ONLY = ["inf_url", "inf_dom", "nar", "meta", "artifact_sha256"]


@pytest.fixture(autouse=True)
def _no_local_warninglists(tmp_path, monkeypatch):
    """merge-batch3: real extractors here; without this the result depended on the local MISP snapshot outside git
    (operator-genes loads all 231 lists, and rfc6761 makes the fixture's `*.example` hosts boilerplate)."""
    from hydra.enrich import warninglists

    monkeypatch.setenv("HYDRA_MISP_WL_DIR", str(tmp_path / "no-warninglists"))
    warninglists.cache_clear()
    yield
    warninglists.cache_clear()


def _settings(root):
    s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                 state_dir=root / "state", workers=1, offline=True, enrich=False)
    s.ensure_dirs()
    return s


def _items(ds, tmp_path):
    out = []
    for src in ("1", "4", "6", "8", "2"):
        dst = tmp_path / "items" / f"new-{src}"
        shutil.copytree(ds / src, dst)
        out.append(dst)
    return out


def _run(s, items):
    res = []
    for it in items:
        _run_id, a = pipeline.assign_new(it, s, only=ONLY)
        d = a.model_dump()
        d["meta"] = {k: v for k, v in d["meta"].items() if k not in ("job_id", "base_run")}
        res.append(d)
    return res


def test_assign_base_cache_gives_identical_assignments(tmp_path, make_png, monkeypatch):
    from core.test_pipeline import corpus

    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    ds = corpus(tmp_path, make_png)
    items = _items(ds, tmp_path)
    out = {}
    for mode, flag in (("off", "0"), ("on", "1")):
        monkeypatch.setenv("HYDRA_ASSIGN_BASE_CACHE", flag)
        pipeline._BASE_MEMO.clear()
        s = _settings(tmp_path / mode)
        pipeline.analyse([ds], s, only=ONLY)
        loads: list[int] = []
        real = pipeline.load_genomes

        def counting(c, ids, *a, loads=loads, real=real, **k):
            loads.append(len(ids))
            return real(c, ids, *a, **k)

        monkeypatch.setattr(pipeline, "load_genomes", counting)
        out[mode] = _run(s, items)
        monkeypatch.setattr(pipeline, "load_genomes", real)
        big = [n for n in loads if n > 1]
        assert len(big) == (len(items) if mode == "off" else 1), loads  # base corpus loaded once with the cache
    assert out["on"] == out["off"]
    assert {a["decision"] for a in out["on"]} >= {"assign"}


def test_assign_base_cache_reloads_a_changed_base(tmp_path, make_png, monkeypatch):
    from contextlib import closing

    from core.test_pipeline import corpus

    from hydra.db import Writer, connect

    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    monkeypatch.setenv("HYDRA_ASSIGN_BASE_CACHE", "1")
    pipeline._BASE_MEMO.clear()
    ds = corpus(tmp_path, make_png)
    items = _items(ds, tmp_path)
    s = _settings(tmp_path / "st")
    pipeline.analyse([ds], s, only=ONLY)
    _run(s, items[:1])
    sig = pipeline._BASE_MEMO["sig"]
    w = Writer(s.db_path)
    try:  # a base incident loses one gene (e.g. an LLM narrative gene deleted outside extraction)
        w.run(lambda c: c.execute("DELETE FROM genes WHERE id ="
                                  " (SELECT MAX(id) FROM genes WHERE incident_id='nask-2')"))
    finally:
        w.close()
    _run(s, items[1:2])
    assert pipeline._BASE_MEMO["sig"] != sig
    with closing(connect(s.db_path, readonly=True)) as c:
        n = c.execute("SELECT COUNT(*) FROM genes WHERE incident_id='nask-2'").fetchone()[0]
    assert sum(1 for _ in pipeline._BASE_MEMO["genomes"]["nask-2"].genes) == n
