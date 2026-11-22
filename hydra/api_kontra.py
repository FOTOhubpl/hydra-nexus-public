"""KONTRA K1 endpoints ("Laboratorium hipotez"), shadow mode: nothing here changes an analysis or an assignment.

GET  /v1/lab/{incident_id}                 latest stored experiment (404 when none), with the CURRENT `status`:
                                           `ok` or `stale_context` (corpus, background prior or fusion changed since
                                           its frozen context; ASTRA-DELTA-02/10)
POST /v1/lab/{incident_id}/run             run the experiment now (real re-scoring; seconds on a small corpus)
POST /v1/lab/{incident_id}/package         evidence package of the latest experiment (immutable, content-addressed);
                                           409 `stale_context` when that experiment no longer matches the state
GET  /v1/lab/packages/{id}                 package JSON;  /report.md, /report.html  human-readable renderings
POST /v1/lab/packages/{id}/send            deliver to the LOCAL demonstration receiver (literal loopback IP only,
                                           no redirects); `acknowledged`/`duplicate` only for a valid receipt of
                                           this package, else `unconfirmed` (ASTRA-DELTA-03/04)
GET  /v1/lab/receiver                      configured receiver URL (no probe)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from hydra import events
from hydra.kontra import (
    KontraError,
    StaleContextError,
    experiment_status,
    load_experiment,
    run_experiment,
    save_experiment,
    valid_id,
)
from hydra.kontra_package import is_loopback_url, load_package, package_dir, receiver_url, save_package, send_package

router = APIRouter(prefix="/v1/lab", tags=["kontra"])


def _settings(request: Request) -> Any:
    return request.app.state.hydra.settings


def _check(x: str) -> str:
    if not valid_id(x):
        raise HTTPException(400, "invalid id")
    return x


def _audit(request: Request, type_: str, payload: dict[str, Any]) -> None:
    try:
        request.app.state.hydra.writer.run(lambda conn: events.append(conn, type_, payload))
    except Exception:  # noqa: BLE001 — the audit row is best effort; the lab result is already stored
        pass


@router.get("/receiver")
def receiver(request: Request) -> dict[str, Any]:
    url = receiver_url()
    return {"url": url, "loopback": is_loopback_url(url),
            "note_pl": "lokalny odbiornik demonstracyjny; uruchom: uv run python scripts/kontra_receiver.py"}


@router.get("/packages/{package_id}")
def get_package(request: Request, package_id: str) -> dict[str, Any]:
    pkg = load_package(_settings(request), _check(package_id))
    if pkg is None:
        raise HTTPException(404, "no such package")
    return pkg


@router.get("/packages/{package_id}/report.md", response_class=PlainTextResponse)
def get_report_md(request: Request, package_id: str) -> PlainTextResponse:
    f = package_dir(_settings(request), _check(package_id)) / "report.md"
    if not f.is_file():
        raise HTTPException(404, "no such package")
    return PlainTextResponse(f.read_text(encoding="utf-8"), media_type="text/markdown; charset=utf-8")


@router.get("/packages/{package_id}/report.html", response_class=HTMLResponse)
def get_report_html(request: Request, package_id: str) -> HTMLResponse:
    f = package_dir(_settings(request), _check(package_id)) / "report.html"
    if not f.is_file():
        raise HTTPException(404, "no such package")
    return HTMLResponse(f.read_text(encoding="utf-8"), headers={
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
        "X-Content-Type-Options": "nosniff"})


@router.post("/packages/{package_id}/send")
async def send(request: Request, package_id: str) -> dict[str, Any]:
    pkg = load_package(_settings(request), _check(package_id))
    if pkg is None:
        raise HTTPException(404, "no such package")
    out = await run_in_threadpool(send_package, pkg)
    await run_in_threadpool(_audit, request, "KONTRA_DELIVERY", {
        "package_id": package_id, "status": out["status"], "receipt_id": (out.get("ack") or {}).get("receipt_id")})
    return out


@router.get("/{incident_id}")
def get_experiment(request: Request, incident_id: str, missing: str | None = None) -> dict[str, Any] | None:
    """`missing=null`: answer 200 `null` instead of 404 when no experiment exists yet (the UI's empty state)."""
    s = _settings(request)
    exp = load_experiment(s, _check(incident_id))
    if exp is None:
        if missing == "null":
            return None
        raise HTTPException(404, "no experiment for this incident yet")
    return {**exp, **experiment_status(s, exp)}


@router.post("/{incident_id}/run")
async def run(request: Request, incident_id: str) -> dict[str, Any]:
    s = _settings(request)
    try:
        exp = await run_in_threadpool(run_experiment, _check(incident_id), s)
    except KontraError as exc:
        raise HTTPException(404, str(exc)) from exc
    except StaleContextError as exc:
        raise HTTPException(409, {"status": "stale_context", "stale_reasons": exc.reasons}) from exc
    await run_in_threadpool(save_experiment, s, exp)
    await run_in_threadpool(_audit, request, "KONTRA_EXPERIMENT", {
        "incident_id": incident_id, "experiment_id": exp["experiment_id"],
        "fingerprint": exp["context"]["fingerprint"], "recomputations": exp["stats"]["recomputations"]})
    return exp


@router.post("/{incident_id}/package")
async def package(request: Request, incident_id: str) -> dict[str, Any]:
    s = _settings(request)
    exp = load_experiment(s, _check(incident_id))
    if exp is None:
        raise HTTPException(404, "run the experiment first")
    st = await run_in_threadpool(experiment_status, s, exp)
    if st["status"] != "ok":
        raise HTTPException(409, {"status": "stale_context", "stale_reasons": st["stale_reasons"],
                                  "detail_pl": "kontekst eksperymentu jest nieaktualny; uruchom przeliczenie ponownie"})
    out = await run_in_threadpool(save_package, s, exp)
    await run_in_threadpool(_audit, request, "KONTRA_PACKAGE", {
        "incident_id": incident_id, "package_id": out["package_id"], "sha256": out["sha256"]})
    return out
