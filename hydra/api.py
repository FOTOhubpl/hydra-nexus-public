"""FastAPI app (loopback only). Generic endpoints: health, import, jobs, artifacts, audit log.

Task endpoints (graph, pairs, reviews, export...) are added from Sat 11:00 in their own routers.
"""

from __future__ import annotations

import contextlib
import errno
import json
import re
import shutil
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, FastAPI, File, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from hydra import __version__, events
from hydra.api_graph import router as graph_router
from hydra.api_hunt import router as hunt_router
from hydra.api_task import router as task_router
from hydra.api_watch import router as watch_router
from hydra.config import Settings, get_settings
from hydra.db import Writer, connect
from hydra.export_safe import content_disposition
from hydra.ingest import import_paths
from hydra.jobs import JobRunner, create_job, get_job, recover_stale_jobs
from hydra.redact import redact_document, redact_obj
from hydra.storage import ContentStore


class AppState:
    def __init__(self, settings: Settings) -> None:
        settings.ensure_dirs()
        self.settings = settings
        self.store = ContentStore(settings.cas_dir)
        self.writer = Writer(settings.db_path)  # creates + migrates the DB
        self.recovered_jobs = recover_stale_jobs(self.writer)
        self.runner = JobRunner(self.writer, max_workers=2)
        self._device: dict[str, Any] | None = None
        from hydra.auth import AuthService

        self.auth = AuthService(self.writer, settings, self.reader)  # AUTH-LOGIN; gate: hydra.api_auth.AuthGate
        if self.auth.enabled:
            self.auth.bootstrap_admin()  # first start: generated admin + ADMIN_CREDENTIALS.txt (0600) + log banner

    def device_info(self) -> dict[str, Any]:
        # pick_device() imports torch: do it once, not on every /health poll.
        if self._device is None:
            try:
                from hydra.ml.device import pick_device

                dev = pick_device()
                self._device = {"device": dev.device, "dtype": dev.dtype}
            except Exception as exc:  # noqa: BLE001
                self._device = {"device": f"unavailable: {type(exc).__name__}"}
        return self._device

    def reader(self) -> contextlib.closing[sqlite3.Connection]:
        # sqlite3's own context manager only commits; closing() actually releases the handle.
        return contextlib.closing(connect(self.settings.db_path, readonly=True))

    def close(self) -> None:
        self.runner.shutdown()
        self.writer.close()


def state(request: Request) -> AppState:
    return request.app.state.hydra


class JobAccepted(BaseModel):
    job_id: str
    status: str = "queued"


class PathImport(BaseModel):
    paths: list[str]


router = APIRouter()

# The spool name is "<5-digit index>-<name>": 200 bytes keeps it below the 255-byte component limit.
UPLOAD_NAME_MAX_BYTES = 200
_BAD_NAME_ERRNOS = {errno.ENAMETOOLONG, errno.EINVAL, errno.EILSEQ}


@router.get("/health")
def health(request: Request) -> dict[str, Any]:
    st = state(request)
    if st.auth.enabled and request.scope.get("hydra.principal") is None:
        return {"status": "ok", "version": __version__, "auth": True}  # minimal before login (AUTH-LOGIN)
    info: dict[str, Any] = {"status": "ok", "version": __version__, "offline": st.settings.offline}
    info.update(st.device_info())
    models_dir = st.settings.models_dir
    present = models_dir is not None and models_dir.exists()
    info["models"] = sorted(p.name for p in models_dir.iterdir() if p.is_dir()) if present else []
    with st.reader() as conn:
        info["artifacts"] = conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0]
    return info


