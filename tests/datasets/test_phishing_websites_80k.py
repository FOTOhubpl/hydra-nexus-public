"""phishing_websites_80k adapter on a synthetic fixture (no real dataset bytes)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from hydra.adapters.datasets import phishing_websites_80k as m

SQL_HEAD = """-- phpMyAdmin SQL Dump
CREATE TABLE `index` (
  `rec_id` int NOT NULL,
  `url` text NOT NULL,
  `website` varchar(50) NOT NULL,
  `result` int NOT NULL,
  `created_date` datetime NOT NULL
);
INSERT INTO `index` (`rec_id`, `url`, `website`, `result`, `created_date`) VALUES
"""
ROWS = [
    (1, "http://login-bank.000webhostapp.com/a/index.php", "101.html", 1, "2021-02-17 20:29:32"),
    (2, "http://login-bank.000webhostapp.com/b/", "102.html", 1, "2021-02-18 10:00:00"),
    (3, "https://www.mathopenref.com/segment.html", "103.html", 0, "2021-10-31 16:35:38"),
    (4, "https://mathopenref.com/how-do-i\\'m-here", "104.html", 0, "2021-10-31 16:36:00"),
    (5, "https://allegro.pl/oferta/1", "105.html", 0, "2021-10-31 16:37:00"),
    (6, "https://missing.example/", "106.html", 0, "2021-10-31 16:38:00"),
    (7, "https://docs.google.com/forms/d/e/x/viewform", "107.html", 1, "2021-02-18 11:00:00"),
    (8, "https://www.google.com/search?q=a", "108.html", 0, "2021-10-31 16:39:00"),
    (9, "http://203.0.113.7/paypal/", "109.html", 1, "2021-02-18 12:00:00"),
]


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "phishing-websites-80k"
    root.mkdir()
    vals = ",\n".join(f"({i}, '{u}', '{w}', {r}, '{d}')" for i, u, w, r, d in ROWS)
    (root / "index.sql").write_text(SQL_HEAD + vals + ";\nCOMMIT;\n")
    with zipfile.ZipFile(root / "dataset_part_1.zip", "w") as zf:
        zf.writestr("dataset-part-1/", "")
        for _, _, w, _, _ in ROWS[:3]:
            zf.writestr(f"dataset-part-1/{w}", f"<html>{w}</html>")
    with zipfile.ZipFile(root / "dataset_part_2.zip", "w") as zf:
        for _, _, w, _, _ in ROWS[3:5] + ROWS[6:]:
            zf.writestr(f"dataset-part-2/{w}", f"<html>{w}</html>")
        zf.writestr("dataset-part-2/999.html", "<html>unindexed</html>")
    return root


def test_index_parses_sql_and_groups_by_site(tmp_path: Path):
    root = _root(tmp_path)
    rows = {r.key: r for r in m.index(root)}
    assert sorted(rows) == ["101", "102", "103", "104", "105", "107", "108", "109"]  # 106: no page; 999: unindexed
    assert rows["101"].label == rows["102"].label == "phish:login-bank.000webhostapp.com"  # tenant, not platform
    assert rows["101"].group == "phish"
    assert rows["103"].label == rows["104"].label == "site:mathopenref.com"
    assert rows["104"].extra["url"] == "https://mathopenref.com/how-do-i'm-here"
    assert rows["107"].label is None and rows["107"].group == "phish"   # abused legit service: unlabelled
    assert rows["108"].label == "site:google.com" and rows["109"].label == "phish:203.0.113.7"
    assert rows["105"].group == "legit" and rows["105"].extra["tld"] == "pl"
    assert (root / ".hydra-index-phishing-websites-80k.json.gz").exists()
    assert [r.key for r in m.index(root)] == [r.key for r in rows.values()]   # cached index, same rows
    assert [r.key for r in rows.values() if m.where(tld="pl")(r)] == ["105"]
    assert sorted(r.key for r in rows.values() if m.where(cls="phish")(r)) == ["101", "102", "107", "109"]


def test_load_yields_html_and_url(tmp_path: Path):
    root = _root(tmp_path)
    items = {x.key: x for x in m.load(root, m.index(root))}
    assert sorted(items) == ["101", "102", "103", "104", "105", "107", "108", "109"]
    x = items["104"]
    assert x.files == {"landing.html": b"<html>104.html</html>", "url.txt": b"https://mathopenref.com/how-do-i'm-here\n"}
    assert x.label == "ext:phishing-websites-80k:site:mathopenref.com"
    assert x.group_label == "ext:phishing-websites-80k:legit"
    assert items["101"].group_label == "ext:phishing-websites-80k:phish"
    assert x.source == "ext:phishing-websites-80k" and x.meta["class"] == "legit"
