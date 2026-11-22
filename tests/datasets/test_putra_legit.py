"""putra_legit adapter on a synthetic fixture (no real dataset bytes)."""

from __future__ import annotations

import csv
import zipfile
from pathlib import Path

from hydra.adapters.datasets import putra_legit as pl
from hydra.adapters.datasets._common import select


def _root(tmp_path: Path, make_png) -> Path:
    root = tmp_path / "phishing-websites-putra-legit"
    root.mkdir()
    sites = [("s0", "https://www.paypal.com/pl/home", "www.paypal.com", "pl"),
             ("s1", "https://www.paypal.com/de/home", "www.paypal.com", "de"),
             ("s2", "https://consent.yahoo.com/v2/collectConsent", "consent.yahoo.com", "en"),
             ("s3", "https://news.example.co.uk/", "news.example.co.uk", "en")]
    with (root / "not-phishing.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["_id", "domain", "language", "scan_date", "url"])
        for sid, url, dom, lang in sites:
            w.writerow([sid, dom, lang, "2023-04-06T12:00:00", url])
    with zipfile.ZipFile(root / "not-phishing_0001-0003.slim.zip", "w") as zf:
        for sid, *_ in (sites[0], sites[2]):
            zf.writestr(f"{sid}/original.html", f"<html>{sid}</html>")
            zf.writestr(f"{sid}/screenshots/original_js_on.jpg", b"\xff\xd8\xff\xe0jpeg")
            zf.writestr(f"{sid}/assets/favicon-0.png", make_png())
            zf.writestr(f"{sid}/assets/../../evil.txt", "x")
            zf.writestr(f"{sid}/asset_details.json", "{}")
    with zipfile.ZipFile(root / "not-phishing_0001-0004.zip", "w") as zf:  # original zip: s0 again + s3
        zf.writestr("s0/original.html", "<html>dup</html>")
        zf.writestr("s3/index.html", "<html>s3</html>")
        zf.writestr("s3/screenshots/index_js_on.jpg", b"\xff\xd8\xff\xe0jpeg")
    return root


def test_index_labels_by_registrable_domain(tmp_path: Path, make_png):
    rows = pl.index(_root(tmp_path, make_png))
    assert pl.site_of("login-x.000webhostapp.com") == "login-x.000webhostapp.com"  # platform tenant, not the platform
    assert pl.site_of("sites.google.com") is None and pl.site_of("storage.googleapis.com") is None  # path tenants
    assert pl.site_of("192.0.2.1") == "192.0.2.1"
    assert [(r.key, r.label, r.group) for r in rows] == [
        ("s0", "site:paypal.com", "legit"), ("s1", "site:paypal.com", "legit"),
        ("s2", "site:yahoo.com", "legit"), ("s3", "site:example.co.uk", "legit")]
    assert sorted(r.key for r in select(rows, min_group=2)) == ["s0", "s1"]
    assert [r.key for r in rows if pl.where(lang="pl")(r)] == ["s0"]


def test_load_reads_slim_first_and_sanitises_paths(tmp_path: Path, make_png):
    root = _root(tmp_path, make_png)
    items = {x.key: x for x in pl.load(root, pl.index(root))}
    assert sorted(items) == ["s0", "s2", "s3"]          # s1 has no zip folder
    s0 = items["s0"]
    assert set(s0.files) == {"landing.html", "screenshot.jpg", "url.txt", "assets/favicon-0.png"}
    assert s0.files["landing.html"] == b"<html>s0</html>"  # slim zip wins over the duplicate in the original zip
    assert s0.roles == {"screenshot.jpg": "landing_screenshot"}
    assert s0.label == "ext:phishing-websites-putra-legit:site:paypal.com"
    assert s0.group_label == "ext:phishing-websites-putra-legit:legit"
    assert s0.meta["lang"] == "pl" and s0.meta["url"] == "https://www.paypal.com/pl/home"
    assert s0.source == "ext:phishing-websites-putra-legit"
    assert items["s3"].files["landing.html"] == b"<html>s3</html>" and "screenshot.jpg" in items["s3"].files
