"""External-dataset adapters on synthetic fixtures (no real dataset bytes in the repo)."""

from __future__ import annotations

import csv
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hydra.adapters import datasets as ds
from hydra.adapters.datasets._common import Row, select
from hydra.corpus import corpus_of, matches


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, row_group_size=3)


@pytest.fixture
def ext_root(tmp_path: Path, make_png) -> Path:
    root = tmp_path / "external-big"
    pp_rows, img_rows = [], []
    brands = ["allegro"] * 4 + ["inpost"] * 3 + ["olx"] + ["other"] * 2
    for i, b in enumerate(brands):
        sha = f"{i:02d}" + "a" * 62
        pp_rows.append({"sha256": sha, "url": f"https://{b}-{i}.example/login",
                        "html": f"<html><title>{b}</title></html>", "label": "phish", "target": b,
                        "date": "2025-05-01", "lang": "pl" if i % 2 else "en", "lang_score": 0.9})
        img_rows.append({"image": {"bytes": make_png((10 * i, 0, 0)), "path": None}, "prompt": "", "completion": "",
                         "label": "phish", "target_brand": b, "sha256": sha, "url": f"https://{b}-{i}.example/login"})
    for i in range(3):
        sha = f"b{i}" + "c" * 62
        pp_rows.append({"sha256": sha, "url": f"https://ok{i}.example/", "html": "<html></html>", "label": "benign",
                        "target": None, "date": "2025-05-02", "lang": "en", "lang_score": 0.8})
        img_rows.append({"image": {"bytes": make_png((0, 9 * i, 0)), "path": None}, "prompt": "", "completion": "",
                         "label": "benign", "target_brand": None, "sha256": sha, "url": f"https://ok{i}.example/"})
    _write(root / "phreshphish" / "data" / "test-000.parquet", pp_rows)
    _write(root / "phreshphish-2k" / "image" / "train-00000-of-00001.parquet", img_rows)
    ads = [{"id": f"ad{i}", "page_id": str(1000 + i // 3), "page_name": "x", "text": f"zarabiaj {i}",
            "rule_score": "", "rule_hits": "", "label_suggested": "1", "image_filename": "a.png",
            "image": {"bytes": make_png((0, 0, 20 * i)), "path": None}, "label": 1 if i < 6 else 0} for i in range(8)]
    _write(root / "purino-scam-ads" / "data" / "train-00000-of-00001.parquet", ads)
    ps = root / "phish-screens"
    meta = ["file_name,label,url,final_url,brand,brand_normalized,page_type,is_blank,is_captcha,is_error_page"]
    for top, sub, page, lab, blank in [("phishing", "zoom", "homepage", 1, "False"), ("phishing", "zoom", "login", 1,
                                       "False"), ("legitimate", "acme", "homepage", 0, "False"),
                                      ("legitimate", "acme", "signup", 0, "True")]:
        f = ps / top / sub / f"{page}.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(make_png())
        meta.append(f"{top}/{sub}/{page}.png,{lab},https://{sub}.example/{page},,{sub},{sub},{page},{blank},False,False")
    (ps / "metadata.csv").write_text("\n".join(meta) + "\n")
    pu = root / "phishing-websites-putra"
    pu.mkdir()
    with (pu / "phishing.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["_id", "brands", "url", "language", "scan_date"])
        for i, b in enumerate(["allegro", "allegro", "dhl"]):
            w.writerow([f"site{i}", json.dumps([b]), f"https://x{i}.example/", "pl", "2023-03-09T10:00:00"])
    with zipfile.ZipFile(pu / "phishing_0001-0003.zip", "w") as zf:
        for i in range(3):
            zf.writestr(f"site{i}/original.html", "<html>kit</html>")
            zf.writestr(f"site{i}/screenshots/original_js_on.jpg", b"\xff\xd8\xff\xe0jpeg")
            zf.writestr(f"site{i}/assets/logo.png", make_png())
            zf.writestr(f"site{i}/asset_details.json", "{}")
    return root


def test_registry_lists_datasets_with_verdicts():
    names = {d.name: d for d in ds.list_datasets()}
    assert {"phreshphish", "phreshphish-2k", "purino-scam-ads", "phish-screens"} <= set(names)
    assert all(d.verdict in ("SHIP-SAFE", "EVAL-ONLY", "EXCLUDED") for d in names.values())
    assert names["phish-screens"].verdict == "EVAL-ONLY"


def test_select_keeps_whole_groups_caps_and_negatives():
    rows = [Row(f"a{i}", "a", "p") for i in range(5)] + [Row(f"b{i}", "b", "p") for i in range(2)] + \
        [Row("c0", "c", "p")] + [Row(f"n{i}", None, "benign") for i in range(4)]
    got = select(rows, min_group=2, per_group_cap=3, unlabelled=2)
    labels = [r.label for r in got]
    assert labels.count("a") == 3 and labels.count("b") == 2 and "c" not in labels and labels.count(None) == 2
    assert select(rows, min_group=2, per_group_cap=3, unlabelled=2) == got  # deterministic
    assert len(select(rows, limit=4, min_group=2)) == 4


def test_phreshphish_labels_and_filters(ext_root: Path):
    items = list(ds.iter_incidents("phreshphish", ext_root, min_group=2, unlabelled=1))
    labels = sorted(m["label"] for _, m in items if m["label"])
    assert labels == ["ext:phreshphish:allegro"] * 4 + ["ext:phreshphish:inpost"] * 3
    files, meta = items[0]
    assert set(files) == {"landing.html", "url.txt"} and meta["source"] == "ext:phreshphish"
    assert meta["group_label"] in ("ext:phreshphish:phish", "ext:phreshphish:benign")
    assert sum(1 for _, m in items if m["label"] is None) == 1
    pl = list(ds.iter_incidents("phreshphish", ext_root, lang="pl"))
    assert pl and all(m["lang"] == "pl" for _, m in pl)


def test_phreshphish_2k_joins_html_and_sets_screenshot_role(ext_root: Path):
    items = list(ds.iter_incidents("phreshphish-2k", ext_root, min_group=3))
    assert len(items) == 7
    for files, meta in items:
        assert {"screenshot.png", "url.txt", "landing.html"} <= set(files)
        assert meta["roles"] == {"screenshot.png": "landing_screenshot"} and meta["html_joined"]


def test_purino_groups_by_advertiser_page(ext_root: Path):
    items = list(ds.iter_incidents("purino-scam-ads", ext_root, scam_only=True, min_group=2))
    assert len(items) == 6
    assert {m["label"] for _, m in items} == {"ext:purino-scam-ads:page:1000", "ext:purino-scam-ads:page:1001"}
    files, _ = items[0]
    assert set(files) == {"ad_creative.png", "ad_text.txt"}


def test_phish_screens_skips_blank_and_labels_site(ext_root: Path):
    items = list(ds.iter_incidents("phish-screens", ext_root))
    labels = sorted(m["label"] for _, m in items)
    assert labels == ["ext:phish-screens:phish:zoom"] * 2 + ["ext:phish-screens:site:acme"]


def test_putra_site_folder_with_assets(ext_root: Path, tmp_path: Path, settings):
    items = list(ds.iter_ext("phishing-websites-putra", ext_root, min_group=2))
    assert [x.label for x in items] == ["ext:phishing-websites-putra:allegro"] * 2
    assert set(items[0].files) == {"landing.html", "screenshot.jpg", "url.txt", "assets/logo.png"}
    out = tmp_path / "pc"
    ds.materialise([("phishing-websites-putra", {})], ext_root, out)
    ds.import_corpus(out, settings)
    with closing(sqlite3.connect(settings.db_path)) as conn:
        roles = sorted(r[0] for r in conn.execute("SELECT DISTINCT role FROM incident_artifacts"))
    assert roles == ["landing_asset", "landing_html", "landing_screenshot", "url"]


def test_materialise_and_import_keep_corpus_flag(ext_root: Path, tmp_path: Path, settings):
    out = tmp_path / "corpus"
    mix = [("phreshphish-2k", {"min_group": 3, "unlabelled": 1}), ("purino-scam-ads", {"scam_only": True,
                                                                                       "min_group": 2})]
    doc = ds.materialise(mix, ext_root, out)
    assert doc["stats"]["incidents"] == 14
    again = ds.materialise(mix, ext_root, out)
    assert again["incidents"] == doc["incidents"]  # idempotent
    reps = ds.import_corpus(out, settings)
    assert len(reps) == 14
    with closing(sqlite3.connect(settings.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT id, source, label, meta FROM incidents").fetchall()
        roles = {r["role"] for r in conn.execute("SELECT role FROM incident_artifacts")}
    assert {r["source"] for r in rows} == {"ext:phreshphish-2k", "ext:purino-scam-ads"}
    assert all(not matches(corpus_of(r["source"]), "nask") for r in rows)  # never in the NASK-only run
    assert all(matches(corpus_of(r["source"]), "extended") for r in rows)
    metas = [json.loads(r["meta"]) for r in rows]
    assert all(m["group_label"].startswith("ext:") and m["ext"]["verdict"] for m in metas)
    assert {"landing_screenshot", "landing_html", "url", "ad_creative", "text"} <= roles
    assert sum(1 for r in rows if r["label"] is None) == 1
    ds.import_corpus(out, settings)  # idempotent re-import keeps labels and group labels
    with closing(sqlite3.connect(settings.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 14
        m = json.loads(conn.execute("SELECT meta FROM incidents LIMIT 1").fetchone()[0])
        assert m["group_label"]
