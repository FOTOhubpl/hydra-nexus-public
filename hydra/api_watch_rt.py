"""WATCH-RT API (`/v1/watch/rt/*`): realtime public-feed watch with persisted items and matches (docs/WATCH.md).

GET  /status                         engine state, sources (health, countdown), totals, sparkline, timeline
POST /engine {action}                start | pause | resume | stop (live mode; offline -> state offline)
POST /sources/{id} {enabled, interval_s}
POST /sources/{id}/poll              poll now (409 when offline / disabled)
GET  /items  GET /matches  GET /runs
POST /matches/{id} {status}          acknowledged | dismissed | new
POST /matches/{id}/incident          candidate incident via hydra.batch.assign_batch (live rows only)
GET|POST /watchlist  DELETE /watchlist/{id}  POST /watchlist/rebuild
GET  /replay/fixtures  POST /replay {fixture, speed}  POST /replay/stop
GET  /metrics                        poll-loop and matching latency percentiles of this process
Live events are pushed on `GET /v1/stream` as watch.poll / watch.item / watch.match / watch.source_state.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from hydra.watch.rt import store
from hydra.watch.rt.engine import WatchRT, for_app, list_fixtures, load_fixture
from hydra.watch.rt.indicators import KIND_PL, KINDS
from hydra.watch.rt.sources import ADAPTERS, SourceError

router = APIRouter(prefix="/v1/watch/rt", tags=["watch-rt"])


def eng(request: Request) -> WatchRT:
    return for_app(request.app.state.hydra, request.app.state)


def require_role(request: Request, role: str) -> None:
    """Defence in depth for the panel auth gate (AUTH-LOGIN): when a principal is attached to the request scope,
    source configuration, poll-now and engine control need `admin` (same rule as api_auth._ADMIN_WRITE: they drive
    outbound connectors); acknowledge,
    dismiss, incident, watchlist and replay need `analyst` (the gate's default for writes). No principal in scope
    (HYDRA_AUTH=0 / no gate) = allowed."""
    p = request.scope.get("hydra.principal")
    if p is not None and hasattr(p, "has_role") and not p.has_role(role):
        raise HTTPException(403, {"code": "forbidden", "message": "Brak uprawnień: wymagana rola "
                                  + ("administratora." if role == "admin" else "analityka.")})


class EngineIn(BaseModel):
    action: Literal["start", "pause", "resume", "stop"]


class SourceIn(BaseModel):
    enabled: bool | None = None
    interval_s: int | None = Field(default=None, ge=60, le=86400)


class MatchIn(BaseModel):
    status: Literal["new", "acknowledged", "dismissed"]


class EntryIn(BaseModel):
    kind: str = Field(min_length=2, max_length=20)
    value: str = Field(min_length=1, max_length=500)
    label: str | None = Field(default=None, max_length=200)


class ReplayIn(BaseModel):
    fixture: str = Field(min_length=1, max_length=200)
    speed: float = Field(default=60.0, ge=1.0, le=3600.0)


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    return await run_in_threadpool(eng(request).status)


@router.post("/engine")
async def engine_action(request: Request, body: EngineIn) -> dict[str, Any]:
    require_role(request, "admin")
    e = eng(request)
    try:
        if body.action == "start":
            return e.start()
        if body.action == "pause":
            return e.pause()
        if body.action == "resume":
            return e.resume()
        return e.stop()
    except RuntimeError as exc:
        raise HTTPException(409, {"status": "locked", "message_pl": str(exc)}) from exc


@router.post("/sources/{sid}")
async def set_source(request: Request, sid: str, body: SourceIn) -> dict[str, Any]:
    require_role(request, "admin")
    if sid not in ADAPTERS:
        raise HTTPException(404, "nieznane źródło")
    return await run_in_threadpool(eng(request).set_source, sid, enabled=body.enabled, interval_s=body.interval_s)


@router.post("/sources/{sid}/poll")
async def poll_now(request: Request, sid: str) -> dict[str, Any]:
    require_role(request, "admin")
    if sid not in ADAPTERS:
        raise HTTPException(404, "nieznane źródło")
    e = eng(request)
    if e.state == "replay":
        raise HTTPException(409, {"status": "replay", "message_pl": "Trwa odtworzenie: zatrzymaj je, aby odpytać "
                                                                    "źródło na żywo."})
    try:
        run = await run_in_threadpool(e.poll_source, sid, force=True, replay=False)
    except SourceError as exc:
        raise HTTPException(409, {"status": exc.status, "message_pl": exc.message_pl}) from exc
    return {"run": run}


@router.get("/items")
async def items(request: Request, source: str | None = None, kind: str | None = None,
                matched: int | None = Query(None, ge=0, le=1), q: str | None = Query(None, max_length=200),
                before_id: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=500),
                replay: int | None = Query(None, ge=0, le=1)) -> dict[str, Any]:
    st = request.app.state.hydra

    def read() -> list[dict[str, Any]]:
        with st.reader() as conn:
            rows = store.list_items(conn, source=source, kind=kind, matched=None if matched is None else bool(matched),
                                    q=q, before_id=before_id, limit=limit)
        return [r for r in rows if replay is None or r["replay"] == bool(replay)]

    rows = await run_in_threadpool(read)
    return {"items": rows, "next_before": rows[-1]["id"] if len(rows) == limit else None}


@router.get("/matches")
async def matches(request: Request, status: str | None = Query(None, pattern="^(new|acknowledged|dismissed)$"),
                  limit: int = Query(100, ge=1, le=500), before_id: int | None = Query(None, ge=1)
                  ) -> dict[str, Any]:
    st = request.app.state.hydra

    def read() -> list[dict[str, Any]]:
        with st.reader() as conn:
            return store.list_matches(conn, status=status, limit=limit, before_id=before_id)

    return {"matches": await run_in_threadpool(read)}


@router.post("/matches/{mid}")
async def match_status(request: Request, mid: int, body: MatchIn) -> dict[str, Any]:
    require_role(request, "analyst")
    m = await run_in_threadpool(eng(request).set_match_status, mid, body.status)
    if m is None:
        raise HTTPException(404, "nieznane dopasowanie")
    return m


@router.post("/matches/{mid}/incident")
async def match_incident(request: Request, mid: int) -> dict[str, Any]:
    require_role(request, "analyst")
    try:
        return await run_in_threadpool(eng(request).create_incident, mid)
    except KeyError as exc:
        raise HTTPException(404, "nieznane dopasowanie") from exc
    except ValueError as exc:
        raise HTTPException(409, {"status": "refused", "message_pl": str(exc)}) from exc


@router.get("/watchlist")
async def watchlist(request: Request, kind: str | None = None, origin: str | None = None,
                    q: str | None = Query(None, max_length=200), limit: int = Query(500, ge=1, le=5000)
                    ) -> dict[str, Any]:
    st = request.app.state.hydra

    def read() -> dict[str, Any]:
        with st.reader() as conn:
            return store.list_watchlist(conn, kind=kind, origin=origin, q=q, limit=limit)

    out = await run_in_threadpool(read)
    out["kinds"] = [{"kind": k, "label_pl": KIND_PL[k]} for k in KINDS]
    return out


@router.post("/watchlist")
async def watchlist_add(request: Request, body: EntryIn) -> dict[str, Any]:
    require_role(request, "analyst")
    if body.kind not in KINDS:
        raise HTTPException(422, f"nieznany rodzaj wskaźnika: {body.kind}")
    e = eng(request)
    try:
        rid = await run_in_threadpool(e.add_entry, body.kind, body.value, body.label)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    st = request.app.state.hydra
    with st.reader() as conn:
        rows = [r for r in store.list_watchlist(conn, origin="manual", limit=5000)["entries"] if r["id"] == rid]
    return rows[0] if rows else {"id": rid}


@router.delete("/watchlist/{entry_id}")
async def watchlist_delete(request: Request, entry_id: int) -> dict[str, Any]:
    require_role(request, "analyst")
    ok = await run_in_threadpool(eng(request).delete_entry, entry_id)
    if not ok:
        raise HTTPException(404, "nie ma takiego ręcznego wpisu (wpisy z kampanii odświeża przebudowa)")
    return {"deleted": entry_id}


@router.post("/watchlist/rebuild")
async def watchlist_rebuild(request: Request) -> dict[str, Any]:
    require_role(request, "analyst")
    return await run_in_threadpool(eng(request).rebuild_watchlist)


@router.get("/runs")
async def runs(request: Request, source: str | None = None, limit: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    st = request.app.state.hydra

    def read() -> list[dict[str, Any]]:
        with st.reader() as conn:
            return store.list_runs(conn, source=source, limit=limit)

    return {"runs": await run_in_threadpool(read)}


@router.get("/replay/fixtures")
def fixtures() -> dict[str, Any]:
    return {"fixtures": [{"name": f.name, "description_pl": f.manifest.get("description_pl"), "ticks": len(f.ticks),
                          "synthetic": f.synthetic} for f in list_fixtures()]}


@router.post("/replay")
async def replay(request: Request, body: ReplayIn) -> dict[str, Any]:
    require_role(request, "analyst")
    if "/" in body.fixture or body.fixture.startswith("."):
        raise HTTPException(422, "nazwa nagrania bez ścieżki")
    try:
        fx = load_fixture(body.fixture)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"nie ma nagrania {body.fixture}") from exc
    return eng(request).start_replay(fx, body.speed)  # needs the running loop (creates the replay task)


@router.post("/replay/stop")
def replay_stop(request: Request) -> dict[str, Any]:
    require_role(request, "analyst")
    e = eng(request)
    e.stop_replay()
    return e.status()


@router.get("/metrics")
def metrics(request: Request) -> dict[str, Any]:
    return eng(request).measurements()
