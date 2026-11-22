"""Legit homepages of Tranco-listed domains (TRAIN stream background corpus, docs/datasets/EXTERNAL.md).

Fetched once on the server by `scripts/fetch_web_legit.py` (operator instruction 2026-11-21: "Tranco-listed sites'
public landing HTML fetched once on the server for statistics only"). Used to compute a background document-frequency
prior of gene values (hashed counts only), so web-common values (CDNs, tracker loaders, CMS templates, cookie / RODO
banners, shop and news footers) do not look rare on a small local corpus. Page bytes are never redistributed.

Layout: `pages/pages-NNN.parquet`, one row per attempted domain, columns `domain, rank, tld_group ('pl'|'global'),
url, final_url, status, content_type, fetched_at, ok, error, bytes, truncated, sha256, html_gz` (gzip of the raw
body, capped at 2 MB). Only `ok` rows (HTTP 200, text/html, non-empty body) are indexed.
One row = one homepage → incident with `landing.html` (raw bytes) + `url.txt` (final URL after redirects).
No labels: every page is unlabelled, `group_label = ext:web-legit-tranco:benign`; select with `unlabelled=N`.
Selection option: `tld` ("pl" | "global").
"""

from __future__ import annotations

import gzip
import zlib
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    HTML_FILE,
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    cached_index,
    iter_parquet,
    iter_parquet_keys,
    parquet_files,
)

INFO = DatasetInfo(
    name="web-legit-tranco", title="Legit homepages of Tranco-listed domains (.pl + global top)",
    homepage="https://tranco-list.eu",
    licence="Tranco list: no own licence stated, citation requested (upstream Majestic CC BY 3.0, CrUX CC BY-SA 4.0, "
            "Cloudflare Radar CC BY-NC 4.0); pages: third-party content",
    verdict="SHIP-SAFE", modality="html+url", label_kind="none", dir="web-legit-tranco",
    citation="Le Pochat et al., Tranco, NDSS 2019 (list ID in SOURCE.md)",
)
GROUP = "benign"
MAX_HTML = 2 * 1024 * 1024 + 65536  # fetch cap plus slack; guards the decompression


def _files(root: Path) -> list[Path]:
    return parquet_files(root, "pages/pages-*.parquet")


def index(root: Path) -> list[Row]:
    def build():
        for r in iter_parquet(_files(root), ["domain", "rank", "tld_group", "ok", "final_url"], batch_size=2048):
            if r.get("ok"):
                yield Row(r["domain"], None, GROUP, {"tld": r.get("tld_group"), "rank": r.get("rank")})
    return cached_index(root, "web-legit-tranco", build)


def where(tld: str | None = None):
    if tld not in (None, "pl", "global"):
        raise ValueError(f"tld must be 'pl' or 'global', got {tld!r}")

    def ok(r: Row) -> bool:
        return tld is None or r.extra.get("tld") == tld
    return ok


def _gunzip(data: bytes) -> bytes:
    return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, MAX_HTML)


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    by_key = {r.key: r for r in rows}
    for rec in iter_parquet_keys(_files(root), "domain", set(by_key),
                                 ["url", "final_url", "html_gz", "tld_group", "rank", "status", "fetched_at",
                                  "sha256", "truncated"]):
        files: dict[str, bytes | Path] = {}
        try:
            html = _gunzip(rec["html_gz"]) if rec.get("html_gz") else b""
        except (zlib.error, gzip.BadGzipFile):
            html = b""
        if html:
            files[HTML_FILE] = html
        url = rec.get("final_url") or rec.get("url")
        if url:
            files[URL_FILE] = (url.strip() + "\n").encode()
        yield ExtIncident(
            dataset=INFO.name, key=rec["domain"], files=files, label=None,
            group_label=f"ext:{INFO.name}:{GROUP}",
            meta={"domain": rec["domain"], "url": rec.get("url"), "final_url": rec.get("final_url"),
                  "tld": rec.get("tld_group"), "rank": rec.get("rank"), "status": rec.get("status"),
                  "fetched_at": rec.get("fetched_at"), "sha256": rec.get("sha256"),
                  "truncated": bool(rec.get("truncated")), "label_kind": INFO.label_kind},
        )
