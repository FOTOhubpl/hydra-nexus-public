"""Scam-ads detection dataset (HF `Purino/scam-ads-detection-dataset`, card licence MIT).

Layout: `data/train-*.parquet`, columns `id, page_id, page_name, text, rule_score, rule_hits, label_suggested,
image_filename, image{bytes}, label (0 non_scam | 1 scam)`. One row = one ad → incident with
`ad_creative.<ext>` (role ad_creative) + `ad_text.txt` (role text).
Profile (2026-11-21, aggregates): 3,705 ads, 1,010 scam ads from real ad-library advertiser pages (145 pages with
>= 2 scam ads, 461 ads in them); the 2,695 non-scam rows are generic product banners under a few synthetic page
ids. Labels: `label = ext:purino-scam-ads:page:<page_id>` (advertiser-page grouping: same advertiser, not
necessarily one campaign), `group_label = ext:purino-scam-ads:<scam|non_scam>`. Option `scam_only`.
Ad creatives are third-party content: aggregates only, never redistribute.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    AD_CREATIVE_STEM,
    AD_TEXT_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    image_ext,
    iter_parquet,
    iter_parquet_keys,
    parquet_files,
    prefixed,
)

INFO = DatasetInfo(
    name="purino-scam-ads", title="Scam ads detection dataset (ad creatives + advertiser page)",
    homepage="https://huggingface.co/datasets/Purino/scam-ads-detection-dataset",
    licence="MIT (card); creatives are third-party ad content", verdict="SHIP-SAFE",
    modality="ad image+text", label_kind="advertiser_page", dir="purino-scam-ads",
)
CLASS = {0: "non_scam", 1: "scam"}


def _files(root: Path) -> list[Path]:
    return parquet_files(root, "data/*.parquet")


def index(root: Path) -> list[Row]:
    out = []
    for r in iter_parquet(_files(root), ["id", "page_id", "label"], batch_size=1024):
        page = str(r.get("page_id") or "").strip()
        out.append(Row(str(r["id"]), f"page:{page}" if page else None, CLASS.get(int(r.get("label") or 0))))
    return out


def where(scam_only: bool = False):
    return (lambda r: r.group == "scam") if scam_only else None


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    by_key = {r.key: r for r in rows}
    for rec in iter_parquet_keys(_files(root), "id", set(by_key), ["page_id", "page_name", "text", "image",
                                                                   "label"], batch_size=32):
        r = by_key[str(rec["id"])]
        files: dict[str, bytes | Path] = {}
        img = (rec.get("image") or {}).get("bytes")
        if img:
            files[AD_CREATIVE_STEM + image_ext(img)] = img
        if (rec.get("text") or "").strip():
            files[AD_TEXT_FILE] = rec["text"].encode("utf-8", errors="replace")
        yield ExtIncident(
            dataset=INFO.name, key=str(rec["id"]), files=files, label=prefixed(INFO.name, r.label),
            group_label=prefixed(INFO.name, r.group),
            meta={"page_id": rec.get("page_id"), "class": r.group, "label_kind": INFO.label_kind},
        )
