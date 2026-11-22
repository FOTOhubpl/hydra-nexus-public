"""E2E fixtures: one real API subprocess per session (tests/e2e/server.py), synthetic corpus, temp state.

The e2e tests are excluded from `just check`: they are collected but skipped unless the marker expression selects
them (`pytest -m e2e`, i.e. `just e2e`) or HYDRA_E2E=1. E2E_SERVER_JSON=<server.json> reuses a running e2e server
(started with `python -m tests.e2e.server`) instead of starting one; never point it at the demo state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.server import DEFAULT_STATE, start

HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):  # noqa: ANN001
    selected = "e2e" in (config.getoption("-m") or "") or os.environ.get("HYDRA_E2E") == "1"
    skip = pytest.mark.skip(reason="e2e suite: run with `just e2e` (pytest -m e2e)")
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.e2e)
            if not selected:
                item.add_marker(skip)


class _Reused(SimpleNamespace):
    def client(self, timeout: float = 120.0, *, auth: bool = True):  # noqa: ANN201
        import httpx

        token = self.info.get("api_token") if auth else None
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False, headers=headers)

    def session_client(self, timeout: float = 120.0):  # noqa: ANN201
        from tests.e2e.server import login_session

        return login_session(self.base_url, self.info.get("admin_user", "admin"),
                             self.info.get("admin_password", ""), timeout=timeout)

    def log_tail(self, n: int = 60) -> str:
        return ""


@pytest.fixture(scope="session")
def srv(tmp_path_factory):  # noqa: ANN001, ANN201
    reuse = os.environ.get("E2E_SERVER_JSON")
    if reuse:
        info = json.loads(Path(reuse).read_text())
        state = Path(info["state_dir"]).resolve()
        assert state != DEFAULT_STATE and DEFAULT_STATE not in state.parents
        yield _Reused(info=info, base_url=info["base_url"], port=info["port"], root=Path(info["root"]),
                      state_dir=state, base=Path(info["base"]), new=Path(info["new"]),
                      base_zip=Path(info["base_zip"]), netlog=Path(info["root"]) / "netlog.jsonl")
        return
    server = start(tmp_path_factory.mktemp("e2e") / "root")
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def api(srv):  # noqa: ANN001, ANN201
    with srv.client() as c:
        yield c


@pytest.fixture(scope="session")
def ctx() -> dict:
    """State handed from one ordered e2e step to the next (campaign ids, batch report...)."""
    return {}
