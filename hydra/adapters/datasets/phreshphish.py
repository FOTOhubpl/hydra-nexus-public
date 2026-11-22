"""PhreshPhish (Dalton et al. 2025, arXiv:2507.10854; HF `phreshphish/phreshphish`, CC BY 4.0).

Layout: `data/{train,test}-NNN.parquet`, columns `sha256, url, html, label ('phish'|'benign'), target (brand),
date, lang, lang_score`. One row = one page → incident with `landing.html` + `url.txt`.
Labels: phishing pages with a named target brand get `label = ext:phreshphish:<brand>` (brand-target grouping,
coarser than a campaign: many kits target one brand); benign pages and phish with target `other`/none are
unlabelled (open-set negatives), `group_label = ext:phreshphish:<phish|benign>`.
Selection options: `lang` (e.g. "pl"), `split` ("train"|"test"), `phish_only`.
"""

from __future__ import annotations

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
    norm_label,
    parquet_files,
    prefixed,
)

INFO = DatasetInfo(
    name="phreshphish", title="PhreshPhish phishing/benign HTML-URL pairs",
    homepage="https://huggingface.co/datasets/phreshphish/phreshphish",
    licence="CC BY 4.0 (card: anti-phishing research use)", verdict="SHIP-SAFE",
    modality="html+url", label_kind="brand_target", dir="phreshphish",
    citation="Dalton et al., PhreshPhish, arXiv:2507.10854",
)
NO_BRAND = {"other", "unknown", "none", "generic"}


def _files(root: Path, split: str | None = None) -> list[Path]:
    pat = f"data/{split}-*.parquet" if split else "data/*.parquet"
    return parquet_files(root, pat)


def brand_of(label: str | None, target: str | None) -> str | None:
    if (label or "").lower() != "phish":
        return None
    b = norm_label(target)
    return None if b is None or b in NO_BRAND else b


def index(root: Path) -> list[Row]:
    def build():
        for f in _files(root):
            split = f.name.split("-", 1)[0]
            for r in iter_parquet([f], ["sha256", "url", "label", "target", "lang", "date"], batch_size=2048):
                yield Row(r["sha256"], brand_of(r.get("label"), r.get("target")), norm_label(r.get("label")),
                          {"lang": norm_label(r.get("lang")), "split": split, "date": str(r.get("date") or "")[:10]})
    return cached_index(root, "phreshphish", build)


def where(lang: str | None = None, split: str | None = None, phish_only: bool = False):
    def ok(r: Row) -> bool:
        return ((lang is None or r.extra.get("lang") == lang) and (split is None or r.extra.get("split") == split)
                and (not phish_only or r.group == "phish"))
    return ok


def load(root: Path, rows: list[Row], shards: list[Path] | None = None) -> Iterator[ExtIncident]:
    """`shards` restricts the scan to some parquet shards (parallel bulk materialisation)."""
    by_key = {r.key: r for r in rows}
    cols = ["url", "html", "label", "target", "date", "lang"]
    for rec in iter_parquet_keys(shards or _files(root), "sha256", set(by_key), cols):
        r = by_key[rec["sha256"]]
        files: dict[str, bytes | Path] = {}
        if rec.get("html"):
            files[HTML_FILE] = rec["html"].encode("utf-8", errors="replace")
        if rec.get("url"):
            files[URL_FILE] = (rec["url"].strip() + "\n").encode()
        yield ExtIncident(
            dataset=INFO.name, key=rec["sha256"][:16], files=files, label=prefixed(INFO.name, r.label),
            group_label=prefixed(INFO.name, r.group),
            meta={"sha256": rec["sha256"], "url": rec.get("url"), "class": norm_label(rec.get("label")),
                  "target": norm_label(rec.get("target")), "lang": norm_label(rec.get("lang")),
                  "date": str(rec.get("date") or "")[:10], "label_kind": INFO.label_kind},
        )
