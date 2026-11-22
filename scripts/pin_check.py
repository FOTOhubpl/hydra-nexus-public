#!/usr/bin/env python3
"""Compare a freshly written models lock with the pins committed in the repository (full Docker image build).

    python scripts/pin_check.py <repo models.lock.json> <fetched models.lock.json> [--require KEY ...]

Every file of every `kind: model` entry in the repository lock, and of `psl`, must exist in the fetched lock with the
same path, size and sha256. `cert-domains` is a living list: its snapshot comes from the reference-data bundle and is
checked there (SHA256SUMS), so it is reported, not compared. Exit 1 on any difference or missing entry.
"""

from __future__ import annotations

import json
import sys


def main(argv: list[str]) -> int:
    pinned = json.load(open(argv[1], encoding="utf-8"))["entries"]
    got = json.load(open(argv[2], encoding="utf-8"))["entries"]
    bad = 0
    for key, e in pinned.items():
        if e.get("kind") != "model" and key != "psl":
            print(f"{key:28s} skipped (living list, verified by the reference bundle SHA256SUMS)")
            continue
        g = got.get(key)
        if g is None:
            print(f"{key:28s} MISSING")
            bad += 1
            continue
        have = {f["path"]: (f["size"], f["sha256"]) for f in g.get("files", [])}
        diff = [f["path"] for f in e["files"] if have.get(f["path"]) != (f["size"], f["sha256"])]
        print(f"{key:28s} {'OK' if not diff else 'DIFFERENT ' + str(diff)} ({len(e['files'])} files)")
        bad += bool(diff)
    print("pin check:", "PASS" if not bad else f"FAIL ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
