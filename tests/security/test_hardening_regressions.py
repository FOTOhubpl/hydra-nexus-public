"""Regressions for the CR-ASTRA-SEC-1/2/3/4/6 fixes that the original ASTRA cases do not pin down."""

import hashlib
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.db import Writer
from hydra.ingest import register_artifact
from hydra.paths import PathNotAllowed, check_input_path, safe_display_name
from hydra.storage import ContentStore, UnsafeStorePath
from tests.security.helpers import run_import, zip_bytes

pytestmark = pytest.mark.timeout(15)


def test_safe_display_name_escapes_controls_bidi_and_surrogates():
    assert safe_display_name("a\nb\x1b\x7f\x85c") == "a\\x0ab\\x1b\\x7f\\x85c"
    assert safe_display_name("photo‮gnp.txt⁦‏") == "photo\\u202egnp.txt\\u2066\\u200f"
    assert safe_display_name("bad\udcff") == "bad\\udcff"
    assert safe_display_name("Łódź/plik ąę.txt") == "Łódź/plik ąę.txt"
    assert safe_display_name("x" * 50, max_len=10) == "x" * 9 + "…"


def test_zip_symlink_entry_is_quarantined_and_case_colliding_names_both_kept(settings, tmp_path):
    link = zipfile.ZipInfo("link.txt")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = tmp_path / "mixed.zip"
    path.write_bytes(zip_bytes([(link, "/etc/passwd"), ("A.txt", "first"), ("a.txt", "second")]))
    result = run_import(settings, [path])
    items = result["items"]
    assert items[0]["status"] == "quarantined" and items[0]["error_code"] == "ZIP_SYMLINK"
    assert [i["status"] for i in items[1:]] == ["stored", "stored"]
    assert items[1]["artifact_id"] != items[2]["artifact_id"]


def test_folder_walk_keeps_inside_links_and_reports_escapes(settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("ASTRA_OUTSIDE_SENTINEL")
    folder = settings.data_dir / "walk"
    folder.mkdir()
    (folder / "real.txt").write_text("inside")
    (folder / "inside-link.txt").symlink_to(folder / "real.txt")
    (folder / "dangling.txt").symlink_to(folder / "missing.txt")
    (folder / "linked-dir").symlink_to(outside, target_is_directory=True)
    result = run_import(settings, [check_input_path(folder, settings)])
    by_ref = {i["source_ref"]: i for i in result["items"]}
    assert sorted(by_ref[r]["status"] for r in ("walk/real.txt", "walk/inside-link.txt")) == ["duplicate", "stored"]
    assert by_ref["walk/dangling.txt"]["error_code"] == "PATH_NOT_ALLOWED"
    assert by_ref["walk/linked-dir"]["error_code"] == "SYMLINK_DIR_NOT_FOLLOWED"
    assert len(result["items"]) == 4


@pytest.mark.parametrize("raw", ["bad\x00name", "~astra-no-such-user/x", "/" + "x" * 5000])
def test_malformed_input_paths_are_path_not_allowed(settings, raw):
    with pytest.raises(PathNotAllowed):
        check_input_path(raw, settings)


def test_malformed_import_paths_are_client_errors(client):
    result = client.post("/v1/import/paths", json={"paths": ["bad\x00name"]})
    assert result.status_code == 400
    assert client.post("/v1/import/paths", json={"paths": "not-a-list"}).status_code == 422


def test_upload_name_error_message_is_polish(client):
    result = client.post("/v1/import", files={"files": ("x" * 300 + ".txt", b"safe")})
    assert result.status_code == 400 and "Nieprawidłowa nazwa pliku" in result.json()["detail"]


@pytest.mark.parametrize(("origin", "code"), [("http://[::1]:5173", 202), ("http://localhost:5173/", 202),
                                               ("ftp://localhost", 403), ("http://localhost/path", 403)])
def test_origin_parsing_allows_local_and_denies_odd_shapes(client, origin, code):
    assert client.post("/v1/import/paths", json={"paths": []}, headers={"Origin": origin}).status_code == code


def test_cas_symlink_is_refused_by_store_and_viewer_routes(settings, tmp_path):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("ASTRA_OUTSIDE_SENTINEL")
    digest = hashlib.sha256(b"synthetic-original-2").hexdigest()
    store = ContentStore(settings.cas_dir)
    path = store.path_for(digest)
    path.parent.mkdir(parents=True)
    path.symlink_to(outside)
    with pytest.raises(UnsafeStorePath):
        store.open(digest)
    writer = Writer(settings.db_path)
    try:
        writer.run(lambda conn: register_artifact(
            conn, digest=digest, mime="text/plain", size=1, source_ref="synthetic", job_id="astra"))
    finally:
        writer.close()
    with TestClient(create_app(settings), raise_server_exceptions=False) as http:
        for suffix in ("raw", "text"):
            response = http.get(f"/v1/artifacts/{digest}/{suffix}")
            assert response.status_code == 404, suffix
            assert "ASTRA_OUTSIDE_SENTINEL" not in response.text
