"""X-FACT Polish adapter on a synthetic fixture (no real claims)."""

from __future__ import annotations

import csv
from pathlib import Path

from hydra.adapters.datasets import _common, xfact_pl

COLS = ["language", "site", "evidence_1", "link_1", "claimDate", "reviewDate", "claimant", "claim", "label"]


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "xfact-pl"
    (root / "pl").mkdir(parents=True)
    data = {"train": [("Jan Kowalski", "Bezrobocie spadło o połowę.", "false"),
                      ("Jan Kowalski", "Płace wzrosły o 5 procent.", "true"),
                      ("Anna Nowak", "Mamy najniższą inflację w UE.", "partly true/misleading"),
                      ("", "Bez autora.", "true")],
            "test": [("Jan Kowalski", "Emerytury rosną najszybciej.", "complicated/hard to categorise"),
                     ("Anna Nowak", "", "false")]}  # empty claim: skipped
    for split, rows in data.items():
        with (root / "pl" / f"{split}-00000-of-00001.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(COLS)
            for who, claim, lab in rows:
                w.writerow(["pl", "demagog.org.pl", "snippet", "https://e.example/", "2019-02-07T00:00:00Z",
                            "2019-02-08T00:00:00Z", who, claim, lab])
    return root


def test_index_groups_by_claimant(tmp_path: Path) -> None:
    rows = xfact_pl.index(_root(tmp_path))
    assert len(rows) == 5
    assert {r.label for r in rows} == {"claimant:jan kowalski", "claimant:anna nowak", None}
    assert all(r.extra["lang"] == "pl" for r in rows)
    assert {r.extra["split"] for r in rows} == {"train", "test"}
    picked = _common.select(rows, min_group=2)
    assert {r.label for r in picked} == {"claimant:jan kowalski"} and len(picked) == 3
    false_like = [r for r in rows if xfact_pl.where(false_only=True)(r)]
    assert {r.group for r in false_like} == {"false", "partly true/misleading"}


def test_load_text_has_claim_only(tmp_path: Path) -> None:
    root = _root(tmp_path)
    rows = xfact_pl.index(root)
    incs = list(xfact_pl.load(root, rows))
    one = next(i for i in incs if i.meta["veracity"] == "false")
    assert one.files["ad_text.txt"] == "Bezrobocie spadło o połowę.\n".encode()
    assert b"Kowalski" not in one.files["ad_text.txt"]
    assert one.label == "ext:xfact-pl:claimant:jan kowalski"
    assert one.group_label == "ext:xfact-pl:false"
    assert one.source == "ext:xfact-pl" and one.meta["lang"] == "pl" and one.meta["claim_date"] == "2019-02-07"
