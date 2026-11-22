"""MIPD - Manipulation and Intention in Polish Disinformation (Modzelewski et al., EMNLP 2024,
github.com/ArkadiusDS/MIPD @51aac23): 15,356 Polish news articles annotated as disinformation or credible, with
manipulation techniques (11) and intentions (9) for the disinformation ones. One article = one text incident
(`ad_text.txt`). No grouping label (articles are independent; no per-article source), `group_label =
ext:mipd:<disinformation|credible>`; techniques, intentions, split and topic category go to meta, for NAR/TXT
manipulation-technique evaluation. Licence CC BY-NC-ND 4.0 → EVAL-ONLY (numbers only; never redistributed).
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    AD_TEXT_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    prefixed,
)

INFO = DatasetInfo(
    name="mipd", title="MIPD Polish disinformation articles (manipulation techniques + intentions)",
    homepage="https://github.com/ArkadiusDS/MIPD", licence="CC BY-NC-ND 4.0 (LICENSE)", verdict="EVAL-ONLY",
    modality="text", label_kind="none", dir="mipd",
    citation="Modzelewski et al., MIPD: Exploring Manipulation and Intention In a Novel Corpus of Polish "
             "Disinformation, EMNLP 2024",
)
SPLITS = ("train", "validation", "test")


def _csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    csv.field_size_limit(1 << 30)
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        return list(csv.DictReader(fh))


def _h(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8", errors="replace")).hexdigest()


def _flags(row: dict, skip: str = "article") -> list[str]:
    return sorted(k.lower() for k, v in row.items() if k != skip and str(v).strip() in ("1", "1.0", "True"))


def index(root: Path) -> list[Row]:
    d = root / "data"
    cats: dict[str, str] = {}
    by_id = {r["article_id"]: r.get("category") for r in _csv(d / "articles_categories.csv") if r.get("article_id")}
    for r in _csv(d / "raw_articles.csv"):
        if r.get("full_article") and r.get("article_id") in by_id:
            cats[_h(r["full_article"])] = by_id[r["article_id"]] or ""
    techniques = {_h(r["article"]): _flags(r) for s in SPLITS for r in _csv(d / "manipulation" / f"{s}.csv")
                  if r.get("article")}
    intentions = {_h(r["article"]): _flags(r) for s in SPLITS for r in _csv(d / "intention" / f"{s}.csv")
                  if r.get("article")}
    out, seen = [], set()
    for s in SPLITS:
        for r in _csv(d / "disinformation" / f"{s}.csv"):
            text = (r.get("article") or "").strip()
            h = _h(text) if text else ""
            if not text or h in seen:
                continue
            seen.add(h)
            cls = "disinformation" if str(r.get("disinformation")).strip() in ("1", "1.0", "True") else "credible"
            out.append(Row(h[:16], None, cls, {"split": s, "sha": h, "category": cats.get(h),
                                               "techniques": techniques.get(h, []),
                                               "intentions": intentions.get(h, [])}))
    return out


def where(split: str | None = None, disinformation_only: bool = False):
    def ok(r: Row) -> bool:
        return (split is None or r.extra.get("split") == split) and (not disinformation_only
                                                                     or r.group == "disinformation")
    return ok


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    want = {r.extra["sha"]: r for r in rows}
    for s in SPLITS:
        for rec in _csv(root / "data" / "disinformation" / f"{s}.csv"):
            text = (rec.get("article") or "").strip()
            r = want.pop(_h(text), None) if text else None
            if r is None:
                continue
            yield ExtIncident(
                dataset=INFO.name, key=r.key, files={AD_TEXT_FILE: text.encode("utf-8", errors="replace")},
                label=None, group_label=prefixed(INFO.name, r.group),
                meta={"split": r.extra["split"], "category": r.extra.get("category"),
                      "techniques": r.extra["techniques"], "intentions": r.extra["intentions"], "lang": "pl",
                      "class": r.group, "label_kind": INFO.label_kind},
            )
