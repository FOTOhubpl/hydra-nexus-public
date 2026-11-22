"""PhiUSIIL Phishing URL Dataset (UCI id 967, Prasad & Chandra 2024, CC BY 4.0): 235,795 URLs, label
1 = legitimate (134,850) and 0 = phishing (100,945). One row = one incident: `url.txt`. No grouping label
(binary phish/legit only); `group_label = ext:phiusiil:<phish|legit>`. A large legitimate counterpart for
rarity priors. `meta.lang = "pl"` when `TLD == pl` (1,339 rows). URL strings only; pages are never fetched.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    prefixed,
)

INFO = DatasetInfo(
    name="phiusiil", title="PhiUSIIL phishing/legitimate URL dataset",
    homepage="https://archive.ics.uci.edu/dataset/967/phiusiil+phishing+url+dataset",
    licence="CC BY 4.0", verdict="SHIP-SAFE", modality="url", label_kind="none", dir="phiusiil",
    citation="Prasad & Chandra, PhiUSIIL, UCI ML Repository id 967 (2024)",
)
CLASS = {"1": "legit", "0": "phish"}


def _file(root: Path) -> Path | None:
    return next(iter(sorted(root.glob("PhiUSIIL_*URL*.csv"))), None)


def _rows(root: Path) -> Iterator[dict]:
    f = _file(root)
    if f is None:
        return
    csv.field_size_limit(1 << 30)
    with f.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
        yield from csv.DictReader(fh)


def index(root: Path) -> list[Row]:
    out = []
    for r in _rows(root):
        url = (r.get("URL") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        cls = CLASS.get(str(r.get("label")).strip())
        if cls is None:
            continue
        out.append(Row(url, None, cls,
                       {"url": url, "lang": "pl" if (r.get("TLD") or "").strip().lower() == "pl" else None}))
    return out


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        url = r.extra["url"]
        yield ExtIncident(
            dataset=INFO.name, key=url, files={URL_FILE: (url + "\n").encode()},
            label=None, group_label=prefixed(INFO.name, r.group),
            meta={"url": url, "class": r.group, "lang": r.extra.get("lang"), "label_kind": INFO.label_kind},
        )
