"""DATAFLOW live stream (GET /v1/stream, docs/ui/STREAM_API.md) against a real uvicorn server on loopback.

Synthetic rows only (tests/core/dataflow_world.py); every write goes through the app's single Writer.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from core.dataflow_world import RUN, World

from hydra import api_cache, api_stream
from hydra.api import create_app
from hydra.config import Settings


class Server:
    def __init__(self, settings: Settings) -> None:
        self.app = create_app(settings)
        cfg = uvicorn.Config(self.app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        t0 = time.monotonic()
        while not self.server.started:
            if time.monotonic() - t0 > 20:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    @property
    def writer(self):
        return self.app.state.hydra.writer

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)


@pytest.fixture
def server(settings: Settings) -> Iterator[Server]:
    api_cache.clear_memory()
    srv = Server(settings)
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def world(server: Server) -> World:
    w = World(server.writer, random.Random(42))
    w.build_base(n_incidents=20, n_campaigns=5)
    return w


def read_events(resp: httpx.Response, *, until: Any, timeout: float = 15.0) -> list[dict[str, Any]]:
    """Parse SSE frames until `until(events)` is true."""
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] = {}
    t0 = time.monotonic()
    it = getattr(resp, "_sse_lines", None)
    if it is None:  # httpx allows one iteration per response: keep the line iterator on the response
        it = resp._sse_lines = resp.iter_lines()  # type: ignore[attr-defined]
    for line in it:
        if time.monotonic() - t0 > timeout:
            raise AssertionError(f"timeout; got {[e.get('event') for e in out]}")
        if line == "":
            if cur:
                if "data" in cur:
                    cur["data"] = json.loads(cur["data"])
                out.append(cur)
                cur = {}
                if until(out):
                    return out
            continue
        if line.startswith(":"):
            out.append({"event": "comment", "data": line})
            if until(out):
                return out
            continue
        k, _, v = line.partition(": ")
        cur[k] = v if k != "data" else cur.get("data", "") + v
    return out


def deltas(evs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in evs if e.get("event") == "delta"]


def new_seen(evs: list[dict[str, Any]]) -> set[str]:
    return {n["id"] for e in deltas(evs) for n in e["data"].get("nodes") or []}


def test_stream_hello_coalesced_deltas_and_consistency(server: Server, world: World) -> None:
    with httpx.Client(base_url=server.base, timeout=30) as c, c.stream("GET", "/v1/stream") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in r.headers  # never gzip-buffered
        hello = read_events(r, until=lambda ev: any(e.get("event") == "hello" for e in ev))
        h = next(e for e in hello if e.get("event") == "hello")["data"]
        assert h["run_id"] == RUN and h["status"] == "ready" and h["coalesce_ms"] == 250
        for _ in range(6):
            world.assign(n_pairs=3)
        evs = read_events(r, until=lambda ev: len(new_seen(ev)) >= 6)
    ds = deltas(evs)
    assert len(ds) < 6, "rapid assignments are coalesced into fewer messages"
    ids = [int(e["id"]) for e in ds]
    assert ids == sorted(ids) and all(int(e["id"]) == e["data"]["seq"] for e in ds)
    last = ds[-1]["data"]
    assert last["kpis"]["new_incidents"] == 6 and sum(e["data"]["n"] for e in ds) == 6
    with httpx.Client(base_url=server.base, timeout=30) as c:
        dash = c.get("/v1/dashboard").json()
        ov = c.get("/v1/graph/overview").json()
        camps = c.get("/v1/campaigns").json()
    assert dash["live"]["seq"] == last["seq"] and dash["live"]["version"] == last["v"]
    assert {k: v for k, v in dash["kpis"].items()} == {k: v for k, v in last["kpis"].items() if k != "new_incidents"}
    assert {p["id"] for p in dash["previews"] if p.get("new")} == set(world.new)
    assert {n["id"] for n in ov["live"]["nodes"]} == set(world.new)
    sizes = {c_["id"]: (c_["size"], c_["new"]) for c_ in camps["items"]}
    for cm in last.get("campaigns") or []:
        if not cm["solo"] and cm["id"] in sizes:
            assert sizes[cm["id"]] == (cm["size"], cm["new"])


def test_resume_with_last_event_id_and_gap(server: Server, world: World) -> None:
    with httpx.Client(base_url=server.base, timeout=30) as c, c.stream("GET", "/v1/stream") as r:
        read_events(r, until=lambda ev: any(e.get("event") == "hello" for e in ev))
        world.assign(n_pairs=2)
        first = read_events(r, until=lambda ev: len(new_seen(ev)) >= 1)
    first_id = int(deltas(first)[-1]["id"])
    world.assign(n_pairs=2)
    world.assign(n_pairs=2)
    with httpx.Client(base_url=server.base, timeout=30) as c, \
            c.stream("GET", "/v1/stream", headers={"Last-Event-ID": str(first_id)}) as r:
        evs = read_events(r, until=lambda ev: len(new_seen(ev)) >= 2)
    hello = next(e for e in evs if e.get("event") == "hello")["data"]
    assert hello["resumed"] is True and hello["replayed"] == 2
    assert new_seen(evs) == set(world.new[1:])
    from hydra import dataflow

    dataflow.build(server.app.state.hydra.settings.db_path, server.writer, RUN)  # new base: older ids are gone
    with httpx.Client(base_url=server.base, timeout=30) as c, \
            c.stream("GET", "/v1/stream", params={"last_event_id": first_id}) as r:
        evs = read_events(r, until=lambda ev: any(e.get("event") == "reset" for e in ev))
    reset = next(e for e in evs if e.get("event") == "reset")["data"]
    assert reset["reason"] == "gap"


def test_reviews_exports_and_jobs_stream(server: Server, world: World) -> None:
    with httpx.Client(base_url=server.base, timeout=30) as c, c.stream("GET", "/v1/stream") as r:
        read_events(r, until=lambda ev: any(e.get("event") == "hello" for e in ev))
        world.export()
        evs = read_events(r, until=lambda ev: any("export" in (e["data"].get("kinds") or []) for e in deltas(ev)))
        d = next(e for e in deltas(evs) if "export" in e["data"]["kinds"])["data"]
        assert d["exports"]["total"] == 1
        world.review()
        evs = read_events(r, until=lambda ev: any("review" in (e["data"].get("kinds") or []) for e in deltas(ev))
                          or len(ev) > 50, timeout=5)
        from hydra.jobs import create_job

        jid = create_job(server.writer, "import", {"source": "test"})
        evs = read_events(r, until=lambda ev: any(e.get("event") == "job" and e["data"]["id"] == jid for e in ev))
        job = next(e for e in evs if e.get("event") == "job")["data"]
        assert job["status"] == "queued" and job["kind"] == "import"


def test_heartbeat(server: Server, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_stream, "HEARTBEAT_S", 0.3)
    with httpx.Client(base_url=server.base, timeout=30) as c, c.stream("GET", "/v1/stream") as r:
        evs = read_events(r, until=lambda ev: any(e.get("event") == "comment" and "hb" in e["data"] for e in ev),
                          timeout=5)
    assert any("hb" in e["data"] for e in evs if e.get("event") == "comment")


def test_local_only_guard_and_origin(server: Server, world: World) -> None:
    with httpx.Client(base_url=server.base, timeout=10) as c:
        assert c.get("/v1/stream", headers={"Host": "evil.example"}).status_code == 403
        assert c.get("/v1/stream", headers={"Origin": "https://evil.example"}).status_code == 403


def test_new_run_resets_followers(server: Server, world: World) -> None:
    import sqlite3

    from hydra import events

    with httpx.Client(base_url=server.base, timeout=30) as c, c.stream("GET", "/v1/stream") as r:
        read_events(r, until=lambda ev: any(e.get("event") == "hello" for e in ev))

        def tx(conn: sqlite3.Connection) -> None:
            now = events.utcnow()
            conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES"
                         " ('syn-df-run-2', 'analyse', '{}', '{}', ?, ?)", ("2999-01-01T00:00:00+00:00", now))
            events.append(conn, "RUN_FINISHED", {"run_id": "syn-df-run-2", "kind": "analyse", "pairs": 0})

        server.writer.run(tx)
        evs = read_events(r, until=lambda ev: any(e.get("event") == "reset" for e in ev))
    reset = next(e for e in evs if e.get("event") == "reset")["data"]
    assert reset["reason"] == "new_run" and reset["latest_run"] == "syn-df-run-2"


def test_watch_alerts_are_forwarded_as_candidates(server: Server, world: World) -> None:
    class FakeWatch:  # the HYDRA Watch engine interface used by the hub (alerts_since)
        def __init__(self) -> None:
            self.rows: list[dict[str, Any]] = []

        def alerts_since(self, since: int, limit: int) -> list[dict[str, Any]]:
            return [r for r in self.rows if r["alert_seq"] > since][:limit]

    eng = FakeWatch()
    server.app.state.watch = eng
    with httpx.Client(base_url=server.base, timeout=30) as c, c.stream("GET", "/v1/stream") as r:
        read_events(r, until=lambda ev: any(e.get("event") == "hello" for e in ev))
        eng.rows.append({"alert_seq": 1, "event_id": "w1", "host_defanged": "example[.]pl", "alerts": []})
        world.export()  # any commit wakes the pump
        evs = read_events(r, until=lambda ev: any(e.get("event") == "watch" for e in ev))
    w = next(e for e in evs if e.get("event") == "watch")["data"]
    assert w["event_id"] == "w1" and "kandydat" in w["note_pl"]


# --------------------------------------------------------------------------------------- units


def test_merge_semantics() -> None:
    a = {"run_id": "r", "v": 1, "seq": 10, "from_seq": 10, "n": 1, "kinds": ["assign"], "kpis": {"incidents": 1},
         "counts": {"decision": {"assign": [1, 1]}, "corpus": {"nask": [5, 1]}},
         "campaigns": [{"id": "c1", "size": 3}], "nodes": [{"id": "n1"}],
         "links": [{"source": "c1", "target": "c2", "level": "kit", "pairs": 1},
                   {"source": "c1", "target": "c2", "level": "campaign", "pairs": 1}],
         "edges": [{"source": "n1", "target": "b1", "bits": 3}]}
    b = {"run_id": "r", "v": 2, "seq": 12, "from_seq": 12, "n": 1, "kinds": ["review"], "kpis": {"incidents": 2},
         "counts": {"decision": {"assign": [2, 2]}}, "campaigns": [{"id": "c1", "size": 4}, {"id": "c3", "size": 1}],
         "links": [{"source": "c1", "target": "c2", "level": "kit", "pairs": 2}], "removed_nodes": ["n1"]}
    m = api_stream.merge(a, b)
    assert m["seq"] == 12 and m["from_seq"] == 10 and m["n"] == 2 and m["kinds"] == ["assign", "review"]
    assert m["kpis"] == {"incidents": 2}
    assert m["counts"] == {"decision": {"assign": [2, 2]}, "corpus": {"nask": [5, 1]}}
    assert {c["id"]: c["size"] for c in m["campaigns"]} == {"c1": 4, "c3": 1}
    assert m["links"] == [{"source": "c1", "target": "c2", "level": "kit", "pairs": 2}]  # key replaced, not merged
    assert m["removed_nodes"] == ["n1"] and not m.get("nodes")
    assert m["edges"] == a["edges"]


def test_subscriber_overflow_flag() -> None:
    import asyncio

    async def go() -> api_stream.Subscriber:
        s = api_stream.Subscriber("r", False)
        for i in range(api_stream.QUEUE_MAX + 5):
            s.offer(("row", i))
        return s

    s = asyncio.run(go())
    assert s.overflow is True and s.queue.qsize() == api_stream.QUEUE_MAX
