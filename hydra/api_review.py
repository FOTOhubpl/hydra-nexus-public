"""Review loop and explanation endpoints (ADOPT_PLAN #7, #14; TheHive-style similar cases). Mounted under /v1 by
hydra.api_task.

- GET  /v1/review/queue            analyst queue ordered by the expected value of a decision (`hydra.review_queue`)
- GET  /v1/review/unsure           active "niepewne" marks
- POST /v1/review/unsure           mark a pair or an assignment "niepewne" (audit event; campaigns unchanged)
- POST /v1/review/unsure/undo      undo a mark (audit event)
- GET  /v1/incidents/{id}/similar  top-k incidents sharing rare discrete evidence (`hydra.similar_cases`)

Accept / reject / undo stay on POST /v1/reviews (`hydra.review.apply_review` publishes a revision).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from hydra.api_auth import acting_analyst
from hydra.api_task import _latest_assignments, _membership, _pair_reviews, _st, resolve_run
from hydra.redact import redact_obj
from hydra.review import mark_unsure, undo_unsure, unsure_marks
from hydra.review_queue import build_queue
from hydra.similar_cases import similar_cases

router = APIRouter(tags=["review"])


class UnsureIn(BaseModel):
    target: dict[str, Any] = Field(default_factory=dict)  # {a, b} or {incident_id}
    note: str | None = Field(default=None, max_length=2000)
    analyst: str = Field(default="operator", max_length=64)


class UnsureUndoIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    analyst: str = Field(default="operator", max_length=64)


def _public(mark: dict[str, Any]) -> dict[str, Any]:
    """Display form of an unsure mark: analyst text is free text, so the recognised-secret policy applies
    (CR-ASTRA-PIPE-305); the stored mark keeps the original."""
    return redact_obj({k: mark.get(k) for k in ("id", "key", "target", "note", "analyst", "created_at")})


@router.get("/review/queue")
def review_queue(request: Request, run_id: str | None = None, limit: int = Query(200, ge=1, le=2000),
                 order: str | None = Query(None, pattern="^(gain|ev)$")) -> dict[str, Any]:
    with _st(request).reader() as conn:
        marks = unsure_marks(conn)
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None, "items": [], "total": 0, "threshold_bits": None, "method_pl": None,
                    "skipped": {"decided": 0, "unsure": 0, "decided_between_campaigns": 0},
                    "unsure": [_public(m) for m in marks]}
        out = build_queue(conn, run, _latest_assignments(conn, run), _pair_reviews(conn, unsure=False),
                          {m["key"] for m in marks}, limit=limit, order=order)
    out["unsure"] = [_public(m) for m in marks]
    return out


@router.get("/review/unsure")
def list_unsure(request: Request) -> dict[str, Any]:
    with _st(request).reader() as conn:
        return {"items": [_public(m) for m in unsure_marks(conn)]}


@router.post("/review/unsure", status_code=201)
def create_unsure(request: Request, body: UnsureIn) -> dict[str, Any]:
    t = body.target
    if not ((t.get("a") and t.get("b")) or t.get("incident_id")):
        raise HTTPException(status_code=400, detail="target needs {a, b} or {incident_id}")
    try:
        mark, event_hash = mark_unsure(_st(request).writer, t, note=body.note,
                                         analyst=acting_analyst(request, body.analyst))
    except ValueError as exc:
        raise HTTPException(status_code=409 if "already" in str(exc) else 400, detail=str(exc)) from exc
    return {"mark": _public(mark), "event_hash": event_hash, "effect": "queue_only",
            "effect_pl": "oznaczono jako niepewne: kampanie bez zmian, pozycja znika z kolejki"}


@router.post("/review/unsure/undo")
def undo_unsure_mark(request: Request, body: UnsureUndoIn) -> dict[str, Any]:
    try:
        event_hash = undo_unsure(_st(request).writer, body.id, analyst=acting_analyst(request, body.analyst))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"undone": body.id, "event_hash": event_hash}


@router.get("/incidents/{incident_id}/similar")
def incident_similar(request: Request, incident_id: str, k: int = Query(10, ge=1, le=50),
                     run_id: str | None = None) -> dict[str, Any]:
    with _st(request).reader() as conn:
        if conn.execute("SELECT 1 FROM incidents WHERE id=?", (incident_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="incident not found")
        run = resolve_run(conn, run_id)
        members = _membership(conn, run)
        out = similar_cases(conn, incident_id, set(members) or None, members, k=k)
    out["run_id"] = run
    return out
