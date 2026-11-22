"""Task commands: `hydra analyse <dir>`, `hydra assign <path|url>`, `hydra assign-batch <dir>` (from hydra/cli.py)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _warn_if_api_running() -> None:
    """SQLite has one writer per database: a CLI write while the API runs competes with the API's Writer."""
    from hydra.config import get_settings
    from hydra.scheduler import api_running

    s = get_settings()
    if api_running(s):
        typer.secho(f"WARNING: the HYDRA API is running on {s.api_host}:{s.api_port} and owns the database writer."
                    " Use the GUI/API (or stop the server) instead of writing from the CLI.", fg="yellow", err=True)


def _thresholds(link: float | None, kit: float | None):
    from hydra.synapse import Thresholds

    th = Thresholds()
    if link is not None:
        th.link = link
    if kit is not None:
        th.kit = kit
    return th


@app.command("analyse")
def analyse_cmd(
    paths: list[Path] = typer.Argument(None, exists=True, help="Dataset folder (one subfolder per incident), "
                                       "an incident folder, or single files (optional with --corpus)."),
    corpus: str = typer.Option(None, "--corpus", help="nask (default) | extended | all | <name>[,<name>]: "
                               "also analyse stored incidents of these corpora (external data is opt-in)."),
    prior: str = typer.Option(None, "--prior", help="External corpora used as a rarity prior, e.g. ext:all."),
    fusion: Path = typer.Option(None, "--fusion", exists=True, help="Fusion model from `hydra train` (opt-in)."),
    only: list[str] = typer.Option(None, "--only", help="Run only these extractors (repeatable)."),
    no_cache: bool = typer.Option(False, "--no-cache", help="Re-extract genes even if unchanged."),
    incremental: bool = typer.Option(False, "--incremental",
                                     help="Reuse every stage whose exact inputs are unchanged (same result as a"
                                          " full run; a new or changed incident recomputes scoring)."),
    link: float = typer.Option(None, help="Same-campaign threshold in bits."),
    kit: float = typer.Option(None, help="Campaign-group (kit) threshold in bits."),
    as_json: bool = typer.Option(False, "--json", help="Print the full run metrics as JSON."),
) -> None:
    """Import → genes → rarity → candidates → pair scores → campaigns and groups (one run id)."""
    _warn_if_api_running()
    from hydra.config import get_settings
    from hydra.jury_report import jury_analyse as analyse  # pipeline.analyse + dataset rules + import report

    if not paths and not corpus:
        raise typer.BadParameter("give dataset paths, or --corpus to analyse stored incidents")
    res = analyse([p.expanduser().resolve() for p in paths or []], get_settings(), only=only or None,
                  use_cache=not no_cache, thresholds=_thresholds(link, kit),
                  corpus=None if corpus in (None, "nask") and paths else corpus, prior=prior, fusion_path=fusion,
                  incremental=incremental)
    if as_json:
        typer.echo(json.dumps({"run_id": res.run_id, **res.metrics}, indent=2, ensure_ascii=False, default=str))
        return
    m = res.metrics
    typer.echo(f"run {res.run_id}: {m['incidents']} incidents, {m['genes']} genes, {m['pairs_scored']} pairs, "
               f"{m['campaigns']} campaigns ({m['multi_member_campaigns']} multi-member), "
               f"{len(m['campaign_groups'])} groups; scorer {m['scorer']}")
    typer.echo(f"timings (s): {m['timings_s']}")
    rep = m.get("import_report") or {}
    if rep:
        typer.echo(f"import: {rep['summary_pl']}")
        for f in rep.get("files", [])[:50]:
            typer.echo(f"  POMINIĘTO {f['path']} [{f['code']}] {f['status_pl']}"
                       + (f" (incydent {f['incident_id']})" if f.get("incident_id") else ""))
        for note in rep.get("notes_pl", []):
            typer.echo(f"  UWAGA: {note}")
        if rep.get("skipped_files", 0) > 50:
            typer.echo(f"  ... i {rep['skipped_files'] - 50} więcej (pełna lista: --json, import_report)")
    for note in (m.get("references") or {}).get("notes_pl", []):
        typer.echo(f"UWAGA: {note}")
    from hydra.enrich.warninglists import report_line

    typer.echo(report_line())
    for c in res.clustering.campaigns:
        typer.echo(f"  {c.id} [{len(c.members)}] {c.label}  group={c.group_id or '-'}  members={','.join(c.members)}")
    for a in m["alerts"]:
        typer.echo(f"  ALERT {a}")


