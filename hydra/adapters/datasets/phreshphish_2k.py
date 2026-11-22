"""PhreshPhish-2k (HF `gt2026workshop/phreshphish-2k`, CC BY 4.0): 2,000 page screenshots from PhreshPhish.

Layout: `image/{train,validation}-*.parquet`, columns `image{bytes}, label, target_brand, sha256, url` (plus
prompt/completion text for VLM tuning, ignored). One row → incident with `screenshot.<ext>` (role
landing_screenshot) + `url.txt`, and, when the PhreshPhish parquet files are present next to it
(`<external-big>/phreshphish`), the page HTML joined by `sha256` → `landing.html`. That gives
screenshot + HTML + URL + brand, the closest public analogue of a NASK landing-page incident.
Labels as in `phreshphish`: `ext:phreshphish-2k:<brand>` for phishing with a named brand.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets import phreshphish as pp
from hydra.adapters.datasets._common import (
    HTML_FILE,
    SCREENSHOT_STEM,
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    image_ext,
    iter_parquet,
    iter_parquet_keys,
    norm_label,
    parquet_files,
    prefixed,
)

INFO = DatasetInfo(
    name="phreshphish-2k", title="PhreshPhish-2k page screenshots (+ HTML joined from PhreshPhish)",
    homepage="https://huggingface.co/datasets/gt2026workshop/phreshphish-2k",
    licence="CC BY 4.0 (card)", verdict="SHIP-SAFE", modality="screenshot+url(+html)",
    label_kind="brand_target", dir="phreshphish-2k",
    citation="gt2026workshop/phreshphish-2k; Dalton et al., arXiv:2507.10854",
)


def _files(root: Path) -> list[Path]:
    return parquet_files(root, "image/*.parquet")


def index(root: Path) -> list[Row]:
    out = []
    for r in iter_parquet(_files(root), ["sha256", "label", "target_brand", "url"], batch_size=512):
        out.append(Row(r["sha256"], pp.brand_of(r.get("label"), r.get("target_brand")), norm_label(r.get("label")),
                       {"url": r.get("url")}))
    return out


def load(root: Path, rows: list[Row], html_root: Path | None = None) -> Iterator[ExtIncident]:
    by_key = {r.key: r for r in rows}
    html_root = html_root if html_root is not None else root.parent / pp.INFO.dir
    html: dict[str, str] = {}
    if html_root.is_dir() and pp._files(html_root):
        for rec in iter_parquet_keys(pp._files(html_root), "sha256", set(by_key), ["html"]):
            if rec.get("html"):
                html[rec["sha256"]] = rec["html"]
    for rec in iter_parquet_keys(_files(root), "sha256", set(by_key), ["image", "label", "target_brand", "url"]):
        r = by_key[rec["sha256"]]
        img = (rec.get("image") or {}).get("bytes")
        files: dict[str, bytes | Path] = {}
        roles: dict[str, str] = {}
        if img:
            name = SCREENSHOT_STEM + image_ext(img)
            files[name] = img
            roles[name] = "landing_screenshot"
        if rec["sha256"] in html:
            files[HTML_FILE] = html[rec["sha256"]].encode("utf-8", errors="replace")
        if rec.get("url"):
            files[URL_FILE] = (rec["url"].strip() + "\n").encode()
        yield ExtIncident(
            dataset=INFO.name, key=rec["sha256"][:16], files=files, roles=roles,
            label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group),
            meta={"sha256": rec["sha256"], "url": rec.get("url"), "class": norm_label(rec.get("label")),
                  "target": norm_label(rec.get("target_brand")), "html_joined": rec["sha256"] in html,
                  "label_kind": INFO.label_kind},
        )
