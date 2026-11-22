"""Network policy (hydra.netguard): SSRF URL checks and the process socket kill switch. No real network."""

import socket

import pytest

from hydra import netguard
from hydra.netguard import OfflineBlocked, UnsafeDestination, check_url, is_public_ip, literal_ip


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://2130706433/", "http://0177.0.0.1/", "http://0x7f000001/", "http://127.1/",
    "http://169.254.169.254/latest/meta-data/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://10.0.0.1/",
    "http://192.168.1.1/", "http://100.64.0.1/", "http://0.0.0.0/", "http://[fe80::1]/", "http://224.0.0.1/",
    "http://localhost:8000/", "http://api.localhost/", "http://metadata.google.internal/", "http://printer.local/",
    "http://intranet/", "file:///etc/passwd", "gopher://example.org/", "data:text/plain,x", "ftp://example.org/",
    "http:///nohost", "http://example.org:99999/",
])
def test_unsafe_destinations_are_rejected_without_dns(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: pytest.fail("DNS must not be reached"))
    with pytest.raises(UnsafeDestination):
        check_url(url)


def test_public_destinations_pass_and_resolution_is_checked(monkeypatch):
    assert check_url("https://8.8.8.8/x") == "8.8.8.8"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    assert check_url("https://public.example/", resolve=True) == "public.example"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(UnsafeDestination):
        check_url("https://rebinding.example/", resolve=True)


def test_literal_forms_and_public_ranges():
    assert str(literal_ip("2130706433")) == "127.0.0.1"
    assert str(literal_ip("0x7f.1")) == "127.0.0.1"
    assert literal_ip("example.org") is None
    assert is_public_ip("8.8.8.8") and not is_public_ip("::ffff:10.0.0.1") and not is_public_ip("2002:7f00:1::")


def test_offline_dominates_explicit_settings(monkeypatch, settings):
    assert netguard.offline(settings.model_copy(update={"offline": True}))
    monkeypatch.setenv("HYDRA_OFFLINE", "1")
    with pytest.raises(OfflineBlocked):
        netguard.require_online("synthetic")
    monkeypatch.setenv("HYDRA_MODEL_DOWNLOAD", "1")
    assert not netguard.model_download_allowed()
    monkeypatch.setenv("HYDRA_OFFLINE", "0")
    assert netguard.model_download_allowed()


def test_process_guard_blocks_remote_and_keeps_loopback(monkeypatch):
    monkeypatch.delenv("HYDRA_SOCKET_GUARD", raising=False)
    monkeypatch.setenv("HYDRA_OFFLINE", "1")
    assert netguard.guard_if_offline()
    try:
        with pytest.raises(OfflineBlocked):
            socket.getaddrinfo("public.example", 443)
        with socket.socket() as s, pytest.raises(OfflineBlocked):
            s.connect(("8.8.8.8", 443))
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s, pytest.raises(OfflineBlocked):
            s.sendto(b"x", ("8.8.8.8", 53))
        with socket.socket() as server:  # loopback still works (the API, the optional local LLM server)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=2):
                pass
        assert not netguard.install_offline_guard()  # idempotent
    finally:
        netguard.uninstall_offline_guard()
    monkeypatch.setenv("HYDRA_SOCKET_GUARD", "0")
    assert not netguard.guard_if_offline()
