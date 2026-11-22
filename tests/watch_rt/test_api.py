"""`/v1/watch/rt/*` endpoints (TestClient, offline temp state) and the live stream (`GET /v1/stream`, real uvicorn on
an ephemeral loopback port). Replay rows only: synthetic demo fixture, no network."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.watch.rt import store
from hydra.watch.rt.engine import FakeClock
from hydra.watch.rt.sources import ADAPTERS

from .conftest import T0

RT = "/v1/watch/rt"


class Principal:
    def __init__(self, holder: dict[str, Any]) -> None:
        self.holder = holder

    def has_role(self, role: str) -> bool:
        return role in self.holder["roles"]


class PrincipalMiddleware:
    """Stands in for the panel auth gate: attaches `hydra.principal` to every HTTP scope when roles are set."""

    def __init__(self, app, holder: dict[str, Any]) -> None:
        self.app = app
        self.holder = holder

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.holder.get("roles") is not None:
            scope["hydra.principal"] = Principal(self.holder)
        await self.app(scope, receive, send)


@pytest.fixture
def roles() -> dict[str, Any]:
    return {"roles": None}


@pytest.fixture
def client(offline, roles) -> Iterator[TestClient]:
    app = create_app(offline)
    app.add_middleware(PrincipalMiddleware, holder=roles)
    with TestClient(app) as c:
        yield c


def _instant_clock(c: TestClient) -> None:
    """The engine of the app (created lazily) gets a FakeClock: replay ticks advance without real waiting."""
    assert c.get(f"{RT}/status").status_code == 200
    c.app.state.watch_rt.clock = FakeClock(T0)


def _replay_done(c: TestClient, fixture: str = "demo") -> dict[str, Any]:
    r = c.post(f"{RT}/replay", json={"fixture": fixture, "speed": 3600})
    assert r.status_code == 200, r.text
    assert r.json()["engine"]["state"] == "replay"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        st = c.get(f"{RT}/status").json()
        if st["engine"]["replay"] and st["engine"]["replay"]["done"]:
            return st
        time.sleep(0.01)
    raise AssertionError("replay did not finish")


def test_status_shape_and_offline_poll(client):
    st = client.get(f"{RT}/status").json()
    assert set(st) >= {"engine", "sources", "totals", "sparkline", "timeline"}
    assert len(st["sparkline"]) == 60 and len(st["timeline"]) == 48
    assert [s["id"] for s in st["sources"]] == list(ADAPTERS)
    assert st["engine"]["state"] == "stopped" and st["engine"]["offline"] is True
    assert st["engine"]["message_pl"] and set(st["totals"]) >= {"items", "matches", "matches_new", "watchlist"}
    r = client.post(f"{RT}/sources/cert-pl/poll")
    assert r.status_code == 409
    assert r.json()["detail"]["status"] == "offline" and "offline" in r.json()["detail"]["message_pl"].lower()
    assert client.post(f"{RT}/sources/nope/poll").status_code == 404
    # engine start offline: state offline, nothing polled
    r = client.post(f"{RT}/engine", json={"action": "start"})
    assert r.status_code == 200 and r.json()["engine"]["state"] == "offline"
    assert client.post(f"{RT}/engine", json={"action": "stop"}).json()["engine"]["state"] == "stopped"
    assert client.get(f"{RT}/runs").json()["runs"] == []
    m = client.get(f"{RT}/metrics").json()
    assert set(m) >= {"loop_ms_p50", "match_us_p95", "samples"}


def test_set_source(client):
    r = client.post(f"{RT}/sources/cert-pl", json={"enabled": True, "interval_s": 120})
    assert r.status_code == 200
    v = r.json()
    assert v["enabled"] is True and v["interval_s"] == 120 and v["state"] == "idle"
    assert client.post(f"{RT}/sources/cert-pl", json={"interval_s": 59}).status_code == 422
    assert client.post(f"{RT}/sources/cert-pl", json={"interval_s": 86401}).status_code == 422
    assert client.post(f"{RT}/sources/nope", json={"enabled": True}).status_code == 404
    assert client.post(f"{RT}/sources/openphish", json={"interval_s": 60}).json()["interval_s"] >= 3600
    v = client.post(f"{RT}/sources/cert-pl", json={"enabled": False}).json()
    assert v["enabled"] is False and v["state"] == "disabled"


def test_replay_fixture_validation(client):
    fx = client.get(f"{RT}/replay/fixtures").json()["fixtures"]
    demo = next(f for f in fx if f["name"] == "demo")
    assert demo["ticks"] == 12 and demo["synthetic"] is True
    for bad in ("../demo", "..", ".demo", "/etc/passwd", "demo/../../etc"):
        assert client.post(f"{RT}/replay", json={"fixture": bad}).status_code == 422, bad
    assert client.post(f"{RT}/replay", json={"fixture": "does-not-exist"}).status_code == 404
    assert client.post(f"{RT}/replay", json={"fixture": "demo", "speed": 0.5}).status_code == 422
    assert client.post(f"{RT}/replay", json={"fixture": ""}).status_code == 422


def test_replay_items_matches_watchlist_runs(client):
    _instant_clock(client)
    st = _replay_done(client)
    assert st["engine"]["replay"]["synthetic"] is True and st["engine"]["replay"]["ticks_done"] == 12
    assert st["engine"]["replay"]["label_pl"].startswith("SYNTETYCZNE")
    assert st["totals"]["matches"] == 8 and st["totals"]["items"] > 0
    assert all(s["state"] == "replay" for s in st["sources"])
    assert sum(m["fetched"] for m in st["sparkline"]) > 0  # replay view shows replay runs
    r = client.post(f"{RT}/sources/cert-pl/poll")
    assert r.status_code == 409 and r.json()["detail"]["status"] == "replay"

    # ---- items: filters and paging
    allit = client.get(f"{RT}/items", params={"limit": 500}).json()["items"]
    assert len(allit) == st["totals"]["items"] and all(i["replay"] for i in allit)
    assert not any(i["external_id"].startswith("replay:") for i in allit)
    cert = client.get(f"{RT}/items", params={"source": "cert-pl", "limit": 500}).json()["items"]
    assert cert and all(i["source"] == "cert-pl" for i in cert)
    urls = client.get(f"{RT}/items", params={"kind": "url", "limit": 500}).json()["items"]
    assert urls and {i["source"] for i in urls} <= {"urlhaus", "openphish"}
    matched = client.get(f"{RT}/items", params={"matched": 1, "limit": 500}).json()["items"]
    assert len(matched) == 6 and all(i["match_count"] > 0 for i in matched)
    assert len(client.get(f"{RT}/items", params={"matched": 0, "limit": 500}).json()["items"]) == len(allit) - 6
    q = client.get(f"{RT}/items", params={"q": "zysk-orlen-demo", "limit": 500}).json()["items"]
    assert q and all("zysk-orlen-demo" in i["value_defanged"].replace("[.]", ".") or
                     any("zysk-orlen-demo" in x["value_display"].replace("[.]", ".") for x in i["indicators"])
                     for i in q)
    page = client.get(f"{RT}/items", params={"limit": 5}).json()
    assert len(page["items"]) == 5 and page["next_before"] == page["items"][-1]["id"]
    page2 = client.get(f"{RT}/items", params={"limit": 5, "before_id": page["next_before"]}).json()["items"]
    assert page2 and max(i["id"] for i in page2) < page["next_before"]
    assert client.get(f"{RT}/items", params={"replay": 0}).json()["items"] == []
    assert all("[.]" in i["value_defanged"] for i in cert)  # hosts are never clickable

    # ---- matches: status update, 404, incident refused for replay rows
    ms = client.get(f"{RT}/matches").json()["matches"]
    assert len(ms) == 8 and all(m["replay"] and m["status"] == "new" for m in ms)
    assert {m["strength"] for m in ms} == {"strong", "weak"}
    mid = ms[0]["id"]
    r = client.post(f"{RT}/matches/{mid}", json={"status": "acknowledged"})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged"
    assert [m["id"] for m in client.get(f"{RT}/matches", params={"status": "acknowledged"}).json()["matches"]] \
        == [mid]
    assert client.post(f"{RT}/matches/{mid}", json={"status": "bogus"}).status_code == 422
    assert client.post(f"{RT}/matches/999999", json={"status": "dismissed"}).status_code == 404
    r = client.post(f"{RT}/matches/{mid}/incident")
    assert r.status_code == 409 and r.json()["detail"]["status"] == "refused"
    assert client.post(f"{RT}/matches/999999/incident").status_code == 404

    # ---- watchlist
    wl = client.get(f"{RT}/watchlist").json()
    assert wl["origins"] == {"replay": 7} and {k["kind"] for k in wl["kinds"]} >= {"host", "phone", "iban"}
    r = client.post(f"{RT}/watchlist", json={"kind": "host", "value": "Bad.Shop-Demo.example", "label": "test"})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["origin"] == "manual" and e["value_display"] == "bad[.]shop-demo[.]example"
    assert client.post(f"{RT}/watchlist", json={"kind": "nope", "value": "x"}).status_code == 422
    assert client.post(f"{RT}/watchlist", json={"kind": "host", "value": "not a host"}).status_code == 422
    assert client.post(f"{RT}/watchlist", json={"kind": "host", "value": ""}).status_code == 422
    assert client.delete(f"{RT}/watchlist/{e['id']}").json() == {"deleted": e["id"]}
    assert client.delete(f"{RT}/watchlist/{e['id']}").status_code == 404
    writer = client.app.state.hydra.writer
    writer.run(lambda c: store.replace_campaign_entries(c, [
        {"kind": "host", "value": "camp-demo.example", "display": "camp-demo[.]example", "campaign_id": "C9",
         "run_id": "r", "label": "x", "incident_ids": [], "df": 1, "n_incidents": 1}], T0))
    with client.app.state.hydra.reader() as conn:
        cid = conn.execute("SELECT id FROM watch_watchlist WHERE origin='campaign'").fetchone()[0]
    assert client.delete(f"{RT}/watchlist/{cid}").status_code == 404  # campaign rows: only a rebuild changes them
    rb = client.post(f"{RT}/watchlist/rebuild").json()
    assert rb["removed"] == 1 and rb["added"] == 0  # no analyse run in this state: the stale campaign row goes

    # ---- runs
    runs = client.get(f"{RT}/runs", params={"limit": 500}).json()["runs"]
    assert runs and all(r["replay"] for r in runs)
    assert sum(r["matched"] for r in runs) == 8
    cr = client.get(f"{RT}/runs", params={"source": "crtsh"}).json()["runs"]
    assert len(cr) == 2 and all(r["source"] == "crtsh" for r in cr)

    # ---- stop: live view again, replay runs excluded from the charts
    st = client.post(f"{RT}/replay/stop").json()
    assert st["engine"]["state"] == "stopped" and st["engine"]["replay"] is None
    assert sum(m["fetched"] for m in st["sparkline"]) == 0 and sum(h["new"] for h in st["timeline"]) == 0


def test_roles(client, roles):
    roles["roles"] = {"analyst"}
    for path, body in ((f"{RT}/sources/cert-pl", {"enabled": True}), (f"{RT}/sources/cert-pl/poll", None),
                       (f"{RT}/engine", {"action": "start"}), (f"{RT}/engine", {"action": "pause"})):
        r = client.post(path, json=body)
        assert r.status_code == 403, path
        assert r.json()["detail"]["code"] == "forbidden" and "administratora" in r.json()["detail"]["message"]
    r = client.post(f"{RT}/watchlist", json={"kind": "host", "value": "role-demo.example"})
    assert r.status_code == 200
    roles["roles"] = {"viewer"}
    assert client.get(f"{RT}/status").status_code == 200  # reads stay open
    assert client.post(f"{RT}/watchlist", json={"kind": "host", "value": "x-demo.example"}).status_code == 403
    assert client.delete(f"{RT}/watchlist/{r.json()['id']}").status_code == 403
    assert client.post(f"{RT}/replay", json={"fixture": "demo"}).status_code == 403
    assert client.post(f"{RT}/matches/1", json={"status": "dismissed"}).status_code == 403
    roles["roles"] = {"admin", "analyst"}
    assert client.post(f"{RT}/sources/cert-pl", json={"enabled": True}).status_code == 200
    roles["roles"] = None  # no principal (HYDRA_AUTH=0): allowed
    assert client.post(f"{RT}/sources/cert-pl", json={"enabled": False}).status_code == 200


# ------------------------------------------------------------------ live stream (SSE)
class Server:
    def __init__(self, settings) -> None:
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
        self.base = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)


@pytest.mark.timeout(90)
def test_stream_forwards_watch_rt_events(offline):
    srv = Server(offline)
    try:
        with httpx.Client(base_url=srv.base, timeout=httpx.Timeout(15.0)) as c, \
                c.stream("GET", "/v1/stream") as resp:
            assert resp.status_code == 200
            lines = resp.iter_lines()
            seen: set[str] = set()
            for line in lines:
                if line == "event: hello":
                    break
            r = c.post(f"{RT}/replay", json={"fixture": "demo", "speed": 3600})
            assert r.status_code == 200, r.text
            deadline = time.monotonic() + 45
            want = {"event: watch.item", "event: watch.match"}
            data_after: dict[str, str] = {}
            prev = None
            for line in lines:
                if time.monotonic() > deadline:
                    raise AssertionError(f"timeout; got {sorted(seen)}")
                if line.startswith("event: watch."):
                    seen.add(line)
                if prev in want and line.startswith("data: "):
                    data_after.setdefault(prev, line)
                prev = line
                if want <= seen and len(data_after) == 2:
                    break
            assert want <= seen
            assert "event: watch.poll" in seen
            assert '"replay":true' in data_after["event: watch.match"].replace(" ", "")
            assert '"watch_seq"' in data_after["event: watch.item"]
        stats = httpx.get(f"{srv.base}/v1/stream/stats", timeout=10).json()
        assert stats["stats"].get("watch_rt_sent", 0) > 0
    finally:
        srv.stop()
