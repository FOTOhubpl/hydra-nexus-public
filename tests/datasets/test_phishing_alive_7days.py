"""phishing_alive_7days adapter on a synthetic fixture (no real dataset bytes)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from hydra.adapters.datasets import phishing_alive_7days as m


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "phishing-alive-7days"
    root.mkdir()
    with zipfile.ZipFile(root / "phishing-alive-sample.slim.zip", "w") as zf:
        for url_dir, ts, shot in [("1-login_example_com", "2024-04-21-12:48", True),
                                  ("1-login_example_com", "2024-04-22-08:00", False),
                                  ("2-paypal_secure_example_net", "2024-04-21-02:50", True)]:
            zf.writestr(f"{url_dir}/{ts}/page.html", f"<html>{url_dir} {ts}</html>")
            zf.writestr(f"{url_dir}/{ts}/status_code.txt", "200")
            if shot:
                zf.writestr(f"{url_dir}/{ts}/screenshot.jpg", b"\xff\xd8\xff\xe0jpeg")
        zf.writestr("3-no_page/2024-04-21-00:00/status_code.txt", "200")   # crawl without a page: skipped
        zf.writestr("../evil/2024-04-21-00:00/page.html", "x")               # unsafe path: skipped
    return root


def test_index_groups_crawls_of_one_url(tmp_path: Path):
    root = _root(tmp_path)
    rows = m.index(root)
    assert [(r.key, r.label, r.group) for r in rows] == [
        ("1-login_example_com/2024-04-21-12:48", "url:1-login_example_com", "phish"),
        ("1-login_example_com/2024-04-22-08:00", "url:1-login_example_com", "phish"),
        ("2-paypal_secure_example_net/2024-04-21-02:50", "url:2-paypal_secure_example_net", "phish")]
    assert rows[0].extra["date"] == "2024-04-21" and rows[0].extra["url_id"] == "1"
    assert [r.key for r in m.index(root)] == [r.key for r in rows]   # cached index
    assert len([r for r in rows if m.where(with_screenshot=True)(r)]) == 2


def test_load_html_screenshot_role(tmp_path: Path):
    root = _root(tmp_path)
    items = list(m.load(root, m.index(root)))
    assert len(items) == 3
    a = items[0]
    assert set(a.files) == {"landing.html", "screenshot.jpg"} and a.roles == {"screenshot.jpg": "landing_screenshot"}
    assert a.label == "ext:phishing-alive-7days:url:1-login_example_com"
    assert a.group_label == "ext:phishing-alive-7days:phish" and a.source == "ext:phishing-alive-7days"
    assert ":" not in a.key and "url.txt" not in a.files
    assert set(items[1].files) == {"landing.html"}
