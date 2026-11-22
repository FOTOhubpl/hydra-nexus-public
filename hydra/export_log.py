"""Export log: every export a user takes out of HYDRA is recorded as a hash-chained `EXPORT` event.

No new table (migrations belong to the architect): the payload carries `kind`, `run_id`, `counts` and a few safe
extras (campaign id, format); local paths are never stored. `export_counts` aggregates the events of one run for
the "Jak to działa" view. Recording never fails the export itself.

Kinds written today (`KINDS`):
- campaign_json, campaign_csv — GET /v1/campaigns/{id}/export
- neo4j_cypher, neo4j_zip, neo4j_nodes_csv, neo4j_edges_csv — GET /v1/export/neo4j
- subgraph_json, subgraph_cypher — POST /v1/graph/export (graph selection)
- abuse_package — GET /v1/campaigns/{id}/abuse-package
- stix_campaign, stix_run — GET /v1/campaigns/{id}/stix, GET /v1/runs/{id}/stix
- dataset — `hydra export-dataset`, GET /v1/export/dataset (extra `format: zip`)
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from typing import Any

from hydra import events
from hydra.redact import redact_obj

log = logging.getLogger(__name__)

EVENT = "EXPORT"
KINDS = ("campaign_json", "campaign_csv", "neo4j_cypher", "neo4j_zip", "neo4j_nodes_csv", "neo4j_edges_csv",
         "subgraph_json", "subgraph_cypher", "abuse_package", "stix_campaign", "stix_run", "dataset")
_SAFE_EXTRA = (str, int, float, bool, type(None))


def record_export(writer: Any, kind: str, run_id: str | None, counts: dict[str, int] | None = None,
                  **extra: Any) -> None:
    """Append one EXPORT event through the single Writer. Never raises (an export must not fail on logging)."""
    try:
        payload: dict[str, Any] = {"kind": kind, "run_id": run_id, "at": events.utcnow(),
                                   "counts": {k: int(v) for k, v in (counts or {}).items() if v is not None}}
        for k, v in extra.items():
            if isinstance(v, _SAFE_EXTRA) and not (isinstance(v, str) and ("/" in v or "\\" in v)):
                payload[k] = v  # no local paths in the audit log
        payload = redact_obj(payload)
        writer.run(lambda conn: events.append(conn, EVENT, payload), timeout=30)
    except Exception:  # noqa: BLE001
        log.warning("export %s of run %s not recorded", kind, run_id, exc_info=True)


def export_counts(conn: sqlite3.Connection, run_id: str | None) -> dict[str, Any]:
    """{"total": n, "by_kind": {kind: n}, "last_at": iso | None, "items": {count key: sum}} for one run."""
    out: dict[str, Any] = {"total": 0, "by_kind": {}, "last_at": None, "items": {}}
    if not run_id:
        return out
    by_kind: Counter = Counter()
    items: Counter = Counter()
    for r in conn.execute("SELECT ts, json_extract(payload, '$.kind') AS kind, json_extract(payload, '$.counts')"
                          " AS counts FROM events WHERE type=? AND json_extract(payload, '$.run_id')=? ORDER BY seq",
                          (EVENT, run_id)):
        by_kind[r["kind"] or "unknown"] += 1
        out["last_at"] = r["ts"]
        try:
            for k, v in (json.loads(r["counts"] or "{}") or {}).items():
                items[k] += int(v or 0)
        except (TypeError, ValueError):
            pass
    out.update(total=sum(by_kind.values()), by_kind=dict(by_kind), items=dict(items))
    return out