@app.command("assign")
def assign_cmd(
    item: str = typer.Argument(..., help="File, folder, ZIP, URL, or (with --kind hint) a keyword."),
    run: str = typer.Option(None, "--run", help="Base analyse run (default: latest)."),
    only: list[str] = typer.Option(None, "--only", help="Run only these extractors (repeatable)."),
    link: float = typer.Option(None, help="Same-campaign threshold in bits."),
    kit: float = typer.Option(None, help="Campaign-group (kit) threshold in bits."),
    kind: str = typer.Option("auto", "--kind", help="auto | url | screenshot | hint | file (auto: URL or file)."),
) -> None:
    """Assign new material to a known campaign, flag a new pattern, or abstain (with reasons in Polish).

    Same dispatcher as the GUI (`analyse_input`): URL (isolated fetch only with HYDRA_ENRICH=1), screenshot
    (OCR → URL → follow), hint (corpus search), file / folder / ZIP / chat log / PDF. Prints the decision, the
    per-file input report and notes."""
    _warn_if_api_running()
    from hydra.config import get_settings
    from hydra.pipeline import analyse_input, detect_input_kind

    k = detect_input_kind(item) if kind == "auto" else kind
    out = analyse_input(k, item, get_settings(), base_run=run, only=only or None, thresholds=_thresholds(link, kit))
    a = out.get("assignment")
    if a is not None:
        a = {**{key: v for key, v in a.items() if key != "candidates"},
             "candidates": [{"campaign_id": c["campaign_id"], "probability": c["probability"], "support": c["support"],
                             "total_bits": (c.get("best_pair") or {}).get("total_bits")} for c in a["candidates"]]}
    keep = ("kind", "incident_id", "run_id", "notes", "fetch", "urls_in_image", "phones_in_image", "input_report",
            "search")
    typer.echo(json.dumps({**{key: out[key] for key in keep if key in out}, "assignment": a}, indent=2,
                          ensure_ascii=False, default=str))


def _batch_items(folder: Path) -> list[Path]:
    """One item per incident subfolder and per top-level file (ZIP, image, video, HTML...); hidden entries skipped."""
    from hydra.batch import batch_items

    return batch_items(folder)


@app.command("assign-batch")
def assign_batch_cmd(
    folder: Path = typer.Argument(..., exists=True, file_okay=False, help="Folder of new material: one subfolder "
                                  "(or ZIP/file) per incident."),
    out: Path = typer.Option(..., "--out", help="Output folder for assignments.json/.csv + new_patterns.json."),
    run: str = typer.Option(None, "--run", help="Base analyse run (default: latest)."),
    link: float = typer.Option(None, help="Same-campaign threshold in bits."),
    kit: float = typer.Option(None, help="Campaign-group (kit) threshold in bits."),
) -> None:
    """Assign every item of a folder against one fixed base run, then compare the new patterns of the batch with
    each other (items of one unseen campaign share a `new_pattern_id`) and mark duplicates (`duplicate_of`).
    Writes assignments.json, assignments.csv and new_patterns.json."""
    from hydra.batch import assign_batch, write_outputs
    from hydra.config import get_settings

    _warn_if_api_running()
    s = get_settings()
    items = _batch_items(folder.expanduser().resolve())

    def _echo(i: int, n: int, row: dict) -> None:
        typer.echo(f"[{i}/{n}] {row['item']}: {row['decision']} {row.get('campaign_id') or ''} "
                   f"({row['seconds']} s)", err=True)

    try:
        report = assign_batch(items, s, base_run=run, thresholds=_thresholds(link, kit), on_item=_echo)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    out = out.expanduser().resolve()
    write_outputs(report, out)
    for p in report["new_patterns"]:
        if p["size"] > 1:
            typer.echo(f"nowy wzorzec {p['id']}: {', '.join(p['items'])}", err=True)
    typer.echo(json.dumps({"base_run": report["base_run"], "batch_run": report["batch_run"],
                           "items": len(report["items"]), "decisions": report["decisions"],
                           **report["summary"], "seconds": report["seconds"], "out": str(out)}, indent=2))


