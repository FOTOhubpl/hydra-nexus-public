"""Import guarantees and the HTTP surface."""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.ingest import import_paths, sniff_mime
from hydra.jobs import JobRunner, create_job, get_job
from hydra.storage import ContentStore


def _run_import(settings: Settings, paths: list[Path]) -> dict:
    w = Writer(settings.db_path)
    r = JobRunner(w, 1)
    try:
        job_id = create_job(w, "import", {})
        r.submit(job_id, lambda ctx: import_paths(ctx, paths, settings, ContentStore(settings.cas_dir))).result()
    finally:
        r.shutdown()
        w.close()
    with connect(settings.db_path, readonly=True) as conn:
        job = get_job(conn, job_id)
        job["items"] = [dict(x) for x in conn.execute("SELECT * FROM job_items WHERE job_id=? ORDER BY idx", (job_id,))]
    return job


def _dataset(root: Path, make_png) -> Path:
    d = root / "in"
    (d / "sub").mkdir(parents=True)
    (d / "a.png").write_bytes(make_png((255, 0, 0)))
    (d / "sub" / "a-copy.png").write_bytes(make_png((255, 0, 0)))      # duplicate bytes
    (d / "b.jpg").write_bytes(make_png((0, 255, 0)))                   # wrong extension, real PNG
    (d / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"garbage")  # sniffed as PNG, content broken
    (d / "ad.txt").write_text("Zainwestuj dziś! Zysk 1000 zł", encoding="utf-8")
    (d / "page.html").write_text("<!doctype html><title>x</title>", encoding="utf-8")
    (d / "blob.bin").write_bytes(bytes(range(256)) * 4)                # not allowed
    (d / "empty.png").write_bytes(b"")
    (d / ".hidden").write_text("skip me")
    return d


