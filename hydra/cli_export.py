"""`hydra export-dataset`: enriched, organised dataset of one run (NASK task §10). Not for public distribution.
`hydra verify-export`: offline check of the signed export (hydra.evidence_sign)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command("export-dataset")
def export_dataset_cmd(
    run: str = typer.Argument("latest", help="Run id or 'latest' (newest analyse run)."),
    out_dir: Path = typer.Argument(..., help="Output folder (keep it outside git: organiser material)."),
) -> None:
    """Write incidents/genes/enrichment/links/assignments parquet (+ CSV), campaigns JSON, README, manifest.json.

    The API serves the same folder as a ZIP: GET /v1/export/dataset?run_id=."""
    from hydra.config import get_settings
    from hydra.db import Writer
    from hydra.export_dataset import export_dataset
    from hydra.export_log import record_export

    s = get_settings()
    res = export_dataset(s, run, out_dir.expanduser().resolve())
    w = Writer(s.db_path)
    try:
        record_export(w, "dataset", res.get("run_id"), res.get("counts"))
    finally:
        w.close()
    typer.echo(json.dumps(res, indent=1))


@app.command("verify-export")
def verify_export_cmd(
    package: Path = typer.Argument(..., exists=True, help="Signed export: ZIP or folder."),
    fingerprint: str | None = typer.Option(None, "--fingerprint", help="Expected key fingerprint (SHA256:...)."),
    local_key: bool = typer.Option(False, "--local-key", help="Expect this installation's signing key."),
    as_json: bool = typer.Option(False, "--json", help="Print the full result as JSON."),
) -> None:
    """Offline check of a signed evidence package: Ed25519 signature, every sha256 and size, no missing or extra
    files, no unsafe names. Prints PASS/FAIL with reasons; exit 1 on FAIL. Integrity, not truth of the material."""
    from hydra.evidence_sign import key_info, verify_package

    if local_key:
        from hydra.config import get_settings

        fingerprint = key_info(get_settings(), create=False)["fingerprint"] or "SHA256:<no local key>"
    res = verify_package(package.expanduser().resolve(), fingerprint)
    if as_json:
        typer.echo(json.dumps(res, indent=1, ensure_ascii=False))
    else:
        verdict = "PASS" if res["ok"] else "FAIL"
        typer.echo(f"{verdict}: {package.name} | pliki/files {res['files_checked']} | run {res['run_id']}"
                   f" | klucz/key {res['key_fingerprint']}")
        head = res.get("audit_chain_head") or {}
        if head:
            typer.echo(f"  audit chain head: seq {head.get('seq')} {head.get('hash')}")
        for e in res["errors"]:
            typer.echo(f"  - {e}")
        if res["ok"]:
            typer.echo("  Integralność potwierdzona (nie prawdziwość materiału). / Integrity verified, not truth.")
    if not res["ok"]:
        raise typer.Exit(1)
