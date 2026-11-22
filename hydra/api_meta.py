"""META-COLLECT API (`/v1/watch/rt/meta/*`): the Meta Ad Library collector inside WATCH-RT (docs/integrations/
META_COLLECTION.md). The schedule, poll-now and the source enable flag are the WATCH-RT source `meta-adlibrary`
(one owner); the token is the existing Integrations secret (never accepted or returned here).

GET  /status?mode=            collector, token status, own budget, queue, separate counters, contract, config
GET  /config  PUT /config     countries, queries, date window, budgets, opt-in media / landing fetch, expansion
POST /diagnose                layered contract diagnostics (admin; counts against the budget)
POST /collector {action}      start | pause | resume | poll_now (admin)
GET  /ads?mode=&view=&q=&before=&limit=    ads with current revision and analysis summary
GET  /ads/{ad_id}?mode=       revisions, artifacts, queries, analyses
POST /ads/{ad_id}/analyse     enqueue / resume (idempotent per revision and context; analyst)
POST /ads/{ad_id}/attach      analyst file -> new revision + analysis (analyst)
GET  /queue?mode=             outbox jobs
GET  /analyses/{id}           correlation, fraud indications, manipulation, review history
POST /analyses/{id}/review    confirmed_by_analyst | dismissed_by_analyst | unreviewed (analyst)
GET  /analyses/{id}/report?format=json|md   single-ad observation report
GET  /report?mode=&since=&until=&format=    period report (same representation)
GET  /ads/{ad_id}/scheme?mode=              P1 "nowa reklama, ten sam schemat" (reads stored analyses only)
GET  /auto?mode=                  BONUS-AUTO "Automatyczne wyszukiwanie": sources, rules, schedule, budget, cycles,
                                  decisions, triage distribution, ads linked to known campaigns, WATCH-RT matches
GET  /campaigns/{id}/links?run=   new Meta ads assigned to this campaign + WATCH-RT matches of its indicators
Role rules (api_auth._ADMIN_WRITE + require_role here): configuration, diagnostics and collector control = admin;
analyse / attach / review = analyst; viewer reads only. Long work returns the outbox job, never a blocking result.
"""

from __future__ import annotations

import time
from contextlib import closing
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from hydra.api_watch_rt import eng, require_role
from hydra.db import connect
from hydra.meta import contract, store
from hydra.meta import report as rep
from hydra.watch.rt.sources import SourceError

router = APIRouter(prefix="/v1/watch/rt/meta", tags=["meta-collect"])
SID = "meta-adlibrary"
Mode = Literal["live", "recorded", "synthetic"]
STATE_PL = {
    "disabled": "Wyłączony", "idle": "Oczekuje", "running": "Działa", "paused": "Wstrzymany",
    "deferred_budget": "Wstrzymany do wznowienia (własny budżet)", "rate_limited": "Odroczony (limit Meta)",
    "needs_config": "Wymaga konfiguracji", "offline": "Offline", "error": "Błąd",
    "engine_stopped": "Silnik WATCH-RT wyłączony", "replay": "Odtworzenie",
}


def _st(request: Request) -> Any:
    return request.app.state.hydra


def _reader(request: Request):
    return closing(connect(_st(request).settings.db_path, readonly=True))


def _ensure(request: Request) -> None:
    _st(request).writer.run(store.ensure_schema)


def _analyst(request: Request) -> str:
    p = request.scope.get("hydra.principal")
    return getattr(p, "username", None) or "operator"


class QueryIn(BaseModel):
    kind: Literal["keyword", "page_ids"] = "keyword"
    value: str = Field(min_length=1, max_length=200)
    label: str | None = Field(default=None, max_length=200)
    enabled: bool = True


class ExpansionIn(BaseModel):
    enabled: bool | None = None
    max_depth: int | None = Field(default=None, ge=0, le=3)
    max_pages: int | None = Field(default=None, ge=0, le=50)


