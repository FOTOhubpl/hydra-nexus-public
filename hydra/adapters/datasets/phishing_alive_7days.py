"""7 Days Later: phishing-site lifespan crawl, sample release (Zenodo 10.5281/zenodo.14885725, CC BY 4.0).

Lim, Lee, Kim, Kwon (2025) re-crawled live phishing URLs every 30 minutes (Puppeteer/Chromium) and stored per crawl
`<id>-<url slug>/<YYYY-MM-DD-HH:MM>/{page.html, screenshot.jpg, status_code.txt, headers.json, ip_address.json,
cookies.json, http_version.txt, error.log}`. The 12.4 GB tarball is stored slim on the server
(`phishing-alive-sample.slim.zip`): the stream was md5-verified against Zenodo while only HTTP-200 crawls whose
page.html differs from every crawl already kept for that URL were written (at most 6 per URL), each with page.html,
screenshot.jpg, status_code.txt, headers.json and ip_address.json. Byte-identical re-crawls are dropped.
One kept crawl → incident folder with `landing.html` + `screenshot.jpg` (role landing_screenshot).
Labels: `ext:phishing-alive-7days:url:<id>-<slug>` = the same phishing URL seen at different times (the page
evolves: cloaking, kit swaps, takedown notices): the closest public analogue of "same campaign, later variant";
`group_label = ext:phishing-alive-7days:phish`. No brand labels and no exact URL in the release (the folder slug
replaces URL punctuation by `_`; it is kept in `meta.url_slug`, never written as `url.txt`).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from hydra.adapters.datasets._common import (
    HTML_FILE,
    SCREENSHOT_STEM,
    DatasetInfo,
    ExtIncident,
    Row,
    prefixed,
    unique_key,
)

INFO = DatasetInfo(
    name="phishing-alive-7days", title="7 Days Later: phishing-site lifespan crawl (sample; HTML + screenshots)",
    homepage="https://zenodo.org/records/14885725", licence="CC BY 4.0 (Zenodo record)", verdict="SHIP-SAFE",
    modality="screenshot+html", label_kind="same_url_over_time", dir="phishing-alive-7days",
    citation="Lim, K., Lee, K., Kim, H., Kwon, Y., 7 Days Later: Analyzing Phishing-Site Lifespan After Detected, "
             "Zenodo 10.5281/zenodo.14885725 (2025)",
)
INDEX_VERSION = 1


def _zips(root: Path) -> list[Path]:
    return sorted(root.glob("*.slim.zip")) or sorted(root.glob("*.zip"))


def _snap(name: str) -> tuple[str, str, str] | None:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or len(p.parts) != 3:
        return None
    return p.parts[0], p.parts[1], p.parts[2]


def _build(root: Path) -> list[Row]:
    out = []
    for z in _zips(root):
        with zipfile.ZipFile(z) as zf:
            snaps: dict[tuple[str, str], set[str]] = {}
            for n in zf.namelist():
                s = _snap(n)
                if s:
                    snaps.setdefault((s[0], s[1]), set()).add(s[2])
            for (url_dir, ts), names in sorted(snaps.items()):
                if "page.html" not in names:
                    continue
                url_id, _, slug = url_dir.partition("-")
                out.append(Row(f"{url_dir}/{ts}", f"url:{url_dir}", "phish",
                               {"zip": z.name, "url_id": url_id, "url_slug": slug, "ts": ts, "date": ts[:10],
                                "shot": "screenshot.jpg" in names}))
    return out


def index(root: Path) -> list[Row]:
    zs = _zips(root)
    sig = hashlib.sha256("|".join([f"v{INDEX_VERSION}", *(f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
                                                         for p in zs)]).encode()).hexdigest()
    cache = root / f".hydra-index-{INFO.name}.json.gz"
    if cache.exists():
        try:
            d = json.loads(gzip.decompress(cache.read_bytes()))
            if d.get("sig") == sig:
                return [Row(r["key"], r["label"], r["group"], r.get("extra") or {}) for r in d["rows"]]
        except (OSError, ValueError, KeyError):
            pass
    rows = _build(root)
    if rows:
        try:
            cache.write_bytes(gzip.compress(json.dumps({"sig": sig, "rows": [r.__dict__ for r in rows]}).encode()))
        except OSError:
            pass  # read-only data folder: no cache
    return rows


def where(with_screenshot: bool = False, date: str | None = None):
    if not with_screenshot and not date:
        return None
    return lambda r: (not with_screenshot or r.extra.get("shot")) and (date is None or r.extra.get("date") == date)


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    by_zip: dict[str, list[Row]] = {}
    for r in rows:
        by_zip.setdefault(r.extra.get("zip") or "", []).append(r)
    for zname in sorted(by_zip):
        z = root / zname
        if not zname or not z.is_file():
            continue
        with zipfile.ZipFile(z) as zf:
            have = set(zf.namelist())
            for r in sorted(by_zip[zname], key=lambda r: r.key):
                page = f"{r.key}/page.html"
                if page not in have:
                    continue
                files: dict[str, bytes | Path] = {HTML_FILE: zf.read(page)}
                roles: dict[str, str] = {}
                if f"{r.key}/screenshot.jpg" in have:
                    files[SCREENSHOT_STEM + ".jpg"] = zf.read(f"{r.key}/screenshot.jpg")
                    roles[SCREENSHOT_STEM + ".jpg"] = "landing_screenshot"
                yield ExtIncident(
                    dataset=INFO.name, key=unique_key(r.key.replace(":", "")), files=files, roles=roles,
                    label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group),
                    meta={"url_id": r.extra.get("url_id"), "url_slug": r.extra.get("url_slug"), "ts": r.extra.get("ts"),
                          "class": r.group, "label_kind": INFO.label_kind},
                )
