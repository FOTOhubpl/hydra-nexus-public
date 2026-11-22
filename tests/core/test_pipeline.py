"""Spine end to end with two trivial test-only extractors on a synthetic corpus (no organiser data).

Corpus: campaign A (a1, a2, a3 share a landing host), campaign B (b1, b2 share a host and a phone), c alone.
a1 and b1 share one kit-level value (same SDK build), so A and B form a campaign group.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

from hydra.config import Settings
from hydra.contracts import Family, Gene, GeneStatus
from hydra.db import connect
from hydra.genome import base
from hydra.pipeline import analyse, assign_new, assign_stored

HOST_RE = re.compile(r"saved from url=\(\d+\)https?://([^/?\s]+)")


def t_page(inc: base.ResolvedIncident, ctx: base.ExtractContext) -> base.ExtractResult:
    out = base.ExtractResult()
    for art in inc.by_role("landing_html"):
        text = art.path.read_text(encoding="utf-8", errors="replace")
        ev = {"artifact_id": art.meta.artifact_id}
        if m := HOST_RE.search(text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.INF, kind="host", value=m.group(1),
                                  extractor="t_page@1", evidence=ev, dependency_group="inf:domain"))
        for sdk in re.findall(r"data-sdk=\"([^\"]+)\"", text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.INF, kind="sdk", value=sdk,
                                  extractor="t_page@1", evidence=ev, dependency_group="kit:sdk"))
        for px in re.findall(r"data-pixel=\"(\d+)\"", text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.INF, kind="tracker_id", value=px,
                                  extractor="t_page@1", evidence=ev, dependency_group="INF:operator:tracker"))
        for acct in re.findall(r"data-acct=\"([^\"]+)\"", text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.INF, kind="operator_id", value=acct,
                                  extractor="t_page@1", evidence=ev, dependency_group="INF:operator:cdn_account"))
        for phone in re.findall(r"\+48 ?\d{3} ?\d{3} ?\d{3}", text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.TXT, kind="entity_phone",
                                  value=phone.replace(" ", ""), extractor="t_page@1", evidence=ev))
        for scheme in re.findall(r"data-scheme=\"([^\"]+)\"", text):
            out.genes.append(Gene(incident_id=inc.incident.id, family=Family.NAR, kind="scheme", value=scheme,
                                  extractor="t_page@1", evidence=ev))
    if not out.genes:
        out.genes.append(Gene(incident_id=inc.incident.id, family=Family.INF, kind="host",
                              status=GeneStatus.UNAVAILABLE, extractor="t_page@1"))
    return out


def t_vec(inc: base.ResolvedIncident, ctx: base.ExtractContext) -> base.ExtractResult:
    from PIL import Image

    out = base.ExtractResult()
    for i, art in enumerate(inc.by_role("feed_screenshot", "ad_creative")):
        with Image.open(art.path) as im:
            v = np.asarray(im.convert("RGB").resize((4, 4)), dtype=np.float32).ravel() + 1.0
        v /= np.linalg.norm(v)
        key = f"{inc.incident.id}|{art.meta.artifact_id}|tvec|{i}"
        out.vectors.append(base.Vector(key=key, model="tvec@1", data=v))
        out.genes.append(Gene(incident_id=inc.incident.id, family=Family.VIS, kind="tvec", vector_ref=key,
                              extractor="t_vec@1", evidence={"artifact_id": art.meta.artifact_id}))
    return out


@pytest.fixture
def test_extractors():
    added = []
    for name, fn, fam in (("t_page", t_page, Family.INF), ("t_vec", t_vec, Family.VIS)):
        if name not in base.REGISTRY:
            base.register(name, family=fam)(fn)
            added.append(name)
    yield ["t_page", "t_vec"]
    for name in added:
        base.REGISTRY.pop(name, None)


def page(d: Path, host: str, extra: str = "") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "Strona.html").write_text(f"<!-- saved from url=(0040)https://{host}/?utm_id=1 -->\n<html><body>{extra}"
                                   "</body></html>", encoding="utf-8")


N_FILLER = 10
N_INC = 6 + N_FILLER
PX = '<i data-pixel="1234567890123456"></i><i data-acct="a1b2c3d4"></i>'


def corpus(root: Path, make_png) -> Path:
    ds = root / "ds"
    page(ds / "1", "evil-a.example", '<i data-sdk="aio-sdk-1.0.28-x"></i><i data-scheme="investment"></i>'
         " +48 700 100 100" + PX)
    page(ds / "2", "evil-a.example", '<i data-scheme="investment"></i> +48 700 100 100' + PX)
    page(ds / "3", "evil-a.example", "+48 700 100 100" + PX)
    page(ds / "4", "shop-b.example", '<i data-sdk="aio-sdk-1.0.28-x"></i> +48 600 100 200')
    page(ds / "5", "shop-b.example", "Zadzwoń +48 600 100 200")
    page(ds / "6", "lonely.example", '<i data-scheme="health"></i>')
    (ds / "6" / "Zrzut ekranu 2026-10-01 101010.png").write_bytes(make_png((10, 200, 30)))
    for k in range(7, 7 + N_FILLER):  # unrelated background incidents
        page(ds / str(k), f"filler{k}.example", f'<i data-acct="acct-{k}"></i>')
    return ds


def test_analyse_finds_campaigns_groups_and_persists(settings: Settings, make_png, tmp_path, test_extractors):
    ds = corpus(tmp_path, make_png)
    res = analyse([ds], settings, only=test_extractors)
    camps = {tuple(c.members): c for c in res.clustering.campaigns}
    assert ("nask-1", "nask-2", "nask-3") in camps
    assert ("nask-4", "nask-5") in camps
    assert ("nask-6",) in camps
    a, b = camps[("nask-1", "nask-2", "nask-3")], camps[("nask-4", "nask-5")]
    assert a.group_id is not None and a.group_id == b.group_id
    assert camps[("nask-6",)].group_id is None
    assert {"host", "entity_phone", "tracker_id"} <= {g.kind for g in a.core_genome}
    pe = res.pairs[("nask-1", "nask-2")]
    assert pe.score.hypothesis == "same_campaign"
    assert any("evil-a.example" in (r.value_a or "") for r in pe.score.reasons)
    assert all(r.explanation_pl for r in pe.score.reasons)
    assert "VIS" in pe.score.missing_families  # unknown, not penalised
    kit = res.pairs[("nask-1", "nask-4")]
    assert kit.score.hypothesis == "shared_template" and kit.kit_bits >= 6
    with connect(settings.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs WHERE id=?", (res.run_id,)).fetchone()[0] == 1
        n_c = conn.execute("SELECT COUNT(*) FROM campaigns WHERE run_id=?", (res.run_id,)).fetchone()[0]
        assert n_c == len(res.clustering.campaigns) == 3 + N_FILLER
        n_pairs = conn.execute("SELECT COUNT(*) FROM pairs WHERE run_id=?", (res.run_id,)).fetchone()[0]
        assert n_pairs == N_INC * (N_INC - 1) // 2
        detail = json.loads(conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a='nask-1' AND b='nask-2'",
                                         (res.run_id,)).fetchone()[0])
        assert detail["hypothesis"] == "same_campaign" and detail["reasons"]
        metrics = json.loads(conn.execute("SELECT metrics FROM runs WHERE id=?", (res.run_id,)).fetchone()[0])
        assert len(metrics["campaign_groups"]) == 1
        assert conn.execute("SELECT COUNT(*) FROM gene_vectors").fetchone()[0] == 1
    # API-PERF: the UI views of the new run are precomputed on disk before analyse returns
    from hydra.api_cache import run_dir

    cached = {f.name.split("-")[0] for f in run_dir(settings, res.run_id).glob("*.json.gz")}
    assert cached == {"graph", "system", "dashboard", "analytics"}
    # second run hits the gene cache
    res2 = analyse([ds], settings, only=test_extractors)
    assert all(s["cached"] for s in res2.metrics["extract"].values())
    assert sorted(tuple(c.members) for c in res2.clustering.campaigns) == sorted(camps)


def test_assign_open_set(settings: Settings, make_png, tmp_path, test_extractors):
    ds = corpus(tmp_path, make_png)
    analyse([ds], settings, only=test_extractors)
    page(tmp_path / "new_a", "evil-a.example", "+48 700 100 100" + PX)
    _, a = assign_new(tmp_path / "new_a", settings, only=test_extractors)
    assert a.decision == "assign" and a.campaign_id is not None and a.reason_pl, a.reason_pl
    page(tmp_path / "new_kit", "fresh.example", '<i data-sdk="aio-sdk-1.0.28-x"></i>')
    _, k = assign_new(tmp_path / "new_kit", settings, only=test_extractors)
    assert k.decision == "new_campaign" and k.group_id is not None
    page(tmp_path / "new_none", "other.example")
    _, n = assign_new(tmp_path / "new_none", settings, only=test_extractors)
    assert n.decision == "new_campaign" and n.group_id is None
    with connect(settings.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assignments WHERE run_id LIKE 'assign-%'").fetchone()[0] == 3
    # assign_stored re-decides from the stored genes (eval.hidden_sim rescore): same decisions, nothing written
    for x in (a, k, n):
        again = assign_stored(x.incident_id, settings).assignment
        assert (again.decision, again.campaign_id, again.group_id) == (x.decision, x.campaign_id, x.group_id)
    with connect(settings.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM assignments WHERE run_id LIKE 'assign-%'").fetchone()[0] == 3


def test_analyse_self_assigns_every_incident_leave_one_out(settings: Settings, make_png, tmp_path, test_extractors):
    ds = corpus(tmp_path, make_png)
    res = analyse([ds], settings, only=test_extractors)
    s = res.metrics["assignment"]
    assert s["mode"] == "leave_one_out" and s["assignments"] == N_INC
    assert sum(s["by_decision"].values()) == N_INC
    member = {m: c.id for c in res.clustering.campaigns for m in c.members}
    with connect(settings.db_path, readonly=True) as conn:
        rows = {r["incident_id"]: (r["decision"], r["campaign_id"], json.loads(r["detail"])) for r in conn.execute(
            "SELECT incident_id, decision, campaign_id, detail FROM assignments WHERE run_id=?", (res.run_id,))}
    assert set(rows) == set(res.incidents)
    for iid in ("nask-1", "nask-2", "nask-3", "nask-4", "nask-5"):  # members come back to their campaign
        assert rows[iid][:2] == ("assign", member[iid]), rows[iid][2]["reason_pl"]
    assert rows["nask-6"][0] == "new_campaign"
    d = rows["nask-1"][2]
    assert d["reason_pl"] and d["meta"]["mode"] == "leave_one_out" and d["meta"]["base_run"] == res.run_id
    assert "conformal" in d  # prediction set stored with the assignment
    assert s["members"] == 5 and s["members_returned"] == 5 and s["singletons"] == N_INC - 5
    assert s["agreement_with_clustering"] == round(s["agree"] / N_INC, 4)
    assert "held_out_campaign_rejection_rate" in s and "self_assignment" in res.metrics["timings_s"]


def test_extractor_timings_survive_the_cache(settings: Settings, make_png, tmp_path, test_extractors):
    ds = corpus(tmp_path, make_png)
    r1 = analyse([ds], settings, only=test_extractors)
    ex1 = r1.metrics["extractors"]
    assert set(test_extractors) <= set(ex1)
    for n in test_extractors:
        assert ex1[n]["incidents_run"] == N_INC and ex1[n]["incidents_cached"] == 0
        assert ex1[n]["original_unknown"] == 0
    assert sum(ex1[n]["observed"] for n in test_extractors) == r1.metrics["genes"] - sum(
        ex1[n]["unavailable"] + ex1[n]["failed"] for n in test_extractors)
    r2 = analyse([ds], settings, only=test_extractors)
    ex2 = r2.metrics["extractors"]
    for n in test_extractors:
        assert ex2[n]["incidents_cached"] == N_INC and ex2[n]["incidents_run"] == 0
        assert ex2[n]["run_seconds"] == 0.0 and ex2[n]["original_unknown"] == 0
        assert ex2[n]["original_seconds"] == pytest.approx(ex1[n]["original_seconds"], abs=1e-3)
        assert ex2[n]["observed"] == ex1[n]["observed"]
    st = next(iter(r2.metrics["extract"].values()))
    assert st["cached"] and st["extractors_cached"] == len(test_extractors) and st["extractors_run"] == 0
    # the per-extractor cache state keeps the original compute time
    from hydra.pipeline import _cache_dir, _read_state

    state = _read_state(_cache_dir(settings, "nask-1"), test_extractors[0])
    assert state is not None and "seconds" in state


def test_failing_extractor_degrades(settings: Settings, make_png, tmp_path, test_extractors):
    def boom(inc, ctx):
        raise RuntimeError("bad input")

    base.register("t_boom", family=Family.TXT)(boom)
    try:
        ds = corpus(tmp_path, make_png)
        res = analyse([ds], settings, only=[*test_extractors, "t_boom"])
        failed = [g for g in res.genomes["nask-1"].genes if g.status == "failed"]
        assert failed and failed[0].kind == "t_boom"
        assert len(res.clustering.campaigns) == 3 + N_FILLER
    finally:
        base.REGISTRY.pop("t_boom", None)


def test_analyse_input_four_kinds(settings: Settings, make_png, tmp_path, test_extractors):
    import zipfile

    from hydra.assign import analyse_input

    ds = corpus(tmp_path, make_png)
    analyse([ds], settings, only=test_extractors)
    # hint: keyword search, diacritic-insensitive, ranks the campaign holding the value
    h = analyse_input("hint", "EVIL-A", settings)
    assert h["search"]["incidents"][0]["incident_id"] in {"nask-1", "nask-2", "nask-3"}
    assert h["search"]["campaigns"] and h["search"]["ad_library"]["status"] == "not_configured"
    # url: no live fetch in tests → unavailable, still a new incident with an assignment
    u = analyse_input("url", "https://unknown.example/?access_token=EAAsecret", settings, only=test_extractors)
    assert u["fetch"]["status"] == "unavailable" and "EAAsecret" not in json.dumps(u)
    assert u["assignment"]["decision"] in ("new_campaign", "abstain")
    # file: incident ZIP → normal import → assigned to campaign A
    page(tmp_path / "zipsrc" / "inc", "evil-a.example", "+48 700 100 100" + PX)
    z = tmp_path / "inc.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(tmp_path / "zipsrc" / "inc" / "Strona.html", "inc/Strona.html")
    f = analyse_input("file", z, settings, only=test_extractors)
    assert f["assignment"]["decision"] == "assign"
    # screenshot: stored as ad_creative; no OCR extractor here → note, no fetch
    img = tmp_path / "shot.png"
    img.write_bytes(make_png((1, 2, 250)))
    s = analyse_input("screenshot", img, settings, only=test_extractors)
    assert s["notes"] and "assignment" in s
    with connect(settings.db_path, readonly=True) as conn:
        role = conn.execute("SELECT role FROM incident_artifacts WHERE incident_id=?", (s["incident_id"],)).fetchone()
    assert role[0] == "ad_creative"


def test_groups_table(settings: Settings, make_png, tmp_path, test_extractors):
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with connect(settings.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM campaign_groups WHERE run_id=?", (res.run_id,)).fetchone()[0] == 1


def test_analyse_corpus_uses_callers_writer_and_labels_do_not_leak(settings: Settings, make_png, tmp_path,
                                                                   test_extractors):
    from hydra.db import Writer
    from hydra.pipeline import analyse_corpus

    ds = corpus(tmp_path, make_png)
    (ds / "README.md").write_text("notes")
    w = Writer(settings.db_path)
    try:
        res = analyse_corpus([ds], settings, w, only=test_extractors)
        w.run(lambda c: c.execute("UPDATE incidents SET label = 'X'"))  # labels must not change the scorer
        res2 = analyse_corpus([ds], settings, w, only=test_extractors)
    finally:
        w.close()
    assert res.metrics["loose_root_files"] == ["ds/README.md"]
    assert res.metrics["scorer"] == res2.metrics["scorer"] == "rarity_bits@1"
    assert all(not pe.score.calibrated for pe in res2.pairs.values())


def test_batch_and_open_set_agree(settings: Settings, make_png, tmp_path, test_extractors):
    """Every incident of a multi-member campaign, assigned against the others, lands in its own campaign
    exactly when the batch scorer links it to a fellow member (same `support`)."""
    from hydra.assign import assign
    from hydra.pipeline import rarity_for

    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    for c in res.clustering.campaigns:
        for m in c.members:
            rest = [x.model_copy(update={"members": [y for y in x.members if y != m]})
                    for x in res.clustering.campaigns]
            rest = [x for x in rest if x.members]
            corpus_g = {k: v for k, v in res.genomes.items() if k != m}
            a = assign(res.genomes[m], corpus_g, rest, res.clustering.groups, rarity_for(res.genomes)).assignment
            linked = any(res.pairs[tuple(sorted((m, o)))].score.hypothesis == "same_campaign"
                         for o in c.members if o != m)
            assert (a.decision == "assign" and a.campaign_id == c.id) == linked, (m, a.reason_pl)


def test_enrichment_records_persisted_with_genes(settings: Settings, make_png, tmp_path, test_extractors, monkeypatch):
    import hydra.pipeline as pl
    from hydra.contracts import EnrichmentRecord

    def fake(resolved, enrich):
        return [EnrichmentRecord(key=f"k-{resolved.incident.id}", source="rdap", query="evil-a.example",
                                 fetched_at="2026-11-21T12:00:00Z", status="observed",
                                 summary={"registrar": "X", "api_key": "s3cret"})]

    monkeypatch.setattr(pl, "enrichment_records", fake)
    base.register("inf_dummy", family=Family.INF)(lambda inc, ctx: base.ExtractResult())
    try:
        analyse([corpus(tmp_path, make_png)], settings, only=[*test_extractors, "inf_dummy"])
    finally:
        base.REGISTRY.pop("inf_dummy", None)
    with connect(settings.db_path, readonly=True) as conn:
        rows = conn.execute("SELECT key, summary FROM enrichment").fetchall()
    assert len(rows) == N_INC and all("s3cret" not in r["summary"] for r in rows)


def test_hint_falls_back_to_page_html_and_screenshot_duplicate_assigns(settings: Settings, make_png, tmp_path,
                                                                       test_extractors):
    from hydra.assign import analyse_input

    ds = corpus(tmp_path, make_png)
    (ds / "4" / "reklama_1.png").write_bytes(make_png((7, 99, 201), size=(320, 240)))
    analyse([ds], settings, only=[*test_extractors, "artifact_sha256"])
    h = analyse_input("hint", "Zadzwoń", settings)  # only in the HTML of incident 5, no TXT genes here
    assert [i["incident_id"] for i in h["search"]["incidents"]] == ["nask-5"]
    assert h["search"]["incidents"][0]["matches"][0]["kind"] == "page_html"
    # the same creative uploaded again as a screenshot returns to its campaign (identical file)
    img = tmp_path / "upload.png"
    img.write_bytes((ds / "4" / "reklama_1.png").read_bytes())
    s = analyse_input("screenshot", img, settings, only=[*test_extractors, "artifact_sha256"])
    camp = {m: c for c in analyse([ds], settings, only=[*test_extractors, "artifact_sha256"]).clustering.campaigns
            for m in c.members}["nask-4"]
    assert s["assignment"]["decision"] == "assign" and s["assignment"]["campaign_id"] == camp.id
    best = s["assignment"]["candidates"][0]
    assert best["campaign_id"] == camp.id and best["best_pair"]["calibrated"] is False


def test_cli_and_gui_paths_agree_on_a_zip_audit_a02(settings: Settings, make_png, tmp_path, test_extractors):
    """Audit reproduction `zip_cli_and_gui_disagree`: the same compressed ZIP through assign_new (CLI) and
    analyse_input("file") (GUI) gives the same incident and the same decision."""
    import zipfile

    from hydra.assign import analyse_input

    analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    z = tmp_path / "known.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Strona.html", '<!-- saved from url=(0040)https://evil-a.example/?utm_id=1 --><html><body>'
                    '+48 700 100 100 <i data-pixel="1234567890123456"></i><i data-acct="a1b2c3d4"></i></body></html>')
    _, cli = assign_new(z, settings, only=test_extractors)
    gui = analyse_input("file", z, settings, only=test_extractors)
    assert cli.decision == gui["assignment"]["decision"] == "assign"
    assert cli.incident_id == gui["incident_id"] and cli.campaign_id == gui["assignment"]["campaign_id"]


def test_first_item_without_corpus_is_onboarded_not_failed_audit_a10(settings: Settings, tmp_path, test_extractors):
    from hydra.assign import analyse_input
    from hydra.pipeline import corpus_ready

    assert corpus_ready(settings)["ready"] is False and corpus_ready(settings)["message_pl"]
    first = tmp_path / "first.txt"
    first.write_text("Synthetic first input")
    out = analyse_input("file", first, settings, only=test_extractors)
    a = out["assignment"]
    assert a["decision"] == "new_campaign" and a["meta"]["onboarding"] is True and "pierwszy" in a["reason_pl"]


def test_nar_extractors_receive_the_incidents_other_genes(settings: Settings, make_png, tmp_path, test_extractors):
    seen: list[int] = []

    def t_dep(inc, ctx):
        genes = ctx.options.get("incident_genes") or []
        seen.append(len(genes))
        n_hosts = sum(1 for g in genes if g.kind == "host")
        return base.ExtractResult(genes=[Gene(incident_id=inc.incident.id, family=Family.NAR, kind="t_dep_hosts",
                                              value=str(n_hosts), extractor="t_dep@1")])

    base.register("t_dep", family=Family.NAR)(t_dep)  # registered AFTER t_page/t_vec, runs after them anyway
    try:
        ds = corpus(tmp_path, make_png)
        res = analyse([ds], settings, only=["t_dep", *test_extractors])  # dependent listed first on purpose
        vals = {g.value for g in res.genomes["nask-1"].genes if g.kind == "t_dep_hosts"}
        assert vals == {"1"} and all(n > 0 for n in seen)
        calls = len(seen)
        analyse([ds], settings, only=["t_dep", *test_extractors])
        assert len(seen) == calls  # everything cached
        base.REGISTRY["t_page"].version = 2  # a base extractor changes -> the dependent's digest changes too
        analyse([ds], settings, only=["t_dep", *test_extractors])
        assert len(seen) == calls + N_INC
    finally:
        base.REGISTRY.pop("t_dep", None)
        base.REGISTRY["t_page"].version = 1


def test_system_flow_of_a_real_analyse_run_has_every_stage(settings: Settings, make_png, tmp_path, test_extractors):
    """/v1/system/flow ("Jak to działa") on a real analyse run: assignment and extractor numbers are not empty."""
    from fastapi.testclient import TestClient

    from hydra.api import create_app

    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    with TestClient(create_app(settings)) as c:
        f = c.get("/v1/system/flow").json()
    assert f["run_id"] == res.run_id
    b = f["boxes"]
    a = b["assignment"]
    assert sum(a["by_decision"].values()) == N_INC and a["self"] == N_INC and a["hint_pl"] is None
    assert a["summary"]["mode"] == "leave_one_out" and a["summary"]["agreement_with_clustering"] is not None
    ex = b["genes"]["extractors"]
    assert set(test_extractors) <= set(ex) and b["genes"]["extractors_run"] == N_INC * len(test_extractors)
    assert ex["t_page"]["observed"] > 0 and "self_assignment" in f["timings_s"]
    assert b["outputs"]["exports"]["total"] == 0 and b["outputs"]["hint_pl"]


def test_candidates_per_query_equal_query_retrieval(settings: Settings, make_png, tmp_path, test_extractors):
    """The one-pass candidate map of the self-assignment is exactly the per-incident query retrieval of assign."""
    from hydra.retrieval import candidates, candidates_per_query

    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    for below in (60, 0):  # all pairs, and the real indexes (exact / near / kNN)
        per = candidates_per_query(res.genomes, res.rarity, all_pairs_below=below, knn_k=2)
        for iid in res.genomes:
            assert per[iid] == candidates(res.genomes, res.rarity, queries=[iid], all_pairs_below=below, knn_k=2)
