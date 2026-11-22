"""web-legit-tranco adapter on a synthetic fixture (no fetched page bytes in the repo)."""

from __future__ import annotations

import gzip
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from hydra.adapters import datasets as ds


def _row(domain: str, rank: int, tld: str, ok: bool = True, html: bytes | None = None) -> dict:
    body = html if html is not None else f"<html><body>{domain} polityka cookies RODO</body></html>".encode()
    return {"domain": domain, "rank": rank, "tld_group": tld, "url": f"https://{domain}/",
            "final_url": f"https://www.{domain}/" if ok else None, "status": 200 if ok else 403,
            "content_type": "text/html; charset=utf-8" if ok else None, "fetched_at": "2026-11-21T17:00:00Z",
            "ok": ok, "error": None if ok else "http_403", "bytes": len(body) if ok else 0, "truncated": False,
            "sha256": None, "html_gz": gzip.compress(body) if ok else None}


@pytest.fixture
def web_root(tmp_path: Path) -> Path:
    root = tmp_path / "external-big"
    rows = [_row("sklep-a.pl", 10, "pl"), _row("news-b.pl", 20, "pl"), _row("blocked.pl", 30, "pl", ok=False),
            _row("example.com", 1, "global"), _row("example.org", 2, "global")]
    p = root / "web-legit-tranco" / "pages" / "pages-000.parquet"
    p.parent.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows), p, row_group_size=2)
    return root


def test_registered_unlabelled_ship_safe():
    info = {d.name: d for d in ds.list_datasets()}["web-legit-tranco"]
    assert info.verdict == "SHIP-SAFE" and info.label_kind == "none" and info.modality == "html+url"


def test_ok_pages_only_with_html_and_final_url(web_root: Path):
    items = list(ds.iter_incidents("web-legit-tranco", web_root, unlabelled=100))
    assert sorted(m["domain"] for _, m in items) == ["example.com", "example.org", "news-b.pl", "sklep-a.pl"]
    files, meta = next((f, m) for f, m in items if m["domain"] == "sklep-a.pl")
    assert set(files) == {"landing.html", "url.txt"}
    assert files["url.txt"] == b"https://www.sklep-a.pl/\n" and b"RODO" in files["landing.html"]
    assert meta["label"] is None and meta["group_label"] == "ext:web-legit-tranco:benign"
    assert meta["source"] == "ext:web-legit-tranco" and meta["tld"] == "pl"


def test_tld_filter(web_root: Path):
    pl = list(ds.iter_incidents("web-legit-tranco", web_root, unlabelled=100, tld="pl"))
    assert sorted(m["domain"] for _, m in pl) == ["news-b.pl", "sklep-a.pl"]
    glob = list(ds.iter_incidents("web-legit-tranco", web_root, unlabelled=100, tld="global"))
    assert {m["tld"] for _, m in glob} == {"global"} and len(glob) == 2
    with pytest.raises(ValueError):
        list(ds.iter_incidents("web-legit-tranco", web_root, unlabelled=1, tld="de"))


def test_materialise_folder(web_root: Path, tmp_path: Path):
    doc = ds.materialise([("web-legit-tranco", {"unlabelled": 3, "tld": "global"})], web_root, tmp_path / "c")
    st = doc["stats"]["datasets"]["web-legit-tranco"]
    assert st["incidents"] == 2 and st["labelled"] == 0 and st["label_kind"] == "none"
    assert (tmp_path / "c" / "web-legit-tranco__example.com" / "landing.html").exists()
