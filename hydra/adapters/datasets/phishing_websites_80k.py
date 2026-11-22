"""Phishing Websites Dataset, 80k (Mendeley Data 10.17632/n96ncsr5g4.1, CC BY 4.0): 50,000 legitimate + 30,000
phishing pages, URL + raw HTML (Ariyadasa, Fernando, Fernando; University of Moratuwa, 2021).

Layout: `index.sql` (phpMyAdmin dump, table `index`: `rec_id, url, website, result, created_date`; `result` 0 = legit,
1 = phish) and `dataset_part_{1..8}.zip` holding `dataset-part-N/<website>` (one saved HTML page each; the zips hold
83,275 pages, the 80,000 indexed ones are used). Legitimate pages come from Google top-5 keyword results (at most 10
per domain) and the Ebbu2017 list; phishing pages from PhishTank, OpenPhish and PhishRepo (Dec 2020 - Oct 2021).
One page → incident folder with `landing.html` + `url.txt`. No screenshots, no language field.
Labels (no brand labels in the source): the registrable domain of the URL (`putra_legit.site_of`: tenant host on
multi-tenant platforms, so `x.000webhostapp.com` stays one site), `ext:phishing-websites-80k:site:<domain>` for
legit pages (several pages of one site: template sharing as a hard positive) and
`ext:phishing-websites-80k:phish:<domain>` for phishing pages (several lures on one phishing host; phishing on a
domain that also serves legit pages in the set, e.g. docs.google.com forms, is left unlabelled);
`group_label = ext:phishing-websites-80k:<legit|phish>`.
Selection options: `cls` ("legit"|"phish"), `tld` (e.g. "pl"). The index (SQL parse + zip listing) is cached as
`<root>/.hydra-index-phishing-websites-80k.json.gz`. HTML is copied as bytes, never parsed or rendered.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import zipfile
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    HTML_FILE,
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
)
from hydra.adapters.datasets.putra_legit import site_of

INFO = DatasetInfo(
    name="phishing-websites-80k", title="Phishing Websites Dataset (80k: 50k legit + 30k phish, URL + HTML)",
    homepage="https://data.mendeley.com/datasets/n96ncsr5g4/1", licence="CC BY 4.0 (Mendeley Data record)",
    verdict="SHIP-SAFE", modality="html+url", label_kind="source_site", dir="phishing-websites-80k",
    citation="Ariyadasa, S., Fernando, S., Fernando, S., Phishing Websites Dataset, Mendeley Data V1, "
             "doi:10.17632/n96ncsr5g4.1 (2021)",
)
CLASSES = {"0": "legit", "1": "phish"}
TUPLE_RE = re.compile(r"\((\d+), '((?:[^'\\]|\\.|'')*)', '((?:[^'\\]|\\.)*)', (\d+), '([^']*)'\)")
ESC_RE = re.compile(r"\\(.)")
HOST_RE = re.compile(r"^[a-z][a-z0-9+.-]*://([^/?#]*)", re.I)
INDEX_VERSION = 2  # bump when labelling changes: part of the cache signature


def _unescape(s: str) -> str:
    return ESC_RE.sub(r"\1", s).replace("''", "'")


def host_of(url: str) -> str | None:
    m = HOST_RE.match(url.strip())
    if not m:
        return None
    h = m.group(1).rsplit("@", 1)[-1]
    h = h[1:].split("]", 1)[0] if h.startswith("[") else h.split(":", 1)[0]
    return norm_label(h.strip("."))


def _sources(root: Path) -> list[Path]:
    return [p for p in [root / "index.sql", *sorted(root.glob("dataset_part_*.zip"))] if p.exists()]


def _build(root: Path) -> list[Row]:
    sql = root / "index.sql"
    if not sql.exists():
        return []
    members: dict[str, tuple[str, str]] = {}
    for z in sorted(root.glob("dataset_part_*.zip")):
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if not n.endswith("/"):
                    members.setdefault(n.rsplit("/", 1)[-1], (z.name, n))
    parsed = []
    for rec_id, url, website, result, created in TUPLE_RE.findall(sql.read_text(encoding="utf-8", errors="replace")):
        website = _unescape(website)
        cls = CLASSES.get(result)
        if cls is None or website not in members:
            continue
        url = _unescape(url).strip()
        host = host_of(url)
        parsed.append((int(rec_id), url, website, cls, created, host, site_of(host)))
    # a phishing page on a site that also serves legit pages here (docs.google.com forms, sharepoint.com, twitter.com,
    # ...) is an abused service, not an operator's site: left unlabelled (open-set) instead of one giant false group
    legit_sites = {s for *_, cls, _, _, s in parsed if cls == "legit" and s}
    out = []
    for rec_id, url, website, cls, created, host, site in parsed:
        if cls == "phish" and site in legit_sites:
            site = None
        tld = host.rsplit(".", 1)[-1] if host and "." in host else None
        label = f"{'site' if cls == 'legit' else 'phish'}:{site}" if site else None
        out.append(Row(website.removesuffix(".html"), label, cls,
                       {"rec_id": rec_id, "url": url, "host": host, "tld": tld, "date": created[:10],
                        "zip": members[website][0], "member": members[website][1]}))
    return out


def index(root: Path) -> list[Row]:
    srcs = _sources(root)
    sig = hashlib.sha256("|".join([f"v{INDEX_VERSION}", *(f"{p.name}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
                                                         for p in srcs)]).encode()).hexdigest()
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


def where(cls: str | None = None, tld: str | None = None):
    if not cls and not tld:
        return None
    return lambda r: (cls is None or r.group == cls) and (tld is None or r.extra.get("tld") == tld)


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    by_zip: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        if r.extra.get("zip"):
            by_zip[r.extra["zip"]].append(r)
    for zname in sorted(by_zip):
        z = root / zname
        if not z.is_file():
            continue
        with zipfile.ZipFile(z) as zf:
            have = set(zf.namelist())
            for r in sorted(by_zip[zname], key=lambda r: r.key):
                if r.extra["member"] not in have:
                    continue
                url = r.extra.get("url") or ""
                files: dict[str, bytes | Path] = {HTML_FILE: zf.read(r.extra["member"])}
                if url.startswith(("http://", "https://")):
                    files[URL_FILE] = (url + "\n").encode()
                yield ExtIncident(
                    dataset=INFO.name, key=r.key, files=files, label=prefixed(INFO.name, r.label),
                    group_label=prefixed(INFO.name, r.group),
                    meta={"rec_id": r.extra.get("rec_id"), "url": url or None, "class": r.group,
                          "host": r.extra.get("host"), "date": r.extra.get("date"), "label_kind": INFO.label_kind},
                )
