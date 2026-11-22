"""Smishing dataset IMC'25 (github.com/reportsmishing/Smishing-Dataset-IMC25, CC BY 4.0): 33,869
user-reported smishing SMS in 69 languages (133 Polish), each labelled with an impersonated entity, scam type
and lure principles. One SMS = one incident: `ad_text.txt` (role text) with the message body. The impersonated
entity is a grouping label (`label = ext:smishing-imc25:<named_entity>`, named_entity kind); `group_label =
ext:smishing-imc25:<scam_type>`. `meta.lang = "pl"` for Polish rows. Texts contain user-reported content
(phone numbers, sender ids): server-side only, never redistributed, aggregates only in public material.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    AD_TEXT_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
    slug,
)

INFO = DatasetInfo(
    name="smishing-imc25", title="Smishing IMC'25 reported SMS (impersonated entity + scam type)",
    homepage="https://github.com/reportsmishing/Smishing-Dataset-IMC25",
    licence="CC BY 4.0", verdict="SHIP-SAFE", modality="text", label_kind="named_entity",
    dir="smishing-imc25", citation="Reporting Smishing, IMC 2025",
)


def _file(root: Path) -> Path | None:
    for c in ("final_dataset_output.csv", "dataset/final_dataset_output.csv"):
        p = root / c
        if p.is_file():
            return p
    return next(iter(sorted(root.rglob("*.csv"))), None)


def _rows(root: Path) -> Iterator[dict]:
    f = _file(root)
    if f is None:
        return
    csv.field_size_limit(1 << 30)
    with f.open(newline="", encoding="utf-8", errors="replace") as fh:
        yield from csv.DictReader(fh)


def index(root: Path) -> list[Row]:
    out = []
    for i, r in enumerate(_rows(root)):
        text = (r.get("text") or "").strip()
        if not text:
            continue
        entity = norm_label(r.get("named_entity"))
        scam = norm_label(r.get("scam_type")) or "others"
        lang = (r.get("language") or "").strip().lower()
        out.append(Row(f"{i:06d}-{slug(text, 16)}", entity, scam,
                       {"text": text, "lang": "pl" if lang == "polish" else None}))
    return out


def where(lang: str | None = None):
    return (lambda r: r.extra.get("lang") == lang) if lang else None


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={AD_TEXT_FILE: r.extra["text"].encode("utf-8", errors="replace")},
            label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group),
            meta={"named_entity": r.label, "scam_type": r.group, "lang": r.extra.get("lang"),
                  "label_kind": INFO.label_kind},
        )
