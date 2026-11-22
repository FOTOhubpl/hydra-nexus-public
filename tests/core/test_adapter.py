"""NASK folder adapter on a synthetic incident that mimics the sample layout (no organiser data)."""

from __future__ import annotations

import json
from pathlib import Path

from hydra.adapters.nask_folder import (
    discover_incidents,
    import_incident,
    import_single,
    load_incident,
    logical_name,
    resolve,
)
from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.storage import ContentStore

MHTML = (
    "From: <Saved by Blink>\r\nSnapshot-Content-Location: https://x.example/\r\nSubject: T\r\n"
    "Date: Sat, 14 Mar 2026 10:15:00 GMT\r\nMIME-Version: 1.0\r\n"
    'Content-Type: multipart/related;\r\n\ttype="text/html";\r\n\tboundary="----B----"\r\n\r\n\r\n'
    "------B----\r\nContent-Type: text/html\r\nContent-Transfer-Encoding: quoted-printable\r\n"
    "Content-Location: https://x.example/\r\n\r\n<html><body>Zysk 3D500 z=C5=82</body></html>\r\n"
    "------B----\r\nContent-Type: text/css\r\nContent-Transfer-Encoding: quoted-printable\r\n"
    "Content-Location: https://x.example/a.css\r\n\r\nbody{color:red}\r\n------B------\r\n"
)