def test_every_input_gets_a_final_status_and_duplicates_dedupe(settings: Settings, make_png, tmp_path):
    d = _dataset(tmp_path, make_png)
    job = _run_import(settings, [d])
    statuses = {Path(i["source_ref"]).name: i["status"] for i in job["items"]}
    assert job["status"] == "done"
    assert job["total"] == 8 == len(job["items"])            # hidden file skipped, nothing else lost
    assert statuses["blob.bin"] == "quarantined"
    assert statuses["empty.png"] == "quarantined"
    assert sorted([statuses["a.png"], statuses["a-copy.png"]]) == ["duplicate", "stored"]
    assert statuses["ad.txt"] == statuses["page.html"] == statuses["b.jpg"] == "stored"
    with connect(settings.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM artifact_sources").fetchone()[0] == 6


def test_reimport_is_idempotent_and_originals_are_byte_identical(settings: Settings, make_png, tmp_path):
    d = _dataset(tmp_path, make_png)
    _run_import(settings, [d])
    job2 = _run_import(settings, [d])
    assert {i["status"] for i in job2["items"]} <= {"duplicate", "quarantined"}
    store = ContentStore(settings.cas_dir)
    for f in [d / "a.png", d / "ad.txt", d / "page.html"]:
        import hashlib

        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        assert store.path_for(digest).read_bytes() == f.read_bytes()


def test_zip_traversal_entries_are_never_written(settings: Settings, make_png, tmp_path):
    z = tmp_path / "pack.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("ok/one.png", make_png((1, 2, 3)))
        zf.writestr("../escape.txt", "evil")
        zf.writestr("/abs.txt", "evil")
    job = _run_import(settings, [z])
    by_ref = {i["source_ref"]: i for i in job["items"]}
    assert by_ref["pack.zip!/ok/one.png"]["status"] == "stored"
    # skipped entries are reported, never written (nothing disappears silently)
    assert by_ref["pack.zip!/../escape.txt"]["error_code"] == "ZIP_PATH_TRAVERSAL"
    assert by_ref["pack.zip!//abs.txt"]["error_code"] == "ZIP_PATH_TRAVERSAL"
    assert len(job["items"]) == 3
    assert not (tmp_path / "escape.txt").exists()


def test_unreadable_zip_is_quarantined_not_job_failure(settings: Settings, make_png, tmp_path, monkeypatch):
    import hydra.ingest as ingest

    good = tmp_path / "a.png"
    good.write_bytes(make_png((9, 9, 9)))
    z = tmp_path / "locked.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("x.png", make_png())

    def boom(*_a, **_k):
        raise RuntimeError("File is encrypted, password required for extraction")

    monkeypatch.setattr(ingest, "_safe_extract", boom)
    job = _run_import(settings, [z, good])
    assert job["status"] != "failed"
    by_ref = {i["source_ref"]: i for i in job["items"]}
    assert by_ref["locked.zip"]["error_code"] == "ZIP_UNREADABLE"
    assert by_ref["a.png"]["status"] == "stored"


def test_office_document_is_not_exploded(settings: Settings, tmp_path):
    d = tmp_path / "doc.docx"
    with zipfile.ZipFile(d, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", "<w:document/>")
    job = _run_import(settings, [d])
    assert [i["source_ref"] for i in job["items"]] == ["doc.docx"]


def test_sniff_mime_polish_text_cut_at_buffer_boundary(tmp_path):
    from hydra.ingest import SNIFF_BYTES

    p = tmp_path / "pl.txt"
    p.write_bytes(b"a" * (SNIFF_BYTES - 1) + "żółć".encode())
    assert sniff_mime(p) == "text/plain"


def test_sniff_mime_cp1250_csv(tmp_path):
    p = tmp_path / "export.csv"
    p.write_bytes("miasto;opis\nŁódź;zażółć gęślą jaźń\n".encode("cp1250"))
    assert sniff_mime(p) == "text/csv"


def test_zip_bomb_is_quarantined(settings: Settings, tmp_path):
    z = tmp_path / "bomb.zip"
    with zipfile.ZipFile(z, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.txt", b"0" * 3_000_000)
    job = _run_import(settings, [z])
    assert job["items"][0]["status"] == "quarantined"
    assert job["items"][0]["error_code"] == "ZIP_BOMB_SUSPECTED"


def test_sniff_mime_uses_bytes_not_extension(tmp_path, make_png):
    p = tmp_path / "fake.txt"
    p.write_bytes(make_png())
    assert sniff_mime(p) == "image/png"


def _wait(client: TestClient, job_id: str, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/v1/jobs/{job_id}").json()
        if job["status"] in ("done", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_api_upload_job_artifacts_content_and_audit(settings: Settings, make_png):
    with TestClient(create_app(settings)) as client:
        health = client.get("/health").json()
        assert health["status"] == "ok" and health["artifacts"] == 0

        files = [("files", ("a.png", io.BytesIO(make_png()), "image/png")),
                 ("files", ("x.html", io.BytesIO(b"<html><script>alert(1)</script></html>"), "text/html"))]
        r = client.post("/v1/import", files=files)
        assert r.status_code == 202
        job = _wait(client, r.json()["job_id"])
        assert job["status"] == "done" and job["done"] == 2 and job["progress"] == 1.0

        listing = client.get("/v1/artifacts").json()
        assert listing["total"] == 2
        html = next(a for a in listing["items"] if a["mime"] == "text/html")
        content = client.get(f"/v1/artifacts/{html['id']}/content")
        assert content.headers["content-type"].startswith("text/plain")   # never rendered as HTML
        assert content.headers["content-security-policy"] == "sandbox"

        assert client.get("/v1/audit/verify").json()["ok"] is True
        assert client.get("/v1/jobs/nope").status_code == 404


def test_api_path_import_reports_missing_paths(settings: Settings):
    with TestClient(create_app(settings)) as client:
        r = client.post("/v1/import/paths", json={"paths": ["/definitely/not/here"]})
        assert r.status_code == 400


def test_upload_over_cap_is_413_and_spool_removed(settings: Settings):
    small = settings.model_copy(update={"max_upload_mb": 1})
    with TestClient(create_app(small)) as client:
        r = client.post("/v1/import", files={"files": ("big.bin", b"x" * (2 * 1024 * 1024))})
        assert r.status_code == 413
    assert not list(small.tmp_dir.glob("upload-*"))


def test_stale_jobs_marked_failed_on_restart(settings: Settings):
    from hydra.db import Writer
    from hydra.jobs import create_job

    w = Writer(settings.db_path)
    try:
        job_id = create_job(w, "import", {})
    finally:
        w.close()
    with TestClient(create_app(settings)) as client:
        job = client.get(f"/v1/jobs/{job_id}").json()
        assert job["status"] == "failed"
        assert job["error_code"] == "INTERRUPTED"
        assert client.get("/v1/audit/verify").json()["ok"] is True


def test_api_prefix_and_spa_static(settings: Settings, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>HYDRA UI</html>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    s = settings.model_copy(update={"ui_dist": dist})
    with TestClient(create_app(s)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        assert client.get("/health").json()["status"] == "ok"
        assert "HYDRA UI" in client.get("/").text
        assert "HYDRA UI" in client.get("/kampanie/abc").text  # SPA deep link
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/v1/does-not-exist").status_code == 404  # CR-D2: no SPA for API paths
        assert client.get("/v1/does-not-exist").status_code == 404
        assert client.get("/api/whatever").status_code == 404


def test_local_only_guard_blocks_rebinding_and_cross_site_writes(settings: Settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health", headers={"Host": "127.0.0.1:8000"}).status_code == 200
        assert client.get("/health", headers={"Host": "localhost:5173"}).status_code == 200
        assert client.get("/health", headers={"Host": "evil.example:8000"}).status_code == 403
        files = {"files": ("a.txt", b"x")}
        assert client.post("/v1/import", files=files, headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/v1/import", files=files, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        ok = client.post("/v1/import", files=files, headers={"Origin": "http://127.0.0.1:5173"})
        assert ok.status_code == 202


def test_scheduler_starts_and_stops_with_app_when_enabled(settings: Settings, monkeypatch):
    import hydra.scheduler as sched

    calls = []
    def fake_start(s, w, tick_s=60.0):
        calls.append(tick_s)
        return lambda: calls.append("stop")

    monkeypatch.setattr(sched, "start_background", fake_start)
    with TestClient(create_app(settings.model_copy(update={"scheduler": True, "scheduler_tick_s": 5.0}))) as c:
        assert c.get("/health").status_code == 200
    assert calls == [5.0, "stop"]
    calls.clear()
    with TestClient(create_app(settings)):
        pass
    assert calls == []


def test_import_paths_uses_the_shared_data_dir_policy(settings: Settings, tmp_path):
    """Audit A11: /v1/import/paths and /v1/analyse share one path allow-list."""
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    inside = settings.data_dir / "inside.txt"
    inside.write_text("y")
    with TestClient(create_app(settings)) as client:
        assert client.post("/v1/import/paths", json={"paths": [str(outside)]}).status_code == 400
        assert client.post("/v1/import/paths", json={"paths": [str(inside)]}).status_code == 202


def test_offline_is_a_hard_kill_switch_for_live_enrichment(settings: Settings):
    """Audit A09."""
    from hydra.enrich.client import live_enabled

    assert live_enabled(settings.model_copy(update={"enrich": True, "offline": True})) is False
    assert live_enabled(settings.model_copy(update={"enrich": True, "offline": False})) is True
    assert live_enabled(settings.model_copy(update={"enrich": False, "offline": False})) is False


def test_sniff_mime_chat_export_starting_with_bracket_is_text(tmp_path):
    """CR-E2E-1: a WhatsApp/iOS export `[dd.mm.yyyy, hh:mm] who: msg` is text, not JSON."""
    p = tmp_path / "czat.txt"
    p.write_text("[01.10.2026, 10:00] +48 733 112 909: InPost: Twoja paczka czeka\n", encoding="utf-8")
    assert sniff_mime(p) == "text/plain"
    q = tmp_path / "czat"
    q.write_text("{01.10.2026} ktoś: hej\n", encoding="utf-8")
    assert sniff_mime(q) == "text/plain"


def test_sniff_mime_json_without_extension(tmp_path):
    from hydra.ingest import SNIFF_BYTES

    p = tmp_path / "doc"
    p.write_text('[{"a": 1}, {"b": "x"}]', encoding="utf-8")
    assert sniff_mime(p) == "application/json"
    lines = tmp_path / "events.log"
    lines.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    assert sniff_mime(lines) == "application/json"
    big = tmp_path / "big"  # longer than the sniffed head: accepted when only the cut tail fails to parse
    big.write_text("[" + ", ".join(f'{{"k": {i}}}' for i in range(SNIFF_BYTES // 8)) + "]", encoding="utf-8")
    assert big.stat().st_size > SNIFF_BYTES
    assert sniff_mime(big) == "application/json"
    chat = tmp_path / "long_chat.txt"
    chat.write_text("[01.10.2026, 10:00] +48 733 112 909: wiadomość\n" * (SNIFF_BYTES // 40), encoding="utf-8")
    assert sniff_mime(chat) == "text/plain"
