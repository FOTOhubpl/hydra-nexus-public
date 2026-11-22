"""JURY-READY import fuzz: every layout and hostile file of `tests/fuzz/jury_corpus.py` ends with a visible status,
and the valid incidents next to it are analysed. Synthetic only (generated per test session, never NASK files).

Cheap extractors only (no model files needed): the subject is import robustness, not gene quality.
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hydra.config import Settings
from hydra.db import connect
from hydra.jury_report import jury_analyse as analyse
from tests.fuzz.jury_corpus import build_fuzz, build_scale

CHEAP = ["artifact_sha256", "inf_dom", "inf_har", "inf_url"]


@pytest.fixture(scope="session")
def layouts(tmp_path_factory) -> dict[str, Path]:
    return build_fuzz(tmp_path_factory.mktemp("jury-fuzz"))


def _run(settings: Settings, path: Path):
    return analyse([path], settings, only=CHEAP, use_cache=False)


def _codes(res) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in res.metrics["import_report"]["files"]:
        out.setdefault(f["code"].split(":")[0], []).append(f["path"])
    return out


@pytest.mark.parametrize("name,incidents", [
    ("nask_folders", 5), ("wrapper", 3), ("top_zip", 3), ("zip_in_zip", 3), ("loose_root", 1),
    ("one_incident", 1), ("singletons", 3), ("identical", 3), ("nested", 2),
])
def test_layout_incident_count(layouts, settings, name, incidents):
    res = _run(settings, layouts[name])
    assert res.metrics["incidents"] == incidents, res.metrics["import_report"]
    rep = res.metrics["import_report"]
    assert rep["incidents"] == incidents and rep["summary_pl"]
    assert "references" in res.metrics


def test_one_incident_has_no_pairs(layouts, settings):
    res = _run(settings, layouts["one_incident"])
    assert res.metrics["pairs_scored"] == 0 and res.metrics["campaigns"] == 1


def test_identical_incidents_form_one_campaign(layouts, settings):
    res = _run(settings, layouts["identical"])
    assert res.metrics["campaigns"] == 1 and res.metrics["multi_member_campaigns"] == 1


def test_singletons_stay_apart(layouts, settings):
    res = _run(settings, layouts["singletons"])
    assert res.metrics["multi_member_campaigns"] == 0 and res.metrics["campaigns"] == 3


def test_byte_copy_of_incident_lands_with_its_original(layouts, settings):
    res = _run(settings, layouts["nask_folders"])
    m = res.clustering.membership
    orig = next(k for k in m if k.endswith("incydent_001"))
    copy = next(k for k in m if "kopia" in k)
    assert m[orig] == m[copy]


def test_nested_categories_get_a_note(layouts, settings):
    res = _run(settings, layouts["nested"])
    notes = res.metrics["import_report"]["notes_pl"]
    assert any("kategoria A" in n for n in notes)


def test_zero_analysable_files_completes_with_statuses(layouts, settings):
    res = _run(settings, layouts["nothing"])
    codes = _codes(res)
    assert len(codes.get("FILE_EMPTY", [])) == 2
    assert not [g for gn in res.genomes.values() for g in gn.genes if g.status == "observed"]


def test_mixed_layout_reports_every_rejection_and_analyses_the_rest(layouts, settings):
    res = _run(settings, layouts["mixed"])
    codes = _codes(res)
    # root level: bomb and password ZIP refused whole or per entry, README and a corrupt ZIP as loose files
    assert any("bomba-w-korzeniu.zip" in p for p in codes["ZIP_BOMB_SUSPECTED"])
    assert any("hasło-w-korzeniu.zip" in p for p in codes["ZIP_ENTRY_ENCRYPTED"])
    assert {"README.txt", "zepsuty.zip"} <= {Path(p).name for p in codes["LOOSE_ROOT_FILE"]}
    # inside the hostile incident
    assert any(p.endswith("pusty.txt") for p in codes["FILE_EMPTY"])
    assert any("bomba.zip" in p for p in codes["ZIP_BOMB_SUSPECTED"])
    assert any("traversal.zip" in p for p in codes["ZIP_PATH_TRAVERSAL"])
    assert any("traversal.zip" in p for p in codes["ZIP_SYMLINK"])
    assert any("zahasłowane.zip" in p for p in codes["ZIP_ENTRY_ENCRYPTED"])
    if hasattr(Path, "symlink_to"):
        assert any("dowiązanie-do-passwd" in p for p in codes["SYMLINK_NOT_FOLLOWED"])
    assert all(f["status_pl"] for f in res.metrics["import_report"]["files"])
    # the valid incidents are analysed: two plain ones, the ZIP incident, the duplicate, the singleton, the hostile
    # incident (its valid files) and the empty folder
    assert res.metrics["incidents"] >= 6
    with connect(settings.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] > 10
    assert not any(p.name == "poza-archiwum.txt" for p in settings.state_dir.parent.rglob("poza-archiwum.txt"))


def test_hostile_incident_types_are_sniffed_by_bytes(layouts, settings):
    res = _run(settings, layouts["weird"])
    inc = next(iter(res.genomes))
    with connect(settings.db_path, readonly=True) as conn:
        rows = {r["path_in_incident"]: r["mime"] for r in conn.execute(
            "SELECT ia.path_in_incident, a.mime FROM incident_artifacts ia JOIN artifacts a ON a.id = ia.artifact_id"
            " WHERE ia.incident_id = ?", (inc,))}
    assert rows["to-jest-png.htm"] == "image/png"
    assert rows["REKLAMA DUŻA.PNG"] == "image/png"
    assert rows["wiadomość-cp1250.txt"].startswith("text/")
    assert rows["wiadomość-iso8859-2.txt"].startswith("text/")
    assert any(k.endswith(".png") and len(k.encode()) > 200 for k in rows)  # the very long name kept
    assert "uszkodzony.png" in rows and "uszkodzony.mp4" in rows           # corrupt media stored, not fatal


def test_rejected_input_zip_does_not_abort_other_inputs(layouts, settings, tmp_path):
    from tests.fuzz.jury_corpus import zip_bomb

    bomb = zip_bomb(tmp_path / "bomba.zip")
    res = analyse([bomb, layouts["singletons"]], settings, only=CHEAP, use_cache=False)
    assert res.metrics["incidents"] == 3
    assert res.metrics["import_report"]["rejected_archives"] == 1


def test_generic_import_gives_every_file_a_status(layouts, settings):
    from hydra.db import Writer
    from hydra.ingest import import_paths
    from hydra.jobs import JobRunner, create_job, get_job
    from hydra.storage import ContentStore

    w = Writer(settings.db_path)
    r = JobRunner(w, 1)
    try:
        job_id = create_job(w, "import", {})
        r.submit(job_id, lambda ctx: import_paths(ctx, [layouts["mixed"]], settings,
                                                  ContentStore(settings.cas_dir))).result()
    finally:
        r.shutdown()
        w.close()
    with connect(settings.db_path, readonly=True) as conn:
        job = get_job(conn, job_id)
        items = [dict(x) for x in conn.execute("SELECT * FROM job_items WHERE job_id=?", (job_id,))]
    assert job["status"] == "done" and job["total"] == len(items) > 20
    assert all(i["status"] in ("stored", "duplicate", "quarantined", "failed") for i in items)
    assert {i["error_code"] for i in items if i["status"] == "quarantined"} >= {"FILE_EMPTY", "MIME_NOT_ALLOWED"}


def _zip_tree(folder: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and not p.is_symlink():
                z.write(p, p.relative_to(folder.parent).as_posix())
    return buf.getvalue()


def test_gui_upload_of_dataset_zip_returns_import_report(layouts, settings, monkeypatch):
    """The real API corpus entry (pipeline.analyse_corpus -> hydra.jury_report.jury_analyse), cheap extractors."""
    import functools

    from hydra import pipeline
    from hydra.api import create_app

    monkeypatch.setattr(pipeline, "analyse", functools.partial(pipeline.analyse, only=CHEAP, use_cache=False))
    data = _zip_tree(layouts["mixed"])
    with TestClient(create_app(settings)) as c:
        r = c.post("/v1/analyse", files={"files": ("zbiór jury.zip", data, "application/zip")})
        assert r.status_code == 202, r.text
        assert r.json()["mode"] == "corpus"
        job_id = r.json()["job_id"]
        for _ in range(600):
            body = c.get(f"/v1/analyse/{job_id}").json()
            if body["job"]["status"] in ("done", "failed"):
                break
            time.sleep(0.1)
        assert body["job"]["status"] == "done", body["job"]
        rep = body["result"]["import_report"]
        assert rep["skipped_files"] >= 5 and rep["incidents"] >= 6
        codes = {f["code"].split(":")[0] for f in rep["files"]}
        assert codes >= {"FILE_EMPTY", "ZIP_BOMB_SUSPECTED", "LOOSE_ROOT_FILE"}
        for path in ("/v1/runs/latest", "/v1/incidents", "/v1/campaigns", "/v1/pairs", "/v1/graph", "/v1/dashboard",
                     "/v1/analytics/overview", "/v1/campaign-groups", "/v1/assignments"):
            assert c.get(path).status_code == 200, path
        assert c.get("/v1/audit/verify").json()["ok"]


def test_gui_upload_of_zip_of_zips_is_a_dataset(layouts, settings, monkeypatch):
    import hydra.api_task as at

    seen = {}

    def entry():
        def run(path, s, writer, **kw):
            seen["path"] = path
            return {"kind": "corpus", "run_id": None}
        return run

    monkeypatch.setattr(at, "_corpus_entry", entry)
    from hydra.api import create_app

    with TestClient(create_app(settings)) as c:
        r = c.post("/v1/analyse", files={"files": ("zip.zip", layouts["zip_in_zip"].read_bytes(), "application/zip")})
        assert r.status_code == 202 and r.json()["mode"] == "corpus", r.text


def test_scale_builder_shapes(tmp_path):
    root = build_scale(tmp_path / "s", 30)
    dirs = sorted(p for p in root.iterdir() if p.is_dir())
    assert len(dirs) == 30 and all(any(d.iterdir()) for d in dirs)
