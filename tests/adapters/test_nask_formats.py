"""NASK folder adapter: dot-files, byte sniffing, capture bundles, funnel stages, ZIP inputs, mis-filed HARs.

Synthetic fixtures only (tmp_path). The last test is an opt-in regression on the real sample: it runs only with
HYDRA_SAMPLE_TEST=1 and when ~/HydraNexus/data/nask/sample exists; it reads aggregates (counts, roles)
and never prints content.
"""

from __future__ import annotations

import json
import os
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from hydra.adapters import nask_folder as nf
from hydra.config import Settings
from hydra.db import Writer
from hydra.storage import ContentStore

HAR_EMPTY = json.dumps({"log": {"version": "1.2", "creator": {"name": "WebInspector"}, "entries": []}})


def har(url: str, pages_title: str | None = None) -> str:
    entry = {"_resourceType": "document", "request": {"method": "GET", "url": url, "headers": []},
             "response": {"status": 200, "content": {"mimeType": "text/html"}}}
    pages = [{"title": pages_title or url}]
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "WebInspector"}, "pages": pages,
                               "entries": [entry]}})


def page(host: str) -> str:
    return f"<!-- saved from url=(0030)https://{host}/?utm_source=fb --><html><title>T</title></html>"


def run_import(settings: Settings, folder: Path) -> nf.ImportReport:
    w = Writer(settings.db_path)
    try:
        return nf.import_incident(folder, w, ContentStore(settings.cas_dir), settings)
    finally:
        w.close()


# ------------------------------------------------------------------------------------- 3. dot-files


