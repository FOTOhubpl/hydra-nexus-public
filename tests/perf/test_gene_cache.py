"""Per-extractor gene cache (audit A06 / A07), from the audit reproductions
(~/HydraNexus/audit-2026-11-21/reproduce_findings.py: handled_extractor_failure_cached,
changed_options_use_stale_genes). Synthetic data only."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import pytest

from hydra import pipeline
from hydra.adapters.nask_folder import import_incident
from hydra.config import Settings
from hydra.contracts import Family, Gene
from hydra.db import Writer, connect
from hydra.genome import base
from hydra.storage import ContentStore


def _settings(root: Path, **kw) -> Settings:
    s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                 state_dir=root / "state", workers=1, offline=True, enrich=False, **kw)
    s.ensure_dirs()
    return s


def _page(folder: Path, account: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.html").write_text(f'<html><body><div data-account="{account}">synthetic</div></body></html>')
    return folder


@pytest.fixture
def reg():
    added: list[str] = []

    def add(name: str, fn, family=Family.INF):
        base.register(name, family=family)(fn)
        added.append(name)
        return name

    yield add
    for n in added:
        base.REGISTRY.pop(n, None)


@pytest.fixture
def one(tmp_path):
    s = _settings(tmp_path / "s")
    w = Writer(s.db_path)
    store = ContentStore(s.cas_dir)
    inc = import_incident(_page(s.data_dir / "one", "acc"), w, store, s).incident
    yield s, w, store, inc
    w.close()


def _gene(inc, kind, value, ex):
    return Gene(incident_id=inc.incident.id, family="INF", kind=kind, value=value, extractor=ex)


def _statuses(s: Settings) -> list[tuple[str, str]]:
    with closing(connect(s.db_path, readonly=True)) as c:
        return [(r[0], r[1]) for r in c.execute("SELECT kind, status FROM genes ORDER BY id")]


def test_handled_failure_is_retried_and_success_stays_cached(one, reg):
    """A06: a handled exception (failed gene) is retried next run; the healthy extractor is not re-run."""
    s, w, store, inc = one
    calls = {"flaky": 0, "ok": 0}

    def flaky(i, ctx):
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("synthetic transient extractor failure")
        return base.ExtractResult(genes=[_gene(i, "retry", "recovered", "t_flaky@1")])

    def ok(i, ctx):
        calls["ok"] += 1
        return base.ExtractResult(genes=[_gene(i, "fine", "v", "t_ok@1")])

    only = [reg("t_ok", ok), reg("t_flaky", flaky)]
    st1 = pipeline.extract([inc], w, store, s, only=only)
    assert _statuses(s) == [("fine", "observed"), ("t_flaky", "failed")]
    assert st1[inc.id]["retry_pending"] is True
    st2 = pipeline.extract([inc], w, store, s, only=only)
    assert calls == {"flaky": 2, "ok": 1}
    assert st2[inc.id]["cached"] is False and st2[inc.id]["extractors_run"] == 1
    assert _statuses(s) == [("fine", "observed"), ("retry", "observed")]
    st3 = pipeline.extract([inc], w, store, s, only=only)
    assert st3[inc.id]["cached"] is True and calls == {"flaky": 2, "ok": 1}


def test_retry_limit_and_backoff(one, reg, monkeypatch):
    s, w, store, inc = one
    calls = []

    def boom(i, ctx):
        calls.append(1)
        raise RuntimeError("always")

    only = [reg("t_boom", boom)]
    monkeypatch.setenv("HYDRA_EXTRACT_RETRIES", "3")
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_S", (0.0, 3600.0))
    pipeline.extract([inc], w, store, s, only=only)
    pipeline.extract([inc], w, store, s, only=only)   # first retry: immediate
    assert len(calls) == 2
    st = pipeline.extract([inc], w, store, s, only=only)  # second retry: backoff not elapsed
    assert len(calls) == 2 and st[inc.id]["cached"] is True
    monkeypatch.setattr(pipeline, "RETRY_BACKOFF_S", (0.0, 0.0))
    pipeline.extract([inc], w, store, s, only=only)   # attempt 3 = limit
    for _ in range(3):
        st = pipeline.extract([inc], w, store, s, only=only)
    assert len(calls) == 3 and st[inc.id]["cached"] is True
    assert _statuses(s) == [("t_boom", "failed")]  # the failure stays visible


def test_changed_options_rerun(one, reg):
    """A07: ExtractContext.options are part of the digest."""
    s, w, store, inc = one
    seen = []

    def opt(i, ctx):
        seen.append(ctx.options["value"])
        return base.ExtractResult(genes=[_gene(i, "option", ctx.options["value"], "t_opt@1")])

    only = [reg("t_opt", opt)]
    pipeline.extract([inc], w, store, s, only=only, options={"value": "old"})
    st = pipeline.extract([inc], w, store, s, only=only, options={"value": "new"})
    assert seen == ["old", "new"] and st[inc.id]["cached"] is False
    st = pipeline.extract([inc], w, store, s, only=only, options={"value": "new"})
    assert seen == ["old", "new"] and st[inc.id]["cached"] is True


def test_model_revision_and_enrichment_state_in_digest(one, reg, monkeypatch):
    s, w, store, inc = one
    calls = {"inf_t": 0, "t_plain": 0}

    def mk(name):
        def fn(i, ctx):
            calls[name] += 1
            return base.ExtractResult(genes=[_gene(i, name, "v", f"{name}@1")])
        return fn

    only = [reg("inf_t", mk("inf_t")), reg("t_plain", mk("t_plain"))]
    monkeypatch.setattr(pipeline, "model_revisions", lambda: {"dinov2-small": "rev-a"})
    pipeline.extract([inc], w, store, s, only=only)
    monkeypatch.setattr(pipeline, "model_revisions", lambda: {"dinov2-small": "rev-b"})
    pipeline.extract([inc], w, store, s, only=only)
    assert calls == {"inf_t": 2, "t_plain": 2}
    s2 = s.model_copy(update={"enrich_cache": s.cache_dir / "other-enrich"})
    st = pipeline.extract([inc], w, store, s2, only=only)
    assert calls == {"inf_t": 3, "t_plain": 2}  # enrichment source state only keys the inf_* extractors
    assert st[inc.id]["extractors_run"] == 1
    v = pipeline.extract_versions(s2, only, None)
    assert v["models"] == {"dinov2-small": "rev-b"} and v["enrich"]["cache"].endswith("other-enrich")


def _rows(db: Path):
    with closing(connect(db, readonly=True)) as c:
        g = [tuple(r) for r in c.execute(
            "SELECT incident_id, family, kind, value, vector_ref, quality, status, provenance, evidence, extractor,"
            " dependency_group FROM genes ORDER BY id")]
        v = [tuple(r) for r in c.execute("SELECT key, model, dim, data FROM gene_vectors ORDER BY key")]
        runs = [json.loads(r[0]) for r in c.execute("SELECT params FROM runs WHERE kind='analyse'")]
    return g, v, runs


@pytest.mark.parametrize("workers", ["0", "3"])
def test_partial_rerun_is_byte_identical(tmp_path, make_png, monkeypatch, workers):
    """Re-running one extractor from the cache gives exactly the rows of a full run (sequential and parallel)."""
    from core.test_pipeline import corpus

    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", workers)
    ds = corpus(tmp_path, make_png)
    only = ["inf_url", "inf_dom", "nar", "meta", "artifact_sha256"]
    s = _settings(tmp_path / "st")
    first = pipeline.analyse([ds], s, only=only)
    full = _rows(s.db_path)
    monkeypatch.setattr(base.REGISTRY["nar"], "version", 99)  # new nar tag: only nar's digest changes
    second = pipeline.analyse([ds], s, only=only)
    part = _rows(s.db_path)
    assert all(v["extractors_run"] == 1 for v in second.metrics["extract"].values())
    assert part[:2] == full[:2]
    assert sorted(tuple(c.members) for c in second.clustering.campaigns) == \
        sorted(tuple(c.members) for c in first.clustering.campaigns)
    third = pipeline.analyse([ds], s, only=only)
    assert all(v["cached"] for v in third.metrics["extract"].values())
    man = part[2][-1]["extract_versions"]
    assert set(man) == {"cache_version", "extractors", "options_sha", "models", "enrich", "references"}
    assert man["extractors"]["nar"] == "nar@99"


def test_gene_cas_reuses_content_across_states_and_incidents(tmp_path, make_png, monkeypatch):
    """CAS-level cache: a second state on the same corpus (shared cache dir) and a copy of an incident under a new id
    get their genes from the CAS, byte-identical to extracting them with the CAS off."""
    import shutil

    from core.test_pipeline import corpus

    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    ds = corpus(tmp_path, make_png)
    shutil.copytree(ds / "1", ds / "1copy")  # same files, new incident id
    only = ["inf_url", "inf_dom", "nar", "meta", "artifact_sha256"]
    cache = tmp_path / "shared-cache"

    def run(name: str, cas: bool):
        monkeypatch.setenv("HYDRA_GENE_CAS", "1" if cas else "0")
        root = tmp_path / name
        # one HYDRA_CACHE_DIR for every state, as on the server (the enrichment replay cache lives there too)
        s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=cache,
                     state_dir=root / "state", workers=1, offline=True, enrich=False)
        s.ensure_dirs()
        res = pipeline.analyse([ds], s, only=only)
        return res, _rows(s.db_path)[:2]

    first, rows1 = run("a", True)
    second, rows2 = run("b", True)
    _, rows_off = run("c", False)
    st = second.metrics["extract"]
    cacheable = [n for n in only if not pipeline.is_dependent(n)]
    assert all(v["extractors_from_cas"] == len(cacheable) for v in st.values()), st
    assert all(v["extractors_run"] == len(only) - len(cacheable) for v in st.values())
    assert rows2 == rows1 == rows_off
    assert first.metrics["extract"]["nask-1copy"]["extractors_from_cas"] in (0, len(cacheable))
    g = {(r[0], r[2], r[3]) for r in rows2[0]}
    assert {(k, v) for i, k, v in g if i == "nask-1"} == {(k, v) for i, k, v in g if i == "nask-1copy"}


def test_gene_cas_key_covers_numerics_and_code(one, monkeypatch):
    s, _w, _store, inc = one
    k = pipeline.cas_key(inc, "fp", s)
    monkeypatch.setenv("HYDRA_TORCH_THREADS", "3")
    assert pipeline.cas_key(inc, "fp", s) != k  # bf16 vectors depend on the thread count
    monkeypatch.delenv("HYDRA_TORCH_THREADS")
    monkeypatch.setattr(pipeline, "extraction_code_digest", lambda: "edited")
    assert pipeline.cas_key(inc, "fp", s) != k
