"""Hostile archives and media remain bounded data, with a final per-input status."""

import stat
import zipfile
from contextlib import closing

import pytest
from PIL import Image

from hydra.db import connect
from hydra.ingest import sniff_mime
from hydra.media import load_work_image
from tests.security.helpers import oversized_png, run_import, zip_bytes

pytestmark = pytest.mark.timeout(15)


@pytest.mark.parametrize("attack", ["ratio", "entries", "file-size", "nested"])
def test_archive_resource_limits(settings, tmp_path, attack):
    small = settings.model_copy(update={"max_file_mb": 1, "max_zip_entries": 4})
    entries = {
        "ratio": [("zeros.txt", b"0" * 2_000_000)],
        "entries": [(f"{i}.txt", b"ok") for i in range(5)],
        "file-size": [("large.txt", b"x" * 1_100_000)],
        "nested": [("inner.zip", zip_bytes([("zeros.txt", b"0" * 2_000_000)], zipfile.ZIP_DEFLATED))],
    }[attack]
    path = tmp_path / "attack.zip"
    path.write_bytes(zip_bytes(entries, zipfile.ZIP_DEFLATED if attack == "ratio" else zipfile.ZIP_STORED))
    good = tmp_path / "sibling.txt"
    good.write_text("Syntetyczny poprawny plik")
    result = run_import(small, [path, good])
    assert result["status"] == "done"
    assert all(i["status"] == "quarantined" for i in result["items"] if i["source_ref"] != "sibling.txt")
    assert result["items"][-1]["status"] == "stored"
    assert not list(settings.tmp_dir.glob("import-*"))


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "ok/../../escape.txt"])
def test_zip_paths_cannot_escape(settings, tmp_path, name):
    path = tmp_path / "paths.zip"
    path.write_bytes(zip_bytes([(name, "sentinel"), ("good.txt", "good")]))
    result = run_import(settings, [path])
    assert result["items"][0]["error_code"] == "ZIP_PATH_TRAVERSAL"
    assert result["items"][1]["status"] == "stored"
    assert not (tmp_path / "escape.txt").exists()


