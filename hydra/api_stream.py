"""Live stream of materialised deltas: `GET /v1/stream` (Server-Sent Events). DATAFLOW, ADR-0009.

Contract: docs/ui/STREAM_API.md. One hub per process:

- the single Writer calls `Hub.notify` after every commit (O(1): sets an asyncio event via the loop), and the hub
  also polls every second (writes from another process, e.g. a CLI);
- one pump task reads new `agg_deltas` rows (read-only connection, in a worker thread, never on the writer) and
  fans them out to bounded per-client queues; job rows and HYDRA Watch alerts are forwarded as ephemeral events;
- each client coalesces deltas of its run for `COALESCE_S` (or until `MAX_MESSAGE_BYTES`) into one message whose
  SSE `id` is the last `events.seq` included; a slow client whose queue fills up gets `reset` (overflow) and
  refetches instead of blocking anyone; heartbeats every `HEARTBEAT_S`;
- resume: `Last-Event-ID` (or `last_event_id`) replays stored deltas after that seq; an id older than the run's
  current base, or beyond the retained deltas, gets `reset` (gap).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from contextlib import closing
from typing import Any

import orjson
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from hydra import dataflow

log = logging.getLogger("hydra.api_stream")
router = APIRouter(prefix="/v1", tags=["stream"])

COALESCE_S = 0.25
HEARTBEAT_S = 15.0
POLL_S = 1.0
JOB_MIN_INTERVAL_S = 0.25
MAX_MESSAGE_BYTES = 256 * 1024
QUEUE_MAX = 2000
REPLAY_MAX = 5000


def _sse(event: str, data: Any, id_: int | None = None) -> bytes:
    head = f"id: {id_}\n" if id_ is not None else ""
    return f"{head}event: {event}\ndata: ".encode() + orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS) + b"\n\n"


# --------------------------------------------------------------------------------------- merging


def merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Coalesce two deltas of one run (b newer). Counters are absolute (b wins), lists are upserts by key; links
    are replaced per (source, target) because a delta carries every level of a changed key."""
    out = dict(b)
    out["from_seq"] = min(a.get("from_seq", a["seq"]), b.get("from_seq", b["seq"]))
    out["n"] = a.get("n", 1) + b.get("n", 1)
    out["kinds"] = sorted(set(a.get("kinds") or []) | set(b.get("kinds") or []))
    counts = {d: dict(v) for d, v in (a.get("counts") or {}).items()}
    for d, v in (b.get("counts") or {}).items():
        counts.setdefault(d, {}).update(v)
    out["counts"] = counts
    for name, key in (("campaigns", "id"), ("groups", "id"), ("nodes", "id")):
        if name in a or name in b:
            by = {x[key]: x for x in a.get(name) or []}
            by.update({x[key]: x for x in b.get(name) or []})
            out[name] = list(by.values())
    if "links" in a or "links" in b:
        keep = {(x["source"], x["target"]) for x in b.get("links") or []}
        out["links"] = [x for x in a.get("links") or [] if (x["source"], x["target"]) not in keep] + list(
            b.get("links") or [])
    if "edges" in a or "edges" in b:
        by = {(x["source"], x["target"]): x for x in a.get("edges") or []}
        by.update({(x["source"], x["target"]): x for x in b.get("edges") or []})
        out["edges"] = list(by.values())
    removed = (set(a.get("removed_nodes") or []) - {n["id"] for n in b.get("nodes") or []}) | set(
        b.get("removed_nodes") or [])
    if removed:
        out["removed_nodes"] = sorted(removed)
        if out.get("nodes"):
            out["nodes"] = [n for n in out["nodes"] if n["id"] not in set(b.get("removed_nodes") or [])]
    for name in ("top_genes", "exports"):
        if name not in b and name in a:
            out[name] = a[name]
    return out


# --------------------------------------------------------------------------------------- hub


