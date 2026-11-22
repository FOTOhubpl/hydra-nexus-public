"""Campaign consistency report (ADV-CAMP, spec M6 gate): `GET /v1/campaigns/{campaign_id}/consistency?run_id=`.

Read-only telemetry computed on demand from the stored run (members, scored pairs); it never changes a partition.
Thresholds are the run's stored `params.thresholds` (else the defaults). Method: `hydra/consistency.py`.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hydra.api_task import _st, resolve_run
from hydra.consistency import report_for_campaign
from hydra.redact import redact_obj
from hydra.synapse import Thresholds

router = APIRouter(prefix="/v1", tags=["consistency"])


def _thresholds(conn: Any, run: str) -> tuple[float, float]:
    th = Thresholds()
    row = conn.execute("SELECT params FROM runs WHERE id=?", (run,)).fetchone()
    try:
        p = (json.loads(row[0] or "{}") or {}).get("thresholds") or {} if row else {}
    except (ValueError, TypeError):
        p = {}
    return float(p.get("link", th.link)), float(p.get("low", th.low))


@router.get("/campaigns/{campaign_id}/consistency")
def campaign_consistency(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        link, low = _thresholds(conn, run)
        out = report_for_campaign(conn, run, campaign_id, link, low)
    if out is None:
        raise HTTPException(status_code=404, detail="campaign not found in this run")
    return redact_obj(out)  # reasons quote gene values: central redaction (CR-ASTRA-SEC-13)
