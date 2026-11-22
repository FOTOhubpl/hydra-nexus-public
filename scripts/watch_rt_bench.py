"""WATCH-RT measurement (synthetic data only, no network): matching latency per item, one poll incl. DB commit,
poll-loop overhead with nothing due, memory. Not part of `just check`.

    uv run python scripts/watch_rt_bench.py --n 10000

Generates the feed with `scripts/watch_rt_fixture.py bench --n N --out <tmp>` (one CERT-format snapshot, 1 % of the
lines carry a watchlist host), replays it through the engine on a temporary state dir (FakeClock) and prints JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import random
import resource
import subprocess
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hydra.config import Settings  # noqa: E402
from hydra.db import Writer  # noqa: E402
from hydra.watch.rt.engine import FakeClock, WatchRT, load_fixture  # noqa: E402


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return round(s[min(len(s) - 1, int(p * len(s)))], 3)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10000)
    ap.add_argument("--loop-iters", type=int, default=1000)
    a = ap.parse_args()
    with tempfile.TemporaryDirectory(prefix="watch-rt-bench-") as tmp:
        t = Path(tmp)
        feed = t / "feed"
        subprocess.run([sys.executable, str(ROOT / "scripts" / "watch_rt_fixture.py"), "bench", "--n", str(a.n),
                        "--out", str(feed)], check=True, capture_output=True)
        s = Settings(root=t, data_dir=t / "data", models_dir=t / "models", cache_dir=t / "cache",
                     state_dir=t / "state", offline=True)
        s.ensure_dirs()
        w = Writer(s.db_path)
        try:
            tracemalloc.start()
            eng = WatchRT(s, w, clock=FakeClock(), rng=random.Random(1), auto_assign=False)
            fx = load_fixture(str(feed))
            eng.prepare_replay(fx, 3600)
            eng.state = "replay"
            t0 = time.perf_counter()
            runs = eng.replay_tick(0)  # fetch + parse + match + one DB transaction + stream events
            poll_ms = (time.perf_counter() - t0) * 1000
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            m = eng.measurements()
            st = eng.status()

            # poll-loop overhead: live state, no enabled source (nothing due), FakeClock
            eng.state = "stopped"
            eng.replay = None
            eng.writer.run(lambda c: c.execute("UPDATE watch_sources SET enabled=0"))
            eng.metrics.loop_ms.clear()
            eng._last_housekeep = eng.clock.time()

            async def loop() -> None:
                eng.state = "live"
                eng.settings = Settings(root=t, data_dir=t / "data", models_dir=t / "models",
                                        cache_dir=t / "cache", state_dir=t / "state", offline=False)
                for _ in range(a.loop_iters):
                    assert await eng.step() == []
                    eng.clock.advance(1.0)
                eng.state = "stopped"

            asyncio.run(loop())
            lm = list(eng.metrics.loop_ms)
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            rss_mb = rss / (1024 * 1024) if platform.system() == "Darwin" else rss / 1024
            out = {
                "items_in_feed": a.n, "fetched": runs[0]["fetched"], "new": runs[0]["new"],
                "matched": runs[0]["matched"], "totals": st["totals"],
                "match_us_p50": m["match_us_p50"], "match_us_p95": m["match_us_p95"],
                "match_samples": m["samples"]["match"],
                "poll_total_ms_incl_commit": round(poll_ms, 1), "poll_engine_duration_ms": runs[0]["duration_ms"],
                "loop_overhead_ms_p50": pct(lm, 0.5), "loop_overhead_ms_p95": pct(lm, 0.95), "loop_samples": len(lm),
                "tracemalloc_peak_mb": round(peak / 1e6, 1), "peak_rss_mb": round(rss_mb, 1),
                "python": platform.python_version(), "machine": f"{platform.system()} {platform.machine()}",
            }
            print(json.dumps(out, indent=1))
        finally:
            w.close()


if __name__ == "__main__":
    main()