class Subscriber:
    def __init__(self, run: str | None, follow_latest: bool) -> None:
        self.run = run
        self.follow_latest = follow_latest
        self.queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.overflow = False
        self.sent = 0
        self.deltas = 0

    def offer(self, item: tuple[str, Any]) -> None:
        if self.overflow:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.overflow = True  # the sender drains and sends `reset`


class Hub:
    def __init__(self, app_state: Any, loop: asyncio.AbstractEventLoop) -> None:
        self.st = app_state
        self.loop = loop
        self.wake = asyncio.Event()
        self.subs: set[Subscriber] = set()
        self.cursor = 0
        self.stats = {"deltas_read": 0, "messages_sent": 0, "coalesced": 0, "overflows": 0, "resets_sent": 0,
                      "jobs_sent": 0, "watch_sent": 0, "pump_reads": 0, "pump_read_ms_max": 0.0}
        self._jobs: dict[str, tuple[Any, ...]] = {}
        self._jobs_at = 0.0
        self._alert_seq = 0
        self._rt_seq = 0  # WATCH-RT event ring cursor (hydra.watch.rt.engine.WatchRT.events_since)
        self.watch_rt: Any = None
        self._latest: str | None = None
        self._conn: sqlite3.Connection | None = None
        self.watch: Any = None
        with closing(self._connect()) as conn:
            self.cursor = self._max_id(conn)
            self._latest = self._latest_run(conn)
            self._alert_seq = 0
        app_state.writer.add_listener(self.notify)
        self.task = loop.create_task(self._pump(), name="hydra-stream-pump")

    # ------------------------------------------------------------------ plumbing
    def _connect(self) -> sqlite3.Connection:
        from hydra.db import connect

        return connect(self.st.settings.db_path, readonly=True)

    @staticmethod
    def _max_id(conn: sqlite3.Connection) -> int:
        try:
            r = conn.execute("SELECT MAX(id) FROM agg_deltas").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(r[0] or 0)

    @staticmethod
    def _latest_run(conn: sqlite3.Connection) -> str | None:
        from hydra.api_task import resolve_run

        return resolve_run(conn, None)

    def notify(self) -> None:
        """Writer thread, after each commit."""
        with contextlib.suppress(RuntimeError):  # loop closed during shutdown
            self.loop.call_soon_threadsafe(self.wake.set)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.st.writer.remove_listener(self.notify)
        self.task.cancel()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                self._conn.close()

    # ------------------------------------------------------------------ pump
    def _read(self) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]], str | None]:
        if self._conn is None:
            self._conn = self._connect()
        conn = self._conn
        t0 = time.perf_counter()
        rows = conn.execute("SELECT id, run_id, seq, version, kind, bytes, payload FROM agg_deltas WHERE id>?"
                            " ORDER BY id LIMIT 1000", (self.cursor,)).fetchall()
        jobs: list[dict[str, Any]] = []
        now = time.monotonic()
        if now - self._jobs_at >= JOB_MIN_INTERVAL_S:
            self._jobs_at = now
            jobs = self._job_changes(conn)
        latest = self._latest_run(conn) if any(r[4] == "run" for r in rows) else self._latest
        ms = (time.perf_counter() - t0) * 1000
        self.stats["pump_reads"] += 1
        self.stats["pump_read_ms_max"] = max(self.stats["pump_read_ms_max"], round(ms, 2))
        return rows, jobs, latest

    def _skip(self) -> None:
        if self._conn is None:
            self._conn = self._connect()
        self.cursor = max(self.cursor, self._max_id(self._conn))

    def _job_changes(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        from hydra.progress import progress_dir

        out = []
        known = list(self._jobs)
        rows = conn.execute("SELECT id, kind, status, total, done, failed FROM jobs WHERE status IN ('queued',"
                            f" 'running') OR id IN ({','.join('?' * len(known)) or 'NULL'})", known).fetchall()
        for r in rows:
            stage = None
            if r[1] == "analyse" and r[2] == "running":
                with contextlib.suppress(OSError, ValueError):
                    p = progress_dir(self.st.settings) / f"{r[0]}.json"
                    if p.is_file():
                        stage = orjson.loads(p.read_bytes()).get("stage")
            sig = (r[2], r[3], r[4], r[5], stage)
            if self._jobs.get(r[0]) != sig:
                self._jobs[r[0]] = sig
                out.append({"id": r[0], "kind": r[1], "status": r[2], "total": r[3], "done": r[4], "failed": r[5],
                            "stage": stage, "progress": (r[4] + r[5]) / r[3] if r[3] else 0.0})
        for jid in [j for j, sig in self._jobs.items() if sig[0] in ("done", "failed")]:
            del self._jobs[jid]  # reported once in a terminal state
        return out

    def _watch_alerts(self) -> list[dict[str, Any]]:
        eng = self.watch
        if eng is None:
            return []
        try:
            rows = eng.alerts_since(self._alert_seq, 200)
        except Exception:  # noqa: BLE001 — the watch engine is optional; never stop the pump
            return []
        if rows:
            self._alert_seq = max(r["alert_seq"] for r in rows)
        return rows

    def _watch_rt_events(self) -> list[dict[str, Any]]:
        eng = self.watch_rt
        if eng is None:
            return []
        try:
            rows = eng.events_since(self._rt_seq, 500)
        except Exception:  # noqa: BLE001 — optional engine; never stop the pump
            return []
        if rows:
            self._rt_seq = rows[-1]["seq"]
        elif self._rt_seq > eng.seq:
            self._rt_seq = 0
        return rows

    async def _pump(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.wake.wait(), timeout=POLL_S)
            self.wake.clear()
            if not self.subs:  # nobody listens: only keep the cursor current
                await asyncio.to_thread(self._skip)
                if self.watch_rt is not None:
                    self._rt_seq = self.watch_rt.seq
                continue
            try:
                rows, jobs, latest = await asyncio.to_thread(self._read)
            except Exception:  # noqa: BLE001 — e.g. state being replaced; try again on the next tick
                log.warning("stream pump: read failed", exc_info=True)
                await asyncio.sleep(POLL_S)
                continue
            for r in rows:
                self.cursor = max(self.cursor, r[0])
                self.stats["deltas_read"] += 1
                if r[4] == "run":  # a new analyse run exists
                    self._latest = latest
                    for s in list(self.subs):
                        if s.follow_latest and latest and latest != s.run:
                            s.offer(("reset", {"reason": "new_run", "run_id": s.run, "latest_run": latest,
                                               "seq": r[2]}))
                    continue
                for s in list(self.subs):
                    if s.run == r[1]:
                        s.offer(("row", r))
            for j in jobs:
                for s in list(self.subs):
                    s.offer(("job", j))
                self.stats["jobs_sent"] += 1
            for a in self._watch_alerts():
                for s in list(self.subs):
                    s.offer(("watch", a))
                self.stats["watch_sent"] += 1
            for ev in self._watch_rt_events():
                for s in list(self.subs):
                    s.offer(("watch_rt", ev))
                self.stats["watch_rt_sent"] = self.stats.get("watch_rt_sent", 0) + 1
            if len(rows) == 1000:
                self.wake.set()  # more to read


def hub_for(request: Request) -> Hub:
    app = request.app
    hub = getattr(app.state, "stream_hub", None)
    if hub is None or hub.task.done():
        hub = Hub(app.state.hydra, asyncio.get_running_loop())
        app.state.stream_hub = hub
    hub.watch = getattr(app.state, "watch", None)  # HYDRA Watch engine, created lazily by /v1/watch/*
    rt = getattr(app.state, "watch_rt", None)  # WATCH-RT engine (/v1/watch/rt/*)
    if rt is not None and hub.watch_rt is not rt:
        hub.watch_rt, hub._rt_seq = rt, rt.seq
    return hub


# --------------------------------------------------------------------------------------- endpoint


def _origin_ok(request: Request) -> bool:
    from hydra.api import _LOCAL_HOSTS, _origin_host

    origin = request.headers.get("origin")
    return not origin or origin == "null" or _origin_host(origin) in _LOCAL_HOSTS


def _prepare(st: Any, run_id: str | None, last: int) -> dict[str, Any]:
    """Thread: resolve the run, make sure its aggregates exist, read the replay."""
    from hydra.api_task import resolve_run

    with st.reader() as conn:
        latest = resolve_run(conn, None)
        run = run_id or latest
        ready = dataflow.ensure(st.settings.db_path, st.writer, conn, run) if run else False
        info = dataflow.live_info(conn, run)
        srow = dataflow.state_row(conn, run) if run and ready else None
        replay: list[tuple[Any, ...]] = []
        gap = False
        if last and srow is not None:
            if last < srow["base_seq"]:
                gap = True
            else:
                replay = conn.execute(
                    "SELECT id, run_id, seq, version, kind, bytes, payload FROM agg_deltas WHERE run_id=? AND seq>?"
                    " AND kind!='reset' ORDER BY id LIMIT ?", (run, last, REPLAY_MAX + 1)).fetchall()
                first = conn.execute("SELECT MIN(seq) FROM agg_deltas WHERE run_id=?", (run,)).fetchone()[0]
                if len(replay) > REPLAY_MAX or (first is not None and last < first - 1 and srow["seq"] > last
                                                and not replay):
                    gap, replay = True, []
        return {"run": run, "latest": latest, "ready": ready, "info": info, "srow": srow, "replay": replay,
                "gap": gap}


async def _events(request: Request, hub: Hub, sub: Subscriber, prep: dict[str, Any], last: int):
    run = prep["run"]
    info = prep["info"]
    yield b"retry: 3000\n\n"
    yield _sse("hello", {"run_id": run, "latest_run": prep["latest"], "version": info["version"],
                         "seq": info["seq"], "status": info["status"],
                         "base_finished_at": info["base_finished_at"], "resumed": bool(last) and not prep["gap"],
                         "replayed": len(prep["replay"]), "heartbeat_s": HEARTBEAT_S,
                         "coalesce_ms": int(COALESCE_S * 1000), "max_message_bytes": MAX_MESSAGE_BYTES})
    sent_seq = last if last and not prep["gap"] else info["seq"]  # a fresh client starts at the GET state
    if prep["gap"]:
        hub.stats["resets_sent"] += 1
        yield _sse("reset", {"reason": "gap", "run_id": run, "latest_run": prep["latest"]}, info["seq"])
    pending: dict[str, Any] | None = None
    pending_bytes = 0
    deadline: float | None = None
    last_beat = time.monotonic()

    def flush() -> bytes | None:
        nonlocal pending, pending_bytes, deadline, sent_seq
        if pending is None:
            return None
        msg, pending, pending_bytes, deadline = pending, None, 0, None
        sent_seq = msg["seq"]
        hub.stats["messages_sent"] += 1
        hub.stats["coalesced"] += msg.get("n", 1) - 1
        sub.sent += 1
        return _sse("delta", msg, msg["seq"])

    def take(row: tuple[Any, ...]) -> list[bytes]:
        nonlocal pending, pending_bytes, deadline
        out: list[bytes] = []
        if row[2] <= sent_seq:
            return out  # already delivered (replay overlap) or older than the state the client started from
        if row[4] == "reset":
            if (b := flush()) is not None:
                out.append(b)
            hub.stats["resets_sent"] += 1
            out.append(_sse("reset", {"reason": "rebuilt", "run_id": row[1], "latest_run": hub._latest}, row[2]))
            return out
        if row[5] > MAX_MESSAGE_BYTES:
            if (b := flush()) is not None:
                out.append(b)
            hub.stats["resets_sent"] += 1
            out.append(_sse("reset", {"reason": "too_large", "run_id": row[1], "latest_run": hub._latest}, row[2]))
            return out
        if pending is not None and pending_bytes + row[5] > MAX_MESSAGE_BYTES:
            if (b := flush()) is not None:
                out.append(b)
        delta = orjson.loads(row[6])
        sub.deltas += 1
        pending = delta if pending is None else merge(pending, delta)
        pending_bytes += row[5]
        if deadline is None:
            deadline = time.monotonic() + COALESCE_S
        return out

    for row in prep["replay"]:
        for b in take(row):
            yield b
    if (b := flush()) is not None:
        yield b
    while True:
        now = time.monotonic()
        timeout = (deadline - now) if deadline is not None else max(0.0, HEARTBEAT_S - (now - last_beat))
        try:
            kind, item = await asyncio.wait_for(sub.queue.get(), timeout=max(0.0, timeout))
        except TimeoutError:
            if deadline is not None and time.monotonic() >= deadline:
                if (b := flush()) is not None:
                    yield b
            if time.monotonic() - last_beat >= HEARTBEAT_S:
                last_beat = time.monotonic()
                yield f": hb {int(time.time())}\n\n".encode()
            if await request.is_disconnected():
                return
            continue
        if sub.overflow:
            while not sub.queue.empty():
                sub.queue.get_nowait()
            sub.overflow = False
            pending, pending_bytes, deadline = None, 0, None
            hub.stats["overflows"] += 1
            hub.stats["resets_sent"] += 1
            yield _sse("reset", {"reason": "overflow", "run_id": run, "latest_run": hub._latest})
            continue
        if kind == "row":
            for b in take(item):
                yield b
        elif kind == "reset":
            if (b := flush()) is not None:
                yield b
            hub.stats["resets_sent"] += 1
            yield _sse("reset", item, item.get("seq"))
        elif kind == "job":
            yield _sse("job", item)
        elif kind == "watch_rt":  # WATCH-RT: watch.poll | watch.item | watch.match | watch.source_state
            yield _sse(item["type"], {**item["data"], "watch_seq": item["seq"]})
        elif kind == "watch":
            yield _sse("watch", {**item, "note_pl": "Alert to kandydat: dopasowanie odcisku kampanii, nie"
                                                   " potwierdzony członek."})
        if deadline is not None and time.monotonic() >= deadline:
            if (b := flush()) is not None:
                yield b


@router.get("/stream")
async def stream(request: Request, run_id: str | None = None,
                 last_event_id: int | None = Query(None, ge=0)) -> StreamingResponse:
    """Server-Sent Events of materialised deltas, job progress and Watch alerts (docs/ui/STREAM_API.md)."""
    if not _origin_ok(request):
        raise HTTPException(status_code=403, detail="Odrzucono strumień z innego pochodzenia (Origin).")
    header = request.headers.get("last-event-id")
    last = last_event_id if last_event_id is not None else (int(header) if header and header.isdigit() else 0)
    hub = hub_for(request)
    st = request.app.state.hydra
    sub = Subscriber(run_id, follow_latest=run_id is None)
    hub.subs.add(sub)  # before the replay read: nothing that lands in between is lost (dedup by seq)
    try:
        prep = await asyncio.to_thread(_prepare, st, run_id, last)
    except BaseException:
        hub.subs.discard(sub)
        raise
    sub.run = prep["run"]

    async def body():
        try:
            async for chunk in _events(request, hub, sub, prep, last):
                yield chunk
        finally:
            hub.subs.discard(sub)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/stream/stats")
def stream_stats(request: Request) -> dict[str, Any]:
    hub = getattr(request.app.state, "stream_hub", None)
    if hub is None:
        return {"subscribers": 0, "cursor": 0, "stats": {}}
    return {"subscribers": len(hub.subs), "cursor": hub.cursor, "latest_run": hub._latest, "stats": dict(hub.stats),
            "clients": [{"run_id": s.run, "follow_latest": s.follow_latest, "deltas": s.deltas, "sent": s.sent,
                         "queued": s.queue.qsize()} for s in list(hub.subs)]}
