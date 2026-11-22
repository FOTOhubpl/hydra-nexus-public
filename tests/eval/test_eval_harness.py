"""EVAL harness tests on synthetic fixtures only (never organiser data)."""

from __future__ import annotations

import io
import json
import random
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval import metrics as M  # noqa: E402
from eval import scorers as S  # noqa: E402
from eval import variants as V  # noqa: E402
from eval.backend import IncidentRef, StubBackend  # noqa: E402
from eval.incidents import materialise  # noqa: E402


def _img(seed: int) -> bytes:
    rng = random.Random(seed)
    im = Image.new("RGB", (160, 120), (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255)))
    d = ImageDraw.Draw(im)
    for _ in range(12):
        x, y = rng.randint(0, 150), rng.randint(0, 110)
        d.rectangle((x, y, x + rng.randint(5, 40), y + rng.randint(5, 40)), fill=(rng.randint(0, 255),) * 3)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def make_incident(root: Path, name: str, domain: str, pixel: str, title: str, words: list[str], img_seed: int,
                  har_name: str | None = None) -> Path:
    d = root / name
    files = d / f"{title}_files"
    files.mkdir(parents=True)
    body = " ".join(words * 3)
    html = (f"<!-- saved from url=(0040)https://{domain}/?utm_id=abc&utm_visit=1 -->\n<html><head><title>{title}"
            f"</title><script>fbq('init', '{pixel}');</script></head><body><h1>{title} headline here</h1>"
            f"<div class='calc'><p>{body}</p></div></body></html>")
    (d / f"{title}.html").write_text(html, encoding="utf-8")
    (files / "hero.png").write_bytes(_img(img_seed))
    (files / "main.css").write_text(f"/* kit {pixel} */ body{{color:red}}", encoding="utf-8")
    (d / "Zrzut ekranu.png").write_bytes(_img(img_seed + 1000))
    har = {"log": {"entries": [
        {"request": {"url": f"https://{domain}/api/v1/trigger/field/0190a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b/"},
         "response": {"status": 200}, "serverIPAddress": "203.0.113.7"},
        {"request": {"url": f"https://www.facebook.com/tr/?id={pixel}&ev=PageView"}, "response": {"status": 200}},
    ]}}
    (d / (har_name or f"{domain}.har")).write_text(json.dumps(har), encoding="utf-8")
    return d


WORDS_A = "platforma inwestycyjna zarabia codziennie tysiące złotych dzięki sztucznej inteligencji".split()
WORDS_B = "suplement diety leczy stawy w siedem dni bez recepty lekarz poleca".split()
WORDS_C = "kasyno bonus darmowe spiny wygrana jackpot aplikacja mobilna graj teraz".split()


@pytest.fixture
def corpus(tmp_path: Path) -> list[IncidentRef]:
    refs = []
    specs = [
        ("a1", "civicnews.live", "1111111111111111", "Zamach na prezesa", WORDS_A, 1, "A", "GA"),
        ("a2", "dailycivic.live", "1111111111111111", "Zamach na prezesa", WORDS_A, 1, "A", "GA"),
        ("a3", "harbornews.live", "1111111111111111", "Zamach na prezesa", WORDS_A, 1, "A", "GA"),
        ("b1", "zdrowie-plus.icu", "2222222222222222", "Stawy zdrowe", WORDS_B, 2, "B", "GB"),
        ("b2", "zdrowie-max.icu", "2222222222222222", "Stawy zdrowe", WORDS_B, 2, "B", "GB"),
        ("c1", "kasyno77.xyz", "3333333333333333", "Słodka wygrana", WORDS_C, 3, "C", "GC"),
    ]
    for name, dom, px, title, words, seed, lab, grp in specs:
        p = make_incident(tmp_path / "inc", name, dom, px, title, words, seed)
        refs.append(IncidentRef(name, p, lab, grp))
    return refs


def _genes(refs: list[IncidentRef], bk: StubBackend) -> dict:
    return {r.iid: bk.extract(r) for r in refs}