def test_zip_symlink_is_not_followed(settings, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("ASTRA_OUTSIDE_SENTINEL")
    link = zipfile.ZipInfo("link.txt")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = tmp_path / "symlink.zip"
    path.write_bytes(zip_bytes([(link, str(outside))]))
    result = run_import(settings, [path])
    assert result["status"] == "done"
    with closing(connect(settings.db_path, readonly=True)) as conn:
        for row in conn.execute("SELECT id FROM artifacts"):
            from hydra.storage import ContentStore
            assert b"ASTRA_OUTSIDE_SENTINEL" not in ContentStore(settings.cas_dir).path_for(row["id"]).read_bytes()


def test_zip_symlink_then_child_does_not_discard_sibling_statuses(settings, tmp_path):
    link = zipfile.ZipInfo("directory")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = tmp_path / "collision.zip"
    path.write_bytes(zip_bytes([(link, "../outside"), ("directory/child.txt", "bad"), ("good.txt", "good")]))
    result = run_import(settings, [path])
    assert result["status"] == "done"
    assert len(result["items"]) == 3
    assert result["items"][-1]["source_ref"].endswith("good.txt")
    assert result["items"][-1]["status"] == "stored"


@pytest.mark.parametrize("name", ["x" * 300 + ".txt", "x" * 300 + "/child.txt"])
def test_long_zip_names_preserve_per_file_results(settings, tmp_path, name):
    path = tmp_path / "long.zip"
    path.write_bytes(zip_bytes([(name, "bad"), ("good.txt", "good")]))
    result = run_import(settings, [path])
    assert result["status"] == "done"
    assert len(result["items"]) == 2
    assert result["items"][0]["status"] in {"failed", "quarantined"}
    assert result["items"][1]["status"] == "stored"


@pytest.mark.parametrize("name", [
    "line\nbreak.txt", "carriage\rreturn.txt", "escape\x1b[31m.txt", "photo\u202egnp.txt",
])
def test_control_and_rtl_names_are_not_displayed_raw(settings, tmp_path, name):
    path = tmp_path / "names.zip"
    path.write_bytes(zip_bytes([(name, "bad"), ("good.txt", "good")]))
    result = run_import(settings, [path])
    item = result["items"][0]
    assert item["status"] in {"failed", "quarantined"} or not any(
        ch in item["source_ref"] for ch in "\r\n\x1b\u202e")
    assert result["items"][1]["status"] == "stored"


def test_bytes_override_false_extension_and_zero_bytes_are_reported(settings, tmp_path, make_png):
    fake = tmp_path / "picture.html"
    fake.write_bytes(make_png())
    empty = tmp_path / "zero.jpg"
    empty.touch()
    assert sniff_mime(fake) == "image/png"
    result = run_import(settings, [fake, empty])
    assert result["items"][0]["status"] == "stored"
    assert result["items"][1]["error_code"] == "FILE_EMPTY"


def test_declared_image_bomb_is_rejected_without_allocating_pixels(settings, tmp_path, make_png):
    bomb = tmp_path / "huge.png"
    bomb.write_bytes(oversized_png())
    with pytest.raises(Image.DecompressionBombError):
        load_work_image(bomb)
    good = tmp_path / "good.png"
    good.write_bytes(make_png())
    result = run_import(settings, [bomb, good])
    # Import is byte-preserving; decoding is the separate guarded boundary.
    assert result["status"] == "done" and len(result["items"]) == 2
    assert result["items"][-1]["status"] == "stored"


@pytest.mark.parametrize(("name", "data"), [
    ("broken.png", b"\x89PNG\r\n\x1a\nbroken"),
    ("broken.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64),
    ("active.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>ASTRA_SENTINEL()</script></svg>'),
    ("active.html", b"<html><script>ASTRA_SENTINEL()</script></html>"),
    ("active.pdf", b"%PDF-1.7\n1 0 obj<</S/JavaScript/JS(ASTRA_SENTINEL())>>endobj\n%%EOF"),
])
def test_hostile_formats_are_imported_as_inert_bytes(settings, tmp_path, name, data, monkeypatch):
    import subprocess

    def no_process(*args, **kwargs):
        raise RuntimeError("Import must not execute an external process")
    monkeypatch.setattr(subprocess, "Popen", no_process)
    path = tmp_path / name
    path.write_bytes(data)
    result = run_import(settings, [path])
    assert result["status"] == "done"
    assert len(result["items"]) == 1
    assert result["items"][0]["status"] in {"stored", "quarantined"}


def test_multipart_overlong_filename_is_a_client_error_and_cleans_spool(client, settings):
    result = client.post("/v1/import", files={"files": ("x" * 300 + ".txt", b"safe")})
    assert 400 <= result.status_code < 500
    assert not list(settings.tmp_dir.glob("upload-*"))


def test_multipart_total_size_limit_and_cleanup(client, settings):
    client.app.state.hydra.settings = settings.model_copy(update={"max_upload_mb": 1})
    result = client.post("/v1/import", files=[
        ("files", ("a.txt", b"a" * 600_000)), ("files", ("b.txt", b"b" * 600_000))])
    assert result.status_code == 413
    assert not list(settings.tmp_dir.glob("upload-*"))


def test_directory_import_does_not_follow_child_symlink_outside_allowed_root(settings, tmp_path):
    from hydra.paths import check_input_path
    from hydra.storage import ContentStore

    outside = tmp_path / "outside.txt"
    outside.write_text("ASTRA_OUTSIDE_SENTINEL")
    folder = settings.data_dir / "untrusted-folder"
    folder.mkdir()
    (folder / "linked.txt").symlink_to(outside)
    allowed = check_input_path(folder, settings)
    result = run_import(settings, [allowed])
    assert result["items"]
    for item in result["items"]:
        if item["artifact_id"]:
            assert b"ASTRA_OUTSIDE_SENTINEL" not in ContentStore(settings.cas_dir).path_for(
                item["artifact_id"]).read_bytes()


def test_direct_symlink_path_outside_allowed_root_is_rejected(settings, tmp_path):
    from hydra.paths import PathNotAllowed, check_input_path

    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic")
    link = settings.data_dir / "link.txt"
    link.symlink_to(outside)
    with pytest.raises(PathNotAllowed):
        check_input_path(link, settings)


def test_corrupt_video_probe_fails_bounded_and_uses_no_shell(tmp_path, monkeypatch):
    import shutil
    import subprocess

    from hydra.media import probe

    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe is not installed; real corrupt-video decoder check unavailable")
    path = tmp_path / "broken.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    original = subprocess.run
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        probe(path)
    assert len(calls) == 1
    assert calls[0][1]["timeout"] <= 60 and not calls[0][1].get("shell", False)
