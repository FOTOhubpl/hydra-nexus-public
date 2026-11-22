"""scripts/ioc_overlap.py: aggregate-only indicator overlap on synthetic files."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "ioc_overlap", Path(__file__).resolve().parents[2] / "scripts" / "ioc_overlap.py")
assert _SPEC and _SPEC.loader
ov = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ov)


def test_scan_counts_only(tmp_path: Path) -> None:
    blob = b"binary ad file bytes"
    sha = hashlib.sha256(blob).hexdigest()
    iocs = {"sha256": {sha}, "host": {"api.bad.link"}, "ip": {"192.0.2.1"}, "package": {"org.example.app"}}
    d = tmp_path / "corpus"
    d.mkdir()
    (d / "ad.bin").write_bytes(blob)
    (d / "page.html").write_text("see http://API.BAD.LINK/x and 192.0.2.1 but not 192.0.2.10 nor xapi.bad.link")
    (d / "clean.txt").write_text("nothing here 1192.0.2.1")
    with zipfile.ZipFile(d / "pack.zip", "w") as zf:
        zf.writestr("inner.txt", "org.example.app inside a zip")
    res = ov.scan(d, iocs, ov.compile_iocs(iocs))
    assert res["blob_sha256_hits"] == 1 and res["blob_sha256_distinct"] == 1
    assert res["kinds"]["host"] == {"indicators": 1, "distinct_matched": 1, "blobs_matched": 1}
    assert res["kinds"]["ip"]["distinct_matched"] == 1 and res["kinds"]["ip"]["blobs_matched"] == 1
    assert res["kinds"]["package"]["blobs_matched"] == 1  # found in the zip member
    text = json.dumps(res)
    for value in ("api.bad.link", "192.0.2.1", "org.example.app", sha, "page.html"):
        assert value not in text  # counts only: no values, no file names


def test_scan_images_counts_only(tmp_path: Path, make_png) -> None:
    ref = tmp_path / "ref"
    ref.mkdir()
    a = make_png((200, 10, 10))
    (ref / "a.png").write_bytes(a)
    shas, ph = ov.image_refs(ref)
    assert len(shas) == 1 and len(ph) == 1
    tgt = tmp_path / "tgt"
    tgt.mkdir()
    (tgt / "same.png").write_bytes(a)
    (tgt / "note.txt").write_text("not an image")
    res = ov.scan_images(tgt, shas, ph, 10)
    assert res["images"] == 1 and res["sha256_hits"] == 1 and res["phash_near"] == 1
    assert res["reference_images_matched"] == 1
    assert "same.png" not in json.dumps(res)
