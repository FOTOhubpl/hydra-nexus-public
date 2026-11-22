"""HYDRA_PUBLIC_HOSTS opens the Host/Origin guard for named demo hosts only, and only with auth on."""
from hydra import api


def _scope(host, method="GET", origin=None):
    h = [(b"host", host.encode())]
    if origin:
        h.append((b"origin", origin.encode()))
    return {"headers": h, "method": method}


def test_public_host_refused_by_default(monkeypatch):
    monkeypatch.delenv("HYDRA_PUBLIC_HOSTS", raising=False)
    assert api._guard_reason(_scope("hacknation.example.org")) is not None


def test_public_host_allowed_with_auth(monkeypatch):
    monkeypatch.setenv("HYDRA_PUBLIC_HOSTS", "hacknation.example.org")
    monkeypatch.setenv("HYDRA_AUTH", "1")
    assert api._guard_reason(_scope("hacknation.example.org")) is None
    assert api._guard_reason(_scope("hacknation.example.org", "POST", "https://hacknation.example.org")) is None
    assert api._guard_reason(_scope("evil.example.org")) is not None
    assert api._guard_reason(_scope("hacknation.example.org", "POST", "https://evil.example.org")) is not None


def test_public_host_ignored_without_auth(monkeypatch):
    monkeypatch.setenv("HYDRA_PUBLIC_HOSTS", "hacknation.example.org")
    monkeypatch.setenv("HYDRA_AUTH", "0")
    assert api._guard_reason(_scope("hacknation.example.org")) is not None
