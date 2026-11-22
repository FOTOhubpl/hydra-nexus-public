"""Variant lineage of a campaign (GAPS_PAPERS G01): `GET /v1/campaigns/{campaign_id}/lineage?run_id=`.

Read-only; computed on demand from the stored run (campaign members, scored pairs, genes), so runs written before
this route existed work without re-analysis. Method and limits: `hydra/lineage.py`, docs/METHODOLOGY.md §8a.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hydra.api_task import _st, resolve_run
from hydra.lineage import lineage_for_campaign
from hydra.redact import redact_obj

router = APIRouter(prefix="/v1", tags=["lineage"])


@router.get("/campaigns/{campaign_id}/lineage")
def campaign_lineage(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no analysis run yet")
        out = lineage_for_campaign(conn, run, campaign_id)
    if out is None:
        raise HTTPException(status_code=404, detail="campaign not found in this run")
    return redact_obj(out)  # gene values shown per edge pass the central redaction (CR-ASTRA-SEC-13)
