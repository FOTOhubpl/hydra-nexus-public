"""The only outbound path: offline fail-closed, enabled flag, netguard, rate limit, audit, secret scrubbing."""

from __future__ import annotations

import json
import socket

import pytest

from hydra.db import Writer, connect
from hydra.integrations import http, state
from integrations.conftest import SECRET


@pytest.fixture
def writer(online_settings):
    w = Writer(online_settings.db_path)
    yield w
    w.close()


def _audit(settings):
    conn = connect(settings.db_path, readonly=True)
    try:
        return [json.loads(r["payload"]) for r in conn.execute(
            "SELECT payload FROM events WHERE type='INTEGRATION_REQUEST' ORDER BY seq")]
    finally:
        conn.close()


def test_offline_blocks_before_transport(offline_settings, transport):
    with pytest.raises(http.IntegrationError) as ei:
        http.request("virustotal", "GET", "https://www.virustotal.com/api/v3/domains/x.example",
                     settings=offline_settings, writer=None, require_enabled=False)
    assert ei.value.status == "offline" and "HYDRA_OFFLINE=0" in ei.value.message_pl
    assert transport.calls == []


def test_disabled_blocks(online_settings, transport, public_dns):
    with pytest.raises(http.IntegrationError) as ei:
        http.request("virustotal", "GET", "https://www.virustotal.com/api/v3/domains/x.example",
                     settings=online_settings, writer=None)
    assert ei.value.status == "disabled" and transport.calls == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://10.0.0.5/events/add", "http://169.254.169.254/",
                                 "http://localhost:9200/_bulk", "ftp://example.com/", "http://[::1]/"])
def test_netguard_blocks_non_public(online_settings, transport, writer, url):
    state.set_enabled(online_settings, "misp", True)
    with pytest.raises(http.IntegrationError) as ei:
        http.request("misp", "POST", url, settings=online_settings, writer=writer, body=b"{}")
    assert ei.value.status == "blocked"
    assert transport.calls == []


def test_netguard_blocks_private_resolution(online_settings, transport, writer, monkeypatch):
    monkeypatch.setattr("hydra.netguard.socket.getaddrinfo",
                        lambda h, p, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.10", p))])
    state.set_enabled(online_settings, "elasticsearch", True)
    with pytest.raises(http.IntegrationError) as ei:
        http.request("elasticsearch", "POST", "https://es.corp-example.org/_bulk", settings=online_settings,
                     writer=writer)
    assert ei.value.status == "blocked" and transport.calls == []


def test_audit_and_scrub(online_settings, transport, writer, public_dns):
    state.set_enabled(online_settings, "google-safe-browsing", True)
    transport.add("safebrowsing", {})
    res = http.request("google-safe-browsing", "POST",
                       f"https://safebrowsing.googleapis.com/v4/threatMatches:find?key={SECRET}", body=b"{}",
                       settings=online_settings, writer=writer, secrets=[SECRET])
    assert res.ok and SECRET not in res.url_redacted
    rows = _audit(online_settings)
    assert rows and rows[-1]["host"] == "safebrowsing.googleapis.com"
    assert rows[-1]["path"] == "/v4/threatMatches:find" and rows[-1]["status"] == 200
    assert set(rows[-1]) == {"connector", "method", "host", "path", "status", "ms", "bytes_out", "bytes_in",
                             "payload_sha256"}
    assert SECRET not in json.dumps(rows)


def test_transport_exception_is_scrubbed(online_settings, writer, public_dns):
    def boom(*a):
        raise OSError(f"connection reset while sending key={SECRET}")

    prev = http.set_transport(boom)
    try:
        state.set_enabled(online_settings, "virustotal", True)
        with pytest.raises(http.IntegrationError) as ei:
            http.request("virustotal", "GET", "https://www.virustotal.com/api/v3/domains/a.example",
                         settings=online_settings, writer=writer, secrets=[SECRET])
    finally:
        http.set_transport(prev)
    assert SECRET not in str(ei.value) and SECRET not in ei.value.message_pl
    assert SECRET not in json.dumps(_audit(online_settings))


def test_url_secret_path_scrubbed():
    hook = "https://hooks.slack.com/services/T000/B000/abcdefghijklmnop"
    assert "abcdefghijklmnop" not in http.scrub(f"error at {hook}", [hook])
    assert "/services/T000" not in http.scrub("path /services/T000/B000/abcdefghijklmnop", [hook])


def test_rate_limit(online_settings, transport, writer, public_dns):
    state.set_enabled(online_settings, "virustotal", True)  # 4/min
    transport.add("virustotal", {"data": {}})
    for _ in range(4):
        http.request("virustotal", "GET", "https://www.virustotal.com/api/v3/domains/a.example",
                     settings=online_settings, writer=writer)
    with pytest.raises(http.IntegrationError) as ei:
        http.request("virustotal", "GET", "https://www.virustotal.com/api/v3/domains/a.example",
                     settings=online_settings, writer=writer)
    assert ei.value.status == "rate_limited" and len(transport.calls) == 4
    assert state.usage(online_settings, "virustotal", 4)["requests_60m"] == 4


def test_loopback_allowed_only_when_asked(online_settings, transport, writer):
    state.set_enabled(online_settings, "misp", True)
    transport.add("127.0.0.1", {"ok": True})
    res = http.request("misp", "GET", "http://127.0.0.1:8765/sink", settings=online_settings, writer=writer,
                       allow_loopback=True)
    assert res.ok