def make_incident(root: Path, make_png) -> Path:
    d = root / "7"
    files = d / "Strona_files"
    files.mkdir(parents=True)
    (d / "Zrzut ekranu 2026-03-14 101500.png").write_bytes(make_png((1, 2, 3)))
    (d / "reklama_1.png").write_bytes(make_png((9, 9, 9)))
    (d / "Strona.html").write_text("<!-- saved from url=(0020)https://x.example/ --><html><title>T</title></html>")
    (files / "main.js.pobrane").write_text("console.log(1)")
    (files / "logo.pobierz").write_bytes(make_png((5, 5, 5)))
    (files / "1234567890123456").write_text("fbq.registerPlugin('config')")
    sub = d / "strona_1"
    sub.mkdir()
    (sub / ".har").write_text(json.dumps({"log": {"version": "1.2", "creator": {}, "entries": []}}))
    (sub / "page.mhtml").write_bytes(MHTML.encode())
    (d / "Ad [123_4].mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    (d / "Ad [123_4].info.json").write_text(json.dumps({"id": "123_4", "title": "Ad", "epoch": 1760000000}))
    (d / "note.txt").write_text("https://x.example/?utm_id=1")
    (d / ".DS_Store").write_bytes(b"junk")
    return d


def test_logical_name_strips_download_suffixes():
    assert logical_name("main.js.pobrane") == "main.js"
    assert logical_name("fbevents.pobierz") == "fbevents"
    assert logical_name("plain.css") == "plain.css"
    assert logical_name("jquery-3.6.0.min_0a1b2c3d.js") == "jquery-3.6.0.min.js"


def test_every_file_gets_a_role_and_mhtml_is_split(settings: Settings, make_png, tmp_path):
    d = make_incident(tmp_path, make_png)
    w = Writer(settings.db_path)
    try:
        rep = import_incident(d, w, ContentStore(settings.cas_dir), settings)
        rep2 = import_incident(d, w, ContentStore(settings.cas_dir), settings)
    finally:
        w.close()
    roles = {a.path_in_incident: a.role for a in rep.incident.artifacts}
    assert roles["Zrzut ekranu 2026-03-14 101500.png"] == "feed_screenshot"
    assert roles["reklama_1.png"] == "ad_creative"
    assert roles["Strona.html"] == "landing_html"
    assert roles["Strona_files/main.js.pobrane"] == "landing_asset"
    assert roles["strona_1/.har"] == "har"
    assert roles["strona_1/page.mhtml"] == "landing_html"
    assert roles["Ad [123_4].mp4"] == "ad_video"
    assert roles["Ad [123_4].info.json"] == "platform_metadata"
    assert roles["note.txt"] == "url"
    assert ".DS_Store" not in roles
    parts = [a for a in rep.incident.artifacts if "#part" in a.path_in_incident]
    assert {p.role for p in parts} == {"landing_html", "landing_asset"}
    assert all(p.meta["derived_from"] for p in parts)
    mime = {a.path_in_incident: a.mime for a in rep.incident.artifacts}
    assert mime["Strona_files/main.js.pobrane"] == "application/javascript"
    assert mime["Strona_files/logo.pobierz"] == "image/png"
    page = {a.path_in_incident: a.meta["page"] for a in rep.incident.artifacts}
    assert page["Strona_files/main.js.pobrane"] == "" and page["strona_1/.har"] == "strona_1"
    stage = {a.path_in_incident: a.meta.get("stage") for a in rep.incident.artifacts}
    assert stage["strona_1/.har"] == 1 and stage["Strona.html"] is None
    assert rep.incident.observed_at == "2025-10-09"  # earliest date: info.json epoch
    assert rep.new_artifacts == len({a.artifact_id for a in rep.incident.artifacts})
    assert rep2.new_artifacts == 0  # idempotent
    with connect(settings.db_path, readonly=True) as conn:
        inc = load_incident(conn, "nask-7")
        n_links = conn.execute("SELECT COUNT(*) FROM incident_artifacts").fetchone()[0]
    assert inc is not None and len(inc.artifacts) == len(rep.incident.artifacts) == n_links
    resolved = resolve(inc, ContentStore(settings.cas_dir))
    assert len(resolved.artifacts) == len(inc.artifacts)
    assert len(resolved.by_role("har")) == 1 and resolved.by_role("har")[0].path.is_file()


def test_discover_dataset_vs_single_folder(make_png, tmp_path):
    make_incident(tmp_path / "ds", make_png)
    (tmp_path / "ds" / "8").mkdir()
    (tmp_path / "ds" / "8" / "a.png").write_bytes(make_png())
    found = discover_incidents(tmp_path / "ds")
    assert [p.name for p in found] == ["7", "8"]
    assert discover_incidents(tmp_path / "ds" / "7") == [(tmp_path / "ds" / "7").resolve()]


def test_single_inputs_url_and_image(settings: Settings, make_png, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(make_png())
    w = Writer(settings.db_path)
    try:
        r1 = import_single("https://bad.example/x?y=1", w, ContentStore(settings.cas_dir), settings)
        r2 = import_single(img, w, ContentStore(settings.cas_dir), settings)
    finally:
        w.close()
    assert r1.incident.id.startswith("new-") and [a.role for a in r1.incident.artifacts] == ["url"]
    assert r1.incident.source == "url"
    assert [a.role for a in r2.incident.artifacts] == ["feed_screenshot"]


def test_dotfiles_with_data_are_kept_and_os_junk_skipped(settings: Settings, make_png, tmp_path):
    d = tmp_path / "9"
    (d / "strona_1").mkdir(parents=True)
    (d / "strona_1" / ".har").write_text(json.dumps({"log": {"version": "1.2", "creator": {}, "entries": []}}))
    (d / ".page.json").write_text('{"a": 1}')
    (d / ".hidden.html").write_text("<html><title>x</title></html>")
    (d / "a.png").write_bytes(make_png())
    for junk in (".DS_Store", "._a.png", "Thumbs.db"):
        (d / junk).write_bytes(b"\x00\x05\x16\x07junk" * 10)
    (d / "__MACOSX").mkdir()
    (d / "__MACOSX" / "x.png").write_bytes(make_png())
    (d / ".git").mkdir()
    (d / ".git" / "config").write_text("[core]")
    w = Writer(settings.db_path)
    try:
        rep = import_incident(d, w, ContentStore(settings.cas_dir), settings)
    finally:
        w.close()
    paths = {a.path_in_incident for a in rep.incident.artifacts}
    assert {"strona_1/.har", ".page.json", ".hidden.html", "a.png"} <= paths
    assert not any(p.startswith(("__MACOSX", ".git")) or p in (".DS_Store", "._a.png", "Thumbs.db") for p in paths)


def test_same_folder_names_from_another_dataset_never_merge(settings: Settings, make_png, tmp_path):
    a = tmp_path / "sample" / "1"
    b = tmp_path / "hidden" / "1"
    for d, color in ((a, (1, 1, 1)), (b, (2, 2, 2))):
        d.mkdir(parents=True)
        (d / "x.png").write_bytes(make_png(color))
    w = Writer(settings.db_path)
    try:
        r1 = import_incident(a, w, ContentStore(settings.cas_dir), settings)
        r2 = import_incident(b, w, ContentStore(settings.cas_dir), settings)
        r1b = import_incident(a, w, ContentStore(settings.cas_dir), settings)  # re-import stays idempotent
    finally:
        w.close()
    assert r1.incident.id == "nask-1" == r1b.incident.id
    assert r2.incident.id.startswith("nask-1-") and r2.incident.id != r1.incident.id
    with connect(settings.db_path, readonly=True) as conn:
        assert len(load_incident(conn, "nask-1").artifacts) == 1


def test_loose_root_files_do_not_turn_a_dataset_into_one_incident(make_png, tmp_path):
    from hydra.adapters.nask_folder import loose_root_files

    ds = tmp_path / "ds"
    for k in ("1", "2", "3"):
        (ds / k).mkdir(parents=True)
        (ds / k / "a.png").write_bytes(make_png())
    (ds / "README.md").write_text("opis")
    (ds / "opis.pdf").write_bytes(b"%PDF-1.4 x")
    assert [p.name for p in discover_incidents(ds)] == ["1", "2", "3"]
    assert loose_root_files(ds) == ["README.md", "opis.pdf"]
    # an incident with funnel sub-pages stays one incident
    inc = tmp_path / "61"
    for k in (1, 2, 3):
        (inc / f"strona_{k}").mkdir(parents=True)
        (inc / f"strona_{k}" / "p.html").write_text("<html></html>")
    (inc / "reklama_1.png").write_bytes(make_png())
    assert discover_incidents(inc) == [inc.resolve()]


def test_size_caps_by_type(settings: Settings, make_png, tmp_path, monkeypatch):
    import hydra.adapters.nask_folder as nf

    monkeypatch.setattr(nf, "DOC_LIMIT_MB", 0)  # every non-video file is "too large"; videos keep the 2 GB cap
    settings.max_file_mb = 0
    d = tmp_path / "big"
    d.mkdir()
    (d / "page.har").write_text(json.dumps({"log": {"version": "1.2", "creator": {}, "entries": []}}))
    (d / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    w = Writer(settings.db_path)
    try:
        rep = import_incident(d, w, ContentStore(settings.cas_dir), settings)
    finally:
        w.close()
    assert [a.path_in_incident for a in rep.incident.artifacts] == ["clip.mp4"]
    assert rep.errors == [{"path": "page.har", "code": "FILE_TOO_LARGE"}]


def test_unzipped_incident_source_ref_is_archive_relative(settings: Settings, make_png, tmp_path):
    """CR-E2E-6: an incident from an unpacked upload stores `zip-<sha8>/base/a1`, not the deleted scratch path;
    two archives with the same folder names still never merge, and a pre-fix absolute ref keeps its id."""
    import zipfile

    from hydra.adapters.nask_folder import unpack_zip

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    zips = []
    for name, color in (("one.zip", (1, 1, 1)), ("two.zip", (2, 2, 2))):
        z = tmp_path / name
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("base/a1/x.png", make_png(color))
            zf.writestr("base/a2/y.png", make_png((3, 3, 3)))
        zips.append(z)
    w = Writer(settings.db_path)
    try:
        store = ContentStore(settings.cas_dir)
        roots = [unpack_zip(z, settings) for z in zips]
        r1 = import_incident(roots[0] / "a1", w, store, settings)
        r2 = import_incident(roots[1] / "a1", w, store, settings)
        r1b = import_incident(roots[0] / "a1", w, store, settings)
        legacy = roots[0] / "a2"  # a row written before the fix holds the absolute folder path
        w.run(lambda c: c.execute("INSERT INTO incidents (id, source, source_ref, title, observed_at, label, meta,"
                                  " created_at) VALUES ('nask-a2', 'nask-sample', ?, NULL, NULL, NULL, '{}',"
                                  " '2026-11-21')", (str(legacy.resolve()),)))
        r3 = import_incident(legacy, w, store, settings)
    finally:
        w.close()
    ref = r1.incident.source_ref
    assert ref.startswith("zip-") and ref.endswith("/base/a1"), ref
    assert str(settings.state_dir) not in ref and not ref.startswith("/")
    assert r1.incident.id == "nask-a1" == r1b.incident.id
    assert r2.incident.id.startswith("nask-a1-") and r2.incident.source_ref != ref
    assert r3.incident.id == "nask-a2" and r3.incident.source_ref.endswith("/base/a2")
