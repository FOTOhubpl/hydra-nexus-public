"""Outbound webhooks (hydra.integrations.webhooks, hydra.api_webhooks): signature, retry/backoff/dead letter, offline
policy, secret hygiene, dispatcher mapping and a real signed loopback delivery end to end. No external network: every
receiver is a real http.server / uvicorn bound to 127.0.0.1:0 inside the test."""

from __future__ import annotations

import hashlib
import heapq
import hmac
import json
import socket
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hydra import events
from hydra.api import create_app
from hydra.config import Settings
from hydra.db import Writer, connect
from hydra.integrations import webhooks as wh

SECRET = "s3cr3t-signing-key-0123456789abcdefXYZW"


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HYDRA_SECRETS_DIR", raising=False)
    monkeypatch.delenv(wh.SECRET_NAME, raising=False)
    monkeypatch.setenv("HYDRA_WEBHOOK_BACKOFF_S", "0.01")
    monkeypatch.setenv("HYDRA_WEBHOOK_TIMEOUT_S", "5")
    monkeypatch.setenv("HYDRA_OFFLINE", "1")
    return home


class Receiver:
    """Tiny loopback HTTP receiver: answers with the scripted status codes (last one repeats) and records requests."""

    def __init__(self, codes: list[int]) -> None:
        self.codes = list(codes)
        self.requests: list[dict[str, Any]] = []
        rec = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                rec.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                     "body": body})
                code = rec.codes.pop(0) if len(rec.codes) > 1 else rec.codes[0]
                self.send_response(code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *_a: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook/path-abcd?token=qq"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def receiver() -> Iterator[Any]:
    made: list[Receiver] = []

    def make(codes: list[int]) -> Receiver:
        r = Receiver(codes)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


@pytest.fixture
def writer(settings: Settings) -> Iterator[Writer]:
    w = Writer(settings.db_path)
    yield w
    w.close()


@pytest.fixture
def mgr(settings: Settings, writer: Writer) -> Iterator[wh.WebhookManager]:
    m = wh.WebhookManager(settings, writer, poll_s=0.05)
    yield m
    m.stop()


def _wait(m: wh.WebhookManager, did: str, statuses: tuple[str, ...], timeout: float = 10) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        d = m.get_delivery(did)
        if d and d["status"] in statuses:
            return d
        time.sleep(0.01)
    raise AssertionError(f"delivery {did} did not reach {statuses}: {m.get_delivery(did)}")


def _audit(settings: Settings) -> list[dict[str, Any]]:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        return [json.loads(r["payload"]) for r in conn.execute(
            "SELECT payload FROM events WHERE type=? ORDER BY seq", (wh.AUDIT_TYPE,))]


# ------------------------------------------------------------------------------------------------ signature


def test_signature_is_hmac_sha256_over_timestamp_dot_body():
    body = b'{"id":"dlv-1","event":"ping"}'
    expected = hmac.new(SECRET.encode(), b"1763770000." + body, hashlib.sha256).hexdigest()
    assert wh.sign(SECRET, 1763770000, body) == f"sha256={expected}"
    # the timestamp is part of the signed message
    assert wh.sign(SECRET, 1763770001, body) != wh.sign(SECRET, 1763770000, body)
    sig = wh.sign(SECRET, 1763770000, body)
    assert wh.verify(SECRET, "1763770000", body, sig, now=1763770100)
    assert not wh.verify(SECRET, "1763770000", body + b" ", sig, now=1763770100)  # tampered body
    assert not wh.verify(SECRET, "1763770001", body, sig, now=1763770100)  # tampered timestamp
    assert not wh.verify("other-key-0123456789", "1763770000", body, sig, now=1763770100)
    assert not wh.verify(SECRET, "1763770000", body, sig, now=1763770000 + 301)  # replay window
    assert not wh.verify(SECRET, "abc", body, sig, now=1763770000)
    assert not wh.verify(SECRET, None, body, sig)
    assert not wh.verify(SECRET, "1763770000", body, None, now=1763770000)


def test_verify_uses_constant_time_compare(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(wh.hmac, "compare_digest", spy)
    ts = int(time.time())
    assert wh.verify(SECRET, ts, b"x", wh.sign(SECRET, ts, b"x"))
    assert calls


def test_redact_url_and_loopback():
    assert wh.redact_url("https://hooks.slack.com/services/T000/B000/XXXXabcd?x=1") == "https://hooks.slack.com/…abcd"
    assert wh.redact_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000/"
    assert wh.redact_url("http://[::1]:9/a") == "http://[::1]:9/…"
    for u in ("http://127.0.0.1:1/x", "http://127.9.9.9/x", "http://[::1]/x", "http://localhost:5/x"):
        assert wh.is_loopback_url(u), u
    for u in ("http://10.0.0.1/x", "https://example.org/x", "http://0.0.0.0/x"):
        assert not wh.is_loopback_url(u), u


# ------------------------------------------------------------------------------------------------ retry policy


def _endpoint(url: str, enabled: bool = True, evs: list[str] | None = None) -> dict[str, Any]:
    return wh.EndpointStore().create("t", url, evs or ["delta"], enabled, SECRET)


class FakeClock:
    """Injected `hydra.integrations.webhooks._now`: time moves only when the test moves it."""

    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


class ScriptedTransport:
    """In-process transport: answers with the scripted codes (the last one repeats) and records each request."""

    def __init__(self) -> None:
        self.codes: list[int] = [200]
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        self.requests.append({"url": url, "headers": {k.lower(): v for k, v in headers.items()}, "body": body})
        return (self.codes.pop(0) if len(self.codes) > 1 else self.codes[0]), b"ok"


@pytest.fixture
def sync_mgr(settings: Settings, writer: Writer, monkeypatch: pytest.MonkeyPatch) -> Iterator[wh.WebhookManager]:
    """Manager without the background worker: retries are driven by `_drain` in the test thread with a fake clock,
    so the retry/backoff/dead-letter tests do not depend on scheduling or wall-clock time."""
    monkeypatch.setenv("HYDRA_WEBHOOK_BACKOFF_S", "2")
    clock = FakeClock()
    monkeypatch.setattr(wh, "_now", clock)
    tr = ScriptedTransport()
    m = wh.WebhookManager(settings, writer, transport=tr, poll_s=0.05)
    monkeypatch.setattr(m, "_ensure_worker", lambda: None)
    m.clock, m.tr = clock, tr  # type: ignore[attr-defined]
    yield m
    m.stop()


def _drain(m: wh.WebhookManager, limit: int = 20) -> list[float]:
    """Run the scheduled attempts in order; jump the fake clock to each due time. Returns the waits between them."""
    waits: list[float] = []
    while m._heap and len(waits) < limit:
        due, did = heapq.heappop(m._heap)
        waits.append(max(0.0, due - m.clock.t))  # type: ignore[attr-defined]
        m.clock.t = max(m.clock.t, due)  # type: ignore[attr-defined]
        m.attempt(did)
    assert not m._heap, "retry heap not drained"
    return waits


LOOP_URL = "http://127.0.0.1:9/hook/path-abcd?token=qq"  # loopback: allowed offline; never opened (scripted transport)


def test_retry_on_500_then_success(sync_mgr, settings):
    sync_mgr.tr.codes = [500, 200]
    ep = _endpoint(LOOP_URL)
    d = sync_mgr.enqueue(ep["id"], "delta", {"run_id": "run-1", "campaigns": 3})
    assert _drain(sync_mgr) == [0.0, 2.0]
    done = sync_mgr.get_delivery(d["id"])
    assert done["status"] == "delivered" and done["attempts"] == 2 and done["last_status_code"] == 200
    assert len(sync_mgr.tr.requests) == 2
    for req in sync_mgr.tr.requests:
        h = req["headers"]
        assert h["x-hydra-event"] == "delta" and h["x-hydra-delivery"] == d["id"]
        assert wh.verify(SECRET, h["x-hydra-timestamp"], req["body"], h["x-hydra-signature"])
        body = json.loads(req["body"])
        assert body["id"] == d["id"] and body["event"] == "delta" and body["data"]["campaigns"] == 3
        assert set(body) == {"id", "event", "created_at", "hydra_version", "data"}
    audit = _audit(settings)
    assert [a["status"] for a in audit] == ["retrying", "delivered"]
    assert all(a["host"] == "127.0.0.1" for a in audit)


def test_retry_on_429(sync_mgr):
    sync_mgr.tr.codes = [429, 429, 204]
    did = sync_mgr.enqueue(_endpoint(LOOP_URL)["id"], "delta", {})["id"]
    assert _drain(sync_mgr) == [0.0, 2.0, 4.0]
    d = sync_mgr.get_delivery(did)
    assert d["status"] == "delivered" and d["attempts"] == 3


def test_4xx_is_dead_immediately(sync_mgr):
    sync_mgr.tr.codes = [400, 200]
    did = sync_mgr.enqueue(_endpoint(LOOP_URL)["id"], "delta", {})["id"]
    assert _drain(sync_mgr) == [0.0]
    d = sync_mgr.get_delivery(did)
    assert d["status"] == "dead" and d["attempts"] == 1 and d["last_status_code"] == 400
    assert len(sync_mgr.tr.requests) == 1
    assert "400" in d["last_error"]


def test_max_attempts_dead_letter_and_manual_retry(sync_mgr):
    sync_mgr.tr.codes = [503]
    did = sync_mgr.enqueue(_endpoint(LOOP_URL)["id"], "delta", {})["id"]
    assert _drain(sync_mgr) == [0.0, 2.0, 4.0, 8.0, 16.0]
    d = sync_mgr.get_delivery(did)
    assert d["status"] == "dead" and d["attempts"] == wh.MAX_ATTEMPTS == 5 and d["max_attempts"] == 5
    assert "dead letter" in d["last_error"] and d["next_retry_at"] is None
    assert len(sync_mgr.tr.requests) == 5
    sync_mgr.tr.codes = [200]
    again = sync_mgr.retry(did)
    assert again["status"] == "delivered" and again["attempts"] == 1 and again["id"] == did
    assert _drain(sync_mgr) == []
    with pytest.raises(wh.Conflict):
        sync_mgr.retry(did)


def test_backoff_is_exponential(sync_mgr):
    sync_mgr.tr.codes = [500]
    d = sync_mgr.create_delivery(_endpoint(LOOP_URL)["id"], "delta", {})
    t0 = sync_mgr.clock.t
    first = sync_mgr.attempt(d["id"])
    assert first["status"] == "retrying"
    assert sync_mgr._deliveries[d["id"]]["_due"] == t0 + 2
    sync_mgr.clock.t += 2
    second = sync_mgr.attempt(d["id"])
    assert second["attempts"] == 2 and sync_mgr._deliveries[d["id"]]["_due"] == t0 + 2 + 4


def test_real_loopback_receiver_retry(mgr, receiver):
    """One check with the real worker and a real loopback socket; generous deadline, no timing assertions."""
    r = receiver([500, 200])
    d = _wait(mgr, mgr.enqueue(_endpoint(r.url)["id"], "delta", {})["id"], ("delivered", "dead"), timeout=60)
    assert d["status"] == "delivered" and d["attempts"] == 2 and len(r.requests) == 2


# ------------------------------------------------------------------------------------------------ network policy


def _no_socket(monkeypatch) -> list[Any]:
    tried: list[Any] = []

    def boom(*a, **k):
        tried.append(a)
        raise AssertionError("no socket may be opened")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    return tried


def test_offline_non_loopback_is_blocked_without_socket(settings, writer, monkeypatch):
    sent = []
    m = wh.WebhookManager(settings, writer, transport=lambda *a: sent.append(a) or (200, b""))
    try:
        ep = _endpoint("https://hooks.example.org/services/abc?token=zz", enabled=True)
        tried = _no_socket(monkeypatch)
        d = m.send_test(ep["id"])
        assert d["status"] == "blocked_offline" and d["attempts"] == 0
        assert "offline" in d["last_error"].lower() and "HYDRA_OFFLINE" in d["last_error"]
        assert not sent and not tried
    finally:
        monkeypatch.undo()
        m.stop()
    audit = _audit(settings)
    assert audit[-1]["status"] == "blocked_offline" and audit[-1]["host"] == "hooks.example.org"


def test_online_private_ip_and_disabled_endpoint_are_blocked(settings, writer, monkeypatch):
    monkeypatch.setenv("HYDRA_OFFLINE", "0")
    online = settings.model_copy(update={"offline": False})
    sent = []
    m = wh.WebhookManager(online, writer, transport=lambda *a: sent.append(a) or (200, b""))
    try:
        assert not m.offline()
        tried = _no_socket(monkeypatch)
        for url in ("http://10.0.0.5/hook", "http://169.254.169.254/latest", "http://192.168.1.1/x",
                    "http://[fc00::1]/x"):
            d = m.send_test(_endpoint(url)["id"])
            assert d["status"] == "blocked_offline" and "SSRF" in d["last_error"], url
        d = m.send_test(_endpoint("https://93.184.216.34/hook", enabled=False)["id"])
        assert d["status"] == "blocked_offline" and "wyłączony" in d["last_error"]
        # an enabled public literal IP passes the policy and reaches the (injected) transport
        d = m.send_test(_endpoint("https://93.184.216.34/hook", enabled=True)["id"])
        assert d["status"] == "delivered" and len(sent) == 1
        assert not tried
    finally:
        monkeypatch.undo()
        m.stop()


def test_loopback_allowed_offline(mgr, receiver):
    assert mgr.offline()
    r = receiver([200])
    d = mgr.send_test(_endpoint(r.url, enabled=False)["id"])
    assert d["status"] == "delivered" and d["event"] == "ping"


def test_redirect_is_not_followed(mgr, receiver):
    target = receiver([200])
    r = receiver([302])
    d = mgr.send_test(_endpoint(r.url)["id"])
    assert d["status"] == "dead" and d["last_status_code"] == 302 and "przekierowania" in d["last_error"]
    assert not target.requests


# ------------------------------------------------------------------------------------------------ dispatcher


def test_dispatcher_maps_audit_events_to_ids_and_counts(mgr, writer, settings, receiver):
    r = receiver([200])
    _endpoint(r.url, evs=["delta", "review", "export", "job", "watch"])

    def seed(conn):
        conn.execute("INSERT INTO jobs (id, kind, status, created_at) VALUES ('job-1','import','done',?)",
                     (events.utcnow(),))
        events.append(conn, "RUN_FINISHED", {"run_id": "run-1", "kind": "analyse", "pairs": 10, "campaigns": 2})
        events.append(conn, "REVIEW", {"id": "rv-1", "action": "cannot_link", "target": {"a": "inc-1", "b": "inc-2"},
                                       "analyst": "jan.kowalski", "note": "TAJNA notatka /home/x/plik.png"})
        events.append(conn, "EXPORT", {"kind": "stix_run", "run_id": "run-1", "counts": {"campaigns": 2},
                                       "file": "/tmp/out.json"})
        events.append(conn, "JOB_DONE", {"job_id": "job-1", "total": 3, "done": 3, "failed": 0})
        events.append(conn, "INCIDENT_IMPORTED", {"incident_id": "inc-9", "source_ref": "/data/secret-name.png"})

    writer.run(seed)

    class Watch:
        def alerts_since(self, since, limit):
            rows = [{"alert_seq": 1, "event_id": "ct-1", "host_defanged": "zly[.]pl", "first_seen": "2026-11-22",
                     "event_bits": 12.5, "assignment": "candidate", "replay": True, "alerts": [{"name": "x"}]}]
            return [a for a in rows if a["alert_seq"] > since]

    mgr.watch_getter = Watch
    with closing(connect(settings.db_path, readonly=True)) as conn:
        cursor, aseq = mgr.poll_once(conn, 0, 0)
        n = len(mgr.deliveries())
        assert n == 5
        mgr.poll_once(conn, cursor, aseq)  # only INTEGRATION_DELIVERY audit rows are new: never re-dispatched
        assert len(mgr.deliveries()) == n
    deadline = time.time() + 10
    while time.time() < deadline and len(r.requests) < 5:
        time.sleep(0.02)
    bodies = {json.loads(q["body"])["event"]: json.loads(q["body"])["data"] for q in r.requests}
    assert set(bodies) == {"delta", "review", "export", "job", "watch"}
    assert bodies["delta"]["run_id"] == "run-1" and bodies["delta"]["campaigns"] == 2
    assert all(isinstance(b.pop("audit_seq", 0), int) for b in bodies.values())
    assert bodies["review"] == {"review_id": "rv-1", "action": "cannot_link", "target_ids": ["inc-1", "inc-2"]}
    assert bodies["export"] == {"kind": "stix_run", "run_id": "run-1", "counts": {"campaigns": 2}}
    assert bodies["job"]["kind"] == "import" and bodies["job"]["status"] == "done" and bodies["job"]["done"] == 3
    assert bodies["watch"]["candidate"] is True and bodies["watch"]["alerts"] == 1
    raw = b"".join(q["body"] for q in r.requests)
    for leaked in (b"TAJNA", b"kowalski", b"/tmp/out.json", b"secret-name", b"/home"):
        assert leaked not in raw


def test_dispatcher_starts_only_with_enabled_endpoint_and_stops(mgr, receiver, writer):
    mgr.sync_dispatcher()
    assert not mgr.dispatcher_running()
    r = receiver([200])
    _endpoint(r.url, enabled=True)
    mgr.sync_dispatcher()
    assert mgr.dispatcher_running()
    time.sleep(0.1)  # let it take its cursor (history is not replayed)
    writer.run(lambda c: events.append(c, "RUN_FINISHED", {"run_id": "run-2", "kind": "analyse", "pairs": 1,
                                                           "campaigns": 1}))
    deadline = time.time() + 10
    while time.time() < deadline and not r.requests:
        time.sleep(0.02)
    assert len(r.requests) == 1 and json.loads(r.requests[0]["body"])["data"]["run_id"] == "run-2"
    mgr.stop()
    assert not mgr.dispatcher_running()


# ------------------------------------------------------------------------------------------------ API + secrets


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as c:
        yield c


def test_api_crud_never_echoes_secret_or_full_url(client, settings, receiver, _env):
    r = receiver([200])
    texts: list[str] = []
    res = client.post("/v1/integrations/webhooks", json={"name": "Test", "url": r.url, "events": ["ping", "delta"],
                                                         "enabled": True, "secret": SECRET})
    assert res.status_code == 201, res.text
    ep = res.json()
    texts.append(res.text)
    assert ep["secret"] == {"configured": True, "last4": SECRET[-4:]} and ep["loopback"] is True
    assert ep["url_redacted"].endswith("/…abcd") and "token" not in ep["url_redacted"]
    res = client.post(f"/v1/integrations/webhooks/{ep['id']}/test")
    texts.append(res.text)
    assert res.status_code == 200 and res.json()["status"] == "delivered"
    new_secret = "another-signing-key-9876543210QRST"
    res = client.patch(f"/v1/integrations/webhooks/{ep['id']}", json={"secret": new_secret, "name": "Nowa"})
    texts.append(res.text)
    assert res.status_code == 200 and res.json()["secret"]["last4"] == "QRST" and res.json()["name"] == "Nowa"
    for path in ("/v1/integrations/webhooks", "/v1/integrations/webhooks/deliveries",
                 "/v1/integrations/webhooks/sink", "/openapi.json"):
        res = client.get(path)
        assert res.status_code == 200, path
        texts.append(res.text)
    overview = client.get("/v1/integrations/webhooks").json()
    assert {e["id"] for e in overview["event_types"]} == {"ping", "delta", "job", "watch", "review", "export"}
    assert all(e["label_pl"] and e["description_pl"] and e["example"] for e in overview["event_types"])
    assert overview["offline"] is True and overview["delivery_possible"] is False
    assert overview["signature"]["header"] == "X-Hydra-Signature" and overview["dispatcher_running"] is True
    assert overview["endpoints"][0]["last_delivery"]["status"] == "delivered"
    joined = "\n".join(texts)
    for s in (SECRET, new_secret, "token=qq", "/hook/path-abcd"):
        assert s not in joined, s

    cfg_dir = _env / ".config" / "hydra"
    files = {
        "webhooks.json": cfg_dir / "webhooks.json",
        "env": cfg_dir / f"webhook-{ep['id']}.env",
        "deliveries": settings.state_dir / "integrations" / "deliveries.jsonl",
    }
    for name, p in files.items():
        assert p.is_file(), name
        assert stat.S_IMODE(p.stat().st_mode) == 0o600, name
    assert new_secret in files["env"].read_text() and SECRET not in files["env"].read_text()
    for name in ("webhooks.json", "deliveries"):
        content = files[name].read_text()
        assert SECRET not in content and new_secret not in content, name
    assert "token=qq" not in files["deliveries"].read_text()
    audit = _audit(settings)
    assert audit and all(a["host"] == "127.0.0.1" for a in audit)
    adump = json.dumps(audit)
    for s in (SECRET, new_secret, "token", "/hook", "path-abcd"):
        assert s not in adump, s
    with closing(connect(settings.db_path, readonly=True)) as conn:
        assert events.verify_chain(conn)[0]
    # never the operator's real config dir
    assert str(files["webhooks.json"]).startswith(str(_env))

    assert client.delete(f"/v1/integrations/webhooks/{ep['id']}").json() == {"deleted": True}
    assert not files["env"].exists()
    assert client.delete(f"/v1/integrations/webhooks/{ep['id']}").status_code == 404
    assert client.post(f"/v1/integrations/webhooks/{ep['id']}/test").status_code == 404


def test_api_validation_and_retry_errors(client):
    base = {"name": "x", "url": "http://127.0.0.1:1/x", "events": ["delta"], "enabled": False, "secret": SECRET}
    for bad in ({"url": "ftp://127.0.0.1/x"}, {"url": "http://user:pw@127.0.0.1/x"}, {"events": ["nope"]},
                {"secret": "short"}, {"name": ""}):
        res = client.post("/v1/integrations/webhooks", json={**base, **bad})
        assert res.status_code == 422, bad
        assert SECRET not in res.text
    assert client.post("/v1/integrations/webhooks/deliveries/dlv-nope/retry").status_code == 404
    assert client.patch("/v1/integrations/webhooks/wh-nope", json={"enabled": True}).status_code == 404


def test_api_offline_external_test_is_blocked(client):
    ep = client.post("/v1/integrations/webhooks", json={"name": "Slack", "events": ["delta"], "enabled": True,
                                                        "url": "https://hooks.slack.com/services/T0/B0/XYZWabcd",
                                                        "secret": SECRET}).json()
    assert ep["loopback"] is False and ep["url_redacted"] == "https://hooks.slack.com/…abcd"
    d = client.post(f"/v1/integrations/webhooks/{ep['id']}/test").json()
    assert d["status"] == "blocked_offline" and "HYDRA_OFFLINE" in d["last_error"]
    retried = client.post(f"/v1/integrations/webhooks/deliveries/{d['id']}/retry").json()
    assert retried["status"] == "blocked_offline"


def test_openapi_has_summaries_and_tag(client):
    spec = client.get("/openapi.json").json()
    ops = [(p, m, o) for p, item in spec["paths"].items() if p.startswith("/v1/integrations/webhooks")
           for m, o in item.items()]
    assert len(ops) >= 9
    for p, m, o in ops:
        assert o["tags"] == ["webhooks"] and o.get("summary") and o.get("description"), (p, m)


def test_sink_rejects_bad_signature(client):
    ep = client.post("/v1/integrations/webhooks", json={"name": "s", "url": "http://127.0.0.1:1/x",
                                                        "events": ["ping"], "secret": SECRET}).json()
    body = b'{"id":"dlv-x","event":"ping"}'
    ts = str(int(time.time()))
    bad = client.post(f"/v1/integrations/webhooks/sink?endpoint_id={ep['id']}", content=body,
                      headers={"X-Hydra-Timestamp": ts, "X-Hydra-Signature": "sha256=" + "0" * 64,
                               "X-Hydra-Event": "ping", "X-Hydra-Delivery": "dlv-x"})
    assert bad.status_code == 401 and bad.json()["signature_ok"] is False
    good = client.post(f"/v1/integrations/webhooks/sink?endpoint_id={ep['id']}", content=body,
                       headers={"X-Hydra-Timestamp": ts, "X-Hydra-Signature": wh.sign(SECRET, ts, body),
                                "X-Hydra-Event": "ping", "X-Hydra-Delivery": "dlv-x"})
    assert good.status_code == 200 and good.json()["signature_ok"] is True
    receipts = client.get("/v1/integrations/webhooks/sink").json()["receipts"]
    assert [x["signature_ok"] for x in receipts[:2]] == [True, False]
    assert client.post("/v1/integrations/webhooks/sink", content=body).status_code == 401  # no endpoint_id


def test_lifespan_stops_dispatcher_and_worker(settings, receiver):
    r = receiver([200])
    app = create_app(settings)
    with TestClient(app) as c:
        c.post("/v1/integrations/webhooks", json={"name": "x", "url": r.url, "events": ["delta"], "enabled": True,
                                                  "secret": SECRET})
        m = app.state.webhooks
        assert m.dispatcher_running()
    assert not m.dispatcher_running()
    assert m._worker is None or not m._worker.is_alive()
