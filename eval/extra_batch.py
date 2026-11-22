"""Summarise `hydra assign-batch` output for an unlabelled held-out batch: decision counts only (no values,
no incident content). python -m eval.extra_batch <assignments.json> [--new-patterns new_patterns.json] --out x.json"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("assignments", type=Path)
    ap.add_argument("--new-patterns", type=Path, default=None)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    rows = json.loads(a.assignments.read_text())
    dec = Counter(str(r.get("decision")) for r in rows)
    out = {"n": len(rows), "decisions": dict(dec),
           "assign_distinct_campaigns": len({r.get("campaign_id") for r in rows if r.get("decision") == "assign"}),
           "with_group_id": sum(1 for r in rows if r.get("group_id")),
           "duplicates_within_batch": sum(1 for r in rows if r.get("duplicate_of")),
           "errors": sum(1 for r in rows if r.get("error"))}
    if a.new_patterns and a.new_patterns.exists():
        np_ = json.loads(a.new_patterns.read_text())
        pats = np_ if isinstance(np_, list) else np_.get("patterns", [])
        out["new_patterns"] = len(pats)
        out["new_pattern_sizes"] = sorted((len(p.get("members", [])) for p in pats), reverse=True)
    a.out.write_text(json.dumps(out, indent=1))
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