class RulesIn(BaseModel):
    keywords: list[str] | None = Field(default=None, max_length=50)
    celebrities: list[str] | None = Field(default=None, max_length=50)
    page_name_patterns: list[str] | None = Field(default=None, max_length=50)
    same_text_min_pages: int | None = Field(default=None, ge=2, le=20)
    alert_min_priority: Literal["low", "medium", "high"] | None = None


class ConfigIn(BaseModel):
    countries: list[str] | None = Field(default=None, max_length=10)
    queries: list[QueryIn] | None = Field(default=None, max_length=50)
    ad_active_status: Literal["ALL", "ACTIVE", "INACTIVE"] | None = None
    languages: list[str] | None = Field(default=None, max_length=10)
    date_window_days: int | None = None
    overlap_days: int | None = None
    budget_per_h: int | None = None
    max_active_queries: int | None = None
    max_pages_per_query: int | None = None
    interval_s: int | None = None
    max_analyses_per_h: int | None = None
    max_media_bytes: int | None = None
    media_fetch: bool | None = None
    landing_fetch: bool | None = None
    expansion: ExpansionIn | None = None
    date_from: str | None = Field(default=None, max_length=10)  # "" clears
    date_to: str | None = Field(default=None, max_length=10)  # "" clears
    platforms: list[str] | None = Field(default=None, max_length=7)
    rules: RulesIn | None = None


class CollectorIn(BaseModel):
    action: Literal["start", "pause", "resume", "poll_now"]


class ReviewIn(BaseModel):
    decision: Literal["confirmed_by_analyst", "dismissed_by_analyst", "unreviewed"]
    note: str | None = Field(default=None, max_length=1000)


def _public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    out = dict(cfg)
    out["queries"] = [{k: q[k] for k in ("id", "kind", "value", "label", "enabled", "origin", "last_status",
                                         "last_run", "depth", "parent_ad_id")} | {"last_error_pl": q["last_error"]}
                      for q in cfg["queries"] if q["origin"] != "replay"]
    return out


def status_view(request: Request, mode: str) -> dict[str, Any]:
    from hydra import netguard
    from hydra.integrations.secrets import meta_token_status
    from hydra.meta import transport

    s = _st(request).settings
    e = eng(request)
    now = time.time()
    with _reader(request) as conn:
        cfg = store.config(conn)
        src = conn.execute("SELECT enabled, state, next_due, last_poll, last_ok, last_error, interval_s FROM "
                           "watch_sources WHERE id=?", (SID,)).fetchone()
        last = conn.execute("SELECT status, reason, finished_at FROM meta_polls WHERE mode='live' AND status != "
                            "'running' ORDER BY id DESC LIMIT 1").fetchone()
        ok_at = conn.execute("SELECT MAX(finished_at) FROM meta_polls WHERE mode=? AND status IN ('ok', 'empty', "
                             "'partial')", (mode,)).fetchone()[0]
        out: dict[str, Any] = {"mode": mode, "mode_label_pl": store.MODE_PL[mode], "queue": store.queue_counts(conn,
                                                                                                             mode),
                               "counters": store.counters(conn, mode),
                               "contract": {"graph_api_version": contract.graph_version(),
                                            "verified_on": contract.VERIFIED_ON,
                                            "last_diagnostics": store.get_state(conn, "diagnostics", None)},
                               "config": _public_config(cfg), "polls": store.last_polls(conn, mode, 10)}
        paused = bool((store.get_state(conn, "collector", {}) or {}).get("paused"))
    tok = meta_token_status()
    tstate = transport.load_state(s)
    enabled = bool(src and src[0])
    offline = netguard.offline(s)
    if mode != "live":
        state, msg = "replay", "Widok danych z odtworzenia: " + store.MODE_PL[mode]
    elif offline:
        state, msg = "offline", transport.MSG_PL["offline"]
    elif not tok["configured"]:
        state, msg = "needs_config", transport.MSG_PL["needs_config"]
    elif tstate.get("token_rejected"):
        st = tstate.get("token_rejected_status") or "token_invalid"
        state, msg = "needs_config", transport.MSG_PL.get(st, transport.MSG_PL["token_invalid"])
    elif not enabled:
        state, msg = ("paused" if paused else "disabled"), ("Kolektor wstrzymany przez operatora." if paused else
                                                            "Kolektor wyłączony. Kliknij Uruchom.")
    elif e.state == "stopped":
        state, msg = "engine_stopped", "Silnik WATCH-RT jest wyłączony: kliknij Uruchom, aby wznowić harmonogram."
    elif last and last[0] in ("deferred_budget", "rate_limited", "needs_config", "error"):
        state, msg = last[0], last[1]
    else:
        state, msg = ("running" if e.state == "live" else "idle"), None
    out["collector"] = {"state": state, "state_pl": STATE_PL.get(state, state), "message_pl": msg,
                        "enabled": enabled, "engine_state": e.state, "next_due": src[2] if src else None,
                        "last_poll": src[3] if src else None, "last_success": ok_at,
                        "last_error_pl": (src[5] if src else None) or (last[1] if last and last[0] != "ok" else None),
                        "interval_s": int(cfg["interval_s"])}
    out["token"] = {"configured": tok["configured"], "source": tok["source"], "last4": tok["last4"]}
    out["budget"] = transport.budget_view(s, int(cfg["budget_per_h"]), now)
    out["offline"] = offline
    out["media_fetch_enabled"] = bool(cfg.get("media_fetch"))
    return out