def test_metrics_pairwise_and_bcubed() -> None:
    truth = {"a": "1", "b": "1", "c": "2", "d": "3"}
    perfect = M.grouping(truth, truth)
    assert perfect["pairwise"]["f1"] == 1.0 and perfect["bcubed"]["f1"] == 1.0
    merged = {k: "x" for k in truth}
    g = M.grouping(merged, truth)
    assert g["pairwise"]["fp"] == 5 and g["pairwise"]["recall"] == 1.0 and g["impure_clusters"] == 1
    assert M.assignment_outcome("new_campaign", None, "1", label_known=False) == "correct_new"
    assert M.assignment_outcome("assign", "1", "1", label_known=True) == "correct_assign"
    assert M.assignment_outcome("assign", "2", "1", label_known=False) == "false_assign"


def test_stub_extract_roles_and_genes(corpus, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYDRA_CACHE_DIR", str(tmp_path / "cache"))
    inc = materialise(corpus[0].path, "a1", tmp_path / "s")
    roles = {str(f.role) for f in inc.files}
    assert {"har", "landing_html", "landing_asset", "feed_screenshot"} <= roles
    genes = StubBackend().extract(corpus[0])
    kinds = {g.kind for g in genes if g.status == "observed"}
    assert {"tracker_id", "host", "url_skeleton", "title", "phash", "asset_sha256"} <= kinds
    assert any(g.kind == "family" and g.family == "ID" and g.status == "not_applicable" for g in genes)


def test_build_and_assign_open_set(corpus, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYDRA_CACHE_DIR", str(tmp_path / "cache"))
    bk = StubBackend("B3_rarity_bits", threshold=10)
    genes = _genes(corpus, bk)
    labels = {r.iid: r.label for r in corpus}
    b = bk.build(genes, labels)
    assert b.member_of["a1"] == b.member_of["a2"] == b.member_of["a3"]
    assert b.member_of["b1"] == b.member_of["b2"] != b.member_of["a1"]
    assert b.member_of["c1"] not in {b.member_of["a1"], b.member_of["b1"]}
    # leave-one-out: a3 back into A
    rest = {k: v for k, v in genes.items() if k != "a3"}
    b2 = bk.build(rest, labels)
    a = bk.assign(b2, "a3", genes["a3"])
    assert a.decision == "assign" and a.campaign_id == b2.member_of["a1"]
    assert a.candidates and a.candidates[0].best_pair.reasons
    # open set: hold out campaign B entirely → new_campaign
    rest = {k: v for k, v in genes.items() if not k.startswith("b")}
    b3 = bk.build(rest, labels)
    assert bk.assign(b3, "b1", genes["b1"]).decision == "new_campaign"


def test_missing_modalities_never_crash(corpus, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYDRA_CACHE_DIR", str(tmp_path / "cache"))
    rng = random.Random(0)
    bk = StubBackend("B3_rarity_bits", threshold=10)
    for muts in (["drop_har"], ["drop_landing"], ["drop_creatives"], ["drop_har", "drop_landing", "drop_creatives"]):
        dst = tmp_path / "v" / "-".join(muts)
        path, log = V.make_variant(corpus[0].path, dst, muts, rng)
        genes = bk.extract(IncidentRef("v", path, "A", "GA"))
        statuses = {(g.family, g.status) for g in genes if g.kind == "family"}
        assert all(st in {"unavailable", "not_applicable"} for _, st in statuses)


@pytest.mark.parametrize("fmt", [(["mhtml"], False), ([], True), (["rename_ext"], False), (["har_dotname"], False),
                                 (["nest_bundle"], False)])
def test_formats_keep_genes(corpus, tmp_path, monkeypatch, fmt) -> None:
    monkeypatch.setenv("HYDRA_CACHE_DIR", str(tmp_path / "cache"))
    muts, as_zip = fmt
    bk = StubBackend()
    orig = {(g.kind, g.value) for g in bk.extract(corpus[0]) if g.status == "observed"}
    path, _ = V.make_variant(corpus[0].path, tmp_path / "f" / "x", muts, random.Random(1), as_zip=as_zip)
    var = {(g.kind, g.value) for g in bk.extract(IncidentRef("x", path, "A", "GA")) if g.status == "observed"}
    for kind in ("tracker_id", "title", "host"):
        assert {v for k, v in orig if k == kind} <= {v for k, v in var if k == kind}, kind


def test_variant_mutations_change_the_right_things(corpus, tmp_path) -> None:
    rng = random.Random(3)
    path, log = V.make_variant(corpus[0].path, tmp_path / "nd", ["new_domain", "headline"], rng)
    text = "".join(p.read_text(errors="ignore") for p in path.rglob("*") if p.suffix in {".html", ".har"})
    assert "civicnews.live" not in text and "1111111111111111" in text
    assert any(lg.startswith("new_domain:civicnews.live->") for lg in log)


def test_ladder_scorers_run(corpus, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYDRA_CACHE_DIR", str(tmp_path / "cache"))
    genes = _genes(corpus, StubBackend())
    labels = {r.iid: r.label for r in corpus}
    for name in S.SCORERS:
        b = StubBackend(name).build(genes, labels)
        assert set(b.member_of) == set(genes)
        assert all(0 <= p.probability <= 1 for p in b.pairs.values())


def test_gate_rules() -> None:
    from eval import gate

    base = {"crashes": 0, "sample+syn": {"loo_accuracy": 0.8, "open_set_rejection": 1.0, "false_merge_pairs": 2}}
    ok = {"crashes": 0, "sample+syn": {"loo_accuracy": 0.85, "open_set_rejection": 1.0, "false_merge_pairs": 2}}
    bad = {"crashes": 1, "sample+syn": {"loo_accuracy": 0.7, "open_set_rejection": 0.9, "false_merge_pairs": 3}}
    assert gate.compare(base, ok) == []
    fails = gate.compare(base, bad)
    assert len(fails) == 4


def test_gate_requires_comparable_harness_and_scorer() -> None:
    """CR-ASTRA-PIPE-202: thresholds may change, the scorer family and harness identity may not."""
    from eval import gate

    h = {"quick": True, "labels_sha256": "abc"}
    m = {"loo_accuracy": 1.0, "open_set_rejection": 1.0, "false_merge_pairs": 0}
    base = {"backend": "hydra-core(link=10.0,kit=6.0)", "crashes": 0, "harness": h, "sample": m}
    assert gate.compare(base, {**base, "backend": "hydra-core(link=9.0,kit=6.0)"}) == []
    assert gate.compare(base, {**base, "harness": {**h, "quick": False}})
    assert gate.compare({k: v for k, v in base.items() if k != "harness"}, base)  # stale stored baseline
    assert gate.compare(base, {**base, "crashes": float("nan")})
    assert gate.compare(base, {**base, "sample": {**m, "false_merge_pairs": float("inf")}})


def test_hydra_backend_isolated_state(corpus, tmp_path, monkeypatch) -> None:
    """CORE adapter on synthetic fixtures with the light extractors only; state stays in tmp."""
    pytest.importorskip("hydra.pipeline")
    from eval.backend import HydraBackend

    monkeypatch.setenv("HYDRA_ENRICH", "0")
    bk = HydraBackend(state_dir=tmp_path / "evalstate", only=["inf_url", "inf_har", "artifact_sha256"],
                      enrich_cache=tmp_path / "empty-enrich")
    try:
        genes = {r.iid: bk.extract(r) for r in corpus}
        assert all(genes.values())
        assert (tmp_path / "evalstate" / "hydra.sqlite").exists()
        labels = {r.iid: r.label for r in corpus}
        b = bk.build(genes, labels)
        assert set(b.member_of) == set(genes)
        rest = {k: v for k, v in genes.items() if k != "a3"}
        a = bk.assign(bk.build(rest, labels), "a3", genes["a3"])
        assert a.decision in {"assign", "new_campaign", "abstain"}
        b2 = bk.build(genes, labels, frozenset({"INF"}))  # ablation path
        assert set(b2.member_of) == set(genes)
    finally:
        bk.close()


def test_disputed_pairs_are_not_negatives() -> None:
    a = {"x": "A", "y": "A", "z": "C"}
    b = {"x": "A", "y": "A", "z": "A"}  # alternative: z joins A
    d = M.disputed_pairs([a, b])
    assert d == {("x", "z"), ("y", "z")}
    pred = {"x": 1, "y": 1, "z": 1}
    assert M.pairwise(pred, a)["fp"] == 2
    assert M.pairwise(pred, a, ignore=d)["fp"] == 0


def test_hydra_assign_matches_product_path(corpus, tmp_path, monkeypatch) -> None:
    """HydraBackend.assign must take the same decision as the product `assign_new` on the same corpus."""
    pytest.importorskip("hydra.pipeline")
    from eval.backend import HydraBackend
    from hydra import pipeline as P
    from hydra.config import Settings

    monkeypatch.setenv("HYDRA_ENRICH", "0")
    only = ["inf_url", "inf_har", "artifact_sha256"]
    base = [r for r in corpus if r.iid != "a3"]
    new = [r for r in corpus if r.iid == "a3"][0]
    s = Settings(state_dir=tmp_path / "product", enrich_cache=tmp_path / "empty")
    run = P.analyse([r.path for r in base], s, only=only)
    _rid, prod = P.assign_new(new.path, s, base_run=run.run_id, only=only, incident_id="a3")
    prod_members = next((c.members for c in run.clustering.campaigns if c.id == prod.campaign_id), None)
    bk = HydraBackend(state_dir=tmp_path / "eval", only=only, enrich_cache=tmp_path / "empty2")
    try:
        # CORE's adapter names incidents after the folders: use the same ids so rarity sees the same corpus
        genes = {r.iid: bk.extract(r) for r in corpus}
        b = bk.build({k: v for k, v in genes.items() if k != "a3"})
        a = bk.assign(b, "a3", genes["a3"])
    finally:
        bk.close()
    assert str(a.decision) == str(prod.decision)
    if prod_members is not None:
        ours = next(c.members for c in b.campaigns if c.id == a.campaign_id)
        assert sorted(ours) == sorted(m.replace("nask-", "") for m in prod_members) or \
            sorted(ours) == sorted(prod_members)


def test_aggregate_metrics_has_no_identifiers() -> None:
    from eval.run import aggregate_metrics

    res = {"meta": {"labels": "/Users/x/data/nask/labels.json", "label_status": {"nask-1": {}}, "backend": "b"},
           "system": {"sample": {"loo": {"summary": {"accuracy": 1.0}, "rows": [{"incident": "nask-1"}]},
                                 "hard_negatives": {"pairs": 2, "violations_campaign": 0, "violations_group": 0,
                                                    "rows": [{"a": "nask-10"}]},
                                 "predicted_campaigns": [["nask-1", "nask-2"]]}},
           "crashes": {"count": 0, "items": []}}
    out = json.dumps(aggregate_metrics(res))
    assert "nask-" not in out and "/Users/" not in out and '"accuracy": 1.0' in out


def test_stability_and_fidelity_on_fixtures(corpus, tmp_path, monkeypatch) -> None:
    pytest.importorskip("hydra.pipeline")
    from eval import run as R
    from eval.backend import HydraBackend

    monkeypatch.setenv("HYDRA_ENRICH", "0")
    bk = HydraBackend(state_dir=tmp_path / "st", only=["inf_url", "inf_har", "artifact_sha256"],
                      enrich_cache=tmp_path / "empty")
    try:
        c = R.Corpus("fx", corpus, bk)
        R.CTX["backend"] = bk
        R.register(c)
        st = R.stability(bk, c)
        assert st["all"]["perturbations"] == 10 and st["all"]["campaigns"] >= 1
        fi = R.fidelity(bk, c)
        assert fi["linked_pairs"] >= 1 and 0 <= fi["fidelity_top3"] <= 1
        tr = R.temporal_replay(bk, c)
        assert "summary" in tr or "note" in tr
    finally:
        bk.close()
        R.CTX["corpora"].pop("fx", None)


def test_gate_rejects_other_corpus_content() -> None:
    from eval import gate

    h = {"quick": True, "labels_sha256": "aa", "corpus_sha256": "c1"}
    base = {"crashes": 0, "backend": "hydra-core(link=10.0,kit=6.0)", "harness": h,
            "sample": {"loo_accuracy": 1.0, "open_set_rejection": 1.0, "false_merge_pairs": 0}}
    same = json.loads(json.dumps(base))
    assert gate.compare(base, same) == []
    other = json.loads(json.dumps(base))
    other["harness"]["corpus_sha256"] = "c2"  # same labels file, different corpus content
    assert any("harness" in f for f in gate.compare(base, other))


def test_corpus_fingerprint_changes_with_content(corpus, tmp_path) -> None:
    from eval.run import corpus_fingerprint

    rows = [{"id": r.iid, "path": str(r.path)} for r in corpus]
    a = corpus_fingerprint(rows, [])
    (corpus[0].path / "extra.txt").write_text("changed")
    b = corpus_fingerprint(rows, [])
    assert a["sha256"] != b["sha256"] and a["incidents"] == len(rows)


def test_gate_rejects_failed_fingerprints() -> None:
    from eval import gate

    h = {"quick": True, "labels_sha256": "aa", "corpus_sha256": None}
    side = {"crashes": 0, "backend": "hydra-core", "harness": h,
            "sample": {"loo_accuracy": 1.0, "open_set_rejection": 1.0, "false_merge_pairs": 0}}
    assert any("fingerprint unavailable" in f for f in gate.compare(side, json.loads(json.dumps(side))))


def test_manifest_steps_jsonl_and_status(tmp_path) -> None:
    from eval.manifest import read_steps, stage_status

    p = tmp_path / "steps.jsonl"
    p.write_text('{"name":"eval","command":"x","exit_code":0,"seconds":1}\n'
                 '{"name":"hidden_sim","command":"y","exit_code":3,"seconds":2}\n')
    steps = read_steps(p)
    assert len(steps) == 2
    st = stage_status(steps, 0)
    assert st["status"] == "PARTIAL" and "hidden_sim" in st["failed_or_missing"]
    assert st["stages"]["extra_batch"] == "missing"
    assert stage_status([{"name": "eval", "exit_code": 1}], 0)["status"] == "FAIL"


def _op_corpus(members: int, total: int):
    from hydra.contracts import Gene
    from hydra.synapse import build_genome

    def g(iid, kind, value, group):
        return Gene(incident_id=iid, family="INF", kind=kind, value=value, extractor="t@1", dependency_group=group)

    rows = {f"m{k}": [g(f"m{k}", "tracker_id", "synthetic-op-c1", "INF:operator:tracker"),
                      g(f"m{k}", "host", f"m{k}.invalid", "INF:operator:host")] for k in range(members)}
    rows.update({f"bg{k}": [g(f"bg{k}", "host", f"bg{k}.invalid", "INF:operator:host")]
                 for k in range(total - members)})
    return {i: build_genome(i, gs, {}) for i, gs in rows.items()}


def test_no_rarity_ablation_accepts_current_scorer_contract() -> None:
    """ASTRA-DELTA-01 probe: the uniform-bits wrapper must pass every keyword (operator=..., kit=...) through."""
    pytest.importorskip("hydra.pipeline")
    from eval.run import MECH, _apply_mechanism
    from hydra.pipeline import score_and_cluster
    from hydra.synapse import Thresholds

    base = score_and_cluster(_op_corpus(5, 20), Thresholds())[1][("m0", "m1")].campaign_bits
    undo = _apply_mechanism("no_rarity")
    try:
        pe = score_and_cluster(_op_corpus(5, 20), Thresholds())[1][("m0", "m1")]
        assert pe.campaign_bits >= 0
    finally:
        for obj, attr, old in reversed(undo):
            setattr(obj, attr, old)
        MECH["active"] = None
    assert score_and_cluster(_op_corpus(5, 20), Thresholds())[1][("m0", "m1")].campaign_bits == base


def test_campaign_split_keeps_parent_when_labels_differ() -> None:
    """ASTRA-DELTA-06 probe: a template clone with its own label stays on its parent's side."""
    from eval.run import campaign_split

    rows = [{"id": "parent-a", "label": "C-a"}, {"id": "parent-b", "label": "C-b"},
            {"id": "hn-b", "label": "HN-hn-b", "parent": "parent-b", "kind": "hard_negative_template"},
            {"id": "var-a", "label": "C-a", "parent": "parent-a"}, {"id": "dup-b", "label": "C-b",
                                                                     "duplicate_of": "parent-b"}]
    a, b, info = campaign_split(rows)
    assert not ({r.get("parent") or r["id"] for r in a} & {r.get("parent") or r["id"] for r in b})
    assert info["intersections"] == {"parent_or_origin": 0, "campaign_label": 0, "duplicate": 0}


def test_stage_status_missing_crash_count_is_not_pass() -> None:
    """ASTRA-DELTA-11 probe."""
    from eval.manifest import stage_status

    steps = [{"name": n, "exit_code": 0} for n in ("eval", "hidden_sim", "extra_batch", "extra_summary",
                                                   "error_report")]
    assert stage_status(steps, None)["status"] != "PASS"
    assert stage_status(steps, 0)["status"] == "PASS"
    assert stage_status(steps, 3)["status"] == "PARTIAL"
