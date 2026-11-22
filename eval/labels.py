"""Label-file loader with the review mask of the label-merge step (script not published).

A label file written by the two-reviewer protocol (`_meta.review`, version 2) is only a partial partition: pairs
nobody reviewed are unknown. Metrics may not treat them as negatives, so this loader keeps a campaign (group)
label only for incidents in the level's `fully_reviewed` subset; every pair inside that subset has a reviewed or
partition-implied relation. Incidents outside it get no label at that level and so drop out of every metric
(eval code already ignores unlabelled incidents). Files without `_meta.review` (the team-hypothesis
`sample_labels.json`) are a complete partition by declaration and load unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path


def load(path: Path) -> dict:
    """Read a label file and apply the review mask to the incidents and to every alternative label set."""
    doc = json.loads(path.read_text()) if path.exists() else {"_meta": {}, "incidents": {}}
    return apply_mask(doc)


def apply_mask(doc: dict) -> dict:
    rv = review(doc)
    if not rv:
        return doc
    meta = dict(doc.get("_meta") or {})
    full = {"label": set(rv["campaign"]["fully_reviewed"]), "group_label": set(rv["group"]["fully_reviewed"])}
    alts = {}
    for name, spec in (meta.get("alternative_label_sets") or {}).items():
        # an override for an excluded incident is dropped (not set to None: None == None would link them)
        inc = {i: {k: v for k, v in e.items() if k not in full or i in full[k]}
               for i, e in (spec.get("incidents") or {}).items()}
        alts[name] = {**spec, "incidents": inc}
    meta["alternative_label_sets"] = alts
    meta["label_mask"] = summary(doc)
    return {**doc, "_meta": meta, "incidents": masked_incidents(doc)}


def review(doc: dict) -> dict | None:
    return (doc.get("_meta") or {}).get("review")


def masked_incidents(doc: dict) -> dict[str, dict]:
    """Incident map with labels outside the fully reviewed subset removed (label/group_label = None and
    `review_excluded` naming the level). Legacy files: returned as they are."""
    inc = doc.get("incidents") or {}
    rv = review(doc)
    if not rv:
        return inc
    full_c, full_g = set(rv["campaign"]["fully_reviewed"]), set(rv["group"]["fully_reviewed"])
    out = {}
    for iid, v in inc.items():
        e = dict(v)
        excl = []
        if iid not in full_c:
            e["label"] = None
            excl.append("campaign")
        if iid not in full_g:
            e["group_label"] = None
            excl.append("group")
        if excl:
            e["review_excluded"] = excl
        out[iid] = e
    return out


def summary(doc: dict) -> dict:
    """Aggregate counts for the results meta (no ids)."""
    rv = review(doc)
    if not rv:
        return {"mask": False, "rule": "legacy label file: complete partition by declaration"}
    n = len(rv["incidents"])
    return {"mask": True, "incidents": n, "reviewed_pairs": len(rv["reviewed_pairs"]),
            "total_pairs": n * (n - 1) // 2,
            "unknown_pairs_campaign": len(rv["campaign"]["unknown_pairs"]),
            "unknown_pairs_group": len(rv["group"]["unknown_pairs"]),
            "fully_reviewed_campaign": len(rv["campaign"]["fully_reviewed"]),
            "fully_reviewed_group": len(rv["group"]["fully_reviewed"]), "rule": rv["rule"]}