def test_hidden_data_files_imported_non_data_reported_junk_silent(settings: Settings, make_png, tmp_path):
    d = tmp_path / "61"
    (d / "strona_1").mkdir(parents=True)
    (d / "strona_1" / ".har").write_text(HAR_EMPTY)
    (d / ".mhtml").write_bytes(b"From: <Saved by Blink>\r\nMIME-Version: 1.0\r\n\r\n")
    (d / ".cover").write_bytes(make_png())           # image by magic bytes, no extension
    (d / ".gitignore").write_text("*.pyc\n")          # hidden, not data
    (d / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1" * 8)
    (d / "__MACOSX" / "strona_1").mkdir(parents=True)
    (d / "__MACOSX" / "strona_1" / "._.har").write_bytes(b"\x00\x05\x16\x07")
    (d / ".git").mkdir()
    (d / ".git" / "HEAD").write_text("ref: refs/heads/main")
    (d / "Thumbs.db").write_bytes(b"\xd0\xcf\x11\xe0junk")
    rep = run_import(settings, d)
    roles = {a.path_in_incident: a.role for a in rep.incident.artifacts}
    assert roles["strona_1/.har"] == "har"
    assert roles[".cover"] == "feed_screenshot"
    assert ".mhtml" in roles
    assert ".gitignore" not in roles
    assert rep.incident.meta["skipped"] == [{"path": ".gitignore", "code": "HIDDEN_NOT_DATA",
                                             "reason": "hidden file that is not a recognised data format"}]
    assert not any(p.startswith(("__MACOSX", ".git/")) or p in (".DS_Store", "Thumbs.db") for p in roles)


# ------------------------------------------------------------------------------------- 5. sniffing


def test_extensionless_and_truncated_resources_are_sniffed_by_bytes(tmp_path, make_png):
    files = tmp_path / "7" / "Strona_files"
    files.mkdir(parents=True)
    (tmp_path / "7" / "Strona.html").write_text(page("x.example"))
    samples = {
        "js": "(function(){var a=1;window.x=a})();",
        "css(1)": "@font-face{font-family:'Roboto';src:url(a.woff2)}",
        "m=el_main_css": ".goog-te-banner{display:none;color:#fff}",
        "fbevents.pobrane": "/*1709*/ !function(f,b,e){if(f.fbq)return;}(window,document);",
        "1234567890123456": "fbq.registerPlugin('config', {__fbEventsPlugin: 1});",
        "sweetalert2@11": "!function(t,e){\"object\"==typeof exports}(this,function(){'use strict'});",
        "notes.txt": "function words in a plain note are still a note",
    }
    for name, text in samples.items():
        (files / name).write_text(text)
    (files / "delivered").write_bytes(make_png())
    (files / "02_persona_.webp").write_bytes(make_png())  # truncated name, real bytes are PNG
    planned, errors, _ = nf.plan_incident(tmp_path / "7", 10 * 2**20)
    mime = {Path(p.rel).name: p.mime for p in planned}
    roles = {Path(p.rel).name: p.role for p in planned}
    assert errors == []
    assert mime["js"] == mime["fbevents.pobrane"] == mime["1234567890123456"] == "application/javascript"
    assert mime["sweetalert2@11"] == "application/javascript"
    assert mime["css(1)"] == mime["m=el_main_css"] == "text/css"
    assert mime["notes.txt"] == "text/plain"
    assert mime["delivered"] == mime["02_persona_.webp"] == "image/png"
    assert set(roles.values()) <= set(nf.ArtifactRole) and all(roles.values())
    assert roles["delivered"] == "landing_asset"


# ------------------------------------------------------------------------------------- 5. bundles, stages


def make_capture_incident(root: Path, make_png) -> Path:
    """Incident 14 shape: Chrome save + a page-capture bundle nested twice + HLS media + phone screenshots."""
    d = root / "14"
    (d / "Slimvita RX_files").mkdir(parents=True)
    (d / "Slimvita RX.html").write_text(page("slimvita.example"))
    (d / "Slimvita RX_files" / "jquery-3.6.0.min.js.pobrane").write_text("var jq=function(){};")
    (d / "IMG_1001.PNG").write_bytes(make_png((1, 1, 1)))
    b = d / "inept_top" / "inept_top"
    for sub in ("resources/media", "resources/script"):
        (b / sub).mkdir(parents=True)
    (b / "manifest.json").write_text(json.dumps({"tool": "page-capture 0.1.0", "page_url": "https://inept.example/"}))
    (b / "dom.html").write_text("<html><body>inept.example</body></html>")
    (b / "page.har").write_text(har("https://inept.example/?key=[REDACTED]"))
    (b / "page.mhtml").write_bytes(
        b"From: <Saved by Blink>\r\nSnapshot-Content-Location: https://inept.example/\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/related; type="text/html"; boundary="B"\r\n\r\n--B\r\n'
        b"Content-Type: text/html\r\nContent-Location: https://inept.example/\r\n\r\n<html>inept</html>\r\n--B--\r\n")
    (b / "screenshot.png").write_bytes(make_png((2, 2, 2)))
    (b / "resources" / "media" / "output_000_5e6f7a8b.ts").write_bytes(b"\x47" + b"\x00" * 187 + b"\x47" * 188)
    (b / "resources" / "media" / "output_9c0d1e2f.m3u8").write_text("#EXTM3U\n#EXTINF:4,\noutput_000.ts\n")
    (b / "resources" / "script" / "jquery-3.6.0.min_0a1b2c3d.js").write_text("var jq=function(){};")
    return d


def test_nested_capture_bundle_is_one_incident_and_every_file_has_a_role(make_png, tmp_path):
    d = make_capture_incident(tmp_path, make_png)
    (d / "IMG_1001.PNG").unlink()  # no loose evidence at the top: the bundle alone must not split the incident
    (d / "Slimvita RX.html").unlink()
    assert nf.discover_incidents(d) == [d.resolve()]
    assert nf.classify_input(d) == "incident"
    make_capture_incident(tmp_path / "again", make_png)
    planned, errors, _ = nf.plan_incident(tmp_path / "again" / "14", 10 * 2**20)
    roles = {p.rel: p.role for p in planned if p.path is not None}
    files = [f for f in (tmp_path / "again" / "14").rglob("*") if f.is_file()]
    assert errors == [] and len(roles) == len(files)
    b = "inept_top/inept_top"
    assert roles[f"{b}/manifest.json"] == "platform_metadata"
    assert roles[f"{b}/page.har"] == "har" and roles[f"{b}/page.mhtml"] == "landing_html"
    assert roles[f"{b}/screenshot.png"] == "landing_screenshot"
    assert roles[f"{b}/resources/media/output_000_5e6f7a8b.ts"] == "landing_asset"
    assert roles["IMG_1001.PNG"] == "feed_screenshot"
    names = {p.meta["name"] for p in planned if p.rel.endswith(".js") or p.rel.endswith(".pobrane")}
    assert names == {"jquery-3.6.0.min.js"}  # capture `_<sha8>` and Chrome `.pobrane` names line up


def test_funnel_stages_only_folder_is_one_incident_with_stage_meta(make_png, tmp_path):
    d = tmp_path / "61"
    for k, host in ((1, "black.example"), (2, "form.example"), (3, "white.example")):
        (d / f"strona_{k}").mkdir(parents=True)
        (d / f"strona_{k}" / "p.html").write_text(page(host))
        (d / f"strona_{k}" / f"{host}.har").write_text(har(f"https://{host}/"))
    assert nf.discover_incidents(d) == [d.resolve()]
    planned, _, _ = nf.plan_incident(d, 10 * 2**20)
    assert sorted({p.meta["stage"] for p in planned}) == [1, 2, 3]
    assert not any("note" in p.meta for p in planned)  # every HAR host is on one of the incident's pages


# ------------------------------------------------------------------------------------- 2. classify_input


def test_classify_input_dataset_incident_file(make_png, tmp_path):
    ds = tmp_path / "hidden"
    for k in ("1", "2"):
        (ds / k).mkdir(parents=True)
        (ds / k / "a.png").write_bytes(make_png())
    (ds / "labels.csv").write_text("incident,campaign\n1,a\n")
    (ds / "README.pdf").write_bytes(b"%PDF-1.4\n")
    assert nf.classify_input(ds) == "dataset"
    assert nf.loose_root_files(ds) == ["README.pdf", "labels.csv"]
    assert nf.classify_input(ds / "1") == "incident"
    assert nf.classify_input(ds / "1" / "a.png") == "file"
    # a dataset of one numbered incident plus a loose screenshot is still a dataset
    (ds / "2" / "a.png").unlink()
    (ds / "2").rmdir()
    (ds / "overview.png").write_bytes(make_png())
    assert [p.name for p in nf.discover_incidents(ds)] == ["1"]
    assert "overview.png" in nf.loose_root_files(ds)


# ------------------------------------------------------------------------------------- 5. ZIP inputs


def zip_dir(src: Path, out: Path, *, wrapper: str | None, extra: dict[str, bytes] | None = None) -> Path:
    with zipfile.ZipFile(out, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                rel = f.relative_to(src).as_posix()
                zf.write(f, f"{wrapper}/{rel}" if wrapper else rel)
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return out


def test_zip_of_incident_and_zip_of_dataset(settings: Settings, make_png, tmp_path):
    src = tmp_path / "src"
    make_capture_incident(src, make_png)
    (src / "14" / "strona_1").mkdir()
    (src / "14" / "strona_1" / ".har").write_text(har("https://slimvita.example/"))
    (src / "3").mkdir()
    (src / "3" / "IMG_1.PNG").write_bytes(make_png((3, 3, 3)))
    inc_zip = zip_dir(src / "14", tmp_path / "14.zip", wrapper="14",
                      extra={"__MACOSX/14/._x": b"\x00", "../evil.txt": b"x", "14/.DS_Store": b"\x00"})
    ds_zip = zip_dir(src, tmp_path / "sample.zip", wrapper="sample")
    flat_zip = zip_dir(src / "3", tmp_path / "3.zip", wrapper=None)

    assert nf.classify_input(inc_zip, settings) == "incident"
    assert nf.classify_input(flat_zip, settings) == "incident"
    assert nf.classify_input(ds_zip, settings) == "dataset"
    (inc,) = nf.discover_incidents(inc_zip, settings)
    assert inc.name == "14" and (inc / "strona_1" / ".har").is_file()
    assert not (inc / "__MACOSX").exists() and not (inc / ".DS_Store").exists()
    assert not any(p.name == "evil.txt" for p in tmp_path.rglob("evil.txt"))
    assert [p.name for p in nf.discover_incidents(ds_zip, settings)] == ["3", "14"]
    assert [p.name for p in nf.discover_incidents(flat_zip, settings)] == [nf.zip_content_name(flat_zip)]

    w = Writer(settings.db_path)
    try:
        store = ContentStore(settings.cas_dir)
        reps = [nf.import_incident(f, w, store, settings) for f in nf.discover_incidents(ds_zip, settings)]
        again = [nf.import_incident(f, w, store, settings) for f in nf.discover_incidents(ds_zip, settings)]
    finally:
        w.close()
    assert [r.incident.id for r in reps] == [r.incident.id for r in again]  # same scratch path → idempotent
    assert all(r.new_artifacts == 0 for r in again)
    har_paths = {a.path_in_incident for a in reps[1].incident.artifacts if a.role == "har"}
    assert "strona_1/.har" in har_paths


def test_zip_in_a_dataset_root_is_an_incident_not_a_loose_file(settings: Settings, make_png, tmp_path):
    ds = tmp_path / "ds"
    (ds / "1").mkdir(parents=True)
    (ds / "1" / "a.png").write_bytes(make_png())
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "b.png").write_bytes(make_png((4, 4, 4)))
    zip_dir(tmp_path / "x", ds / "2.zip", wrapper=None)
    assert [p.name for p in nf.discover_incidents(ds, settings)] == ["1", nf.zip_content_name(ds / "2.zip")]
    assert nf.loose_root_files(ds) == []


def test_identical_zip_under_another_name_and_in_parallel_gives_one_folder(settings: Settings, make_png, tmp_path):
    """Audit A08: the unpack result depends only on the bytes (no name-dependent folder, no shared marker)."""
    from concurrent.futures import ThreadPoolExecutor

    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "a.png").write_bytes(make_png())
    one = zip_dir(tmp_path / "x", tmp_path / "one.zip", wrapper=None)
    two = tmp_path / "two.zip"
    two.write_bytes(one.read_bytes())
    assert nf.unpack_zip(one, settings) == nf.unpack_zip(two, settings)
    three = tmp_path / "three.zip"
    three.write_bytes(one.read_bytes() + b"")  # same bytes again, fresh scratch: race four imports
    import shutil

    shutil.rmtree(nf.unpack_zip(one, settings).parent)
    with ThreadPoolExecutor(4) as ex:
        outs = set(ex.map(lambda z: nf.unpack_zip(z, settings), [one, two, three, one]))
    assert len(outs) == 1 and (next(iter(outs)) / "a.png").is_file()
    assert not [p for p in settings.tmp_dir.iterdir() if p.name.startswith(".unzip-")]  # no leftovers


