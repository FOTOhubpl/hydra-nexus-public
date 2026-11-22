"""B3 batch cross-comparison and B17 duplicates on synthetic data (no organiser data).

Base corpus: tests/core/test_pipeline.py (campaign A on evil-a.example, campaign B, fillers). The batch holds three
items of one unseen campaign (same host, tracker pixel and phone, different page text), an unrelated item, a
variant of known campaign A, and a byte-identical copy of the first unseen item in another folder.
"""

from __future__ import annotations

import csv
import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from hydra.batch import assign_batch, write_outputs
from hydra.config import Settings
from hydra.db import connect
from hydra.pipeline import analyse
from tests.core.test_pipeline import PX, corpus, page, test_extractors  # noqa: F401 — fixture

NEW_PX = '<i data-pixel="9988776655443322"></i><i data-acct="zz-new-acct"></i>'


def batch_folder(root: Path) -> Path:
    b = root / "batch"
    for k, text in enumerate(("Pierwsza wersja", "Druga wersja tekstu", "Trzecia odsłona"), 1):
        page(b / f"x{k}", "fresh-scam.example", f"{text} +48 511 222 333 {NEW_PX}")
    page(b / "x4", "solo-unrelated.example", "Nic wspólnego")
    page(b / "x5", "evil-a.example", "+48 700 100 100" + PX)
    shutil.copytree(b / "x1", b / "x6")  # identical files, other folder name
    return b