@router.post("/v1/import", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
def import_upload(request: Request, files: Annotated[list[UploadFile], File()]) -> JobAccepted:
    """Multipart upload (files or a ZIP). Files are spooled to state/tmp, then imported by a job.

    Sync on purpose: FastAPI runs it in the threadpool, so blocking copies never stall the event loop.
    """
    st = state(request)
    cap = st.settings.max_upload_mb * 1024 * 1024
    spool = Path(tempfile.mkdtemp(dir=st.settings.tmp_dir, prefix="upload-"))
    saved: list[Path] = []
    total = 0
    try:
        for i, f in enumerate(files):
            name = Path(f.filename or f"upload-{i}").name or f"upload-{i}"
            target = spool / f"{i:05d}-{name}"
            try:
                if len(name.encode("utf-8", "surrogatepass")) > UPLOAD_NAME_MAX_BYTES:
                    raise ValueError("name too long")
                out = target.open("xb")
            except (ValueError, OSError) as exc:  # overlong, NUL, unencodable: a client error (CR-ASTRA-SEC-3)
                if isinstance(exc, OSError) and exc.errno not in _BAD_NAME_ERRNOS:
                    raise
                raise HTTPException(status_code=400, detail=(
                    f"Nieprawidłowa nazwa pliku nr {i + 1}: zbyt długa (limit {UPLOAD_NAME_MAX_BYTES} bajtów)"
                    " lub zawiera niedozwolone znaki. Zmień nazwę pliku i wyślij ponownie.")) from None
            with out:
                while chunk := f.file.read(1024 * 1024):
                    total += len(chunk)
                    if total > cap:
                        raise HTTPException(status_code=413, detail=(
                            f"Przesyłane pliki przekraczają limit {st.settings.max_upload_mb} MB."))
                    out.write(chunk)
            saved.append(target)
        job_id = create_job(st.writer, "import", {"source": "upload", "files": len(saved)})
    except BaseException:
        shutil.rmtree(spool, ignore_errors=True)
        raise

    def handler(ctx):
        try:
            import_paths(ctx, saved, st.settings, st.store)
        finally:
            shutil.rmtree(spool, ignore_errors=True)

    st.runner.submit(job_id, handler)
    return JobAccepted(job_id=job_id)


@router.post("/v1/import/paths", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
def import_local_paths(request: Request, body: PathImport) -> JobAccepted:
    """Import files/folders/ZIPs already on this machine (e.g. the organiser's dataset dir)."""
    st = state(request)
    from hydra.paths import PathNotAllowed, check_input_path

    paths: list[Path] = []
    rejected: list[str] = []
    for raw in body.paths:
        try:
            paths.append(check_input_path(raw, st.settings))
        except PathNotAllowed as exc:
            rejected.append(str(exc))
    if rejected:
        raise HTTPException(status_code=400, detail={"rejected": rejected})
    job_id = create_job(st.writer, "import", {"source": "paths", "paths": [str(p) for p in paths]})
    st.runner.submit(job_id, lambda ctx: import_paths(ctx, paths, st.settings, st.store))
    return JobAccepted(job_id=job_id)


@router.get("/v1/jobs")
def list_jobs(request: Request, limit: int = Query(50, le=500)) -> list[dict[str, Any]]:
    with state(request).reader() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]
        # Same display policy as /v1/jobs/{id} (CR-ASTRA-SEC-13, CR-ASTRA-PIPE-305): params carry hostile names.
        return redact_obj([j for j in (get_job(conn, i) for i in ids) if j])


@router.get("/v1/jobs/{job_id}")
def job_status(request: Request, job_id: str) -> dict[str, Any]:
    with state(request).reader() as conn:
        job = get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        problems = conn.execute(
            "SELECT idx, source_ref, status, error_code, detail FROM job_items"
            " WHERE job_id=? AND status IN ('failed','quarantined') ORDER BY idx LIMIT 100",
            (job_id,),
        ).fetchall()
        job["problems"] = [dict(r) for r in problems]
        # File names and paths come from hostile material: no token in a name reaches the UI (CR-ASTRA-SEC-13).
        return redact_obj(job)


@router.get("/v1/artifacts")
def list_artifacts(
    request: Request,
    limit: int = Query(100, le=1000),
    offset: int = Query(0, ge=0),
    mime_prefix: str | None = None,
) -> dict[str, Any]:
    where, args = ("WHERE mime LIKE ?", [f"{mime_prefix}%"]) if mime_prefix else ("", [])
    with state(request).reader() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM artifacts {where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT id, mime, size, source_ref, observed_at, status, created_at FROM artifacts {where}"
            " ORDER BY created_at, id LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ).fetchall()
    return {"items": [redact_obj(dict(r)) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/v1/artifacts/{artifact_id}")
def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
    with state(request).reader() as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        art = dict(row)
        art["meta"] = json.loads(art["meta"])
        art["sources"] = [dict(r) for r in conn.execute(
            "SELECT source_ref, job_id, imported_at FROM artifact_sources WHERE artifact_id=?", (artifact_id,)
        )]
        return redact_obj(art)


# Document types a browser could execute or render as an active document (CR-ASTRA-SEC-5): never served with
# their own MIME. Everything matching is sent as text/plain under `sandbox` + `nosniff`.
_ACTIVE_MIME = ("html", "xml", "svg", "javascript", "ecmascript", "json", "x-sh", "x-python")


def _inert_mime(mime: str) -> bool:
    m = (mime or "").lower().split(";")[0].strip()
    return m.startswith("text/") or any(t in m for t in _ACTIVE_MIME)


@router.get("/v1/artifacts/{artifact_id}/content")
def artifact_content(request: Request, artifact_id: str, original: bool = False) -> Response:
    """Artifact bytes. Text-like content is a preview: served as text/plain with credentials redacted
    (CR-ASTRA-SEC-13). `original=1` returns the byte-identical CAS original as an attachment (evidence)."""
    st = state(request)
    with st.reader() as conn:
        row = conn.execute("SELECT mime FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    try:
        path = st.store.safe_path(artifact_id)  # no symlinks, never outside the CAS (CR-ASTRA-SEC-6)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact registered but original missing from the store") from None
    # Never let the browser render untrusted HTML/SVG/XHTML from the dataset (spec §19).
    mime = row["mime"] or "application/octet-stream"
    unsafe = _inert_mime(mime)
    media_type = "text/plain; charset=utf-8" if unsafe else mime
    headers = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}
    if original:
        headers["Content-Disposition"] = content_disposition(f"{artifact_id}.bin")["Content-Disposition"]
        return FileResponse(path, media_type="application/octet-stream", headers=headers)
    if unsafe:
        data = path.read_bytes()
        safe = redact_document(data, mime)
        if safe is not data:
            headers["X-Hydra-Redacted"] = "1"
        return Response(safe, media_type=media_type, headers=headers)
    return FileResponse(path, media_type=media_type, headers=headers)


@router.get("/v1/audit/verify")
def audit_verify(request: Request) -> dict[str, Any]:
    with state(request).reader() as conn:
        ok, n, bad = events.verify_chain(conn)
    return {"ok": ok, "events": n, "first_bad_seq": bad}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from hydra.netguard import guard_if_offline

        guard_if_offline(settings)  # HYDRA_OFFLINE=1: process-level socket kill switch (CR-ASTRA-SEC-8)
        app.state.hydra = AppState(settings)
        # API-PERF: fill the per-run view cache in the background so no request waits on a cold computation
        from hydra.api_cache import warm_async

        warm_async(app.state.hydra, delay_s=1.0, all_runs=True)  # newest per corpus first, then every older run
        stop_scheduler = None
        if settings.scheduler:
            # Same process, same single Writer (review #11). Off by default: it may do network lookups.
            from hydra.scheduler import start_background

            stop_scheduler = start_background(settings, app.state.hydra.writer, tick_s=settings.scheduler_tick_s)
        from hydra.api_webhooks import start_if_configured

        start_if_configured(app)  # resumes the outbound webhook dispatcher only when endpoints are configured
        from hydra.watch.rt.engine import env_flag

        if env_flag("HYDRA_WATCH_RT"):  # WATCH-RT: realtime public-feed watch (off by default; offline-safe)
            from hydra.watch.rt.engine import for_app

            try:
                for_app(app.state.hydra, app.state).start()
            except RuntimeError:  # another process (`hydra watch run`) holds the engine lock
                pass
        try:
            yield
        finally:
            if stop_scheduler is not None:
                stop_scheduler()
            watch = getattr(app.state, "watch", None)  # lazily attached by hydra.api_watch (CR-ASTRA-PIPE-309)
            if watch is not None:
                watch.stop()  # joins the replay/live/post workers before the Writer they use is closed
            if (watch_rt := getattr(app.state, "watch_rt", None)) is not None:
                watch_rt.close()  # WATCH-RT task + engine lock, before the Writer is closed
            if (webhooks := getattr(app.state, "webhooks", None)) is not None:
                webhooks.stop()  # outbound webhook dispatcher + delivery worker (hydra.api_webhooks)
            hub = getattr(app.state, "stream_hub", None)  # DATAFLOW live stream (hydra.api_stream)
            if hub is not None:
                hub.close()
            app.state.hydra.close()

    app = FastAPI(title="HYDRA NEXUS", version=__version__, lifespan=lifespan)
    from hydra.api_integrations import install_validation_redaction

    install_validation_redaction(app)  # 422 bodies on /v1/integrations* never echo submitted values (keys, URLs)
    from hydra.api_auth import AuthGate, install_auth_errors
    from hydra.api_auth import router as auth_router

    install_auth_errors(app)
    app.include_router(auth_router)  # /v1/auth/* (login, session, tokens, users)
    app.include_router(router)
    app.include_router(__import__("hydra.api_contract", fromlist=["router"]).router)  # G5 /v1/jobs/{id}/result
    app.include_router(graph_router)  # before task_router: /v1/graph/* sub-paths
    app.include_router(task_router)
    app.include_router(__import__("hydra.api_progress", fromlist=["router"]).router)  # /analyse live progress
    app.include_router(__import__("hydra.api_exports", fromlist=["router"]).router)  # CORE exports
    app.include_router(__import__("hydra.api_batch", fromlist=["router"]).router)  # batch import (B3)
    app.include_router(__import__("hydra.api_meta", fromlist=["router"]).router)  # META-COLLECT /v1/watch/rt/meta
    app.include_router(__import__("hydra.api_watch_rt", fromlist=["router"]).router)  # WATCH-RT, before /v1/watch
    app.include_router(watch_router)
    app.include_router(__import__("hydra.api_stream", fromlist=["router"]).router)  # DATAFLOW: GET /v1/stream
    app.include_router(hunt_router)
    app.include_router(__import__("hydra.api_likeness", fromlist=["router"]).router)  # likeness triage card
    app.include_router(__import__("hydra.api_artifacts", fromlist=["router"]).router)  # artefact viewer
    app.include_router(__import__("hydra.api_lineage", fromlist=["router"]).router)  # variant lineage (G01)
    app.include_router(__import__("hydra.api_reference", fromlist=["router"]).router)  # GALLERY reference base
    app.include_router(__import__("hydra.api_consistency", fromlist=["router"]).router)  # M6 gate report (ADV-CAMP)
    app.include_router(__import__("hydra.api_kontra", fromlist=["router"]).router)  # KONTRA K1 hypothesis lab
    app.include_router(__import__("hydra.api_webhooks", fromlist=["router"]).router)  # before /v1/integrations
    # integrations
    app.include_router(__import__("hydra.api_integrations", fromlist=["router"]).router)  # connector hub
    # API-PERF: multi-MB JSON (graph, dashboard) compresses ~8-10x; level 5 keeps the CPU cost low.
    app.add_middleware(GZipMiddleware, minimum_size=2048, compresslevel=5)
    # HYDRA_AUTH=1: session cookie / bearer token, CSRF and roles; sees the path after the /api strip below.
    app.add_middleware(AuthGate)
    # Same contract as the Vite dev proxy: /api/<path> is served as /<path>.
    app.add_middleware(_StripApiPrefix)
    # Outermost: reject non-local Host headers (DNS rebinding) and cross-site writes (CSRF) — review #6.
    app.add_middleware(_LocalOnlyGuard)
    if settings.ui_dist is not None and (settings.ui_dist / "index.html").is_file():
        app.mount("/", _SpaStaticFiles(directory=settings.ui_dist, html=True), name="ui")
    return app


_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# Host header / URL authority: bracketed IPv6 or a plain name, optional numeric port. Nothing else (no userinfo).
_AUTHORITY_RE = re.compile(r"(?:\[(?P<v6>[0-9a-f:.]+)\]|(?P<name>[a-z0-9._-]+))(?::(?P<port>[0-9]{1,5}))?")


def _host_only(value: str) -> str | None:
    """Lower-case host of a Host header or URL authority, without brackets and port; None when malformed."""
    m = _AUTHORITY_RE.fullmatch(value.strip().lower())
    if m is None:
        return None
    return m.group("v6") or m.group("name")


def _origin_host(origin: str) -> str | None:
    """Host of an Origin header (scheme://host[:port]); None for anything unparsable or non-origin-shaped."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(origin.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    return _host_only(parts.netloc)


def _public_hosts() -> set[str]:
    """Extra public host names (HYDRA_PUBLIC_HOSTS, comma-separated) for a demo behind a reverse proxy / tunnel.
    Honoured ONLY with HYDRA_AUTH=1 (login required); the API still binds to loopback, the tunnel reaches it there."""
    import os

    raw = os.environ.get("HYDRA_PUBLIC_HOSTS", "")
    if not raw or os.environ.get("HYDRA_AUTH", "1") != "1":
        return set()
    return {h for h in (_host_only(x) for x in raw.split(",") if x.strip()) if h}


def _guard_reason(scope) -> str | None:
    """Why a request must be refused before dispatch, or None. Parse errors are denials, never 500s."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    allowed = _LOCAL_HOSTS | _public_hosts()
    if _host_only(headers.get("host", "127.0.0.1")) not in allowed:
        return "Niedozwolony nagłówek Host (dostęp tylko lokalny)."
    if scope.get("method", "GET") in _UNSAFE:
        origin = headers.get("origin")
        if origin and origin != "null":
            if _origin_host(origin) not in allowed:
                return "Odrzucono zapis z innego pochodzenia (Origin)."
        elif origin == "null" or headers.get("sec-fetch-site") == "cross-site":
            return "Odrzucono zapis z innej witryny."
    return None


class _LocalOnlyGuard:
    """Loopback binding alone does not stop a web page in the operator's browser from talking to us:
    DNS rebinding (foreign Host header) and cross-site form/fetch POSTs. Reject both."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            try:
                reason = _guard_reason(scope)
            except Exception:  # noqa: BLE001 — an unparsable header is a denial, not a server error (CR-ASTRA-SEC-4)
                reason = "Nieprawidłowe nagłówki żądania."
            if reason:
                body = json.dumps({"detail": reason}).encode()
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class _StripApiPrefix:
    """ASGI middleware: rewrite /api/... to /... so one port serves both the UI and the API."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == "/api" or path.startswith("/api/"):
                scope = dict(scope)
                scope["path"] = path[4:] or "/"
                scope["raw_path"] = scope["path"].encode()
                scope["hydra.api"] = True  # never answer an API path with the SPA (CR-D2)
        await self.app(scope, receive, send)


class _SpaStaticFiles(StaticFiles):
    """Static UI with SPA fallback: unknown non-file paths return index.html (client-side routing)."""

    async def get_response(self, path: str, scope):
        if scope.get("hydra.api") or path == "v1" or path.startswith("v1/"):
            raise StarletteHTTPException(status_code=404, detail="unknown API path")
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in path.rsplit("/", 1)[-1]:
                return await super().get_response("index.html", scope)
            raise
