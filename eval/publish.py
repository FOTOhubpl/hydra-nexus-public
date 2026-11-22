"""Copy the latest eval run (runs.kind='eval') from the isolated eval state into the main HYDRA state, so the UI
(which reads the latest eval run) shows it. Writes only through `hydra.db.Writer`; copies the runs row only
(aggregate metrics, no incidents/genes).

    uv run python -m eval.publish [--from DB] [--to DB] [--run RUN_ID]
    uv run python -m eval.publish --json results.json [--to DB]     # e.g. results copied from the server
"""

from __future__ import annotations

import argparse
from contextlib import closing
from pathlib import Path

from eval.backend import default_eval_state


def main() -> int:
    from hydra.config import Settings
    from hydra.db import connect

    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", type=Path, default=Settings(state_dir=default_eval_state()).db_path)
    ap.add_argument("--to", dest="dst", type=Path, default=None, help="default: the main state DB (HYDRA_STATE_DIR)")
    ap.add_argument("--run", default=None)
    ap.add_argument("--json", type=Path, default=None, help="publish a results.json instead of a DB row")
    a = ap.parse_args()
    dst = a.dst or Settings().db_path
    if a.json:
        import json
        import time

        from eval.run import ui_metrics
        from hydra.events import canonical_json, utcnow

        res = json.loads(a.json.read_text())
        run_id = a.run or res.get("meta", {}).get("run_id") or f"eval-{time.strftime('%Y%m%dT%H%M%S')}"
        row = {"id": run_id, "params": canonical_json({"backend": res["meta"].get("backend"), "source": str(a.json)}),
               "metrics": canonical_json(ui_metrics(res)), "created_at": utcnow(), "finished_at": utcnow()}
        return _write(row, dst)
    if dst.resolve() == a.src.resolve():
        print("source and destination are the same DB; nothing to do")
        return 1
    with closing(connect(a.src, readonly=True)) as conn:
        q = "SELECT * FROM runs WHERE kind = 'eval'" + (" AND id = ?" if a.run else "") + \
            " ORDER BY created_at DESC LIMIT 1"
        row = conn.execute(q, (a.run,) if a.run else ()).fetchone()
    if row is None:
        print(f"no eval run in {a.src}")
        return 1
    return _write(dict(row), dst)


def _write(row: dict, dst: Path) -> int:
    from hydra.db import Writer

    w = Writer(dst)
    try:
        w.run(lambda c: c.execute(
            "INSERT OR REPLACE INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES (?,?,?,?,?,?)",
            (row["id"], "eval", row["params"], row["metrics"], row["created_at"], row["finished_at"])))
    finally:
        w.close()
    print(f"published {row['id']} ({len(row['metrics'])} bytes of metrics) -> {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