def test_zip_total_size_and_free_space_limits(settings: Settings, tmp_path, monkeypatch):
    import shutil

    z = tmp_path / "big.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.bin", b"\x01" * (2 * nf.MB))
    monkeypatch.setattr(nf, "ZIP_MAX_TOTAL_MB", 1)
    with pytest.raises(nf.ZipRejected) as e:
        nf.unpack_zip(z, settings)
    assert e.value.code == "ZIP_TOO_LARGE"
    monkeypatch.setattr(nf, "ZIP_MAX_TOTAL_MB", 8192)
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda p: real(p)._replace(free=nf.MB))
    with pytest.raises(nf.ZipRejected) as e:
        nf.unpack_zip(z, settings)
    assert e.value.code == "ZIP_NO_SPACE"
    assert not [p for p in settings.tmp_dir.iterdir() if "unzip" in p.name]  # nothing half-unpacked is kept


def test_is_data_file_is_the_shared_upload_rule(make_png):
    assert nf.is_data_file("strona_1/.har") and nf.is_data_file(".har") and nf.is_data_file("x/.page.json")
    assert nf.is_data_file(".cover", make_png()) and not nf.is_data_file(".cover")
    assert nf.is_data_file("61\\strona_1\\.har")
    assert nf.is_data_file("IMG_1.PNG") and nf.is_data_file("Strona_files/js")
    for junk in (".DS_Store", "._a.png", "__MACOSX/x/a.png", ".git/config", "Thumbs.db", "desktop.ini", ""):
        assert not nf.is_data_file(junk, make_png()), junk
    assert not nf.is_data_file(".gitignore", b"*.pyc\n") and not nf.is_data_file(".env", b"KEY=1")


