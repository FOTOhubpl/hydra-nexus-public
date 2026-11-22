"""Phase timings of the per-run graph base (hydra.graph_lod.build_base) for every finished analyse run.

    HYDRA_STATE_DIR=<copy> uv run python scripts/bench_graph_base.py [run_id ...]

Read-only: opens the DB read-only and writes nothing (no cache).
"""

from __future__ import annotations

import sys
import time

from hydra import graph_lod as lod
from hydra.api_task import _groups, _membership, run_incidents
from hydra.config import Settings
from hydra.db import connect


def main() -> None:
    s = Settings()
    conn = connect(s.db_path, readonly=True)
    runs = sys.argv[1:] or [r[0] for r in conn.execute(
        "SELECT id FROM runs WHERE kind='analyse' AND finished_at IS NOT NULL ORDER BY created_at DESC")]
    for run in runs:
        n_pairs = conn.execute("SELECT COUNT(*) FROM pairs WHERE run_id=?", (run,)).fetchone()[0]
        t = {}
        t0 = time.perf_counter()
        rows = conn.execute("SELECT a, b, total_bits, probability, hypothesis, detail FROM pairs"
                            " WHERE run_id=? AND hypothesis!='independent'", (run,)).fetchall()
        t["fetch"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        edges = [lod.edge_from_row(r[0], r[1], r[2], r[3], r[4], lod.loads(r[5])) for r in rows]
        t["parse"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        members = _membership(conn, run)
        groups = _groups(conn, run)
        ids = sorted(set(run_incidents(conn, run)) | {x for e in edges for x in (e[0], e[1])})
        camps = {r[0]: {"id": r[0], "group_id": None} for r in conn.execute(
            "SELECT id FROM campaigns WHERE run_id=?", (run,))}
        nodes = lod.node_rows(conn, ids, members, camps)
        t["nodes"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        lod.pack_layout(nodes, camps, groups)
        t["layout"] = time.perf_counter() - t0
        print(f"{run}: pairs {n_pairs} links {len(edges)} nodes {len(nodes)} campaigns {len(camps)} groups"
              f" {len(groups)} | " + " ".join(f"{k} {v:.2f}s" for k, v in t.items()), flush=True)


if __name__ == "__main__":
    main()
