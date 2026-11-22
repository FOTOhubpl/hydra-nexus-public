"""PhishTank online-valid feed (data.phishtank.com/data/online-valid.csv, Cisco Talos). Free download; the FAQ
confirms commercial and non-commercial use of the data is allowed; attribution to PhishTank required. One
verified-online phishing URL = one incident: `url.txt`. The `target` column (impersonated brand) is a weak
grouping label (`label = ext:phishtank:<brand>`, brand_target); `group_label = ext:phishtank:phish`.
`meta.lang = "pl"` when the URL host is under `.pl` (Polish-targeted share; Allegro/InPost are the top targets).
URL strings only; pages are never fetched. Rows (URLs) are shown only as aggregates with attribution.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

from hydra.adapters.datasets._common import (
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
)

INFO = DatasetInfo(
    name="phishtank", title="PhishTank online-valid phishing URLs (brand target)",
    homepage="https://phishtank.org/developer_info.php",
    licence="PhishTank/Cisco Talos: free use incl. commercial, attribution to PhishTank required",
    verdict="SHIP-SAFE", modality="url", label_kind="brand_target", dir="phishtank",
    citation="PhishTank (Cisco Talos), online-valid.csv",
)


def _file(root: Path) -> Path | None:
    return next(iter(sorted(root.glob("online-valid*.csv"), reverse=True)), None)


def _tld_pl(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host.endswith(".pl")


def _rows(root: Path) -> Iterator[dict]:
    f = _file(root)
    if f is None:
        return
    csv.field_size_limit(1 << 30)
    with f.open(newline="", encoding="utf-8", errors="replace") as fh:
        yield from csv.DictReader(fh)


def index(root: Path) -> list[Row]:
    out = []
    for r in _rows(root):
        url = (r.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        brand = norm_label(r.get("target"))
        if brand in ("other", None):
            brand = None
        out.append(Row(str(r.get("phish_id") or url), brand, "phish",
                       {"url": url, "lang": "pl" if _tld_pl(url) else None,
                        "date": (r.get("submission_time") or "")[:10]}))
    return out


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        url = r.extra["url"]
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={URL_FILE: (url + "\n").encode()},
            label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group),
            meta={"url": url, "target": r.label, "class": r.group, "lang": r.extra.get("lang"),
                  "date": r.extra.get("date"), "label_kind": INFO.label_kind},
        )
