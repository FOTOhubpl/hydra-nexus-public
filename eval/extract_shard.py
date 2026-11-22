"""One extraction shard (subprocess of eval/run.py --extract-workers N): CORE import + genes for a list of
incidents into its own isolated state dir. Separate processes, separate SQLite files, so shards never share a
writer; the parent then reads genes and vectors from each shard DB.

    python -m eval.extract_shard --state DIR --rows rows.json [--enrich-cache EMPTY_DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if "--hydra-root" in sys.argv:  # evaluate another checkout's `hydra` (eval-gate); must precede any hydra import
    sys.path.insert(0, str(Path(sys.argv[sys.argv.index("--hydra-root") + 1]).resolve()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--rows", type=Path, required=True)
    ap.add_argument("--enrich-cache", type=Path, default=None)
    ap.add_argument("--hydra-root", default=None)
    a = ap.parse_args()
    from eval.backend import HydraBackend, IncidentRef

    rows = json.loads(a.rows.read_text())
    bk = HydraBackend(state_dir=a.state, enrich_cache=a.enrich_cache)
    failed = 0
    try:
        for r in rows:
            try:
                bk.extract(IncidentRef(r["id"], Path(r["path"]), r.get("label"), r.get("group_label")))
            except Exception as exc:  # noqa: BLE001 — reported to the parent, which counts it as a crash
                failed += 1
                print(f"SHARD-FAIL {r['id']}: {type(exc).__name__}: {exc}"[:400], file=sys.stderr, flush=True)
    finally:
        bk.close()
    print(json.dumps({"done": len(rows) - failed, "failed": failed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
