"""`hydra watch ...`: WATCH-RT daemon and replay (docs/WATCH.md).

- `hydra watch run`: live daemon in this process (own Writer, engine lock shared with the API: never both). Polls
  only with HYDRA_OFFLINE=0 and enabled sources; offline it stays in state `offline` and polls nothing.
- `hydra watch replay <fixture-dir|name> [--speed 60]`: feeds the fixture through the same engine (replay rows,
  labelled SYNTETYCZNE / ODTWORZENIE), then prints the counts.
- `hydra watch status`, `hydra watch rebuild` (watchlist from the latest analyse run),
  `hydra watch enable <source> [--interval 60]`, `hydra watch disable <source>`.
"""

from __future__ import annotations

import asyncio
import json
import signal

import typer

from hydra.config import get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Watch realtime (WATCH-RT): feeds, matching.")


def _engine():
    from hydra.db import Writer
    from hydra.watch.rt.engine import WatchRT

    s = get_settings()
    s.ensure_dirs()
    w = Writer(s.db_path)
    return WatchRT(s, w), w


def _print(obj: object) -> None:
    typer.echo(json.dumps(obj, ensure_ascii=False, indent=1, default=str))


@app.command("status")
def status_cmd() -> None:
    eng, w = _engine()
    try:
        st = eng.status()
        _print({"engine": st["engine"], "totals": st["totals"],
                "sources": [{k: s[k] for k in ("id", "enabled", "state", "interval_s", "last_poll", "last_error_pl")}
                            for s in st["sources"]]})
    finally:
        w.close()


@app.command("rebuild")
def rebuild_cmd() -> None:
    """Rebuild the campaign-derived watchlist from the latest analyse run."""
    eng, w = _engine()
    try:
        _print(eng.rebuild_watchlist())
    finally:
        w.close()


@app.command("enable")
def enable_cmd(source: str, interval: int = typer.Option(None, "--interval", min=60)) -> None:
    eng, w = _engine()
    try:
        _print(eng.set_source(source, enabled=True, interval_s=interval))
    finally:
        w.close()


@app.command("disable")
def disable_cmd(source: str) -> None:
    eng, w = _engine()
    try:
        _print(eng.set_source(source, enabled=False))
    finally:
        w.close()


@app.command("run")
def run_cmd(iterations: int = typer.Option(None, help="Stop after N scheduler iterations (tests).")) -> None:
    """Live daemon: poll enabled sources until SIGINT/SIGTERM."""
    eng, w = _engine()

    async def main() -> None:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        st = eng.start()
        typer.echo(f"watch-rt: {st['engine']['state_pl']} ({st['engine'].get('message_pl') or 'odpytywanie'})")
        if eng._task is not None:
            eng._task.cancel()  # run the loop here instead, so --iterations works
            eng._task = None
        runner = asyncio.create_task(eng.run_forever(max_iterations=iterations))
        done, _ = await asyncio.wait({runner, asyncio.create_task(stop.wait())}, return_when=asyncio.FIRST_COMPLETED)
        eng.stop()
        runner.cancel()

    try:
        asyncio.run(main())
    finally:
        w.close()


@app.command("replay")
def replay_cmd(fixture: str = typer.Argument(..., help="Fixture directory (manifest.json) or fixture name."),
               speed: float = typer.Option(60.0, min=1.0, max=3600.0, help="1 = one tick per minute.")) -> None:
    """Feed a recorded/synthetic fixture through the engine (no network). Rows are labelled replay."""
    from hydra.watch.rt.engine import REPLAY_LABEL_PL, load_fixture

    fx = load_fixture(fixture)
    eng, w = _engine()

    async def main() -> None:
        eng.prepare_replay(fx, speed)
        eng.state = "replay"
        typer.echo(f"{REPLAY_LABEL_PL}: {fx.name}, {len(fx.ticks)} odpytań, prędkość {speed}x")
        await eng.run_replay()

    try:
        asyncio.run(main())
        st = eng.status()
        _print({"fixture": fx.name, "synthetic": fx.synthetic, "totals": st["totals"],
                "metrics": eng.measurements()})
    finally:
        w.close()