# ------------------------------------------------------------------------------------- 6. mis-filed HAR


def test_misfiled_har_gets_an_analyst_note_not_a_move(settings: Settings, tmp_path):
    d = tmp_path / "2"
    (d / "Strona_files").mkdir(parents=True)
    (d / "Strona.html").write_text(page("przyklad-oferta.test"))
    (d / "przyklad-oferta.test.har").write_text(har("https://www.przyklad-oferta.test/a?x=1"))
    (d / "zdrowie-link.test.har").write_text(har("https://zdrowie-link.test/l?clickid=1"))
    (d / "replay.har").write_text(har("https://www.googletagmanager.com/gtm.js",
                                      pages_title="file:///C:/Users/jan/Downloads/x.html"))
    rep = run_import(settings, d)
    meta = {a.path_in_incident: a.meta for a in rep.incident.artifacts}
    assert meta["zdrowie-link.test.har"]["note"] == nf.MISFILED_NOTE
    assert meta["zdrowie-link.test.har"]["main_host"] == "zdrowie-link.test"
    assert "note" not in meta["przyklad-oferta.test.har"]
    assert meta["replay.har"]["har_kind"] == "replay" and "note" not in meta["replay.har"]
    assert "jan" not in json.dumps(meta["replay.har"])  # the local path (Windows user name) is not stored
    # no saved page in the incident: unknown, never flagged
    solo = tmp_path / "3"
    solo.mkdir()
    (solo / "zdrowie-link.test.har").write_text(har("https://zdrowie-link.test/"))
    planned, _, _ = nf.plan_incident(solo, 10 * 2**20)
    assert "note" not in planned[0].meta


