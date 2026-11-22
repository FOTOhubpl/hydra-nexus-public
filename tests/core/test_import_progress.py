"""IMPORT-PROGRESS: import counters from the adapter, the listing in jury_report, the /analyse progress snapshot."""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hydra import api_progress, import_progress
from hydra.adapters import nask_folder
from hydra.config import Settings
from hydra.db import Writer
from hydra.storage import ContentStore


def _dataset(root: Path, make_png, incidents: int = 3, files: int = 12) -> tuple[Path, int]:
    """A dataset folder of numbered incident folders; returns (root, data files the adapter walks)."""
    walked = 0
    for i in range(1, incidents + 1):
        d = root / str(i)
        d.mkdir(parents=True)
        (d / "reklama.png").write_bytes(make_png((i * 40 % 255, 10, 10)))
        walked += 1
        for k in range(files - 1):
            (d / f"notatka_{k}.txt").write_text(f"incydent {i} notatka {k} https://example-{i}.invalid/\n")
            walked += 1
    (root / "1" / "pusty.txt").write_bytes(b"")          # FILE_EMPTY: walked, rejected
    (root / "2" / ".ukryty.bin").write_bytes(b"\x00\x01")  # HIDDEN_NOT_DATA: rejected, not walked
    return root, walked + 1


@pytest.fixture
def recorded(monkeypatch):
    """Every state the sink would write (min_interval 0), in order."""
    seen: list[dict] = []
    orig = import_progress.ImportProgress._flush

    def flush(self, force=False):
        seen.append(dict(self.state))
        orig(self, force=True)

    monkeypatch.setattr(import_progress.ImportProgress, "_flush", flush)
    return seen


def test_dataset_import_counts_monotonic_reaches_100_and_counts_rejected(settings: Settings, tmp_path, make_png,
                                                                         recorded):
    from hydra.jury_report import list_import
    from hydra.pipeline import import_paths_as_incidents

    root, walked = _dataset(tmp_path / "data" / "zbior", make_png)
    writer = Writer(settings.db_path)
    try:
        with import_progress.tracking(settings, "job-ds") as sink:
            list_import([root], settings)
            assert sink.state["files_total"] == walked and sink.state["incidents_total"] == 3
            with nask_folder.dataset_rules():
                incs = import_paths_as_incidents([root], writer, ContentStore(settings.cas_dir), settings)
        assert len(incs) == 3
    finally:
        writer.close()
    files = [s["files_done"] for s in recorded]
    pct = [s["percent"] for s in recorded if s["percent"] is not None]
    assert files == sorted(files) and pct == sorted(pct), "counters never go back"
    assert all(p < 100 for s in recorded if s["phase"] != "done" for p in [s["percent"] or 0])
    final = import_progress.read(settings, "job-ds")
    assert final["phase"] == "done" and final["percent"] == 100
    assert final["files_done"] == final["files_total"] == walked
    assert final["rejected"] == 2  # the empty file and the hidden non-data file
    assert final["incidents_done"] == 3 and final["bytes_done"] == final["bytes_total"] > 0
    assert any(s["phase"] == "importing" and 0 < (s["percent"] or 0) < 100 for s in recorded)
    assert any(s["current"] and s["current"].endswith(".txt") for s in recorded)


def test_single_item_total_known_after_planning(settings: Settings, tmp_path, make_png, recorded):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "ad.png").write_bytes(make_png())
    (inc / "strona.html").write_text("<html><body>oferta</body></html>")
    writer = Writer(settings.db_path)
    try:
        with import_progress.tracking(settings, "job-1", incidents_total=1):
            nask_folder.import_single(inc, writer, ContentStore(settings.cas_dir), settings)
    finally:
        writer.close()
    assert recorded[0]["files_total"] is None and recorded[0]["phase"] == "listing"  # indeterminate first
    final = import_progress.read(settings, "job-1")
    assert final["files_total"] == 2 and final["files_done"] == 2 and final["percent"] == 100
    assert final["phase"] == "done"


def test_no_sink_no_file(settings: Settings, tmp_path, make_png):
    inc = tmp_path / "inc"
    inc.mkdir()
    (inc / "ad.png").write_bytes(make_png())
    writer = Writer(settings.db_path)
    try:
        nask_folder.import_single(inc, writer, ContentStore(settings.cas_dir), settings)
    finally:
        writer.close()
    assert not list((settings.state_dir / "progress").glob("*.import.json"))


