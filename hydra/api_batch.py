"""Batch import of new material through the API (GAP-AUDIT B3): the same path as `hydra assign-batch`.

- `POST /v1/assign-batch`: JSON {path, run_id?} (a local folder under the allowed roots: one subfolder or file per
  item) or multipart `files` (each top-level file or folder is one item; a single dropped folder is the batch
  folder and its entries are the items). Returns a job id.
- `GET /v1/assign-batch/{key}`: job progress, then the batch report (`hydra.batch`): per item decision, new pattern
  id, duplicate, and the new patterns of the batch with members and reasons. `key` = job id or batch run id.
- `GET /v1/assign-batches`: recent batch runs with their summary.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from hydra.jobs import JobContext, create_job, get_job

router = APIRouter(prefix="/v1", tags=["batch"])
JOB_KIND = "assign_batch"


class BatchJson(BaseModel):
    path: str
    run_id: str | None = None


def _st(request: Request) -> Any:
    return request.app.state.hydra


def _submit(st: Any, items: list[Path], base_run: str | None, params: dict[str, Any],
            cleanup: Path | None = None) -> str:
    job_id = create_job(st.writer, JOB_KIND, params)

    def handler(ctx: JobContext) -> None:
        from hydra.batch import assign_batch

        try:
            ctx.set_total(len(items))

            def _on_item(i: int, _n: int, row: dict[str, Any]) -> None:
                ctx.record_item(i - 1, str(row.get("item")), "failed" if row.get("decision") == "error" else "done",
                                detail=str(row.get("decision")))

            assign_batch(items, st.settings, base_run=base_run, writer=st.writer, job_id=job_id, on_item=_on_item)
        finally:
            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)

    st.runner.submit(job_id, handler)
    return job_id


@router.post("/assign-batch", status_code=status.HTTP_202_ACCEPTED)
async def assign_batch(request: Request) -> dict[str, Any]:
    """Assign a batch of new items against the latest (or given) analyse run and group the new patterns."""
    from hydra.api_task import _spool_uploads, allowed_local_path
    from hydra.batch import batch_items

    st = _st(request)
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        uploads = [v for v in form.getlist("files") if isinstance(v, UploadFile)]
        base_run = str(form.get("run_id") or "") or None
        if not uploads:
            raise HTTPException(status_code=400, detail="no files in field 'files'")
        skipped: list[dict[str, str]] = []
        try:
            spool = await run_in_threadpool(_spool_uploads, st, uploads, skipped)
        finally:
            await form.close()
        root = spool / "incident"
        top = batch_items(root) if root.exists() else []
        if len(top) == 1 and top[0].is_dir():  # one dropped folder = the batch folder: its entries are the items
            root = top[0]
        items = batch_items(root) if root.exists() else []
        if not items:
            shutil.rmtree(spool, ignore_errors=True)
            raise HTTPException(status_code=400, detail={"message": "brak plików z danymi", "skipped": skipped})
        job_id = await run_in_threadpool(_submit, st, items, base_run,
                                         {"source": "upload", "items": len(items), "skipped": skipped}, spool)
        return {"job_id": job_id, "status": "queued", "items": len(items), "skipped": skipped}
    if ctype.split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="send multipart/form-data files or application/json {path}")
    try:
        body = BatchJson.model_validate(await request.json())
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=400, detail="send JSON {path} or multipart 'files'") from exc
    local = allowed_local_path(st.settings, body.path)
    if not local.is_dir():
        raise HTTPException(status_code=400, detail="path must be a folder: one subfolder or file per item")
    items = batch_items(local)
    if not items:
        raise HTTPException(status_code=400, detail="the folder holds no items")
    job_id = await run_in_threadpool(_submit, st, items, body.run_id,
                                     {"source": "path", "items": len(items), "path": local.name})
    return {"job_id": job_id, "status": "queued", "items": len(items)}


@router.get("/assign-batch/{key}")
def assign_batch_result(request: Request, key: str) -> dict[str, Any]:
    from hydra.batch import load_batch

    with _st(request).reader() as conn:
        job = get_job(conn, key)
        if job is not None and job["kind"] != JOB_KIND:
            job = None
        report = load_batch(conn, key)
        if report is not None and job is None and report.get("job_id"):
            job = get_job(conn, report["job_id"])
    if job is None and report is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if job is not None and job.get("error"):
        from hydra.api_task import safe_ref

        job["error"] = safe_ref(job["error"])
    return {"job": job, "report": report}


@router.get("/assign-batches")
def list_batches(request: Request, limit: int = Query(20, ge=1, le=200)) -> dict[str, Any]:
    import json

    with _st(request).reader() as conn:
        rows = conn.execute("SELECT id, params, metrics, created_at FROM runs WHERE kind='assign_batch'"
                            " ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    items = []
    for r in rows:
        params, metrics = json.loads(r["params"]), json.loads(r["metrics"])
        items.append({"batch_run": r["id"], "created_at": r["created_at"], "base_run": params.get("base_run"),
                      "job_id": params.get("job_id"), "summary": metrics.get("summary"),
                      "decisions": metrics.get("decisions")})
    return {"items": items, "total": len(items)}