# ------------------------------------------------------------------------------------- real sample (opt-in)

SAMPLE = Path.home() / "HydraNexus" / "data" / "nask" / "sample"
# Measured on integration @472807a (before this change): original files per incident and their roles.
EXPECTED = {
    "1": (21, {"feed_screenshot": 1, "har": 1, "landing_asset": 18, "landing_html": 1}),
    "2": (21, {"har": 2, "landing_asset": 18, "landing_html": 1}),
    "3": (15, {"feed_screenshot": 1, "landing_asset": 13, "landing_html": 1}),
    "5": (2, {"ad_video": 1, "platform_metadata": 1}),
    "10": (33, {"feed_screenshot": 6, "har": 1, "landing_asset": 25, "landing_html": 1}),
    "14": (80, {"feed_screenshot": 10, "har": 1, "landing_asset": 63, "landing_html": 3, "landing_screenshot": 1,
                "platform_metadata": 1, "screen_recording": 1}),
    "36": (38, {"feed_screenshot": 1, "har": 1, "landing_asset": 35, "landing_html": 1}),
    "47": (29, {"har": 1, "landing_asset": 26, "landing_html": 1, "screen_recording": 1}),
    "48": (32, {"feed_screenshot": 1, "har": 1, "landing_asset": 29, "landing_html": 1}),
    "61": (58, {"ad_creative": 1, "har": 3, "landing_asset": 51, "landing_html": 3}),
}


@pytest.mark.skipif(not SAMPLE.is_dir() or os.environ.get("HYDRA_SAMPLE_TEST") != "1",
                    reason="organiser sample not present or HYDRA_SAMPLE_TEST!=1")
def test_real_sample_counts_roles_and_flags():
    assert nf.classify_input(SAMPLE) == "dataset"
    found = nf.discover_incidents(SAMPLE)
    assert sorted(p.name for p in found) == sorted(EXPECTED)
    notes, replays = [], []
    for folder in found:
        assert nf.discover_incidents(folder) == [folder]
        planned, errors, _ = nf.plan_incident(folder, 2048 * 2**20)
        originals = [p for p in planned if p.path is not None]
        assert errors == []
        n, roles = EXPECTED[folder.name]
        assert (len(originals), dict(Counter(p.role.value for p in originals))) == (n, roles), folder.name
        notes += [f"{folder.name}/{p.rel}" for p in planned if p.meta.get("note")]
        replays += [f"{folder.name}/{p.rel}" for p in planned if p.meta.get("har_kind") == "replay"]
    # exactly one landing HAR is filed under another incident's folder (FORMATS.md §3); no names embedded
    assert len(notes) == 1 and notes[0].startswith("2/") and notes[0].endswith(".har")
    assert replays == ["61/strona_1/.har"]      # FORMATS.md §23: local replay
