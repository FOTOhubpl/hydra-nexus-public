"""Export endpoints (CORE): abuse/takedown package (XARF v4 ZIP), STIX 2.1, lifecycle timeline, pivot queries,
enriched dataset (ZIP of the `hydra export-dataset` folder).

All read-only; nothing is sent anywhere. Wired into the app with one include line in hydra/api.py.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask

router = APIRouter(prefix="/v1", tags=["exports"])


def _settings(request: Request) -> Any:
    return request.app.state.hydra.settings


def _record(request: Request, kind: str, run_id: str | None, counts: dict[str, int], **extra: Any) -> None:
    from hydra.export_log import record_export
    from hydra.pipeline import latest_run

    if run_id is None:
        with request.app.state.hydra.reader() as conn:
            run_id = latest_run(conn)
    record_export(request.app.state.hydra.writer, kind, run_id, counts, **extra)


def _guard(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc).strip("'")) from exc


@router.get("/campaigns/{campaign_id}/abuse-package")
def abuse_package(request: Request, campaign_id: str, run_id: str | None = None) -> Response:
    """ZIP: XARF v4 reports per host, Polish cover notes, manifest. Never sent automatically."""
    from hydra.export_abuse import abuse_package as build

    data, manifest = _guard(build, _settings(request), campaign_id, run_id)
    _record(request, "abuse_package", manifest.get("run_id") or run_id,
            {"hosts": manifest["hosts"], "reports": len(manifest["reports"])}, campaign_id=campaign_id)
    from hydra.export_safe import content_disposition

    return Response(content=data, media_type="application/zip", headers={
        **content_disposition(f"abuse-{campaign_id}.zip", "abuse.zip"),
        "X-Hydra-Hosts": str(manifest["hosts"]), "X-Hydra-Abuse-Coverage": str(manifest["abuse_contact_coverage"])})


@router.get("/campaigns/{campaign_id}/stix")
def campaign_stix(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    from hydra.export_stix import export_stix

    bundle = _guard(export_stix, _settings(request), campaign_id, run_id)
    _record(request, "stix_campaign", run_id, {"objects": len(bundle.get("objects", []))}, campaign_id=campaign_id)
    return bundle


@router.get("/runs/{run_id}/stix")
def run_stix(request: Request, run_id: str, multi_only: bool = True) -> dict[str, Any]:
    from hydra.export_stix import export_stix

    bundle = _guard(export_stix, _settings(request), None, run_id, multi_only)
    _record(request, "stix_run", run_id, {"objects": len(bundle.get("objects", []))})
    return bundle


# GET /campaigns/{campaign_id}/lifecycle is owned by hydra/api_visual.py (incident Gantt + this domain lifecycle,
# CR-ASTRA-PIPE-401): the second registration here was shadowed and published a duplicate operation id.


@router.get("/runs/{run_id}/lead-times")
def run_lead_times(request: Request, run_id: str) -> dict[str, Any]:
    from hydra.lifecycle import corpus_lead_times

    return _guard(corpus_lead_times, _settings(request), run_id)


@router.get("/campaigns/{campaign_id}/pivots")
def campaign_pivots(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    from hydra.pivot_queries import campaign_pivots as build

    return _guard(build, _settings(request), campaign_id, run_id)


@router.get("/export/dataset")
def export_dataset_zip(request: Request, run_id: str | None = None) -> Response:
    """ZIP of the enriched dataset folder, the same files as `hydra export-dataset` (incidents, genes, enrichment,
    links, assignments, campaigns, groups, README_DATASET.md, manifest.json). Built in a temp dir under
    `settings.tmp_dir`, streamed, then deleted. Organiser material: not for public distribution."""
    import shutil
    import tempfile
    import zipfile
    from pathlib import Path

    from hydra.api_task import resolve_run
    from hydra.export_dataset import export_dataset
    from hydra.export_safe import content_disposition, safe_filename

    settings = _settings(request)
    with request.app.state.hydra.reader() as conn:
        run = resolve_run(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="no analysis run yet")
    tmp = Path(tempfile.mkdtemp(dir=settings.tmp_dir, prefix="dataset-"))
    try:
        res = export_dataset(settings, run, tmp / "dataset")
        safe_run = safe_filename(res["run_id"], "run")  # no path parts or line breaks from a run id (CR-ASTRA-SEC-7)
        archive = tmp / f"hydra-dataset-{safe_run}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted((tmp / "dataset").iterdir()):
                zf.write(f, f"hydra-dataset-{safe_run}/{f.name}")
    except ValueError as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _record(request, "dataset", res["run_id"], res["counts"], format="zip")
    return FileResponse(archive, media_type="application/zip",
                        background=BackgroundTask(shutil.rmtree, tmp, ignore_errors=True),
                        headers={**content_disposition(archive.name, "hydra-dataset.zip"), "X-Hydra-Run": safe_run,
                                 "X-Hydra-Signing-Key": res["signature"]["key_fingerprint"]})


@router.get("/exports/signing-key")
def signing_key(request: Request) -> dict[str, Any]:
    """Fingerprint of this installation's Ed25519 export key (created on first use; compare out of band)."""
    from hydra.evidence_sign import key_info

    return key_info(_settings(request))


@router.post("/exports/verify")
async def verify_export(request: Request) -> dict[str, Any]:
    """Verify an uploaded signed export ZIP (raw request body, application/zip). Same checks as
    `hydra verify-export`; the ZIP is read in a temp file and never extracted. The app is loopback-only."""
    import tempfile
    from pathlib import Path

    from hydra.evidence_sign import verify_package

    settings = _settings(request)
    limit = settings.max_upload_mb * 1024 * 1024
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=settings.tmp_dir, prefix="verify-", suffix=".zip") as tmp:
        n = 0
        async for chunk in request.stream():
            n += len(chunk)
            if n > limit:
                raise HTTPException(status_code=413, detail="package too large")
            tmp.write(chunk)
        tmp.flush()
        res = verify_package(Path(tmp.name), request.query_params.get("fingerprint"))
    res["package"] = "upload.zip"
    return res
