"""Phishing Website Dataset, legitimate half (Zenodo 10.5281/zenodo.8041387, CC BY 4.0): 5,244 legitimate sites.

The direct counterpart of `putra_phishing` (same record, same crawler, same folder layout and features), so a
phish-vs-legit comparison is not confounded by capture differences. Layout: `not-phishing.csv` (`_id, url, domain,
language, scan_date, remote_ip_*, whois_*, security_*, features.*`) and `not-phishing_NNNN-MMMM[.slim].zip` holding one
folder per site: `<_id>/{original.html, asset_details.json, assets/*, screenshots/*.jpg}`.
On the server the zips are stored slim (`*.slim.zip`): the originals were fetched and md5/sha256-verified, then only the
members this adapter reads were kept (original.html or index.html, the first screenshot of `SHOT_ORDER`,
asset_details.json, the first `MAX_ASSETS` assets of <= `MAX_ASSET_BYTES` in name order); the original zips work too.
One site → incident folder with `landing.html`, `screenshot.jpg` (role landing_screenshot), `url.txt`, `assets/<file>`.
Labels: `ext:phishing-websites-putra-legit:site:<registrable domain>` (source site: e.g. 42 `consent.yahoo.com`
interstitials, 15 www.paypal.com pages: hard positives and a brand trap against the paypal phishing kits);
`group_label = ext:phishing-websites-putra-legit:legit`. Selection option: `lang` (e.g. "pl").
Zip members are read, never extracted outside the incident folder; paths are sanitised.
"""

from __future__ import annotations

import csv
import zipfile
from collections.abc import Iterator
from pathlib import Path

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
from hydra.adapters.datasets.putra_phishing import MAX_ASSET_BYTES, MAX_ASSETS, SHOT_ORDER, _safe

INFO = DatasetInfo(
    name="phishing-websites-putra-legit",
    title="Phishing Website Dataset, legitimate half (screenshot, HTML, assets, WHOIS; 5,244 sites)",
    homepage="https://zenodo.org/records/8041387", licence="CC BY 4.0 (Zenodo record)", verdict="SHIP-SAFE",
    modality="screenshot+html+assets+url", label_kind="source_site", dir="phishing-websites-putra-legit",
    citation="Putra, I K. A. A., Phishing Website Dataset, Zenodo 10.5281/zenodo.8041387 (2023)",
)
CLASS = "legit"


def _rows(root: Path) -> Iterator[dict[str, str]]:
    csv.field_size_limit(1 << 30)
    p = root / "not-phishing.csv"
    if not p.exists():
        return
    with p.open(newline="", encoding="utf-8", errors="replace") as fh:
        yield from csv.DictReader(fh)


# shared hosts whose tenants live in the URL path, not the host name: their pages share no owner
PATH_TENANT_HOSTS = ("storage.googleapis.com", "firebasestorage.googleapis.com", "s3.amazonaws.com", "ipfs.io",
                     "dweb.link", "cloudflare-ipfs.com", "gateway.pinata.cloud", "web.archive.org")


def site_of(host: str | None) -> str | None:
    """Source-site label of a host: the registrable domain (offline PSL incl. private suffixes); on a multi-tenant
    platform (`hydra.enrich.domains.is_platform`: 000webhostapp.com, blogspot.com, ...) the tenant host without
    `www.`, so unrelated customers of one platform never share a label; the IP for IP hosts. None when the host is
    the platform itself (sites.google.com, storage.googleapis.com, ...): tenants there are told apart by path only."""
    from hydra.enrich.domains import PLATFORM_SUFFIXES, is_ip, is_platform, registrable_domain

    h = norm_label((host or "").split(":", 1)[0].strip("."))
    if not h:
        return None
    if is_ip(h):
        return h
    bare = h.removeprefix("www.")
    if bare in PATH_TENANT_HOSTS:
        return None
    try:
        if is_platform(h):
            return None if bare in PLATFORM_SUFFIXES else bare
        return registrable_domain(h) or None
    except ValueError:
        return None


def index(root: Path) -> list[Row]:
    out = []
    for r in _rows(root):
        if not r.get("_id"):
            continue
        host = norm_label(r.get("domain"))
        site = site_of(host)
        out.append(Row(r["_id"], f"site:{site}" if site else None, CLASS,
                       {"lang": norm_label(r.get("language")), "host": host, "date": (r.get("scan_date") or "")[:10]}))
    return out


def where(lang: str | None = None):
    return (lambda r: r.extra.get("lang") == lang) if lang else None


def _zips(root: Path) -> list[Path]:
    """Slim zips first, so a folder holding both a slim and an original zip reads each site once."""
    return sorted(root.glob("not-phishing_*.zip"), key=lambda p: (not p.name.endswith(".slim.zip"), p.name))


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    by_key = {r.key: r for r in rows}
    urls = {r["_id"]: (r.get("url") or "").strip() for r in _rows(root) if r.get("_id") in by_key}
    done: set[str] = set()
    for z in _zips(root):
        with zipfile.ZipFile(z) as zf:
            members: dict[str, list[zipfile.ZipInfo]] = {}
            for i in zf.infolist():
                top = i.filename.split("/", 1)[0]
                if top in by_key and top not in done and not i.is_dir() and "/" in i.filename:
                    members.setdefault(top, []).append(i)
            for key in sorted(members):
                done.add(key)
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
                if not files:
                    continue
                yield ExtIncident(
                    dataset=INFO.name, key=key, files=files, roles=roles, label=prefixed(INFO.name, r.label),
                    group_label=prefixed(INFO.name, r.group),
                    meta={"site_id": key, "url": urls.get(key) or None, "class": CLASS, "host": r.extra.get("host"),
                          "lang": r.extra.get("lang"), "date": r.extra.get("date"), "label_kind": INFO.label_kind},
                )
