"""tests/auth run with HYDRA_AUTH=1 (the product default); the rest of the suite has it off (tests/conftest.py)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.config import Settings


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 22, 4, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def auth_settings(tmp_path: Path, **kw) -> Settings:
    s = Settings(root=tmp_path, data_dir=tmp_path / "data", models_dir=tmp_path / "models",
                 cache_dir=tmp_path / "cache", state_dir=tmp_path / "state", auth=True, **kw)
    s.ensure_dirs()
    return s


def admin_password(s: Settings) -> str:
    lines = s.admin_credentials_path.read_text(encoding="utf-8").splitlines()
    return next(ln.split(": ", 1)[1] for ln in lines if ln.startswith("hasło / password: "))


class Harness:
    def __init__(self, client: TestClient, settings: Settings, clock: FakeClock) -> None:
        self.c, self.settings, self.clock = client, settings, clock

    @property
    def svc(self):
        return self.c.app.state.hydra.auth

    def client(self) -> TestClient:
        """A second, cookie-less client on the same app."""
        return TestClient(self.c.app)

    def login(self, client: TestClient, user: str, password: str, remember: bool = False):
        r = client.post("/v1/auth/login", json={"username": user, "password": password, "remember": remember})
        if r.status_code == 200:
            client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return r

    def admin(self, new_password: str = "Root-Test-Passw0rd!") -> TestClient:
        """Log the primary client in as admin and complete the forced password change."""
        pw = admin_password(self.settings)
        assert self.login(self.c, "admin", pw).status_code == 200
        r = self.c.post("/v1/auth/password", json={"current_password": pw, "new_password": new_password})
        assert r.status_code == 200, r.text
        return self.c

    def admin_login(self, password: str = "Root-Test-Passw0rd!") -> TestClient:
        assert self.login(self.c, "admin", password).status_code == 200
        return self.c

    def user(self, role: str, name: str | None = None) -> TestClient:
        """Create a user with `role` through the admin API, log in on a fresh client, change the password."""
        name = name or f"u-{role}"
        r = self.c.post("/v1/auth/users", json={"username": name, "role": role})
        assert r.status_code == 201, r.text
        cl = self.client()
        pw = r.json()["password"]
        assert self.login(cl, name, pw).status_code == 200
        assert cl.post("/v1/auth/password", json={"current_password": pw,
                                                  "new_password": f"{role}-Passw0rd-xyz!"}).status_code == 200
        return cl


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def h(tmp_path: Path, clock: FakeClock, monkeypatch) -> Iterator[Harness]:
    monkeypatch.delenv("HYDRA_ADMIN_PASSWORD", raising=False)
    s = auth_settings(tmp_path)
    with TestClient(create_app(s)) as c:
        c.app.state.hydra.auth.clock = clock
        yield Harness(c, s, clock)
