"""HYDRA Watch API (`/v1/watch/*`): live discovery ticker, recorder/replayer, analyst-click fetch.

GET  /v1/watch/status               mode (idle|live|replay + REPLAY flag/file/sha256/speed), counters, sparkline
GET  /v1/watch/events?since=&limit= processed events (newest last), URLs defanged
GET  /v1/watch/stream?since=        Server-Sent Events with the same rows
GET  /v1/watch/alerts?since=        campaign fingerprint alerts (candidates) + lead-time records
GET  /v1/watch/recordings           recording files (name, lines, bytes)
POST /v1/watch/replay {file, speed} replay a recording at speed× (default 60)
POST /v1/watch/live                 follow the recorder's newest file
POST /v1/watch/stop
POST /v1/watch/fetch {event_id}     analyst click: one no-JS GET via hydra.enrich.fetch (live only with
                                    HYDRA_ENRICH=1, i.e. on the server); never automatic
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from hydra.watch.engine import WatchEngine, list_recordings
from hydra.watch.names import defang

router = APIRouter(prefix="/v1/watch", tags=["watch"])
_LOCK = threading.Lock()


def engine(request: Request) -> WatchEngine:
    with _LOCK:
        eng = getattr(request.app.state, "watch", None)
        if eng is None:
            eng = WatchEngine(request.app.state.hydra.settings)
            request.app.state.watch = eng
        return eng


class ReplayIn(BaseModel):
    file: str = Field(min_length=1, max_length=200)
    speed: float = Field(default=60.0, ge=1.0, le=36000.0)


class FetchIn(BaseModel):
    event_id: str = Field(min_length=3, max_length=64)


@router.get("/status")
def status(request: Request) -> dict[str, Any]:
    return engine(request).status()


@router.get("/events")
def events(request: Request, since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    eng = engine(request)
    rows = eng.events_since(since, limit)
    return {"mode": eng.mode, "replay": eng.mode == "replay", "seq": eng.seq, "events": rows}


@router.get("/alerts")
def alerts(request: Request, since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """Campaign fingerprint alerts. Every alert is a CANDIDATE (saved-query match), never a confirmed member."""
    eng = engine(request)
    fps = eng.fingerprints.summary() if eng.fingerprints is not None else None
    return {"mode": eng.mode, "fingerprints": fps, "alerts": eng.alerts_since(since, limit),
            "lead_time": eng.lead[-200:],
            "note_pl": "Alert to kandydat: dopasowanie zapisanego odcisku kampanii, nie potwierdzony członek."}


@router.get("/stream")
async def stream(request: Request, since: int = Query(0, ge=0)) -> StreamingResponse:
    eng = engine(request)

    async def gen():
        last = since
        while not await request.is_disconnected():
            rows = eng.events_since(last, 200)
            for r in rows:
                last = max(last, r["seq"])
                yield f"id: {r['seq']}\nevent: watch\ndata: {json.dumps(r, ensure_ascii=False)}\n\n"
            if not rows:
                yield ": keepalive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/recordings")
def recordings(request: Request) -> dict[str, Any]:
    return {"recordings": list_recordings(request.app.state.hydra.settings)}


@router.post("/replay")
def replay(request: Request, body: ReplayIn) -> dict[str, Any]:
    try:
        return engine(request).start_replay(body.file, body.speed)
    except FileNotFoundError as exc:
        raise HTTPException(404, f"recording not found: {body.file}") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/live")
def live(request: Request) -> dict[str, Any]:
    return engine(request).start_live()


@router.post("/stop")
def stop(request: Request) -> dict[str, Any]:
    eng = engine(request)
    eng.stop()
    return eng.status()


@router.post("/fetch")
def fetch(request: Request, body: FetchIn) -> dict[str, Any]:
    from hydra.enrich.fetch import fetch_url
    from hydra.redact import redact_url

    eng = engine(request)
    ev = eng.by_id.get(body.event_id)
    if ev is None:
        raise HTTPException(404, "unknown event")
    settings = request.app.state.hydra.settings
    url = ev.url or f"http://{ev.host}/"
    res = fetch_url(url, settings)  # live only with HYDRA_ENRICH=1 (server); else cached fetch or "disabled"
    rec = res.record()
    summ = rec.summary
    out = {"event_id": ev.event_id, "url_defanged": defang(url), "status": res.status,
           "live": bool(settings.enrich), "fetched_at": res.fetched_at,
           "final_url_defanged": defang(redact_url(res.final_url) or "") if res.final_url else None,
           "hops": [{"status": s, "url_defanged": defang(redact_url(u) or "")} for s, u in summ.get("chain", [])],
           "white_page": summ.get("white_page"), "server_ips": summ.get("server_ips"),
           "evidence_sha256": rec.response_sha256}
    if res.status == "disabled":
        out["note_pl"] = ("Pobieranie strony wyłączone na tym komputerze (HYDRA_ENRICH=0). Uruchom na serwerze "
                          "analitycznym; strona jest pobierana tylko na kliknięcie analityka, bez JavaScriptu.")
    ev.enrichment = {**(ev.enrichment or {}), "fetch": out}
    return out
