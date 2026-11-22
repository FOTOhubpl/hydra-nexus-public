"""Phishing Website Dataset (Zenodo 10.5281/zenodo.8041387, CC BY 4.0): 5,151 phishing sites, 86 target brands.

Layout: `phishing.csv` (`_id, brands (JSON list), url, domain, language, scan_date, remote_ip_*, whois_*,
security_*, features.*`), `brands.csv`, and `phishing_NNNN-MMMM.zip` holding one folder per site:
`<_id>/{original.html, index.html, clean.html, asset_details.json, assets/*, screenshots/*_js_{on,off}.jpg}`.
One site → incident folder with `landing.html` (= `original.html`, the page as served), `screenshot.jpg`
(`original_js_on.jpg`, role landing_screenshot), `url.txt` and `assets/<file>` (role landing_asset via the
`assets` page-unit folder: asset sha256 / favicon genes can link pages built from one kit).
Labels: `ext:phishing-websites-putra:<brand>` (first listed brand; `genericemail` = generic webmail kits).
The legitimate half (`not-phishing_*.zip`) has its own adapter, `putra_legit` (slimmed copies).
Zip members are read, never extracted to disk outside the incident folder, and paths are sanitised
(no absolute paths, no `..`).
"""

from __future__ import annotations

import csv
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from hydra.adapters.datasets._common import (
    HTML_FILE,
    SCREENSHOT_STEM,
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
)

INFO = DatasetInfo(
    name="phishing-websites-putra", title="Phishing Website Dataset (screenshot, HTML, assets, WHOIS; 86 brands)",
    homepage="https://zenodo.org/records/8041387", licence="CC BY 4.0 (Zenodo record)", verdict="SHIP-SAFE",
    modality="screenshot+html+assets+url", label_kind="brand_target", dir="phishing-websites-putra",
    citation="Phishing Website Dataset, Zenodo 10.5281/zenodo.8041387 (2023)",
)
MAX_ASSETS = 80
MAX_ASSET_BYTES = 5 * 1024 * 1024
SHOT_ORDER = ("screenshots/original_js_on.jpg", "screenshots/index_js_on.jpg", "screenshots/original_js_off.jpg")


def _rows(root: Path) -> Iterator[dict[str, str]]:
    csv.field_size_limit(1 << 30)
    p = root / "phishing.csv"
    if not p.exists():
        return
    with p.open(newline="", encoding="utf-8", errors="replace") as fh:
        yield from csv.DictReader(fh)


def _brands(s: str | None) -> list[str]:
    try:
        v = json.loads(s or "[]")
    except ValueError:
        return []
    return [b for b in (norm_label(x) for x in (v if isinstance(v, list) else [v])) if b]


def index(root: Path) -> list[Row]:
    out = []
    for r in _rows(root):
        b = _brands(r.get("brands"))
        out.append(Row(r["_id"], b[0] if b else None, "phish", {"lang": norm_label(r.get("language")),
                                                                "brands": b, "date": (r.get("scan_date") or "")[:10]}))
    return out


def where(lang: str | None = None):
    return (lambda r: r.extra.get("lang") == lang) if lang else None


def _safe(rel: str) -> str | None:
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return None
    return p.as_posix()


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    by_key = {r.key: r for r in rows}
    urls = {r["_id"]: (r.get("url") or "").strip() for r in _rows(root) if r["_id"] in by_key}
    for z in sorted(root.glob("phishing_*.zip")):
        with zipfile.ZipFile(z) as zf:
            members: dict[str, list[zipfile.ZipInfo]] = {}
            for i in zf.infolist():
                top = i.filename.split("/", 1)[0]
                if top in by_key and not i.is_dir():
                    members.setdefault(top, []).append(i)
            for key in sorted(members):
                r = by_key[key]
                names = {i.filename.split("/", 1)[1]: i for i in members[key]}
                files: dict[str, bytes | Path] = {}
                roles: dict[str, str] = {}
                page = names.get("original.html") or names.get("index.html")
                if page is not None:
                    files[HTML_FILE] = zf.read(page)
                shot = next((names[s] for s in SHOT_ORDER if s in names), None)
                if shot is not None:
                    files[SCREENSHOT_STEM + ".jpg"] = zf.read(shot)
                    roles[SCREENSHOT_STEM + ".jpg"] = "landing_screenshot"
                if urls.get(key, "").startswith(("http://", "https://")):
                    files[URL_FILE] = (urls[key] + "\n").encode()
                assets = sorted(n for n in names if n.startswith("assets/") and names[n].file_size <= MAX_ASSET_BYTES)
                for n in assets[:MAX_ASSETS]:
                    rel = _safe(n)
                    if rel:
                        files[rel] = zf.read(names[n])
                yield ExtIncident(
                    dataset=INFO.name, key=key, files=files, roles=roles, label=prefixed(INFO.name, r.label),
                    group_label=prefixed(INFO.name, r.group),
                    meta={"site_id": key, "url": urls.get(key) or None, "brands": r.extra.get("brands"),
                          "lang": r.extra.get("lang"), "date": r.extra.get("date"), "label_kind": INFO.label_kind},
                )
