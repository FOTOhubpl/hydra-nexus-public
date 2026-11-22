"""WATCH-RT test fixtures: temporary state, FakeClock, seeded jitter, injected fetchers / HTTP transport.

No real network (the root conftest socket guard stays on), no real sleeps (FakeClock; `Clock.sleep` patched where the
CLI uses the real clock), no organiser data: every value is synthetic (.example / .test, RFC 2606).
"""

from __future__ import annotations

import random
import socket
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from hydra.config import Settings
from hydra.db import Writer
from hydra.integrations import http
from hydra.watch.rt import store
from hydra.watch.rt.engine import FakeClock, WatchRT
from hydra.watch.rt.sources import Resp, SourceError

SEED = 1234
T0 = 1_800_000_000.0
PUBLIC_IP = "93.184.216.34"


def make_settings(tmp_path: Path, *, offline: bool) -> Settings:
    s = Settings(root=tmp_path, data_dir=tmp_path / "data", models_dir=tmp_path / "models",
                 cache_dir=tmp_path / "cache", state_dir=tmp_path / "state", offline=offline, max_file_mb=5)
    s.ensure_dirs()
    return s


@pytest.fixture(autouse=True)
def _isolated_secrets(tmp_path: Path, monkeypatch) -> None:
    """Secrets (URLhaus key) never come from the developer's HOME / environment."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HYDRA_SECRETS_DIR", str(home / "secrets"))
    for name in ("URLHAUS_AUTH_KEY", "HYDRA_WATCH_AUTO_ASSIGN", "HYDRA_WATCH_RT", "HYDRA_WATCH_FIXTURES",
                 "HYDRA_WATCH_RETENTION_DAYS", "HYDRA_WATCH_RUNS_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def online(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, offline=False)


@pytest.fixture
def offline(tmp_path: Path) -> Settings:
    return make_settings(tmp_path, offline=True)


class FakeFetch:
    """Injected fetcher (connector_id, method, url, headers) -> Resp. Records every call; `fail` makes it raise."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.fail: SourceError | None = None
        self.bodies: dict[str, bytes] = {}  # connector id -> GET body

    def __call__(self, connector_id: str, method: str, url: str, headers: dict[str, str]) -> Resp:
        self.calls.append((connector_id, method, url))
        if self.fail is not None:
            raise self.fail
        if method == "HEAD":
            return Resp(200, {"content-length": "0"}, b"")
        body = self.bodies.get(connector_id, b"")
        if connector_id == "cert-pl":
            return Resp(206, {}, body)
        return Resp(200, {}, body)

    def count(self, connector_id: str | None = None) -> int:
        return sum(1 for c in self.calls if connector_id is None or c[0] == connector_id)


@pytest.fixture
def fetch() -> FakeFetch:
    return FakeFetch()


@pytest.fixture
def make_engine() -> Iterator[Callable[..., WatchRT]]:
    """WatchRT on its own Writer, FakeClock at T0, rng seeded with SEED. Closed (lock + writer) at teardown."""
    made: list[tuple[WatchRT, Writer]] = []

    def make(settings: Settings, fetcher: Any = None, *, clock: FakeClock | None = None, seed: int = SEED
             ) -> WatchRT:
        w = Writer(settings.db_path)
        eng = WatchRT(settings, w, clock=clock or FakeClock(T0), fetcher=fetcher, rng=random.Random(seed),
                      auto_assign=False)
        made.append((eng, w))
        return eng

    yield make
    for eng, w in made:
        eng.close()
        w.close()


def source_row(eng: WatchRT, sid: str) -> dict[str, Any]:
    with eng.reader() as conn:
        return store.sources(conn)[sid]


def count(eng: WatchRT, sql: str, *args: Any) -> int:
    with eng.reader() as conn:
        return int(conn.execute(sql, args).fetchone()[0] or 0)


@pytest.fixture
def public_dns(monkeypatch) -> None:
    """netguard.check_url(resolve=True) resolves every name to one public address (no real DNS)."""

    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    monkeypatch.setattr("hydra.netguard.socket.getaddrinfo", fake)


class Transport:
    """hydra.integrations.http transport stub: (method, url, headers, body, timeout, max_bytes) -> (code, h, data)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.get_body = b""

    def __call__(self, method, url, headers, body, timeout, max_bytes):
        self.calls.append((method, url))
        if method == "HEAD":
            return 200, {"Content-Length": str(len(self.get_body))}, b""
        return 206, {}, self.get_body


@pytest.fixture
def transport() -> Iterator[Transport]:
    t = Transport()
    prev = http.set_transport(t)
    try:
        yield t
    finally:
        http.set_transport(prev)