@router.get("/status", summary="Stan kolektora Meta / Meta collector status")
async def status(request: Request, mode: Mode = "live") -> dict[str, Any]:
    _ensure(request)
    return await run_in_threadpool(status_view, request, mode)


@router.get("/config", summary="Konfiguracja kolektora / Collector configuration")
async def get_config(request: Request) -> dict[str, Any]:
    _ensure(request)
    with _reader(request) as conn:
        return _public_config(store.config(conn))


@router.put("/config", summary="Zmień konfigurację / Update configuration (admin)")
async def put_config(request: Request, body: ConfigIn) -> dict[str, Any]:
    require_role(request, "admin")
    _ensure(request)
    patch = body.model_dump(exclude_none=True)
    if body.queries is not None:
        patch["queries"] = [q.model_dump() for q in body.queries]
    try:
        cfg = _st(request).writer.run(lambda c: store.put_config(c, patch, time.time(), actor=_analyst(request)))
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid", "message_pl": str(exc)}) from exc
    if body.interval_s is not None:
        await run_in_threadpool(eng(request).set_source, SID, interval_s=int(cfg["interval_s"]))
    return _public_config(cfg)


@router.post("/diagnose", summary="Diagnostyka kontraktu / Layered contract diagnostics (admin)")
async def diagnose(request: Request) -> dict[str, Any]:
    require_role(request, "admin")
    from hydra.meta import diagnose as dg

    st = _st(request)
    return await run_in_threadpool(dg.run, st.settings, st.writer)


