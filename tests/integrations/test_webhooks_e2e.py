"""End to end: the real app served by uvicorn on 127.0.0.1:0 delivers a signed `ping` to its own built-in sink.

The endpoint, the test button, the delivery log and the sink receipts all go through the HTTP API; the sink verifies
the HMAC with the endpoint's stored key. Nothing leaves the machine (loopback, offline mode on)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from hydra.api import create_app
from hydra.config import Settings

SECRET = "e2e-signing-key-ABCDEFGHIJKLMNOP1234"


@pytest.fixture
def server(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HYDRA_SECRETS_DIR", raising=False)
    monkeypatch.setenv("HYDRA_OFFLINE", "1")
    monkeypatch.setenv("HYDRA_WEBHOOK_BACKOFF_S", "0.01")
    srv = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="warning",
                                        lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 30
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    assert srv.started
    port = srv.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        t.join(timeout=15)


def test_signed_ping_reaches_builtin_sink(server: str):
    with httpx.Client(base_url=server, timeout=20) as c:
        api = "/api/v1/integrations/webhooks"
        ep = c.post(api, json={"name": "Odbiornik wbudowany", "url": f"{server}/api/v1/x", "events": ["ping"],
                               "enabled": False, "secret": SECRET}).json()
        sink_url = f"{server}{api}/sink?endpoint_id={ep['id']}"
        assert c.patch(f"{api}/{ep['id']}", json={"url": sink_url}).status_code == 200
        d = c.post(f"{api}/{ep['id']}/test").json()
        assert d["status"] == "delivered" and d["last_status_code"] == 200 and d["attempts"] == 1
        receipts = c.get(f"{api}/sink").json()["receipts"]
        assert receipts[0]["delivery_id"] == d["id"] and receipts[0]["signature_ok"] is True
        assert receipts[0]["event"] == "ping" and receipts[0]["endpoint_id"] == ep["id"]
        log = c.get(f"{api}/deliveries", params={"endpoint_id": ep["id"]}).json()["deliveries"]
        assert log[0]["id"] == d["id"] and log[0]["status"] == "delivered"

        # a wrong key on the endpoint side -> the sink answers 401 -> dead at once (4xx), visible in the log
        assert c.patch(f"{api}/{ep['id']}", json={"secret": "x" * 20}).status_code == 200
        c2 = httpx.Client(base_url=server, timeout=20)
        try:
            # keep the receiver's key different from the sender's by pointing at a second endpoint's sink
            other = c2.post(api, json={"name": "inny", "url": f"{server}/api/v1/y", "events": ["ping"],
                                       "secret": SECRET}).json()
            c2.patch(f"{api}/{ep['id']}", json={"url": f"{server}{api}/sink?endpoint_id={other['id']}"})
            bad = c2.post(f"{api}/{ep['id']}/test").json()
        finally:
            c2.close()
        assert bad["status"] == "dead" and bad["last_status_code"] == 401
        assert c.get(f"{api}/sink").json()["receipts"][0]["signature_ok"] is False
        assert SECRET not in c.get(api).text
