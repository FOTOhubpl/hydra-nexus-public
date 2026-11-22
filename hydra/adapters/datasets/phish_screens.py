"""Phishing Website Screenshots (HF `shresthsamyak/phishing-website-screenshots`; licence "other": defensive
research and education, third-party rights reserved) → EVAL-ONLY.

Layout: `legitimate/<brand>/<page>.png`, `phishing/<source>/<page>.png`, `metadata.csv` (`file_name, label,
label_name, url, final_url, brand, brand_normalized, page_type, is_blank, is_captcha, is_error_page, ...`).
One screenshot → incident with `screenshot.png` (role landing_screenshot) + `url.txt` (final URL if known).
Labels: phishing → `ext:phish-screens:phish:<brand or source folder>` (target brand); legitimate →
`ext:phish-screens:site:<brand>` (several pages of one real site: homepage/login/signup/checkout = a
same-source/template group, a useful hard positive and a false-merge trap across brands).
Blank, captcha and error-page captures are skipped.
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    SCREENSHOT_STEM,
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
)

INFO = DatasetInfo(
    name="phish-screens", title="Phishing website screenshots (legitimate by brand/page type, phishing by target)",
    homepage="https://huggingface.co/datasets/shresthsamyak/phishing-website-screenshots",
    licence="other (card: defensive research and education; review third-party rights before redistribution or "
            "commercial use)", verdict="EVAL-ONLY", modality="screenshot+url", label_kind="brand_target|site",
    dir="phish-screens",
)
TRUE = {"1", "true", "yes"}


def _meta(root: Path) -> dict[str, dict[str, str]]:
    p = root / "metadata.csv"
    if not p.exists():
        return {}
    with p.open(newline="", encoding="utf-8", errors="replace") as fh:
        return {r["file_name"]: r for r in csv.DictReader(fh) if r.get("file_name")}


def index(root: Path) -> list[Row]:
    meta = _meta(root)
    out = []
    for top, cls in (("phishing", "phish"), ("legitimate", "legit")):
        for f in sorted((root / top).glob("*/*.png")):
            rel = f.relative_to(root).as_posix()
            m = meta.get(rel, {})
            if any((m.get(k) or "").strip().lower() in TRUE for k in ("is_blank", "is_captcha", "is_error_page")):
                continue
            brand = norm_label(m.get("brand_normalized")) or norm_label(m.get("brand")) or norm_label(f.parent.name)
            lab = f"phish:{brand}" if cls == "phish" else f"site:{brand}"
            out.append(Row(rel, lab, cls, {"page_type": norm_label(m.get("page_type"))}))
    return out


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    meta = _meta(root)
    for r in rows:
        f = root / r.key
        if not f.is_file():
            continue
        m = meta.get(r.key, {})
        url = (m.get("final_url") or m.get("url") or "").strip()
        files: dict[str, bytes | Path] = {SCREENSHOT_STEM + ".png": f}
        if url.startswith(("http://", "https://")):
            files[URL_FILE] = (url + "\n").encode()
        yield ExtIncident(
            dataset=INFO.name, key=r.key.removesuffix(".png").replace("/", "_"), files=files,
            roles={SCREENSHOT_STEM + ".png": "landing_screenshot"},
            label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group),
            meta={"file": r.key, "url": url or None, "class": r.group, "page_type": r.extra.get("page_type"),
                  "label_kind": "brand_target" if r.group == "phish" else "site"},
        )