def test_flushes_throttled_to_four_per_second(settings: Settings):
    sink = import_progress.ImportProgress(settings, "job-t")
    sink.expect({Path("/x"): (100_000, 1)}, incidents=1)
    sink.begin_incident(Path("/x"), 100_000, 1, 0)
    before = sink.writes
    t0 = time.monotonic()
    for i in range(100_000):
        sink.file_done(f"f{i}.png", 1)
    elapsed = time.monotonic() - t0
    assert sink.writes - before <= int(elapsed / 0.25) + 2
    assert sink.state["files_done"] == 100_000 and sink.state["percent"] == 99  # 100 only when the phase is done
    sink.end_incident()
    assert import_progress.read(settings, "job-t")["percent"] == 100


def test_failed_import_keeps_counters(settings: Settings):
    with pytest.raises(RuntimeError), import_progress.tracking(settings, "job-f") as sink:
        sink.expect({Path("/a"): (10, 10)})
        sink.begin_incident(Path("/a"), 10, 10, 1, rejected_files=1)
        raise RuntimeError("dysk pełny")
    final = import_progress.read(settings, "job-f")
    assert final["phase"] == "failed" and "dysk pełny" in final["error"]
    assert final["files_done"] == 1 and final["rejected"] == 1 and final["percent"] == 10


# ------------------------------------------------------------------ overall progress


@pytest.mark.parametrize("seq", [[
    ("import", None), ("genes", {"incidents_done": 0}), ("genes", {"incidents_done": 25}),
    ("rarity", {}), ("candidates", {}), ("scoring", {"pairs_scored": 100, "pairs_total": 400}),
    ("scoring", {"pairs_scored": 400, "pairs_total": 400}), ("clustering", {}), ("assign", {}),
    ("assign", {"self_assigned": 10}), ("assign", {"self_assigned": 50}),
]])
def test_corpus_overall_monotonic_and_below_100_until_done(seq):
    keys = [k for k, _ in api_progress.CORPUS_STAGES]
    weights, _ = api_progress.corpus_weights(50)
    total = sum(weights.values())
    last, stages = -1.0, []
    for stage, extra in seq:
        core = {"stage": stage, "incidents_total": 50, **(extra or {})}
        key, frac, _ = api_progress._corpus_stage(core, {"percent": 40})
        i = keys.index(key)
        pct = (sum(weights[k] for k in keys[:i]) + weights.get(key, 0) * (frac or 0)) * 100 / total
        assert pct >= last and pct < 100
        last = pct
        stages.append(key)
    assert stages[-1] == "persist" and "calibration" in stages and "self_assign" in stages


def test_api_progress_snapshot_and_stream(settings: Settings, monkeypatch, make_png):
    import hydra.api_task as at
    from hydra.api import create_app

    def fake(kind, payload: Path, s, *, writer):
        target = nask_folder.discover_incidents(payload, s)[0]  # as pipeline.import_input does for an incident ZIP
        rep = nask_folder.import_single(target, writer, ContentStore(s.cas_dir), s)
        return {"kind": kind, "incident_id": rep.incident.id, "assignment": None, "notes": []}

    monkeypatch.setattr(at, "_pipeline_entry", lambda: fake)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ad.png", make_png())
        for k in range(20):
            zf.writestr(f"notatka_{k}.txt", f"notatka {k}\n")
        zf.writestr("pusty.txt", b"")
    with TestClient(create_app(settings)) as c:
        r = c.post("/v1/analyse", files=[("files", ("incydent.zip", buf.getvalue(), "application/zip"))])
        assert r.status_code == 202, r.text
        job = r.json()["job_id"]
        for _ in range(500):
            snap = c.get(f"/v1/analyse/{job}/progress").json()
            if snap["status"] in ("done", "failed"):
                break
            time.sleep(0.02)
        assert snap["status"] == "done" and snap["overall_percent"] == 100 and snap["stage"] == "done"
        imp = snap["import"]
        assert imp["phase"] == "done" and imp["files_total"] == 22 and imp["files_done"] == 22
        assert imp["rejected"] == 1 and "input_ref" not in imp
        assert [s["state"] for s in snap["stages"]] == ["done"] * len(snap["stages"])
        with c.stream("GET", f"/v1/analyse/{job}/progress/stream") as s:
            text = "".join(s.iter_text())
        assert text.count("event: progress") == 1 and '"status":"done"' in text
        assert c.get("/v1/analyse/nope/progress").status_code == 404
