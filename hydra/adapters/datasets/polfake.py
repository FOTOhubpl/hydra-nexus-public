"""POLfake - Polish fake news dataset (github.com/MateuszWalczakTUL/POLfake @c63fdd2): 5,774 fact-checked Polish
claims (demagog.org.pl, fake_news.pl; labels Prawda / Fałsz / Manipulacja / W większości fałsz / W większości
prawda) and 1,453 tweets linked to their claim by `claim_id`. One claim or tweet = one text incident
(`ad_text.txt`). Grouping: `label = ext:polfake:claim:<claim id>` for the claim and every tweet referring to it
(claim-tweet paraphrase linking = one narrative), `label_kind = claim_id`; `group_label = ext:polfake:<truthfulness>`.
The fact-checking site is kept as `meta.origin` (demagog_org | fake_news_pl); the corpus flag stays
`source = ext:polfake`. The images under `images/` are stock illustrations chosen by the authors with a
vision-language model (Openverse / Wikimedia, per-image licences), not original post media: they are not used, so
they cannot create false visual links. No repository licence → EVAL-ONLY (nothing shipped is fitted on it;
never redistributed).
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
)

INFO = DatasetInfo(
    name="polfake", title="POLfake Polish fact-checked claims + tweets (claim_id linking)",
    homepage="https://github.com/MateuszWalczakTUL/POLfake",
    licence="no licence (GitHub licence null, no LICENSE file)", verdict="EVAL-ONLY", modality="text",
    label_kind="claim_id", dir="polfake",
    citation="Walczak et al., POLfake - Polish fake news dataset, github.com/MateuszWalczakTUL/POLfake @c63fdd2",
)
CLAIMS = "claim-tweet-challenge/claims-train.csv"  # all 5,774 claims (the claim-only split is a re-split)
TWEETS = "claim-tweet-challenge/tweets-test.csv"


def _base(root: Path) -> Path:
    return root / "repo" if (root / "repo").is_dir() else root


def _rows(path: Path) -> Iterator[dict]:
    if not path.is_file():
        return
    csv.field_size_limit(1 << 30)
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        yield from csv.DictReader(fh)


def index(root: Path) -> list[Row]:
    b = _base(root)
    out = []
    for r in _rows(b / CLAIMS):
        text = (r.get("content") or "").strip()
        if text and r.get("id"):
            out.append(Row(f"claim-{r['id']}", f"claim:{r['id']}", norm_label(r.get("label")) or "unknown",
                           {"kind": "claim", "text": text, "origin": r.get("source"), "tags": r.get("tags"),
                            "date": (r.get("publication_date") or "")[:10], "author": r.get("author")}))
    for r in _rows(b / TWEETS):
        text = (r.get("content") or "").strip()
        if text and r.get("id"):
            cid = (r.get("claim_id") or "").strip()
            out.append(Row(f"tweet-{r['id']}", f"claim:{cid}" if cid else None,
                           norm_label(r.get("label")) or "unknown",
                           {"kind": "tweet", "text": text, "date": (r.get("publication_date") or "")[:10]}))
    return out


def where(kind: str | None = None):
    return (lambda r: r.extra.get("kind") == kind) if kind else None


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        meta = {k: v for k, v in r.extra.items() if k != "text" and v not in (None, "")}
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={AD_TEXT_FILE: r.extra["text"].encode("utf-8", errors="replace")},
            label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group),
            meta=meta | {"truthfulness": r.group, "lang": "pl", "label_kind": INFO.label_kind},
        )
