"""Integration contract suite (G5-CONTRACT): the real app on uvicorn 127.0.0.1:<free port> in this process, with
HYDRA_AUTH=1, its own temp state, the synthetic corpus (tests/ui_api/synthetic.py) and one REAL analysis of a
synthetic PNG submitted by the reference client (examples/client/hydra_client.py). Nothing leaves the machine."""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn
from PIL import Image
from ui_api.synthetic import seed

from hydra.api import create_app
from hydra.auth import Principal
from hydra.config import Settings

REPO = Path(__file__).resolve().parents[2]
ADMIN_PASSWORD = "Contract-Admin-Passw0rd-2026"


def load_client_module():
    spec = importlib.util.spec_from_file_location("hydra_client", REPO / "examples" / "client" / "hydra_client.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hydra_client"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def hc():
    return load_client_module()


def _token(svc, username: str, role: str) -> str:
    if username == "admin":
        with svc.reader() as conn:
            uid = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
    else:
        user, _pw = svc.create_user("admin", username, role, must_change=False)
        uid = user["id"]
    token, _item = svc.create_token(Principal(uid, username, role, False, "token"), f"contract-{username}")
    return token


@pytest.fixture(scope="module")
def live(tmp_path_factory, hc):
    root = tmp_path_factory.mktemp("contract")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HYDRA_SECRETS_DIR", str(root / "secrets"))
        mp.setenv("HYDRA_OFFLINE", "1")
        mp.setenv("HYDRA_WEBHOOK_BACKOFF_S", "0.05")
        mp.setenv("HYDRA_WEBHOOK_POLL_S", "0.2")
        mp.delenv("HYDRA_ADMIN_PASSWORD", raising=False)
        s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                     state_dir=root / "state", max_file_mb=5, auth=True, admin_password=ADMIN_PASSWORD)
        s.ensure_dirs()
        seed(s.db_path, s.cas_dir)
        app = create_app(s)
        srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        deadline = time.time() + 60
        while not srv.started and time.time() < deadline:
            time.sleep(0.02)
        assert srv.started, "contract API did not start"
        port = srv.servers[0].sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        svc = app.state.hydra.auth
        tokens = {"admin": _token(svc, "admin", "admin"), "analyst": _token(svc, "ana", "analyst"),
                  "viewer": _token(svc, "vio", "viewer")}
        png = root / "ad.png"
        Image.new("RGB", (96, 64), (20, 120, 220)).save(png, format="PNG")
        analyst = hc.HydraClient(base, tokens["analyst"], timeout=30)
        try:
            walk = hc.walk_through(analyst, [png], root / "out", log=lambda *_a: None)
            yield SimpleNamespace(base=base, app=app, settings=s, tokens=tokens, root=root, png=png, walk=walk,
                                  job_id=walk["submit"]["job_id"],
                                  client=lambda role=None, **kw: hc.HydraClient(base, tokens.get(role), **kw))
        finally:
            srv.should_exit = True
            t.join(timeout=20)
