"""Integration hub tests: temporary HOME (secrets), synthetic run, injected HTTP transport. No network at all."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ui_api.synthetic import seed

from hydra.api import create_app
from hydra.config import Settings
from hydra.integrations import http

FIXTURES = Path(__file__).parent / "fixtures"
PUBLIC_IP = "93.184.216.34"
SECRET = "sk-TEST-0123456789abcdefSECRETvalue"


def fixture(provider: str, name: str) -> dict:
    return json.loads((FIXTURES / provider / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def temp_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "integ-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HYDRA_SECRETS_DIR", raising=False)
    monkeypatch.setenv("HYDRA_META_ENV_FILE", str(home / ".config" / "hydra" / "meta.env"))
    for name in ("VT_API_KEY", "URLSCAN_API_KEY", "URLHAUS_AUTH_KEY", "ABUSEIPDB_KEY", "GOOGLE_SAFE_BROWSING_KEY",
                 "MISP_URL", "MISP_KEY", "ES_URL", "ES_API_KEY", "SPLUNK_HEC_URL", "SPLUNK_HEC_TOKEN",
                 "SLACK_WEBHOOK_URL", "HYDRA_OFFLINE"):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def public_dns(monkeypatch):
    """netguard.check_url(resolve=True) resolves every name to a public address (no real DNS)."""
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    monkeypatch.setattr("hydra.netguard.socket.getaddrinfo", fake)


class Recorder:
    """Injected transport: answers from a routing function, records every call."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.routes: list[tuple[str, int, object]] = []  # (url substring, status, body)

    def add(self, needle: str, body: object, status: int = 200) -> None:
        self.routes.append((needle, status, body))

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        for needle, status, resp in self.routes:
            if needle in url:
                data = resp if isinstance(resp, bytes) else json.dumps(resp).encode()
                return status, {"content-type": "application/json"}, data
        return 404, {}, b"{}"


@pytest.fixture
def transport():
    rec = Recorder()
    prev = http.set_transport(rec)
    yield rec
    http.set_transport(prev)


def _settings(tmp_path: Path, offline: bool) -> Settings:
    s = Settings(root=tmp_path, data_dir=tmp_path / "data", models_dir=tmp_path / "models",
                 cache_dir=tmp_path / "cache", state_dir=tmp_path / "state", offline=offline, max_file_mb=5)
    s.ensure_dirs()
    return s


@pytest.fixture
def online_settings(tmp_path: Path) -> Settings:
    return _settings(tmp_path, offline=False)


@pytest.fixture
def offline_settings(tmp_path: Path) -> Settings:
    return _settings(tmp_path, offline=True)


@pytest.fixture
def online_client(online_settings: Settings, public_dns, transport):
    seed(online_settings.db_path, online_settings.cas_dir)
    with TestClient(create_app(online_settings)) as c:
        c.transport_rec = transport  # type: ignore[attr-defined]
        c.settings = online_settings  # type: ignore[attr-defined]
        yield c


@pytest.fixture
def offline_client(offline_settings: Settings, transport):
    seed(offline_settings.db_path, offline_settings.cas_dir)
    with TestClient(create_app(offline_settings)) as c:
        c.transport_rec = transport  # type: ignore[attr-defined]
        c.settings = offline_settings  # type: ignore[attr-defined]
        yield c
