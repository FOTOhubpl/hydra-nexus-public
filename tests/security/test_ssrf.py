"""No real network: DNS and transport are controlled; public-IP enforcement remains real."""

import socket
import time
from unittest.mock import Mock

import pytest

from hydra.enrich import fetch
from hydra.enrich.client import Enricher


@pytest.mark.parametrize(("host", "resolved"), [
    ("127.0.0.1", "127.0.0.1"), ("169.254.169.254", "169.254.169.254"),
    ("[::1]", "::1"), ("0.0.0.0", "0.0.0.0"), ("2130706433", "127.0.0.1"),
    ("0177.0.0.1", "127.0.0.1"), ("0x7f000001", "127.0.0.1"), ("10.1.2.3", "10.1.2.3"),
])
def test_private_and_obfuscated_addresses_are_blocked(settings, monkeypatch, host, resolved):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET6 if ":" in resolved else socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, 80))])
    connect = Mock(side_effect=RuntimeError("Transport must not be reached"))
    monkeypatch.setattr(socket, "create_connection", connect)
    result = fetch.fetch_url(f"http://{host}/", settings.model_copy(update={"offline": False, "enrich": True}))
    assert result.status.startswith("blocked:")
    connect.assert_not_called()


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://127.0.0.1/", "data:text/html,synthetic"])
def test_non_http_schemes_are_blocked(settings, monkeypatch, url):
    dns = Mock(side_effect=RuntimeError("DNS must not be reached"))
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    result = fetch.fetch_url(url, settings.model_copy(update={"offline": False, "enrich": True}))
    assert result.status.startswith("blocked:")
    dns.assert_not_called()


@pytest.mark.parametrize("redirect", ["http://127.0.0.1/private", "http://169.254.169.254/latest/meta-data/"])
def test_redirect_target_is_rechecked(settings, monkeypatch, redirect):
    original = fetch._one
    calls = []

    def one(url, deadline=None):
        calls.append(url)
        if url == "https://public.example/start":
            return fetch.Hop(url, status=302, redirect_to=redirect)
        return original(url, deadline)
    monkeypatch.setattr(fetch, "_one", one)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (redirect.split("/")[2], 80))])
    connect = Mock(side_effect=RuntimeError("Private target must never connect"))
    monkeypatch.setattr(socket, "create_connection", connect)
    # Favicon is independently guarded in production; isolate the redirect path here.
    monkeypatch.setattr(fetch, "_favicon", lambda *_a: None)
    result = fetch.fetch_url("https://public.example/start",
                             settings.model_copy(update={"offline": False, "enrich": True}))
    assert result.status.startswith("blocked:")
    assert calls == ["https://public.example/start", redirect]
    connect.assert_not_called()


def test_offline_default_fetch_makes_no_socket_or_dns_calls(settings, monkeypatch):
    connect = Mock(side_effect=OSError("synthetic blocked socket"))
    dns = Mock(side_effect=OSError("synthetic blocked DNS"))
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    result = fetch.fetch_url("https://public.example/", settings.model_copy(update={"enrich": True, "offline": True}))
    assert result.status == "disabled"
    connect.assert_not_called()
    dns.assert_not_called()


def test_force_cannot_override_offline_socket_kill_switch(settings, monkeypatch):
    connect = Mock(side_effect=OSError("synthetic blocked socket"))
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    result = fetch.fetch_url("https://public.example/", settings.model_copy(update={"offline": True}), force=True)
    assert connect.call_count == 0
    assert result.status == "disabled"


@pytest.mark.parametrize("explicit_live", [
    False, True,
])
def test_offline_enricher_does_not_call_fetcher(settings, monkeypatch, explicit_live):
    connect = Mock(side_effect=OSError("synthetic blocked socket"))
    monkeypatch.setattr(socket.socket, "connect", connect)

    def provider():
        with socket.socket() as sock:
            sock.connect(("8.8.8.8", 443))
    enricher = Enricher(settings=settings.model_copy(update={"offline": True, "enrich": True}),
                        live=True if explicit_live else None)
    result = enricher.get("astra", "synthetic", provider)
    assert result.status == "unavailable"
    connect.assert_not_called()


def test_expired_fetch_deadline_prevents_socket_connection(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    connect = Mock(side_effect=RuntimeError("Expired request must not connect"))
    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(fetch.DeadlineExceeded):
        fetch._one("https://public.example/", time.monotonic() - 1)
    connect.assert_not_called()


def test_public_fetch_connects_to_vetted_ip_without_second_dns_resolution(monkeypatch):
    dns = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    connect = Mock(side_effect=OSError("synthetic stopped transport"))
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(OSError, match="synthetic stopped transport"):
        fetch._one("https://public.example/")
    dns.assert_called_once()
    assert connect.call_args.args[0] == ("8.8.8.8", 443)


def registry_redirect_visits(monkeypatch, destination):
    import io
    import urllib.request
    from email.message import Message
    from urllib.response import addinfourl

    from hydra.enrich.client import http_json

    visited = []

    class SyntheticTransport(urllib.request.HTTPHandler, urllib.request.HTTPSHandler):
        def http_open(self, request):
            url = request.full_url
            visited.append(url)
            headers = Message()
            if url == "https://registry.example/record":
                headers["Location"] = destination
                response = addinfourl(io.BytesIO(b""), headers, url, 302)
                response.msg = "Found"
            else:
                response = addinfourl(io.BytesIO(b'{"synthetic":true}'), headers, url, 200)
                response.msg = "OK"
            return response
        https_open = http_open

    # Real urllib redirect machinery; only the HTTP transport is replaced. No server, DNS or socket.
    monkeypatch.setattr(urllib.request, "_opener", urllib.request.build_opener(SyntheticTransport()))
    http_json("https://registry.example/record", tries=1)
    return visited


@pytest.mark.parametrize("private", ["http://127.0.0.1/private", "http://169.254.169.254/latest/meta-data/"])
def test_registry_redirect_cannot_reach_private_destination(monkeypatch, private):
    visited = registry_redirect_visits(monkeypatch, private)
    assert visited == ["https://registry.example/record"]


def test_registry_public_redirect_remains_supported(monkeypatch):
    target = "https://second-registry.example/record"
    assert registry_redirect_visits(monkeypatch, target) == ["https://registry.example/record", target]
