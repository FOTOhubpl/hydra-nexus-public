"""Corpus flag, live progress, external rarity prior and explicit fusion training (synthetic data only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra.adapters.nask_folder import import_incident
from hydra.config import Settings
from hydra.corpus import corpus_of, matches
from hydra.db import Writer, connect
from hydra.pipeline import analyse
from hydra.progress import Progress, run_progress
from hydra.storage import ContentStore
from tests.core.test_pipeline import N_INC, corpus, page, test_extractors  # noqa: F401 — fixture


def ext_dataset(root: Path, groups: int = 8, per: int = 4, name: str = "ext") -> Path:
    ds = root / name
    for gi in range(groups):
        for k in range(per):
            page(ds / f"g{gi}_{k}", f"brand{gi}.example",
                 f'<i data-acct="kit-{gi}"></i><i data-pixel="{9000 + gi}"></i>')
    return ds


def import_ext(settings: Settings, ds: Path, source: str = "ext:test", labelled: bool = True) -> list[str]:
    w = Writer(settings.db_path)
    try:
        ids = []
        for d in sorted(ds.iterdir()):
            rep = import_incident(d, w, ContentStore(settings.cas_dir), settings, source=source, prefix="ext-test",
                                  label=d.name.split("_")[0] if labelled else None)
            ids.append(rep.incident.id)
        return ids
    finally:
        w.close()


def test_corpus_mapping_and_specs():
    assert corpus_of("nask-sample") == "nask" and corpus_of("upload") == "upload"
    assert corpus_of("ext:phish") == "ext:phish" and corpus_of("public:certpl") == "public:certpl"
    assert matches("nask", None) and matches("upload", "nask") and not matches("ext:phish", None)
    assert matches("ext:phish", "extended") and not matches("public:certpl", "extended")
    assert matches("public:certpl", "all") and matches("ext:phish", "ext:phish,public:x")
    assert matches("ext:a", "ext:all") and not matches("nask", "ext:all")


def test_runs_are_scoped_to_their_corpus(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    ext_ids = import_ext(settings, ext_dataset(tmp_path, groups=2, per=2))
    nask = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    assert len(nask.incidents) == N_INC and not set(nask.incidents) & set(ext_ids)
    again = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors, corpus="nask")
    assert sorted(again.incidents) == sorted(nask.incidents)  # explicit nask spec: stored external data stays out
    ext = analyse([], settings, only=test_extractors, corpus="ext:test")
    assert sorted(ext.incidents) == sorted(ext_ids) and ext.metrics["corpora"] == {"ext:test": 4}
    both = analyse([], settings, only=test_extractors, corpus="all")
    assert len(both.incidents) == N_INC + 4
    with connect(settings.db_path, readonly=True) as conn:
        params = {r["id"]: json.loads(r["params"]) for r in conn.execute("SELECT id, params FROM runs")}
    assert params[nask.run_id]["corpus"] == "nask" and params[ext.run_id]["corpus"] == "ext:test"


def test_progress_is_reported_and_throttled(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    p = run_progress(res.job_id, settings)
    assert p["stage"] == "done" and p["status"] == "done" and p["job"]["status"] == "done"
    assert p["incidents_done"] == p["incidents_total"] == N_INC
    assert p["genes_by_family"].get("INF", 0) > 0 and p["pairs_scored"] == N_INC * (N_INC - 1) // 2
    assert p["campaigns"] == len(res.clustering.campaigns) and p["run_id"] == res.run_id
    assert run_progress("nope", settings) is None
    # throttling: 100 incident updates within the interval cost at most a handful of writes
    w = Writer(settings.db_path)
    try:
        from hydra.jobs import create_job

        jid = create_job(w, "analyse", {})
        calls = {"n": 0}
        real = w.run

        def counting(fn, timeout=60):
            calls["n"] += 1
            return real(fn, timeout)

        w.run = counting  # type: ignore[method-assign]
        pr = Progress(settings, w, jid, min_interval=60)
        pr.stage("genes", incidents_total=1000)
        for _ in range(100):
            pr.incident_done([])
        assert calls["n"] <= 3
    finally:
        w.run = real  # type: ignore[method-assign]
        w.close()


def test_external_prior_lowers_bits_of_values_common_outside(settings: Settings, make_png, tmp_path,
                                                            test_extractors, monkeypatch):  # noqa: F811
    # exact prior size below: the shipped prior must be off also when the run sets HYDRA_SAMPLE_TEST=1 (conftest then
    # keeps the product default `mixed`, and the prior becomes shipped + ext; SCORE-GROWTH 2026-11-22)
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "0")
    from hydra.entropy import BackgroundPrior, build_rarity
    from hydra.synapse import build_genome, score_pair
    from tests.core.test_synapse import g

    genes = {f"i{k}": [g(f"i{k}", "host", f"h{k}")] for k in range(6)}
    genes["i0"].append(g("i0", "operator_id", "common", "INF:operator:acct"))
    genes["i1"].append(g("i1", "operator_id", "common", "INF:operator:acct"))
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    r = build_rarity(genes, {})
    before = score_pair(gen["i0"], gen["i1"], r).campaign_bits
    r.prior = BackgroundPrior("ext:x", 1000, {"operator_id": 1000}, {("operator_id", "common"): 400})
    after = score_pair(gen["i0"], gen["i1"], r).campaign_bits
    assert after < before / 3
    # pipeline: prior built from external incidents only, recorded in metrics
    import_ext(settings, ext_dataset(tmp_path, groups=3, per=2))
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors, prior="ext:all")
    assert res.metrics["prior"] == {"corpus": "ext:all", "incidents": 6}
    assert res.rarity.prior is not None and res.rarity.prior.n == 6  # 6 external incidents, no NASK ones


def test_train_on_labelled_external_corpus(settings: Settings, tmp_path, test_extractors):  # noqa: F811
    from hydra.train import load_fusion, train_fusion

    import_ext(settings, ext_dataset(tmp_path, groups=10, per=4))
    analyse([], settings, only=test_extractors, corpus="ext:test")  # genes for the external incidents
    prov = train_fusion(settings, "ext:test", min_pairs=20)
    assert prov["includes_organiser_data"] is False and prov["groups"]["test"] == 3
    assert prov["held_out"]["fused"]["n"] > 0 and "ece" in prov["held_out"]["fused"]
    fm = load_fusion(Path(prov["path"]), settings)
    outside = tmp_path / "x.pkl"
    outside.write_bytes(Path(prov["path"]).read_bytes())
    outside.with_suffix(".json").write_text(Path(prov["path"]).with_suffix(".json").read_text())
    with pytest.raises(ValueError, match="load only from"):
        load_fusion(outside, settings)
    res = analyse([], settings, only=test_extractors, corpus="ext:test", fusion_path=Path(prov["path"]))
    assert res.metrics["scorer"].startswith("lr_isotonic@1") and any(pe.score.calibrated for pe in res.pairs.values())
    Path(prov["path"]).write_bytes(Path(prov["path"]).read_bytes() + b"x")
    with pytest.raises(ValueError, match="sha256"):
        load_fusion(Path(prov["path"]), settings)
    assert fm.n_pairs >= 20


def test_train_refuses_organiser_data_by_default(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    from hydra.train import train_fusion

    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with pytest.raises(ValueError, match="external corpora only"):
        train_fusion(settings, "nask")


def test_analyse_input_progress_and_explanation(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    from hydra.pipeline import analyse_input, explain_assignment
    from tests.core.test_pipeline import PX

    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    page(tmp_path / "new_a", "evil-a.example", "+48 700 100 100" + PX)
    out = analyse_input("file", tmp_path / "new_a", settings, only=test_extractors)
    p = run_progress(out["job_id"], settings)
    assert p["stage"] == "done" and p["status"] == "done" and p["decision"] == "assign"
    assert p["stages"] == ["import", "genes", "enrich", "candidates", "scoring", "decision", "done"]
    assert set(p["extractors"]) == {"t_page", "t_vec"} and p["extractors"]["t_page"]["genes"] > 0
    assert p["genes_by_family"]["INF"] > 0 and p["candidates"]["total"] == p["pairs_total"] > 0
    assert p["candidates"]["exact"]["host"] >= 1 and p["pairs_scored"] >= 1
    for key in (out["job_id"], out["incident_id"]):
        e = explain_assignment(key, settings)
        assert e["decision"] == "assign" and e["incident_id"] == out["incident_id"]
        m = e["meta"]
        assert len(m["top_candidates"]) >= 1 and m["top_candidates"][0]["campaign_id"] == e["campaign_id"]
        top = m["top_candidates"][0]
        assert {"campaign_bits", "kit_bits", "asset_bits", "campaign_by_family", "support"} <= set(top)
        assert m["missing_bits_to_link"] == 0 and m["link_rule_met"] is True and m["margin_bits"] > 0
        assert m["best_link_robustness"]["link"] == "campaign" and "conformal" in m
    assert explain_assignment("unknown-id", settings) is None or explain_assignment("unknown-id", settings)[
        "assignment"] is None


def materialised(root: Path, n: int = 4, kind: str = "advertiser_page") -> Path:
    out = root / "corpus-mat"
    doc = {"incidents": {}}
    for k in range(n):
        folder = f"x{k}"
        page(out / folder, f"adv{k // 2}.example", f'<i data-acct="page-{k // 2}"></i>')
        doc["incidents"][folder] = {"id": f"ext-mat-{k}", "dataset": "mat", "source": "ext:mat",
                                    "label": f"ext:mat:p{k // 2}", "label_kind": kind, "verdict": "SHIP-SAFE",
                                    "group_label": "ext:mat:g", "roles": {}}
    (out / "hydra_corpus.json").write_text(json.dumps(doc))
    return out


def test_materialised_external_corpus_keeps_its_flag(settings: Settings, make_png, tmp_path,
                                                     test_extractors):  # noqa: F811
    out = materialised(tmp_path)
    res = analyse([out], settings, only=test_extractors)  # default NASK mode
    with connect(settings.db_path, readonly=True) as conn:
        src = {r["id"]: r["source"] for r in conn.execute("SELECT id, source FROM incidents")}
    assert all(src[f"ext-mat-{k}"] == "ext:mat" for k in range(4))
    assert res.incidents == [] and len(res.metrics["excluded_by_corpus_flag"]) == 4
    assert analyse([], settings, only=test_extractors, corpus="nask").incidents == []
    assert len(analyse([], settings, only=test_extractors, corpus="ext:mat").incidents) == 4
    from hydra.pipeline import import_input

    w = Writer(settings.db_path)
    try:
        with pytest.raises(ValueError, match="external corpus"):
            import_input(out / "x0", w, ContentStore(settings.cas_dir), settings)
    finally:
        w.close()


def test_train_refuses_brand_labels_and_marks_page_labels_weak(settings: Settings, tmp_path,
                                                               test_extractors):  # noqa: F811
    from hydra.train import train_fusion

    brand = materialised(tmp_path / "b", n=4, kind="brand_target")
    analyse([brand], settings, only=test_extractors)
    with pytest.raises(ValueError, match="never\\s+campaign labels|never campaign"):
        train_fusion(settings, "ext:mat")
