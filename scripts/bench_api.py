"""Time read endpoints in-process (FastAPI TestClient) against the state in HYDRA_STATE_DIR.

    uv run python scripts/bench_api.py <run_id> [endpoint-to-profile]

Prints server time (ms, includes JSON encoding), raw and gzip payload size per endpoint and repetition. Rep 0 is
cold (no response cache), later reps show the per-run cache. With a second argument, cProfile of rep 0 of that
endpoint. Never point it at a live demo state: it only reads, but the response cache writes under work/cache.
"""

from __future__ import annotations

import cProfile
import io
import json
import os
import pstats
import sys
import time

from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import Settings

FUNCS = {"graph": "hydra.api_graph:explore", "flow": "hydra.api_visual:system_flow",
         "dashboard": "hydra.api_task:dashboard", "runs": "hydra.api_task:list_runs",
         "campaigns": "hydra.api_task:list_campaigns", "pairs": "hydra.api_task:list_pairs",
         "incidents": "hydra.api_task:list_incidents"}


def _profile_direct(app: object, name: str, url: str) -> None:
    """TestClient runs the app in a worker thread, so cProfile calls the route function itself."""
    import importlib
    import inspect
    from types import SimpleNamespace
    from urllib.parse import parse_qsl, urlsplit

    mod, fn = (os.environ.get("PROFILE_FUNC") or FUNCS[name]).split(":")
    func = getattr(importlib.import_module(mod), fn)
    kwargs: dict[str, object] = {}
    for pname, p in inspect.signature(func).parameters.items():
        if pname != "request" and p.default is not inspect.Parameter.empty:
            kwargs[pname] = getattr(p.default, "default", p.default)  # Query(...) -> its default
    kwargs |= dict(parse_qsl(urlsplit(url).query))
    pr = cProfile.Profile()
    pr.enable()
    func(SimpleNamespace(app=app), **kwargs)
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats("cumulative").print_stats(45)
    print(s.getvalue()[:14000])


def main() -> None:
    run = sys.argv[1]
    prof = sys.argv[2] if len(sys.argv) > 2 else None
    eps = {
        "graph": f"/v1/graph/explore?run_id={run}",
        "flow": f"/v1/system/flow?run_id={run}",
        "dashboard": f"/v1/dashboard?run_id={run}",
        "runs": "/v1/runs",
        "campaigns": f"/v1/campaigns?run_id={run}",
        "pairs": f"/v1/pairs?run_id={run}",
        "incidents": f"/v1/incidents?run_id={run}",
    }
    if os.environ.get("EXTRA_EPS"):
        eps.update(json.loads(os.environ["EXTRA_EPS"].replace("RUN", run)))
    only = set(filter(None, os.environ.get("ONLY", "").split(",")))
    reps = int(os.environ.get("REPS", "2"))
    with TestClient(create_app(Settings())) as c:
        for name, url in eps.items():
            if (prof and name != prof) or (only and name not in only):
                continue
            for i in range(reps):
                if prof and i == 0:
                    _profile_direct(c.app, name, url)
                t = time.perf_counter()
                r = c.get(url, headers={"accept-encoding": "identity"})
                dt = time.perf_counter() - t
                rz = c.get(url, headers={"accept-encoding": "gzip"})
                gz = rz.headers.get("content-length") if rz.headers.get("content-encoding") == "gzip" else None
                gz_kb = f"{int(gz) / 1024:8.0f} KB" if gz else "       -"
                print(f"{name:10s} rep{i} {r.status_code} {dt * 1000:9.0f} ms  raw {len(r.content) / 1024:8.0f} KB"
                      f"  gzip {gz_kb}", flush=True)


if __name__ == "__main__":
    main()
