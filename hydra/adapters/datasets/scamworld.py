"""ScamWorld (HF `oscarwu/ScamWorld`, CC BY-NC 4.0 for websites and task data → EVAL-ONLY): research
reconstructions of 363 reported scam websites and 53 legitimate control sites, 430 web-agent tasks in 26 languages
and 16 scenarios (investment, shopping, prizes, parcel, login, ...). Polish: 5 scam tasks (PKN ORLEN investment
clone, Steam prize, two fake fashion shops, an Allegro Lokalnie listing).

Layout: `data/tasks.parquet` (`start_page{bytes}` = WebP screenshot of the start page, `task, scenario, language,
legitimate_site, fraud_families, variant_of, request, site_pages, site_files`) and, fetched for the Polish tasks
only, `tasks/<task>/site/**.html` + `tasks/<task>/source/provenance.json` (`source.url` = the reported scam URL).
One task = one incident: `screenshot.<ext>` (role landing_screenshot), `landing.html` (the site's
`index.html`, when fetched) and `url.txt` (reported URL, when known). The agent `request` is benchmark prompt
text, not scam content, and is not used. Labels: `label = ext:scamworld:site:<base task>` (`variant_of`, or the
task id before the first dot: tasks re-using one site share it), `group_label = ext:scamworld:<scam|legit>`;
`meta.scenario`, `meta.fraud_families`, `meta.lang`. Sites carry benchmark-planted fake elements next to the
captured scam content (README disclaimer): evaluation only, never redistributed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

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
    name="scamworld", title="ScamWorld reconstructed scam websites (screenshot + HTML for the Polish tasks)",
    homepage="https://huggingface.co/datasets/oscarwu/ScamWorld",
    licence="CC BY-NC 4.0 (card: websites and task data; research and evaluation only)", verdict="EVAL-ONLY",
    modality="screenshot(+html+url)", label_kind="source_site", dir="scamworld",
    citation="Wu et al., ScamWorld, arXiv:2611.04879 (2026)",
)
COLS = ["task", "scenario", "language", "legitimate_site", "fraud_families", "variant_of", "site_pages"]


def _files(root: Path) -> list[Path]:
    return parquet_files(root, "data/*.parquet")


def _site(task: str) -> str:
    return task.split(".", 1)[0]


def index(root: Path) -> list[Row]:
    out = []
    for r in iter_parquet(_files(root), COLS, batch_size=512):
        task = str(r["task"])
        lang = (r.get("language") or "").strip().lower() or None
        base = norm_label(r.get("variant_of")) or _site(task)
        out.append(Row(task, f"site:{base}", "legit" if r.get("legitimate_site") else "scam",
                       {"lang": lang, "scenario": norm_label(r.get("scenario")),
                        "fraud_families": [norm_label(x) for x in (r.get("fraud_families") or []) if norm_label(x)],
                        "site_pages": r.get("site_pages")}))
    return out


def where(lang: str | None = None, scam_only: bool = False, scenario: str | None = None):
    if not (lang or scam_only or scenario):
        return None
    return lambda r: ((lang is None or r.extra.get("lang") == lang) and (not scam_only or r.group == "scam")
                      and (scenario is None or r.extra.get("scenario") == norm_label(scenario)))


def _task_dir(root: Path, task: str) -> Path | None:
    for t in (task, _site(task)):
        d = root / "tasks" / t
        if d.is_dir():
            return d
    return None


def _source_url(d: Path) -> str | None:
    p = d / "source" / "provenance.json"
    try:
        url = (json.loads(p.read_text(encoding="utf-8")).get("source") or {}).get("url")
    except (OSError, ValueError, AttributeError):
        return None
    return url.strip() if isinstance(url, str) and url.strip().startswith(("http://", "https://")) else None


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    by_key = {r.key: r for r in rows}
    for rec in iter_parquet_keys(_files(root), "task", set(by_key), ["start_page"], batch_size=32):
        r = by_key[str(rec["task"])]
        files: dict[str, bytes | Path] = {}
        roles: dict[str, str] = {}
        img = (rec.get("start_page") or {}).get("bytes")
        if img:
            name = SCREENSHOT_STEM + image_ext(img)
            files[name] = img
            roles[name] = "landing_screenshot"
        d = _task_dir(root, r.key)
        url = _source_url(d) if d else None
        pages: list[str] = []
        if d is not None:
            idx = d / "site" / "index.html"
            if idx.is_file():
                files[HTML_FILE] = idx
            pages = sorted(p.relative_to(d / "site").as_posix() for p in (d / "site").rglob("*.html"))
        if url:
            files[URL_FILE] = (url + "\n").encode()
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files=files, label=prefixed(INFO.name, r.label),
            group_label=prefixed(INFO.name, r.group), roles=roles,
            meta={"class": r.group, "lang": r.extra.get("lang"), "scenario": r.extra.get("scenario"),
                  "fraud_families": r.extra.get("fraud_families"), "source_url": url, "site_pages": pages,
                  "label_kind": INFO.label_kind},
        )
