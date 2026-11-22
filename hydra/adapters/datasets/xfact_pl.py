"""X-FACT Polish split (HF `utahnlp/x-fact` config `pl`, MIT on the card and on github.com/utahnlp/x-fact):
1,768 Polish political claims fact-checked by demagog.org.pl, each with the claimant, claim / review dates and a
veracity label (true / false / partly true/misleading / complicated/hard to categorise) plus five search-result
evidence snippets (not used). Profile (2026-11-21): 1,768 claims, 446 claimants, 259 with >= 2 claims
(1,581 claims in them).

Layout: `pl/{train,dev,test}-00000-of-00001.csv` (`language, site, evidence_1..5, link_1..5, claimDate,
reviewDate, claimant, claim, label`). One claim = one incident with `ad_text.txt` (role text) holding the claim
only (the claimant is the label and never enters the text). Grouping: `label = ext:xfact-pl:claimant:<name>`
(same speaker, the closest provided analogue of a recurring narrative source), `group_label =
ext:xfact-pl:<veracity>`. Option `false_only` keeps false + partly true/misleading. `meta.lang = "pl"`.
Claims are public statements of politicians; aggregates only in public material.
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
    name="xfact-pl", title="X-FACT Polish claims (demagog.org.pl; claimant + veracity)",
    homepage="https://huggingface.co/datasets/utahnlp/x-fact",
    licence="MIT (HF card and GitHub LICENSE); claims/evidence are third-party text", verdict="SHIP-SAFE",
    modality="text", label_kind="claimant", dir="xfact-pl",
    citation="Gupta & Srikumar, X-FACT: A New Benchmark Dataset for Multilingual Fact Checking, ACL 2021",
)
FALSE_LIKE = {"false", "partly true/misleading", "mostly_false", "partly_true"}


def _files(root: Path) -> list[Path]:
    fs = sorted(root.glob("pl/*.csv"))
    return fs or sorted(root.glob("*.csv"))


def index(root: Path) -> list[Row]:
    csv.field_size_limit(1 << 30)
    out = []
    for f in _files(root):
        split = f.name.split("-", 1)[0]
        with f.open(newline="", encoding="utf-8-sig", errors="replace") as fh:
            for i, r in enumerate(csv.DictReader(fh)):
                claim = (r.get("claim") or "").strip()
                if not claim:
                    continue
                who = norm_label(r.get("claimant"))
                lang = norm_label(r.get("language")) or "pl"
                out.append(Row(f"{split}-{i:05d}-{slug(claim, 16)}", f"claimant:{who}" if who else None,
                               norm_label(r.get("label")) or "unknown",
                               {"text": claim, "lang": lang, "split": split, "site": norm_label(r.get("site")),
                                "claim_date": (r.get("claimDate") or "")[:10],
                                "review_date": (r.get("reviewDate") or "")[:10]}))
    return out


def where(lang: str | None = None, split: str | None = None, false_only: bool = False):
    if not (lang or split or false_only):
        return None
    return lambda r: ((lang is None or r.extra.get("lang") == lang) and (split is None or r.extra.get("split") ==
                      split) and (not false_only or r.group in FALSE_LIKE))


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={AD_TEXT_FILE: (r.extra["text"] + "\n").encode("utf-8")},
            label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group),
            meta={"claimant": (r.label or "").removeprefix("claimant:") or None, "veracity": r.group,
                  "lang": r.extra.get("lang"), "split": r.extra.get("split"), "site": r.extra.get("site"),
                  "claim_date": r.extra.get("claim_date"), "review_date": r.extra.get("review_date"),
                  "label_kind": INFO.label_kind},
        )
