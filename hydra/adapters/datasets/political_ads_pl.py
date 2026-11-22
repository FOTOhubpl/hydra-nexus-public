"""Polish Political Advertising (HF `laugustyniak/political-advertising-pl` @ed0114d, CC BY-NC-SA 4.0): 1,705
human-annotated Polish tweets of election campaigns, token-tagged with nine political-advertising categories
(healthcare, welfare, society, ...). One tweet = one text incident (`ad_text.txt`, tokens joined). No grouping
label; `group_label = ext:political-ads-pl:<dominant category | none>`; all categories and the split go to meta,
for ad-text pattern evaluation (legitimate political ad copy, Polish). Non-commercial share-alike → EVAL-ONLY.
"""

from __future__ import annotations

import ast
import hashlib
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    AD_TEXT_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    iter_parquet,
    prefixed,
)

INFO = DatasetInfo(
    name="political-ads-pl", title="Polish Political Advertising tweets (9 campaign categories)",
    homepage="https://huggingface.co/datasets/laugustyniak/political-advertising-pl",
    licence="CC BY-NC-SA 4.0 (dataset card)", verdict="EVAL-ONLY", modality="text", label_kind="none",
    dir="political-ads-pl", citation="Augustyniak et al., Polish Political Advertising dataset (HF laugustyniak)",
)
SPLITS = {"train": "train", "dev": "validation", "test": "test"}
NO_SPACE_BEFORE = {".", ",", "!", "?", ":", ";", ")", "%"}


def _list(v: object) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    try:
        out = ast.literal_eval(str(v))  # the parquet stores Python list literals as strings
    except (ValueError, SyntaxError):
        return []
    return [str(x) for x in out] if isinstance(out, list) else []


def detok(tokens: list[str]) -> str:
    out = ""
    for t in tokens:
        out += t if (not out or t in NO_SPACE_BEFORE or out.endswith("(")) else " " + t
    return out


def index(root: Path) -> list[Row]:
    rows = []
    for stem, split in SPLITS.items():
        f = root / f"{stem}.parquet"
        if not f.is_file():
            continue
        for i, r in enumerate(iter_parquet([f], ["tokens", "tags"])):
            toks, tags = _list(r.get("tokens")), _list(r.get("tags"))
            text = detok(toks)
            if not text:
                continue
            cats = Counter(t[2:].lower() for t in tags if t.startswith(("B-", "I-")))
            dominant = cats.most_common(1)[0][0] if cats else "none"
            key = f"{split}-{i:05d}-{hashlib.sha256(text.encode()).hexdigest()[:8]}"
            rows.append(Row(key, None, dominant, {"text": text, "split": split, "categories": sorted(cats)}))
    return rows


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={AD_TEXT_FILE: r.extra["text"].encode("utf-8", errors="replace")},
            label=None, group_label=prefixed(INFO.name, r.group),
            meta={"split": r.extra["split"], "categories": r.extra["categories"], "lang": "pl",
                  "label_kind": INFO.label_kind},
        )
