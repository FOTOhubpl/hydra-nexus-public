"""Reviewer findings #7, #12, #13, #16, #17 (hostile input hardening)."""

from __future__ import annotations

import socket
import threading
import time

from fixtures import _synthetic_warninglists, web_incident  # noqa: F401

from hydra.enrich import dns, fetch
from hydra.enrich.client import Enricher
from hydra.genome import base, webparse
from hydra.genome.dom import inf_dom
from hydra.genome.har import inf_har


def test_dns_rejects_option_like_names(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(dns.subprocess, "run", lambda *a, **k: called.append(a))
    enr = Enricher(cache_dir=tmp_path, live=True)
    for bad in ("-f/etc/passwd", "a.-b.com", "a b.com", "x.com-", "-q", "a..b", "", "a" * 64 + ".com"):
        r = dns.query(enr, bad, "A")
        assert r.status == "error:invalid", bad
        assert dns._dig(bad, "A")[1] == "error:invalid"
    assert not called
    assert dns.NAME_OK.match("www.shop-42deals.xyz") and dns.NAME_OK.match("20.100.83.45.origin.asn.cymru.com")


def test_dig_gets_name_after_q_flag(monkeypatch):
    seen = {}

    class P:
        stdout = ";; ->>HEADER<<- opcode: QUERY, status: NOERROR\nx.com. 60 IN A 192.0.2.1\n"

    def run(args, **k):
        seen["args"] = args
        return P()

    monkeypatch.setattr(dns.shutil, "which", lambda n: "/usr/bin/dig")
    monkeypatch.setattr(dns.subprocess, "run", run)
    url, status, data = dns._dig("x.com", "A")
    assert status == "ok" and data["answers"] == ["192.0.2.1"]
    a = seen["args"]
    assert a[a.index("-q") + 1] == "x.com" and a[-1] == "x.com"


def test_drip_server_hits_the_deadline(monkeypatch):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def drip():
        c, _ = srv.accept()
        c.recv(4096)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100000\r\n\r\n")
        while not stop.is_set():
            try:
                c.sendall(b"a")
            except OSError:
                break
            time.sleep(0.05)
        c.close()

    threading.Thread(target=drip, daemon=True).start()
    monkeypatch.setattr(fetch, "_public_ip", lambda h, p: "127.0.0.1")
    monkeypatch.setattr(fetch, "HOP_DEADLINE", 0.6)
    monkeypatch.setenv("HYDRA_OFFLINE", "0")  # force=True alone never overrides the kill switch
    t0 = time.monotonic()
    try:
        res = fetch.fetch_url(f"http://127.0.0.1:{port}/", force=True, out_dir=None)
    finally:
        stop.set()
        srv.close()
    assert time.monotonic() - t0 < 5
    assert res.status == "error:DeadlineExceeded"


def test_white_page_regex_is_bounded():
    body = b"<script>" + b"<" * 4_000_000  # unterminated script: worst case for <script.*?</script>
    t0 = time.monotonic()
    fetch.white_page_signals(fetch.Hop(url="https://x.example/", status=200, body=body), "https://x.example/")
    assert time.monotonic() - t0 < 3


def test_oversized_files_are_unavailable(tmp_path, settings, monkeypatch):
    monkeypatch.setattr(webparse, "MAX_FILE_BYTES", 100)
    webparse._BUNDLES.clear()
    inc = web_incident(tmp_path)
    ctx = base.ExtractContext(settings=settings, work_dir=tmp_path)
    har = inf_har(inc, ctx).genes
    assert har and all(g.status == "unavailable" for g in har)
    assert any(g.evidence.note == webparse.FILE_TOO_LARGE for g in har)
    dom = inf_dom(inc, ctx).genes
    assert any(g.kind == "page_title" and g.status == "unavailable" for g in dom)
    webparse._BUNDLES.clear()


def test_bundle_cache_is_small(tmp_path):
    webparse._BUNDLES.clear()
    for i in range(5):
        webparse.bundle(web_incident(tmp_path / str(i), f"i{i}", har=False))
    assert len(webparse._BUNDLES) <= webparse.BUNDLE_CACHE