@app.command("export-neo4j")
def export_neo4j_cmd(
    out: Path = typer.Argument(..., help="Output folder for CSV files + import.cypher."),
    run: str = typer.Option(None, "--run", help="Analyse run (default: latest)."),
    min_df: int = typer.Option(2, help="Export gene-value nodes held by at least this many incidents."),
) -> None:
    """Neo4j export (Cypher + CSV) of a run: incidents, campaigns, groups, gene values, links."""
    from contextlib import closing

    from hydra.config import get_settings
    from hydra.db import connect
    from hydra.export_neo4j import export_run
    from hydra.pipeline import latest_run

    s = get_settings()
    with closing(connect(s.db_path, readonly=True)) as conn:
        run_id = run or latest_run(conn)
        if run_id is None:
            raise typer.BadParameter("no analyse run yet")
        counts = export_run(conn, run_id, out.expanduser().resolve(), min_df=min_df)
    typer.echo(json.dumps({"run_id": run_id, "out": str(out), **counts}, indent=2))


scheduler_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Watchlist and periodic jobs.")
app.add_typer(scheduler_app, name="scheduler")


@scheduler_app.command("status")
def scheduler_status_cmd() -> None:
    """Print the scheduler status JSON (same as GET /v1/scheduler/status)."""
    from hydra.config import get_settings
    from hydra.scheduler import scheduler_status

    typer.echo(json.dumps(scheduler_status(get_settings()), indent=2, ensure_ascii=False, default=str))


@scheduler_app.command("watch")
def scheduler_watch_cmd(keyword: list[str] = typer.Option(None, "--keyword", "-k"),
                        domain: list[str] = typer.Option(None, "--domain", "-d"),
                        page_id: list[str] = typer.Option(None, "--page-id", help="Meta page id to sweep.")) -> None:
    """Add keywords/domains/Meta page ids to the watchlist."""
    from hydra.config import get_settings
    from hydra.scheduler import watch_add

    wl = watch_add(get_settings(), keywords=keyword or [], domains=domain or [], page_ids=page_id or [])
    typer.echo(json.dumps(wl, indent=2, ensure_ascii=False))


@scheduler_app.command("run")
def scheduler_run_cmd(force: bool = typer.Option(False, "--force", help="Run all jobs now."),
                      loop: bool = typer.Option(False, "--loop", help="Keep running due jobs.")) -> None:
    """Run due jobs once (or forever with --loop)."""
    _warn_if_api_running()
    from hydra.config import get_settings
    from hydra.db import Writer
    from hydra.scheduler import run_due, run_forever

    s = get_settings()
    s.ensure_dirs()
    w = Writer(s.db_path)
    try:
        if loop:
            run_forever(s, w)
        st = run_due(s, w, force=force)
        typer.echo(json.dumps(st, indent=2, ensure_ascii=False, default=str))
    finally:
        w.close()


@app.command("train")
def train_cmd(
    corpus: str = typer.Option(..., "--corpus", help="Labelled external corpora, e.g. ext:phish_kits."),
    label: str = typer.Option("label", "--label", help="label | group_label"),
    test_frac: float = typer.Option(0.3, help="Share of label groups held out for calibration metrics."),
    seed: int = typer.Option(0),
    allow_nask: bool = typer.Option(False, "--allow-nask", help="Allow organiser incidents (leaks EVAL truth)."),
    allow_weak_labels: bool = typer.Option(True, "--allow-weak-labels/--no-weak-labels",
                                           help="Advertiser-page labels as WEAK campaign labels (brand never)."),
) -> None:
    """Fit the LR + isotonic fusion on labelled external data (explicit step; analyse uses it only via --fusion)."""
    from hydra.config import get_settings
    from hydra.train import train_fusion

    prov = train_fusion(get_settings(), corpus, label_field=label, test_frac=test_frac, seed=seed,
                        allow_nask=allow_nask, allow_weak_labels=allow_weak_labels)
    typer.echo(json.dumps(prov, indent=2, ensure_ascii=False, default=str))


@app.command("corpora")
def corpora_cmd() -> None:
    """Incident counts per corpus (nask, upload, ext:<name>, public:<name>)."""
    from contextlib import closing

    from hydra.config import get_settings
    from hydra.corpus import corpus_counts
    from hydra.db import connect

    s = get_settings()
    with closing(connect(s.db_path, readonly=True)) as conn:
        typer.echo(json.dumps(corpus_counts(conn), indent=2))
