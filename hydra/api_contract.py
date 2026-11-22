"""External integration contract (G5-CONTRACT, docs/INTEGRATION_CONTRACT.md): the stable, versioned surface an
outside system uses. Everything else it needs already exists and is only referenced here.

    POST /v1/analyse                 submit material (existing; `Idempotency-Key` header handled by this module)
    GET  /v1/analyse/{id}/progress   progress snapshot (existing, hydra/api_progress.py)
    GET  /v1/jobs/{id}/result        stable result envelope, `schema_version` "1" (this module)
    GET  /v1/jobs/{id}/report        Markdown report of a finished job (this module)
    GET  /v1/jobs/{id}/package       Ed25519-signed ZIP: result.json + evidence.json + report.md (this module)
    GET  /v1/exports/signing-key     key fingerprint to pin (existing, hydra/api_exports.py)
    POST /v1/exports/verify          server-side verification of a package (existing)
    webhooks `job` event             HMAC-signed delivery with retry/dead letter (existing, hydra/integrations)

Idempotency: a client may send `Idempotency-Key` (1-128 chars of `A-Za-z0-9._:-`) on `POST /v1/analyse`. The key is
scoped to the authenticated user (sha256 of user id + key, the raw key is never stored). The first request creates
the job and stores the key hash and a sha256 of the request (JSON body, or every uploaded file's bytes) in the job's
params. A repeat with the same key and the same request returns the same `job_id` (202, header
`Idempotency-Replayed: true`) and starts no second job; the same key with a different request is 409
`idempotency_conflict`. A per-key lock makes a retry that races the first (client timeout) wait for it instead of
creating a duplicate. No DB migration: the jobs table already stores params as JSON.

No scoring code is touched: the result envelope is a read-only projection of `GET /v1/analyse/{id}`.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from hydra import __version__
from hydra.events import canonical_json

router = APIRouter(prefix="/v1/jobs", tags=["contract"])

RESULT_SCHEMA = "hydra.job_result.v1"
SCHEMA_VERSION = "1"
SUPPORTED_VERSIONS = (SCHEMA_VERSION,)
IDEMPOTENCY_HEADER = "Idempotency-Key"
_KEY_RE = re.compile(r"[A-Za-z0-9._:\-]{1,128}")
DISCLAIMER_PL = ("Wynik to ocena heurystyczna: materiał podejrzany ze wspólnymi śladami, do weryfikacji, nie dowód "
                 "sprawstwa. Bity są miarą siły dowodów, nie prawdopodobieństwem.")
DISCLAIMER_EN = ("Heuristic assessment: suspicious material with shared traces, to be verified; not proof of "
                 "authorship. Bits measure evidence strength, they are not probabilities.")
EVIDENCE_GENES_CAP = 500


def _err(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


# ------------------------------------------------------------------------------------------ idempotency

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def idempotency_key(request: Request) -> str | None:
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if raw is None:
        return None
    if not _KEY_RE.fullmatch(raw):
        raise _err(400, "invalid_idempotency_key",
                   "Idempotency-Key: 1-128 znaków A-Z a-z 0-9 . _ : - / 1-128 chars of A-Z a-z 0-9 . _ : -")
    principal = request.scope.get("hydra.principal")
    owner = str(principal.user_id) if principal is not None else "anonymous"
    return hashlib.sha256(f"{owner}\x00{raw}".encode()).hexdigest()


def request_hash_json(body: Any) -> str:
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def request_hash_tree(root: Path, kind: str) -> str:
    """sha256 over (relative path, sha256 of bytes) of every spooled upload + the form `kind`."""
    h = hashlib.sha256(f"multipart\x00{kind}\x00".encode())
    for p in sorted(x for x in root.rglob("*") if x.is_file()):
        fh = hashlib.sha256()
        with p.open("rb") as f:
            while chunk := f.read(1 << 20):
                fh.update(chunk)
        h.update(f"{p.relative_to(root).as_posix()}\x00{fh.hexdigest()}\n".encode())
    return h.hexdigest()


def _find(st: Any, key_sha: str) -> dict[str, Any] | None:
    with st.reader() as conn:
        row = conn.execute(
            "SELECT id, status, params FROM jobs WHERE kind='analyse'"
            " AND json_extract(params, '$.idempotency.key_sha256')=? ORDER BY created_at LIMIT 1",
            (key_sha,)).fetchone()
    if row is None:
        return None
    return {"job_id": row["id"], "status": row["status"], "params": json.loads(row["params"] or "{}")}


def submit_idempotent(st: Any, key_sha: str | None, req_sha: str, response: dict[str, Any],
                      submit: Callable[[dict[str, Any]], str], cleanup: Path | None = None) -> JSONResponse | dict:
    """Run `submit(extra_params)` at most once per key. Returns the 202 body (or a replay JSONResponse)."""
    if key_sha is None:
        return {"job_id": submit({}), "status": "queued", **response}
    with _locks_guard:
        lock = _locks.setdefault(key_sha, threading.Lock())
    with lock:
        prior = _find(st, key_sha)
        if prior is not None:
            if cleanup is not None:
                shutil.rmtree(cleanup, ignore_errors=True)
            idem = prior["params"].get("idempotency") or {}
            if idem.get("request_sha256") != req_sha:
                raise _err(409, "idempotency_conflict",
                           "Ten Idempotency-Key użyto już dla innego żądania. / This Idempotency-Key was already "
                           "used for a different request.", job_id=prior["job_id"])
            body = {"job_id": prior["job_id"], "status": prior["status"], **(idem.get("response") or {}),
                    "replayed": True}
            return JSONResponse(body, status_code=202, headers={
                "Idempotency-Replayed": "true", "Location": f"/v1/jobs/{prior['job_id']}/result"})
        job_id = submit({"idempotency": {"key_sha256": key_sha, "request_sha256": req_sha, "response": response}})
    return {"job_id": job_id, "status": "queued", **response}


# ----------------------------------------------------------------------------------------- result envelope


def _st(request: Request) -> Any:
    return request.app.state.hydra


def _check_version(request: Request) -> None:
    wanted = request.query_params.get("schema_version") or request.headers.get("Accept-Version")
    if wanted is not None and wanted.strip() not in SUPPORTED_VERSIONS:
        raise _err(406, "unsupported_schema_version",
                   f"Obsługiwane wersje schematu / supported schema versions: {', '.join(SUPPORTED_VERSIONS)}",
                   supported=list(SUPPORTED_VERSIONS))


def _candidate(c: dict[str, Any]) -> dict[str, Any]:
    bp = c.get("best_pair") or {}
    return {"campaign_id": c.get("campaign_id"), "probability": c.get("probability"), "support": c.get("support"),
            "best_pair_bits": bp.get("total_bits")}


def build_result(request: Request, job_id: str) -> dict[str, Any]:
    from hydra.api_task import analyse_result
    from hydra.jobs import get_job

    with _st(request).reader() as conn:
        job = get_job(conn, job_id)
    if job is None:
        raise _err(404, "job_not_found", "Nie ma takiego zadania. / No such job.")
    status = job["status"]
    out: dict[str, Any] = {
        "schema": RESULT_SCHEMA, "schema_version": SCHEMA_VERSION, "product_version": __version__,
        "job_id": job_id, "kind": job["kind"], "status": status, "terminal": status in ("done", "failed"),
        "progress": {"total": job.get("total", 0), "done": job.get("done", 0), "failed": job.get("failed", 0)},
        "created_at": job.get("created_at"), "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"), "error": None, "result": None, "evidence": None,
        "links": {"self": f"/v1/jobs/{job_id}/result", "progress": f"/v1/analyse/{job_id}/progress",
                  "report": f"/v1/jobs/{job_id}/report", "package": f"/v1/jobs/{job_id}/package",
                  "verify": "/v1/exports/verify", "signing_key": "/v1/exports/signing-key"},
        "disclaimer": {"pl": DISCLAIMER_PL, "en": DISCLAIMER_EN},
    }
    if status == "failed":
        from hydra.api_task import safe_ref

        out["error"] = {"code": job.get("error_code") or "FAILED", "message": safe_ref(job.get("error"))}
    if job["kind"] != "analyse" or status != "done":
        return out
    full = analyse_result(request, job_id)
    a = full.get("assignment") or {}
    res = full.get("result") or {}
    live = full.get("live") or {}
    params = full["job"].get("params") or {}
    out["result"] = {
        "mode": params.get("input_kind") or params.get("mode") or params.get("source"),
        "run_id": res.get("run_id") or live.get("assign_run_id"),
        "incident_id": a.get("incident_id") or res.get("incident_id") or live.get("incident_id"),
        "decision": a.get("decision"), "campaign_id": a.get("campaign_id"), "group_id": a.get("group_id"),
        "reason_pl": a.get("reason_pl"), "model": a.get("model"),
        "candidates": [_candidate(c) for c in (a.get("candidates") or [])[:10]],
        "metrics": res.get("metrics") if isinstance(res.get("metrics"), dict) else None,
    }
    out["evidence"] = {"genes_total": live.get("genes_total", 0), "genes_by_family": live.get("genes_by_family", {}),
                       "artifacts_by_role": live.get("artifacts_by_role", {}),
                       "pairs_scored": live.get("pairs_scored"),
                       "top_pairs": [{k: p.get(k) for k in ("other", "probability", "total_bits", "hypothesis")}
                                     for p in live.get("top_pairs") or []]}
    return out


@router.get("/{job_id}/result", summary="Wynik zadania (schemat v1) / Versioned job result")
def job_result(request: Request, job_id: str) -> JSONResponse:
    """Stable envelope for external clients. 200 for every known job (check `terminal`/`status`); `result` and
    `evidence` are filled once an analyse job is `done`. `schema_version` changes only on a breaking change; new
    optional fields may be added within a version. `?schema_version=` / `Accept-Version:` other than 1 -> 406."""
    _check_version(request)
    body = build_result(request, job_id)
    return JSONResponse(body, headers={"X-Hydra-Schema": RESULT_SCHEMA, "Cache-Control": "no-store"})


def _done(request: Request, job_id: str) -> dict[str, Any]:
    body = build_result(request, job_id)
    if body["kind"] != "analyse":
        raise _err(409, "not_an_analyse_job", "Raport i pakiet tylko dla zadań analizy. / Analyse jobs only.")
    if body["status"] != "done":
        raise _err(409, "job_not_finished", "Zadanie jeszcze się nie zakończyło (lub nie powiodło się). / The job "
                   "has not finished successfully.", job_status=body["status"])
    return body


def render_report(body: dict[str, Any]) -> str:
    r = body["result"] or {}
    ev = body["evidence"] or {}
    lines = [f"# Raport HYDRA NEXUS: zadanie {body['job_id']}", "",
             f"- schemat / schema: `{body['schema']}` (wersja {body['schema_version']})",
             f"- wersja produktu / product version: {body['product_version']}",
             f"- tryb / mode: {r.get('mode')}", f"- przebieg / run: `{r.get('run_id')}`",
             f"- incydent / incident: `{r.get('incident_id')}`",
             f"- decyzja / decision: **{r.get('decision')}**, kampania / campaign: `{r.get('campaign_id')}`",
             f"- zakończone / finished: {body.get('finished_at')}", ""]
    if r.get("reason_pl"):
        lines += ["## Uzasadnienie", "", str(r["reason_pl"]), ""]
    if r.get("candidates"):
        lines += ["## Kandydaci / Candidates", "", "| kampania | p | wsparcie | bity najlepszej pary |",
                  "|---|---|---|---|"]
        lines += [f"| `{c['campaign_id']}` | {c['probability']} | {c['support']} | {c['best_pair_bits']} |"
                  for c in r["candidates"]]
        lines.append("")
    lines += ["## Dowody / Evidence", "", f"- geny / genes: {ev.get('genes_total', 0)}"]
    for fam, by_status in sorted((ev.get("genes_by_family") or {}).items()):
        lines.append(f"  - {fam}: " + ", ".join(f"{k} {v}" for k, v in sorted(by_status.items())))
    if ev.get("top_pairs"):
        lines += ["", "| powiązany incydent | p | bity | hipoteza |", "|---|---|---|---|"]
        lines += [f"| `{p['other']}` | {p['probability']} | {p['total_bits']} | {p['hypothesis']} |"
                  for p in ev["top_pairs"]]
    lines += ["", "## Zastrzeżenie / Disclaimer", "", DISCLAIMER_PL, "", DISCLAIMER_EN, ""]
    return "\n".join(lines)


@router.get("/{job_id}/report", response_class=PlainTextResponse, summary="Raport zadania / Job report (Markdown)")
def job_report(request: Request, job_id: str) -> PlainTextResponse:
    return PlainTextResponse(render_report(_done(request, job_id)), media_type="text/markdown; charset=utf-8")


def _evidence_genes(request: Request, incident_id: str | None) -> list[dict[str, Any]]:
    if not incident_id:
        return []
    from hydra.redact import redact_obj

    with _st(request).reader() as conn:
        rows = conn.execute("SELECT family, kind, value, status, quality, provenance FROM genes WHERE incident_id=?"
                            " AND kind NOT LIKE '%embedding%' ORDER BY family, kind, id LIMIT ?",
                            (incident_id, EVIDENCE_GENES_CAP)).fetchall()
    return redact_obj([dict(r) for r in rows])


@router.get("/{job_id}/package", summary="Podpisany pakiet dowodowy / Signed evidence package (ZIP)")
def job_package(request: Request, job_id: str) -> Response:
    """ZIP `hydra-job-<id>/{result.json, evidence.json, report.md, SIGNED_MANIFEST.json, SIGNED_MANIFEST.sig}`,
    signed with this installation's Ed25519 key (hydra/evidence_sign.py). Verify with a PINNED fingerprint:
    `POST /v1/exports/verify?fingerprint=<pinned>` or `hydra verify-export`; without pinning, a package re-signed by
    another key would still be self-consistent."""
    from hydra.evidence_sign import sign_package
    from hydra.export_safe import content_disposition, safe_filename

    body = _done(request, job_id)
    st = _st(request)
    name = f"hydra-job-{safe_filename(job_id, 'job')}"
    tmp = Path(tempfile.mkdtemp(dir=st.settings.tmp_dir, prefix="jobpkg-"))
    try:
        folder = tmp / name
        folder.mkdir()
        (folder / "result.json").write_text(canonical_json(body), encoding="utf-8")
        incident = (body.get("result") or {}).get("incident_id")
        (folder / "evidence.json").write_text(canonical_json(
            {"schema": "hydra.job_evidence.v1", "job_id": job_id, "incident_id": incident,
             "genes": _evidence_genes(request, incident), "genes_cap": EVIDENCE_GENES_CAP}), encoding="utf-8")
        (folder / "report.md").write_text(render_report(body), encoding="utf-8")
        manifest = sign_package(st.settings, folder, (body.get("result") or {}).get("run_id"))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(folder.iterdir()):
                zf.write(f, f"{name}/{f.name}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    data = buf.getvalue()
    return Response(data, media_type="application/zip", headers={
        **content_disposition(f"{name}.zip", "hydra-job.zip"), "X-Hydra-Signing-Key": manifest["key_fingerprint"],
        "X-Hydra-Package-Sha256": hashlib.sha256(data).hexdigest(), "Cache-Control": "no-store"})
