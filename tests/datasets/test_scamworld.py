"""ScamWorld adapter on a synthetic fixture (no real sites)."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from hydra.adapters.datasets import _common, scamworld

WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 fake"


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "scamworld"
    rows = []
    for task, lang, legit, variant in [("invest-95", "pl", False, None), ("shop-64", "pl", False, None),
                                       ("shop-62", "en", False, None), ("shop-62.budget", "en", False, "shop-62"),
                                       ("ok-account-01", "en", True, None)]:
        rows.append({"start_page": {"bytes": WEBP, "path": None}, "task": task, "scenario": "Online shopping",
                     "language": lang, "legitimate_site": legit, "fraud_families": ["Non-delivery fraud"],
                     "variant_of": variant, "request": "Kup mi to", "site_pages": 1, "site_files": 3})
    (root / "data").mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), root / "data" / "tasks.parquet", row_group_size=2)
    site = root / "tasks" / "invest-95" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<html><title>Inwestuj z ORLEN</title></html>")
    (site / "regulamin.html").write_text("<html>regulamin</html>")
    src = root / "tasks" / "invest-95" / "source"
    src.mkdir()
    (src / "provenance.json").write_text(json.dumps({"case_id": "invest-95",
                                                     "source": {"url": "https://scam.example/send.php?k=1"}}))
    (root / "tasks" / "shop-64" / "source").mkdir(parents=True)
    (root / "tasks" / "shop-64" / "source" / "provenance.json").write_text('{"source": {"url": "javascript:x"}}')
    return root


def test_index_labels_variants_and_filters(tmp_path: Path) -> None:
    rows = scamworld.index(_root(tmp_path))
    by = {r.key: r for r in rows}
    assert by["shop-62.budget"].label == by["shop-62"].label == "site:shop-62"
    assert by["ok-account-01"].group == "legit" and by["invest-95"].group == "scam"
    assert by["invest-95"].extra["lang"] == "pl" and by["invest-95"].extra["scenario"] == "online shopping"
    assert sorted(r.key for r in rows if scamworld.where(lang="pl")(r)) == ["invest-95", "shop-64"]
    assert [r.label for r in _common.select(rows, min_group=2)] == ["site:shop-62", "site:shop-62"]


def test_load_screenshot_html_url(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rows = [r for r in scamworld.index(root) if r.extra["lang"] == "pl"]
    incs = {i.key: i for i in scamworld.load(root, rows)}
    inv = incs["invest-95"]
    assert set(inv.files) == {"screenshot.webp", "landing.html", "url.txt"}
    assert inv.roles == {"screenshot.webp": "landing_screenshot"}
    assert inv.files["landing.html"] == root / "tasks" / "invest-95" / "site" / "index.html"
    assert inv.files["url.txt"] == b"https://scam.example/send.php?k=1\n"
    assert inv.meta["site_pages"] == ["index.html", "regulamin.html"] and inv.meta["lang"] == "pl"
    assert inv.source == "ext:scamworld" and inv.group_label == "ext:scamworld:scam"
    assert inv.label == "ext:scamworld:site:invest-95"
    shop = incs["shop-64"]
    assert set(shop.files) == {"screenshot.webp"} and shop.meta["source_url"] is None  # non-http URL dropped
