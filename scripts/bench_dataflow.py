"""DATAFLOW benchmarks (ADR-0009). Run on a COPY of a state dir, never on ~/HydraNexus/state.

  views    GET /v1/dashboard, /v1/system/flow, /v1/graph/overview against a running API: first request (cold) and
           warm repeats, wall time, X-Hydra-Cache, bytes on the wire (gzip).
  inject   K synthetic new-material assignments against the latest analyse run through `pipeline._write_run`
           (the upload / assign-batch path), with this process as the only writer (no API may be running).
  latency  load test: API in this process (one Writer) on --port, an SSE client in a separate process; N synthetic
           assignments are written through the app's Writer; event -> SSE receipt latency p50/p95, messages,
           coalescing, server and client CPU. The client stores every delta so the UI patch cost can be replayed
           (ui/src/api/stream.bench.test.ts).
  client   the SSE client of `latency` (internal).

Synthetic rows: incidents `dfbench-*`, source `synthetic:dataflow-bench`, assign runs `assign-dfbench-*`, pairs to
real members of the base run with synthetic bits. They only ever go into the copy given by --state.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import resource
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

HOME_STATE = Path("~/HydraNexus/state").expanduser().resolve()


def _guard_state(state: Path) -> Path:
    state = state.expanduser().resolve()
    if state == HOME_STATE or HOME_STATE in state.parents:
        sys.exit(f"refusing to write synthetic rows into the main state {state}; use a copy")
    return state


def pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, round(p / 100 * (len(xs) - 1))))
    return round(xs[k], 4)


# --------------------------------------------------------------------------------------- synthetic writes


class Injector:
    def __init__(self, conn: sqlite3.Connection, seed: int = 7, run: str | None = None) -> None:
        from hydra.pipeline import latest_run

        self.rng = random.Random(seed)
        self.base = run or latest_run(conn)
        if self.base is None:
            sys.exit("no analyse run in this state")
        self.members = [r[0] for r in conn.execute("SELECT incident_id FROM campaign_members WHERE run_id=?",
                                                    (self.base,))]
        self.camps = sorted({r[0] for r in conn.execute("SELECT campaign_id FROM campaign_members WHERE run_id=?",
                                                         (self.base,))})
        self.n = conn.execute("SELECT COUNT(*) FROM incidents WHERE id LIKE 'dfbench-%'").fetchone()[0]

    def tx(self) -> tuple[str, Any]:
        """(incident id, writer transaction) for one new item with 2-8 pairs to base members."""
        from hydra import events
        from hydra.contracts import Assignment, PairScore
        from hydra.pipeline import _write_run
        from hydra.synapse import PairEval

        r = self.rng
        self.n += 1
        iid = f"dfbench-{os.getpid()}-{self.n:05d}"
        run_id = f"assign-dfbench-{os.getpid()}-{self.n:05d}"
        decision = r.choice(("assign", "assign", "new_campaign", "abstain"))
        camp = r.choice(self.camps) if decision == "assign" and self.camps else None
        pairs = {}
        for o in r.sample(self.members, min(len(self.members), r.randint(2, 8))):
            a, b = sorted((iid, o))
            fb = {f: round(r.uniform(-2, 14), 2) for f in r.sample(("VIS", "TXT", "INF", "ID"), r.randint(1, 3))}
            hyp = r.choice(("same_campaign", "shared_template", "shared_asset", "abstain", "independent"))
            score = PairScore(a=a, b=b, family_bits=fb, missing_families=[], contradictions=[], reasons=[],
                              total_bits=round(sum(fb.values()), 3), probability=round(r.random(), 3),
                              calibrated=False, hypothesis=hyp, candidate_sources=["exact"], model="dfbench@1")
            pairs[(a, b)] = PairEval(score=score, campaign_bits=r.uniform(0, 12), kit_bits=r.choice((0.0, 7.0)),
                                     asset_bits=r.uniform(0, 4))
        asg = Assignment(incident_id=iid, decision=decision, campaign_id=camp, reason_pl="syntetyczny test obciążenia",
                         model="dfbench@1", candidates=[])
        now = events.utcnow()
        genes = [(iid, f, k, f"{k}-{r.randint(0, 50)}", None, 1.0, "observed", "OBS", "{}", "dfbench@1", None, now)
                 for f, k in (("VIS", "pdq"), ("TXT", "ocr_line"), ("INF", "domain"), ("INF", "tracker_id"))]

        def fn(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO incidents (id, source, source_ref, title, observed_at, label, meta, created_at)"
                         " VALUES (?, 'synthetic:dataflow-bench', ?, ?, ?, NULL, '{}', ?)",
                         (iid, f"synthetic/{iid}", f"Syntetyczny materiał {self.n}", now[:10], now))
            conn.executemany("INSERT INTO genes (incident_id, family, kind, value, vector_ref, quality, status,"
                             " provenance, evidence, extractor, dependency_group, created_at) VALUES (?, ?, ?, ?, ?,"
                             " ?, ?, ?, ?, ?, ?, ?)", genes)
            _write_run(conn, run_id, "assign", {"base_run": self.base, "item": f"synthetic/{iid}"},
                       {"decision": decision}, pairs, None, [asg])

        return iid, fn


def cmd_inject(args: argparse.Namespace) -> None:
    from contextlib import closing

    from hydra.config import get_settings
    from hydra.db import Writer, connect

    os.environ["HYDRA_STATE_DIR"] = str(_guard_state(Path(args.state)))
    s = get_settings()
    from hydra.scheduler import api_running

    if api_running(s):
        sys.exit("an API is running on this state: stop it (single Writer)")
    with closing(connect(s.db_path, readonly=True)) as conn:
        inj = Injector(conn, args.seed)
    w = Writer(s.db_path)
    t = []
    try:
        for _ in range(args.n):
            _iid, fn = inj.tx()
            t0 = time.perf_counter()
            w.run(fn)
            t.append(time.perf_counter() - t0)
    finally:
        w.close()
    print(json.dumps({"base_run": inj.base, "n": args.n, "writer_s_p50": pct(t, 50), "writer_s_p95": pct(t, 95)}))


# --------------------------------------------------------------------------------------- views


VIEW_PATHS = ["/v1/dashboard", "/v1/system/flow", "/v1/graph/overview", "/v1/campaigns"]


def _with_run(path: str, run: str | None) -> str:
    return f"{path}{'&' if '?' in path else '?'}run_id={run}" if run else path


def views(url: str, repeat: int, label: str = "", run: str | None = None) -> dict[str, Any]:
    import httpx

    out: dict[str, Any] = {"url": url, "label": label, "run": run, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    with httpx.Client(base_url=url, timeout=900, headers={"Accept-Encoding": "gzip"}) as c:
        for p in VIEW_PATHS:
            rows = []
            live: dict[str, Any] = {}
            for i in range(repeat + 1):
                t0 = time.perf_counter()
                r = c.get(_with_run(p, run))
                dt = time.perf_counter() - t0
                rows.append({"s": round(dt, 4), "status": r.status_code, "cache": r.headers.get("x-hydra-cache"),
                             "wire_bytes": int(r.headers.get("content-length") or len(r.content)),
                             "bytes": len(r.content)})
                if i == 0 and r.status_code == 200:
                    live = r.json().get("live") or {}
            out[p] = {"first_s": rows[0]["s"], "first_cache": rows[0]["cache"],
                      "warm_p50_s": pct([x["s"] for x in rows[1:]], 50), "wire_bytes": rows[0]["wire_bytes"],
                      "live_seq": live.get("seq"), "live_new": live.get("new_incidents")}
    return out


def cmd_views(args: argparse.Namespace) -> None:
    print(json.dumps(views(args.url, args.repeat, args.label, args.run), indent=1))


def _serve_in_process(state: Path, port: int) -> tuple[Any, Any, threading.Thread, str]:
    import uvicorn

    os.environ.update(HYDRA_STATE_DIR=str(state), HYDRA_API_PORT=str(port), HYDRA_SCHEDULER="0", HYDRA_API_WARM="0")
    from hydra.api import create_app
    from hydra.config import get_settings

    app = create_app(get_settings())
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)
    return app, server, th, f"http://127.0.0.1:{port}"


def cmd_scenario(args: argparse.Namespace) -> None:
    """Same steps for any code version (PYTHONPATH selects it): cold + warm views in a fresh process, then new
    material through the app's single Writer and the first view requests after it."""
    from contextlib import closing

    from hydra.db import connect

    state = _guard_state(Path(args.state))
    app, server, th, url = _serve_in_process(state, args.port)
    res: dict[str, Any] = {"label": args.label, "state": str(state), "code": os.environ.get("PYTHONPATH", "")}
    try:
        res["cold"] = views(url, args.repeat, "cold", args.run)
        with closing(connect(app.state.hydra.settings.db_path, readonly=True)) as conn:
            inj = Injector(conn, args.seed, args.run)
        res["base_run"] = inj.base
        for k in (1, 10):
            t0 = time.perf_counter()
            for _ in range(k):
                app.state.hydra.writer.run(inj.tx()[1])
            res[f"assign_{k}_write_s"] = round(time.perf_counter() - t0, 3)
            res[f"after_assign_{k}"] = views(url, args.repeat, f"after {k} new assignment(s)", args.run)
    finally:
        server.should_exit = True
        th.join(timeout=60)
    print(json.dumps(res, indent=1))
    if args.out:
        Path(args.out).expanduser().write_text(json.dumps(res, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------------------- latency


def cmd_client(args: argparse.Namespace) -> None:
    import httpx

    out = open(args.out, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally
    seen: set[str] = set()
    t_end = time.time() + args.timeout
    try:
        with httpx.Client(timeout=None) as c, c.stream("GET", _with_run(f"{args.url}/v1/stream", args.run)) as r:
            event, data = None, ""
            for line in r.iter_lines():
                now = time.time()
                if line.startswith("event: "):
                    event = line[7:]
                elif line.startswith("data: "):
                    data += line[6:]
                elif line == "" and event:
                    d = json.loads(data) if data else None
                    rec: dict[str, Any] = {"t": now, "event": event, "bytes": len(data)}
                    if event == "delta" and d is not None:
                        ids = [n["id"] for n in d.get("nodes") or []]
                        seen.update(ids)
                        rec.update(seq=d["seq"], n=d.get("n"), nodes=ids, delta=d if args.keep else None)
                    elif d is not None:
                        rec["data"] = d
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
                    event, data = None, ""
                    if args.expect and len(seen) >= args.expect:
                        break
                if now > t_end:
                    break
    finally:
        ru = resource.getrusage(resource.RUSAGE_SELF)
        out.write(json.dumps({"event": "_client_cpu", "user_s": ru.ru_utime, "sys_s": ru.ru_stime}) + "\n")
        out.close()


def cmd_latency(args: argparse.Namespace) -> None:
    from contextlib import closing

    import httpx
    import uvicorn

    state = _guard_state(Path(args.state))
    os.environ.update(HYDRA_STATE_DIR=str(state), HYDRA_API_PORT=str(args.port), HYDRA_SCHEDULER="0",
                      HYDRA_API_WARM="0")
    from hydra.api import create_app
    from hydra.config import get_settings
    from hydra.db import connect

    s = get_settings()
    app = create_app(s)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{args.port}"
    t0 = time.perf_counter()
    with httpx.Client(base_url=url, timeout=600) as c:
        c.get(_with_run("/v1/dashboard", args.run))  # materialises the run's aggregates once (timed separately)
    prep_s = time.perf_counter() - t0
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    client_log = out_dir / "client.jsonl"
    client = subprocess.Popen([sys.executable, __file__, "client", "--url", url, "--out", str(client_log),
                               *(["--run", args.run] if args.run else []),
                               "--expect", str(args.n), "--timeout", str(args.timeout), *(["--keep"] if args.keep
                                                                                        else [])])
    for _ in range(200):  # wait for the client's hello
        if client_log.exists() and '"hello"' in client_log.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    writer = app.state.hydra.writer
    snap = ("/v1/dashboard", "/v1/campaigns", "/v1/graph/overview",
            "/v1/graph/overview?levels=campaign,kit,asset,review")

    def save(tag: str) -> None:  # GET payloads for the UI replay (patch(before, deltas) == after)
        with httpx.Client(base_url=url, timeout=600) as c:
            for i, path in enumerate(snap):
                body = c.get(_with_run(path, args.run)).json()
                (out_dir / f"{tag}-{i}.json").write_text(json.dumps({"path": path, "body": body}),
                                                         encoding="utf-8")

    if args.keep:
        save("before")
    with closing(connect(s.db_path, readonly=True)) as conn:
        inj = Injector(conn, args.seed, args.run)
    submit: dict[str, float] = {}
    commit_s: list[float] = []
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    w0 = time.perf_counter()
    gap = 1.0 / args.rate if args.rate else 0.0
    for i in range(args.n):
        iid, fn = inj.tx()
        t = time.time()
        submit[iid] = t
        writer.run(fn)
        commit_s.append(time.time() - t)
        if gap:
            nxt = w0 + (i + 1) * gap
            time.sleep(max(0.0, nxt - time.perf_counter()))
    write_wall = time.perf_counter() - w0
    client.wait(timeout=args.timeout + 30)
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    stats = httpx.get(f"{url}/v1/stream/stats").json()
    if args.keep:
        save("after")
    with httpx.Client(base_url=url, timeout=600) as c:
        t1 = time.perf_counter()
        dash = c.get(_with_run("/v1/dashboard", args.run)).json()
        dash_s = time.perf_counter() - t1
    server.should_exit = True
    th.join(timeout=30)
    recv: dict[str, float] = {}
    msgs = []
    client_cpu = None
    for line in client_log.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec["event"] == "delta":
            msgs.append(rec)
            for nid in rec["nodes"]:
                recv.setdefault(nid, rec["t"])
        elif rec["event"] == "_client_cpu":
            client_cpu = rec
    lat = [recv[i] - submit[i] for i in submit if i in recv]
    res = {
        "state": str(state), "base_run": inj.base, "n": args.n, "rate_per_s": args.rate or "max",
        "aggregates_ready_s": round(prep_s, 3), "write_wall_s": round(write_wall, 3),
        "writer_commit_s": {"p50": pct(commit_s, 50), "p95": pct(commit_s, 95), "max": pct(commit_s, 100)},
        "event_to_sse_s": {"received": len(lat), "p50": pct(lat, 50), "p95": pct(lat, 95), "max": pct(lat, 100)},
        "messages": len(msgs), "deltas_per_message_mean": round(statistics.mean([m.get("n") or 1 for m in msgs]), 2)
        if msgs else None, "message_bytes_p95": pct([m["bytes"] for m in msgs], 95),
        "message_bytes_max": pct([m["bytes"] for m in msgs], 100),
        "server_cpu_s": round((ru1.ru_utime - ru0.ru_utime) + (ru1.ru_stime - ru0.ru_stime), 3),
        "client_cpu_s": round(client_cpu["user_s"] + client_cpu["sys_s"], 3) if client_cpu else None,
        "hub": stats, "dashboard_after_s": round(dash_s, 3),
        "dashboard_new_incidents": (dash.get("live") or {}).get("new_incidents"),
    }
    (out_dir / "latency.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("views")
    v.add_argument("--run", default=None, help="base analyse run (default: latest)")
    v.add_argument("--url", default="http://127.0.0.1:8800")
    v.add_argument("--repeat", type=int, default=3)
    v.add_argument("--label", default="")
    sc = sub.add_parser("scenario")
    sc.add_argument("--run", default=None, help="base analyse run (default: latest)")
    sc.add_argument("--state", required=True)
    sc.add_argument("--port", type=int, default=8800)
    sc.add_argument("--repeat", type=int, default=3)
    sc.add_argument("--seed", type=int, default=7)
    sc.add_argument("--label", default="")
    sc.add_argument("--out", default="")
    i = sub.add_parser("inject")
    i.add_argument("--state", required=True)
    i.add_argument("--n", type=int, default=1)
    i.add_argument("--seed", type=int, default=7)
    lt = sub.add_parser("latency")
    lt.add_argument("--run", default=None, help="base analyse run (default: latest)")
    lt.add_argument("--state", required=True)
    lt.add_argument("--port", type=int, default=8800)
    lt.add_argument("--n", type=int, default=1000)
    lt.add_argument("--rate", type=float, default=50.0, help="assignments per second (0 = as fast as possible)")
    lt.add_argument("--seed", type=int, default=7)
    lt.add_argument("--timeout", type=float, default=600.0)
    lt.add_argument("--keep", action="store_true", help="store full deltas in client.jsonl (for the UI replay)")
    lt.add_argument("--out", default="/tmp/dataflow-bench")
    cl = sub.add_parser("client")
    cl.add_argument("--run", default=None, help="base analyse run (default: latest)")
    cl.add_argument("--url", required=True)
    cl.add_argument("--out", required=True)
    cl.add_argument("--expect", type=int, default=0)
    cl.add_argument("--timeout", type=float, default=600.0)
    cl.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    {"views": cmd_views, "inject": cmd_inject, "latency": cmd_latency, "client": cmd_client,
     "scenario": cmd_scenario}[args.cmd](args)


if __name__ == "__main__":
    main()
