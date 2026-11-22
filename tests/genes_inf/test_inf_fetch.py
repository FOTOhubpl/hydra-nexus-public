from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fixtures import _synthetic_warninglists  # noqa: F401

from hydra.contracts import Incident, IncidentArtifact
from hydra.enrich import fetch
from hydra.genome import base
from hydra.genome.dom import inf_dom
from hydra.genome.har import inf_har

PAGE = (
    b"<html><head><title>Lucky offer</title><meta http-equiv='refresh' content='0; url=/final'></head>"
    b"<body>x</body></html>"
)
FINAL = (
    b"<html><head><title>Final</title><link rel='icon' href='/fav.ico'></head><body>"
    b"<script>fbq('init','111222333444555');</script><form><input name='phone'></form>"
    + b"<p>word </p>" * 100
    + b"</body></html>"
)


class H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        routes = {
            "/start": (302, {"Location": "/landing"}, b""),
            "/landing": (200, {}, PAGE),
            "/final": (200, {"X-Powered-By": "PHP/8.1"}, FINAL),
            "/fav.ico": (200, {}, b"\x00ICO" * 300),
        }
        code, hdrs, body = routes.get(self.path, (404, {}, b"nope"))
        self.send_response(code)
        ctype = "image/x-icon" if self.path.endswith(".ico") else "text/html"
        self.send_header("Content-Type", ctype)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_fetch_disabled_by_default(settings, tmp_path):
    assert fetch.fetch_url("http://127.0.0.1:1/x", settings, out_dir=tmp_path).status == "disabled"


def test_private_addresses_are_blocked(settings, tmp_path, server):
    online = settings.model_copy(update={"offline": False})
    res = fetch.fetch_url(server + "/start", online, force=True, out_dir=tmp_path)
    assert res.status.startswith("blocked:")


def test_fetch_chain_to_har_runs_extractors(settings, tmp_path, server, monkeypatch):
    monkeypatch.setattr(fetch, "_public_ip", lambda host, port: "127.0.0.1")
    # force=True still needs HYDRA_OFFLINE=0: the kill switch dominates (CR-ASTRA-SEC-8)
    online = settings.model_copy(update={"offline": False})
    res = fetch.fetch_url(server + "/start", online, force=True, out_dir=tmp_path)
    assert res.ok, res.status
    assert [h.status for h in res.hops] == [302, 200, 200]
    assert res.final_url.endswith("/final") and res.favicon
    assert res.record().source == "http_fetch"
    data = res.har_path.read_bytes()
    import hashlib

    meta = IncidentArtifact(
        artifact_id=hashlib.sha256(data).hexdigest(),
        role="har",
        path_in_incident="fetch.har",
        mime="application/json",
        size=len(data),
    )
    inc = base.ResolvedIncident(
        Incident(id="f", source="url", source_ref="u", artifacts=[meta]), [base.ResolvedArtifact(meta, res.har_path)]
    )
    ctx = base.ExtractContext(settings=settings, work_dir=tmp_path)
    kinds = {(g.kind, g.value) for g in inf_har(inc, ctx).genes}
    assert any(k == "redirect_hop" for k, _ in kinds)
    assert any(k == "redirect_skeleton" and "meta_refresh" in v for k, v in kinds)
    assert ("tracker_id", "fb_pixel:111222333444555") in kinds
    assert ("x_powered_by", "PHP/8.1") in kinds
    dom = {(g.kind, g.value) for g in inf_dom(inc, ctx).genes}
    assert ("page_title", "Final") in dom and any(k == "favicon_mmh3" for k, _ in dom)


def test_white_page_signals():
    hop = fetch.Hop(url="https://www.google.com/", status=200, body=b"<html><body>Just a moment...</body></html>")
    sig = fetch.white_page_signals(hop, "https://scam.example/x")
    assert {"bot_challenge", "tiny_text", "redirect_to_mainstream"} <= set(sig)
