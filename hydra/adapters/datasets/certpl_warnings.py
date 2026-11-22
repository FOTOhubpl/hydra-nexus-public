"""CERT Polska Warning List (hole.cert.pl/domains/v2/domains.json): domains of phishing/scam sites targeting
Poland. Public list, free use, no endorsement implied (cert.pl/lista-ostrzezen). One active entry = one
incident: `url.txt` with the domain as `http://<domain>/` (synthesised; flagged in meta). No grouping label
(the list has no brand/campaign field); `group_label = ext:certpl-warnings:phish`. Every entry is a Polish
campaign domain (list of phishing targeting Polish users), so `meta.lang = "pl"` marks the Polish share
(targeting, not page language). Domain strings only; pages are never fetched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
)

INFO = DatasetInfo(
    name="certpl-warnings", title="CERT Polska Warning List (Polish phishing/scam domains)",
    homepage="https://cert.pl/lista-ostrzezen/",
    licence="public list, free use; must not imply endorsement by CERT Polska", verdict="SHIP-SAFE",
    modality="url", label_kind="none", dir="certpl-warnings",
    citation="CERT Polska Lista ostrzezen, hole.cert.pl/domains/v2/domains.json",
)


def _file(root: Path) -> Path | None:
    return next(iter(sorted(root.glob("domains*.json"), reverse=True)), None)


def index(root: Path) -> list[Row]:
    f = _file(root)
    if f is None:
        return []
    data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
    out = []
    for e in data:
        dom = norm_label(e.get("DomainAddress"))
        if dom is None or e.get("DeleteDate"):
            continue
        out.append(Row(dom, None, "phish",
                       {"lang": "pl", "insert_date": str(e.get("InsertDate") or "")[:10],
                        "position_id": e.get("RegisterPositionId")}))
    return out


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        url = f"http://{r.key}/"
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={URL_FILE: (url + "\n").encode()},
            label=None, group_label=prefixed(INFO.name, r.group),
            meta={"domain": r.key, "url": url, "url_synthesised": True, "class": r.group,
                  "lang": "pl", "insert_date": r.extra.get("insert_date"), "label_kind": INFO.label_kind},
        )