@pytest.fixture
def batch_run(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    ds = corpus(tmp_path, make_png)
    base = analyse([ds], settings, only=test_extractors)
    items = sorted((batch_folder(tmp_path)).iterdir())
    report = assign_batch(items, settings, base_run=base.run_id, only=test_extractors)
    return settings, base, report


def _rows(report):
    return {r["item"]: r for r in report["items"]}


def test_unseen_campaign_items_share_one_new_pattern(batch_run):
    _s, _base, report = batch_run
    rows = _rows(report)
    for k in ("x1", "x2", "x3", "x4"):
        assert rows[k]["decision"] == "new_campaign", rows[k]["reason_pl"]
        assert rows[k]["campaign_id"] is None
    pid = rows["x1"]["new_pattern_id"]
    assert pid and pid.startswith("N-")
    assert rows["x2"]["new_pattern_id"] == pid == rows["x3"]["new_pattern_id"]
    assert rows["x1"]["new_pattern_size"] == 3
    assert rows["x4"]["new_pattern_id"] != pid and rows["x4"]["new_pattern_size"] == 1
    pat = next(p for p in report["new_patterns"] if p["id"] == pid)
    assert sorted(pat["items"]) == ["x1", "x2", "x3"]
    assert pat["links"] and all(lk["hypothesis"] == "same_campaign" for lk in pat["links"])
    assert any("fresh-scam.example" in (g.get("value_a") or "") for g in pat["core_genome"])
    assert "fresh-scam.example" in json.dumps(pat["links"], ensure_ascii=False)
    assert pat["reason_pl"].startswith(f"Nowy wzorzec {pid}")
    assert report["summary"]["multi_member_new_patterns"] == 1
    assert report["summary"]["items_in_multi_member_patterns"] == 3


def test_known_campaign_is_not_touched_and_variant_still_assigns(batch_run):
    _s, base, report = batch_run
    rows = _rows(report)
    camp_a = next(c.id for c in base.clustering.campaigns if c.members == ["nask-1", "nask-2", "nask-3"])
    assert rows["x5"]["decision"] == "assign" and rows["x5"]["campaign_id"] == camp_a
    assert "new_pattern_id" not in rows["x5"]
    for p in report["new_patterns"]:  # no new pattern contains a known-campaign item
        assert rows["x5"]["incident_id"] not in p["members"]


def test_duplicate_is_named(batch_run):
    _s, _base, report = batch_run
    rows = _rows(report)
    assert rows["x6"]["duplicate_of"] == rows["x1"]["incident_id"]
    assert rows["x6"]["duplicate_of_item"] == "x1"
    assert rows["x6"]["new_pattern_id"] == rows["x1"]["new_pattern_id"]  # follows its original
    pat = next(p for p in report["new_patterns"] if p["id"] == rows["x1"]["new_pattern_id"])
    assert rows["x6"]["incident_id"] not in pat["members"]  # a copy does not inflate the pattern
    # x5 has the bytes of base incident 3 (same page): a duplicate of a known incident, still assigned
    assert [d["item"] for d in report["duplicates"]] == ["x5", "x6"]
    assert rows["x5"]["duplicate_of"] == "nask-3" and rows["x5"]["duplicate_of_item"] is None


def test_batch_is_persisted_and_assignments_annotated(batch_run):
    s, _base, report = batch_run
    rows = _rows(report)
    pid = rows["x1"]["new_pattern_id"]
    with connect(s.db_path, readonly=True) as conn:
        run = conn.execute("SELECT kind, metrics FROM runs WHERE id=?", (report["batch_run"],)).fetchone()
        assert run["kind"] == "assign_batch"
        assert json.loads(run["metrics"])["summary"]["new_patterns"] == len(report["new_patterns"])
        members = {r[0] for r in conn.execute("SELECT incident_id FROM campaign_members WHERE run_id=? AND"
                                              " campaign_id=?", (report["batch_run"], pid))}
        assert members == {rows[k]["incident_id"] for k in ("x1", "x2", "x3")}
        detail = json.loads(conn.execute("SELECT detail FROM assignments WHERE run_id=? AND incident_id=?",
                                         (rows["x2"]["assign_run"], rows["x2"]["incident_id"])).fetchone()[0])
        assert detail["meta"]["new_pattern_id"] == pid
        assert detail["meta"]["new_pattern"]["size"] == 3
        assert f"wspólny nowy wzorzec {pid}" in detail["reason_pl"]
        dup = json.loads(conn.execute("SELECT detail FROM assignments WHERE run_id=? AND incident_id=?",
                                      (rows["x6"]["assign_run"], rows["x6"]["incident_id"])).fetchone()[0])
        assert dup["meta"]["duplicate_of"] == rows["x1"]["incident_id"]


def test_outputs_csv_and_patterns_file(batch_run, tmp_path):
    _s, _base, report = batch_run
    out = tmp_path / "out"
    write_outputs(report, out)
    with (out / "assignments.csv").open(encoding="utf-8") as fh:
        rows = {r["item"]: r for r in csv.DictReader(fh)}
    assert rows["x2"]["new_pattern_id"] == rows["x1"]["new_pattern_id"] != ""
    assert rows["x6"]["duplicate_of"] == rows["x1"]["incident_id"]
    pats = json.loads((out / "new_patterns.json").read_text(encoding="utf-8"))
    assert pats["summary"]["multi_member_new_patterns"] == 1


def test_unrelated_new_items_stay_separate(settings: Settings, make_png, tmp_path, test_extractors):  # noqa: F811
    ds = corpus(tmp_path, make_png)
    base = analyse([ds], settings, only=test_extractors)
    b = tmp_path / "b2"
    page(b / "u1", "alpha-one.example", "jeden")
    page(b / "u2", "beta-two.example", "dwa")
    report = assign_batch(sorted(b.iterdir()), settings, base_run=base.run_id, only=test_extractors)
    ids = [r["new_pattern_id"] for r in report["items"]]
    assert len(set(ids)) == 2 and report["summary"]["multi_member_new_patterns"] == 0


def test_cli_assign_batch_writes_new_pattern_columns(settings: Settings, make_png, tmp_path, test_extractors,  # noqa: F811
                                                     monkeypatch):
    from hydra import cli_task
    from hydra.config import get_settings

    ds = corpus(tmp_path, make_png)
    analyse([ds], settings, only=test_extractors)
    b = batch_folder(tmp_path)
    import hydra.batch as hb

    real = hb.assign_batch
    monkeypatch.setattr(hb, "assign_batch", lambda items, s, **kw: real(items, settings, only=test_extractors, **kw))
    monkeypatch.setattr(cli_task, "_warn_if_api_running", lambda: None)
    get_settings.cache_clear()
    try:
        res = CliRunner().invoke(cli_task.app, ["assign-batch", str(b), "--out", str(tmp_path / "o")])
    finally:
        get_settings.cache_clear()
    assert res.exit_code == 0, res.output
    with (tmp_path / "o" / "assignments.csv").open(encoding="utf-8") as fh:
        rows = {r["item"]: r for r in csv.DictReader(fh)}
    assert rows["x1"]["new_pattern_id"] == rows["x3"]["new_pattern_id"]
    assert "nowy wzorzec" in res.output


def test_api_batch_import_groups_new_items(settings: Settings, make_png, tmp_path, test_extractors, monkeypatch):  # noqa: F811
    from hydra.api import create_app

    ds = corpus(tmp_path, make_png)
    analyse([ds], settings, only=test_extractors)
    import hydra.batch as hb

    real = hb.assign_batch
    monkeypatch.setattr(hb, "assign_batch", lambda items, s, **kw: real(items, s, only=test_extractors, **kw))
    b = batch_folder(settings.data_dir)
    with TestClient(create_app(settings)) as c:
        assert c.post("/v1/assign-batch", json={"path": str(tmp_path / "elsewhere")}).status_code == 400
        r = c.post("/v1/assign-batch", json={"path": str(b)})
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        for _ in range(300):
            body = c.get(f"/v1/assign-batch/{job_id}").json()
            if body["job"]["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert body["job"]["status"] == "done", body
        rep = body["report"]
        assert rep["summary"]["multi_member_new_patterns"] == 1
        rows = {r["item"]: r for r in rep["items"]}
        assert rows["x1"]["new_pattern_id"] == rows["x2"]["new_pattern_id"]
        assert c.get(f"/v1/assign-batch/{rep['batch_run']}").json()["report"]["batch_run"] == rep["batch_run"]
        assert c.get("/v1/assign-batches").json()["items"][0]["batch_run"] == rep["batch_run"]
        card = c.get(f"/v1/campaigns/{rows['x1']['new_pattern_id']}", params={"run_id": rep["batch_run"]})
        assert card.status_code == 200 and len(card.json()["members"]) == 3
        lin = c.get(f"/v1/campaigns/{rows['x1']['new_pattern_id']}/lineage", params={"run_id": rep["batch_run"]})
        assert lin.status_code == 200, lin.text
        assert len(lin.json()["nodes"]) == 3 and len(lin.json()["edges"]) == 2  # variant tree of the new pattern
        assert c.get("/v1/assign-batch/nope").status_code == 404


def test_dataset_export_carries_new_pattern_and_duplicate(batch_run, tmp_path):
    import pandas as pd

    from hydra.export_dataset import export_dataset

    s, base, report = batch_run
    rows = _rows(report)
    export_dataset(s, base.run_id, tmp_path / "ds-out")
    df = pd.read_csv(tmp_path / "ds-out" / "assignments.csv").set_index("incident_id")
    pid = rows["x1"]["new_pattern_id"]
    assert df.loc[rows["x2"]["incident_id"], "new_pattern_id"] == pid
    assert df.loc[rows["x6"]["incident_id"], "duplicate_of"] == rows["x1"]["incident_id"]


def test_reuploaded_single_file_is_one_duplicate_field(settings: Settings, make_png, tmp_path,
                                                       test_extractors):  # noqa: F811
    """merge-batch3: a single file analysed earlier (any run) and dropped again into a batch under another name is
    reused by `analyse_input` (inputs) and reported with the same `duplicate_of` field as the batch rules
    (variants-pipe), in the row, the report and the stored Assignment.meta."""
    from hydra.pipeline import analyse_input

    base = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    shot = tmp_path / "shot.png"
    shot.write_bytes(make_png((10, 200, 90), (80, 60)))
    first = analyse_input("file", shot, settings, base_run=base.run_id, only=test_extractors)
    again = tmp_path / "b" / "copy-of-shot.png"
    again.parent.mkdir()
    shutil.copy(shot, again)
    report = assign_batch([again], settings, base_run=base.run_id, only=test_extractors)
    row = report["items"][0]
    assert row["incident_id"] == first["incident_id"]
    assert row["duplicate_of"] == first["incident_id"] and row["duplicate_of_item"] is None
    assert [d["duplicate_of"] for d in report["duplicates"]] == [first["incident_id"]]
    assert row["input_report"]["files"]
    with connect(settings.db_path, readonly=True) as conn:
        meta = json.loads(conn.execute("SELECT detail FROM assignments WHERE run_id=? AND incident_id=?",
                                       (row["assign_run"], row["incident_id"])).fetchone()["detail"])["meta"]
    assert meta["duplicate_of"] == first["incident_id"]