@router.post("/collector", summary="Start / pauza / wznowienie / skanuj teraz (admin)")
async def collector(request: Request, body: CollectorIn) -> dict[str, Any]:
    require_role(request, "admin")
    _ensure(request)
    e = eng(request)
    w = _st(request).writer
    run = None
    if body.action in ("start", "resume"):
        with _reader(request) as conn:
            interval = int(store.config(conn)["interval_s"])
        w.run(lambda c: store.set_state(c, "collector", {"paused": False}, time.time()))
        await run_in_threadpool(e.set_source, SID, enabled=True, interval_s=interval)
        if e.state == "stopped":
            try:
                e.start()
            except RuntimeError as exc:
                raise HTTPException(409, {"status": "locked", "message_pl": str(exc)}) from exc
        elif e.state == "paused":
            e.resume()
        e.start_meta_worker()
    elif body.action == "pause":
        w.run(lambda c: store.set_state(c, "collector", {"paused": True}, time.time()))
        await run_in_threadpool(e.set_source, SID, enabled=False)
    else:
        if e.state == "replay":
            raise HTTPException(409, {"status": "replay", "message_pl": "Trwa odtworzenie: zatrzymaj je, aby "
                                                                       "odpytać Meta na żywo."})
        try:
            run = await run_in_threadpool(e.poll_source, SID, force=True, replay=False)
        except SourceError as exc:
            raise HTTPException(409, {"status": exc.status, "message_pl": exc.message_pl}) from exc
        e.start_meta_worker()
    out = await run_in_threadpool(status_view, request, "live")
    out["run"] = run
    return out


