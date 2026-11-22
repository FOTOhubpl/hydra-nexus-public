"""GALLERY: the local reference base (face gallery, logo templates) built from the operator's own imported material.

- POST /v1/reference/build (admin): background job, progress in the jobs row (total/done), result in the event log;
- GET  /v1/reference/status: counts of the base in use + the last build job;
- GET  /v1/reference/build/{job_id}: one build job with its result;
- POST /v1/reference/reanalyse (analyst): "Przelicz analizę" — re-run the analyse on the stored NASK corpus so the
  ID/logo genes appear (an `analyse` job: poll GET /v1/analyse/{job_id}).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from hydra import events
from hydra.api_task import ANALYSE_EVENT, _run_result_dict, _st
from hydra.jobs import JobContext, create_job, get_job

router = APIRouter(prefix="/v1/reference", tags=["reference"])
BUILD_EVENT = "REFERENCE_BUILT"
EXPLAIN_PL = ("Z zaimportowanych materiałów wyodrębniane są: (1) wektory twarzy (SFace) z obrazów, w których jest"
              " dokładnie jedna twarz dobrej jakości, a sam materiał podaje imię i nazwisko (alt, title, podpis"
              " zdjęcia, nazwa pliku); (2) znaki marek (logo) powtarzające się w co najmniej 2 incydentach, nazwane"
              " w nazwie pliku lub tekście alternatywnym. Nazwy pochodzą wyłącznie z materiałów; role osób publicznych"
              " z pliku persons.json, pozostałe nazwiska to persony kampanii (nie identyfikacja). Baza zostaje"
              " lokalnie w katalogu stanu, nie trafia do repozytorium ani do sieci.")


class BuildBody(BaseModel):
    faces: bool = True
    logos: bool = True


def _with_result(conn: sqlite3.Connection, job: dict[str, Any] | None) -> dict[str, Any] | None:
    if job is None:
        return None
    ev = conn.execute("SELECT payload FROM events WHERE type=? AND json_extract(payload, '$.job_id')=?"
                      " ORDER BY seq DESC LIMIT 1", (BUILD_EVENT, job["id"])).fetchone()
    job["result"] = (json.loads(ev["payload"]) or {}).get("result") if ev else None
    return job


@router.get("/status")
def reference_status(request: Request) -> dict[str, Any]:
    from hydra import reference_build as rb

    st = _st(request)
    with st.reader() as conn:
        row = conn.execute("SELECT id FROM jobs WHERE kind='reference_build' ORDER BY created_at DESC LIMIT 1"
                           ).fetchone()
        job = _with_result(conn, get_job(conn, row["id"])) if row else None
    return {**rb.status(st.settings), "job": job, "explain_pl": EXPLAIN_PL}


@router.get("/build/{job_id}")
def reference_job(request: Request, job_id: str) -> dict[str, Any]:
    with _st(request).reader() as conn:
        job = get_job(conn, job_id)
        if job is None or job["kind"] != "reference_build":
            raise HTTPException(status_code=404, detail="reference build job not found")
        return _with_result(conn, job) or {}


@router.post("/build", status_code=status.HTTP_202_ACCEPTED)
def reference_build(request: Request, body: BuildBody | None = None) -> dict[str, Any]:
    from hydra import reference_build as rb

    st = _st(request)
    b = body or BuildBody()
    with st.reader() as conn:
        running = conn.execute("SELECT id FROM jobs WHERE kind='reference_build' AND status IN ('queued','running')"
                               " LIMIT 1").fetchone()
    if running is not None:
        return {"job_id": running["id"], "already_running": True}
    job_id = create_job(st.writer, "reference_build", {"faces": b.faces, "logos": b.logos, "source": "state"})

    def handler(ctx: JobContext) -> None:
        incs = list(rb.incidents_from_state(st.settings))
        ctx.set_total(max(len(incs) * (int(b.faces) + int(b.logos)), 1))
        done = {"n": 0}

        def progress(i: int, n: int, what: str) -> None:
            ctx.record_item(done["n"], what[:200], "done")
            done["n"] += 1

        if incs:
            res = rb.build(st.settings, incs, faces=b.faces, logos=b.logos, progress=progress)
        else:
            res = {"ok": False, "incidents": 0, "message_pl": rb.EMPTY_PL}
            ctx.record_item(0, "brak materiału", "done")
        res.pop("path", None)  # no local paths in the API answer
        for k in ("faces", "logos"):
            if isinstance(res.get(k), dict):
                res[k].pop("path", None)
        st.writer.run(lambda conn: events.append(conn, BUILD_EVENT, {"job_id": job_id, "result": res}))

    st.runner.submit(job_id, handler)
    return {"job_id": job_id, "already_running": False}


@router.post("/reanalyse", status_code=status.HTTP_202_ACCEPTED)
def reference_reanalyse(request: Request) -> dict[str, Any]:
    """Przelicz analizę: the existing analyse on the same stored corpus (gene cache reused; only ident/logo
    re-extract because the reference fingerprint is part of their cache key)."""
    st = _st(request)
    job_id = create_job(st.writer, "analyse", {"source": "reference_reanalyse", "mode": "corpus", "corpus": "nask"})

    def handler(ctx: JobContext) -> None:
        from hydra.api_cache import warm_async
        from hydra.jury_report import jury_analyse

        result = json.loads(json.dumps(_run_result_dict(jury_analyse(
            [], st.settings, writer=st.writer, corpus="nask", job_id=job_id)), default=str))
        st.writer.run(lambda conn: events.append(conn, ANALYSE_EVENT, {
            "job_id": job_id, "input_kind": "corpus", "assignment": None, "result": result}))
        warm_async(st)

    st.runner.submit(job_id, handler)
    return {"job_id": job_id, "mode": "corpus"}
