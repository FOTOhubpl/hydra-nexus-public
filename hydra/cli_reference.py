"""`hydra reference build | status`: the face gallery / logo templates from the operator's own imported material
(hydra.reference_build). Writes only to the local reference dir (state / $HYDRA_VIS_REFERENCE_DIR), never git."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from hydra.config import get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Galeria referencyjna (twarze, logo) z zaimportowanych materiałów")


@app.command("build")
def build_cmd(
    from_state: bool = typer.Option(False, "--from-state", help="Imported incidents of the state (default)."),
    from_dir: Path = typer.Option(None, "--from", exists=True, file_okay=False,
                                  help="Dataset folder (one subfolder per incident), read in place."),
    faces: bool = typer.Option(False, "--faces", help="Only the face gallery (default: both)."),
    logos: bool = typer.Option(False, "--logos", help="Only the logo templates (default: both)."),
    out: Path = typer.Option(None, "--out", help="Reference dir (default: $HYDRA_VIS_REFERENCE_DIR, else"
                             " <state>/reference)."),
    as_json: bool = typer.Option(False, "--json"),
) -> None:
    """Build the reference base; then re-run `hydra analyse` so ID/logo genes appear."""
    from hydra import reference_build as rb

    if from_state and from_dir:
        raise typer.BadParameter("give --from-state or --from DIR, not both")
    both = not faces and not logos
    res = rb.build(get_settings(), from_dir, faces=faces or both, logos=logos or both, out=out,
                   progress=lambda i, n, what: typer.echo(f"[{i + 1}/{n}] {what}", err=True))
    if as_json:
        typer.echo(json.dumps(res, indent=2, ensure_ascii=False, default=str))
    else:
        typer.echo(res["message_pl"])
    if not res.get("ok"):
        raise typer.Exit(1)


@app.command("status")
def status_cmd() -> None:
    """Counts of the reference base in use (no vectors, no person names)."""
    from hydra import reference_build as rb

    typer.echo(json.dumps(rb.status(get_settings()), indent=2, ensure_ascii=False, default=str))