@router.get("/ads", summary="Reklamy i rewizje / Ads with revisions")
async def ads(request: Request, mode: Mode = "live",
              view: Literal["all", "unprocessed", "new_revisions", "alerts"] = "all",
              q: str | None = Query(None, max_length=200), before: str | None = Query(None, max_length=200),
              limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
    _ensure(request)
    with _reader(request) as conn:
        return store.list_ads(conn, mode, view=view, q=q, before=before, limit=limit)


@router.get("/ads/{ad_id}", summary="Szczegóły reklamy / Ad detail")
async def ad(request: Request, ad_id: str, mode: Mode = "live") -> dict[str, Any]:
    _ensure(request)
    with _reader(request) as conn:
        d = store.ad_detail(conn, mode, ad_id)
    if d is None:
        raise HTTPException(404, "nieznana reklama")
    return d


@router.post("/ads/{ad_id}/analyse", summary="Zleć / wznów analizę (analyst)")
async def analyse(request: Request, ad_id: str, mode: Mode = "live") -> dict[str, Any]:
    require_role(request, "analyst")
    _ensure(request)
    try:
        job, created = _st(request).writer.run(lambda c: store.request_analysis(c, mode, ad_id, time.time()))
    except KeyError as exc:
        raise HTTPException(404, "nieznana reklama") from exc
    w = eng(request).start_meta_worker()
    if w is not None:
        w.wake()
    return {"job": job, "created": created}


@router.post("/ads/{ad_id}/attach", summary="Dołącz plik analityka / Attach an analyst file (analyst)")
async def attach(request: Request, ad_id: str, mode: Mode = "live") -> dict[str, Any]:
    require_role(request, "analyst")
    _ensure(request)
    from hydra.meta import media

    form = await request.form()
    up = form.get("file")
    if not isinstance(up, UploadFile):
        raise HTTPException(422, {"code": "invalid", "message_pl": "Brak pliku (pole `file`)."})
    with _reader(request) as conn:
        limit = int(store.config(conn)["max_media_bytes"]) or 25_000_000
    data = await up.read(limit + 1)
    st = _st(request)
    try:
        rid, _dg = await run_in_threadpool(media.attach, st.settings, st.writer, mode=mode, ad_id=ad_id, data=data,
                                           filename=up.filename or "plik", now=time.time(), max_bytes=limit)
        job, _created = st.writer.run(lambda c: store.request_analysis(c, mode, ad_id, time.time()))
    except KeyError as exc:
        raise HTTPException(404, "nieznana reklama") from exc
    except ValueError as exc:
        raise HTTPException(422, {"code": "invalid_media", "message_pl": str(exc)}) from exc
    w = eng(request).start_meta_worker()
    if w is not None:
        w.wake()
    return {"revision_id": rid, "job": job}


@router.get("/queue", summary="Kolejka analiz / Analysis outbox")
async def queue(request: Request, mode: Mode | None = None, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
    _ensure(request)
    with _reader(request) as conn:
        return {"items": store.queue(conn, mode, limit), "counts": store.queue_counts(conn, mode)}


@router.get("/analyses/{aid}", summary="Wynik analizy / Analysis result")
async def analysis(request: Request, aid: int) -> dict[str, Any]:
    _ensure(request)
    with _reader(request) as conn:
        a = store.analysis(conn, aid)
    if a is None:
        raise HTTPException(404, "nieznana analiza")
    a.pop("idem_key", None)
    a["coverage_pl"] = a["correlation"].get("coverage_pl") or a.get("reason_pl")
    return a


@router.post("/analyses/{aid}/review", summary="Decyzja analityka / Analyst decision (analyst)")
async def review(request: Request, aid: int, body: ReviewIn) -> dict[str, Any]:
    require_role(request, "analyst")
    ok = _st(request).writer.run(lambda c: store.review(c, aid, body.decision, _analyst(request), body.note,
                                                        time.time()))
    if not ok:
        raise HTTPException(404, "nieznana analiza")
    with _reader(request) as conn:
        a = store.analysis(conn, aid)
    assert a is not None
    a.pop("idem_key", None)
    eng(request).emit("meta.analysis", {"analysis_id": aid, "review_status": body.decision})
    return a


def _fmt(data: dict[str, Any], fmt: str, name: str) -> Any:
    if fmt == "md":
        return PlainTextResponse(rep.to_markdown(data), media_type="text/markdown; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.md"'})
    return data


@router.get("/analyses/{aid}/report", summary="Raport reklamy / Single-ad report")
async def ad_report(request: Request, aid: int, format: Literal["json", "md"] = "json") -> Any:  # noqa: A002
    _ensure(request)
    with _reader(request) as conn:
        r = rep.ad_report(conn, aid)
    if r is None:
        raise HTTPException(404, "nieznana analiza")
    return _fmt(r, format, r["report_id"] or f"meta-{aid}")


@router.get("/report", summary="Raport okresu / Period report")
async def period_report(request: Request, mode: Mode = "live", since: str | None = Query(None, max_length=40),
                        until: str | None = Query(None, max_length=40),
                        format: Literal["json", "md"] = "json") -> Any:  # noqa: A002
    _ensure(request)
    with _reader(request) as conn:
        r = rep.period_report(conn, mode, since, until)
    return _fmt(r, format, f"meta-{mode}-report")


@router.get("/ads/{ad_id}/scheme", summary="Nowa reklama, ten sam schemat (P1)")
async def scheme(request: Request, ad_id: str, mode: Mode = "live") -> dict[str, Any]:
    _ensure(request)
    from hydra.meta import scheme as sch

    with _reader(request) as conn:
        r = sch.compare(conn, mode, ad_id)
    if r is None:
        raise HTTPException(404, "nieznana reklama")
    return r


@router.get("/auto", summary="Automatyczne wyszukiwanie: pełny obieg / Automatic search overview (BONUS-AUTO)")
async def auto_overview(request: Request, mode: Mode = "live") -> dict[str, Any]:
    from hydra.meta import auto

    _ensure(request)
    with _reader(request) as conn:
        out = auto.overview(conn, mode)
    st = status_view(request, mode)
    out["collector"] = st["collector"]
    out["budget"] = st["budget"]
    out["offline"] = st["offline"]
    out["token_configured"] = st["token"]["configured"]
    e = eng(request)
    out["engine"] = {"state": e.state, "replay": bool(getattr(e, "replay", None))}
    return out


@router.get("/campaigns/{campaign_id}/links", summary="Nowe materiały powiązane z kampanią (BONUS-AUTO)")
async def campaign_links(request: Request, campaign_id: str, run: str | None = Query(None, max_length=200)
                         ) -> dict[str, Any]:
    from hydra.meta import auto

    _ensure(request)
    with _reader(request) as conn:
        return auto.campaign_links(conn, campaign_id[:200], run)
