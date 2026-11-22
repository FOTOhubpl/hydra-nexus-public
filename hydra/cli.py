"""`hydra` command line: serve, import, status, verify."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

import typer

from hydra import __version__, events
from hydra.config import get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="HYDRA NEXUS command line")


@app.callback()
def _network_policy() -> None:
    """HYDRA_OFFLINE=1 (default) installs the process socket kill switch before any command (hydra.netguard)."""
    from hydra.netguard import guard_if_offline

    guard_if_offline(get_settings())


from hydra.cli_task import app as _task_app  # noqa: E402 — task commands: analyse, assign

app.add_typer(_task_app, name=None)
from hydra.cli_export import app as _export_app  # noqa: E402 — export-dataset, verify-export

app.add_typer(_export_app, name=None)
from hydra.cli_user import app as _user_app  # noqa: E402 — panel accounts: hydra user create|reset-password|list|disable

app.add_typer(_user_app, name="user")
from hydra.cli_watch import app as _watch_app  # noqa: E402 — WATCH-RT: watch run / replay / status

app.add_typer(_watch_app, name="watch")
from hydra.cli_reference import app as _reference_app  # noqa: E402 — hydra reference build|status

app.add_typer(_reference_app, name="reference")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command()
def serve(reload: bool = typer.Option(False, help="Auto-reload on code changes (dev).")) -> None:
    """Run the API on 127.0.0.1:$HYDRA_API_PORT (loopback only)."""
    import uvicorn

    s = get_settings()
    uvicorn.run("hydra.api:create_app", factory=True, host=s.api_host, port=s.api_port, reload=reload,
                timeout_graceful_shutdown=10)  # open SSE streams kept the old process alive for minutes


@app.command("import")
def import_cmd(paths: list[Path] = typer.Argument(..., exists=True, help="Files, folders or ZIPs.")) -> None:
    """Import synchronously (no server needed) and print the per-status summary."""
    from hydra.db import Writer, connect
    from hydra.ingest import import_paths
    from hydra.jobs import JobRunner, create_job, get_job
    from hydra.storage import ContentStore

    s = get_settings()
    s.ensure_dirs()
    writer = Writer(s.db_path)
    runner = JobRunner(writer, max_workers=1)
    store = ContentStore(s.cas_dir)
    resolved = [p.expanduser().resolve() for p in paths]
    job_id = create_job(writer, "import", {"source": "cli", "paths": [str(p) for p in resolved]})
    runner.submit(job_id, lambda ctx: import_paths(ctx, resolved, s, store)).result()
    runner.shutdown()
    writer.close()
    with closing(connect(s.db_path, readonly=True)) as conn:
        typer.echo(json.dumps(get_job(conn, job_id), indent=2, default=str))


@app.command()
def status() -> None:
    """Counts of artifacts by MIME and of jobs by status."""
    from hydra.db import connect

    s = get_settings()
    if not s.db_path.exists():
        typer.echo("no database yet")
        raise typer.Exit(0)
    with closing(connect(s.db_path, readonly=True)) as conn:
        by_mime = conn.execute("SELECT mime, COUNT(*) n FROM artifacts GROUP BY mime ORDER BY n DESC").fetchall()
        jobs = conn.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    typer.echo(json.dumps({"artifacts": {r["mime"]: r["n"] for r in by_mime},
                           "jobs": {r["status"]: r["n"] for r in jobs}}, indent=2))


@app.command()
def verify() -> None:
    """Re-hash every stored original and verify the audit hash chain. Exit 1 on any mismatch."""
    from hydra.db import connect
    from hydra.storage import ContentStore

    s = get_settings()
    store = ContentStore(s.cas_dir)
    bad = [d for d in store.iter_digests() if not store.verify(d)]
    ok_chain, n, first_bad = (True, 0, None)
    if s.db_path.exists():
        with closing(connect(s.db_path, readonly=True)) as conn:
            ok_chain, n, first_bad = events.verify_chain(conn)
    typer.echo(json.dumps({"cas_mismatches": bad, "audit_ok": ok_chain, "audit_events": n,
                           "audit_first_bad_seq": first_bad}, indent=2))
    if bad or not ok_chain:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
