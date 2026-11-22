"""Throwaway HYDRA API for the end-to-end suites: own temp state, own free port, synthetic corpus analysed.

Never touches ~/HydraNexus/state, the laptop demo (:8000) or the server demo (:8100): the port is picked
free from the OS (and 8000/8100 are refused), state/cache/data dirs live under the given root, models are read
from HYDRA_MODELS_DIR (read-only) when present.

CLI (used by the Playwright specs and scripts/e2e_all.sh):

    uv run python -m tests.e2e.server --root /tmp/hydra-e2e --ready /tmp/hydra-e2e/server.json [--ui-dist ui/dist]

writes {base_url, port, pid, root, state_dir, data_dir, base, new, analyse_job, analyse_seconds, admin_user,
admin_password, api_token} once the base corpus is analysed, then serves until SIGTERM/SIGINT.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

REPO = Path(__file__).resolve().parents[2]
FORBIDDEN_PORTS = {8000, 8100, 5173, 18100}
# AUTH-LOGIN: the e2e API runs with the product default HYDRA_AUTH=1 and this known first-admin password
# (HYDRA_ADMIN_PASSWORD, so no credentials file). start() logs in, proves the gate (401 without credentials, 401 on a
# wrong password, CSRF on session writes) and issues a personal access token that every harness client sends.
E2E_ADMIN_USER = "admin"
E2E_ADMIN_PASSWORD = "e2e-Admin-Passw0rd-2026"
DEFAULT_STATE = (Path.home() / "HydraNexus" / "state").resolve()


def free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in FORBIDDEN_PORTS:
            return port


@dataclass
class Server:
    root: Path
    port: int
    proc: subprocess.Popen[bytes]
    log: Path
    base: Path
    new: Path
    base_zip: Path
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def netlog(self) -> Path:
        return self.root / "netlog.jsonl"

    def client(self, timeout: float = 120.0, *, auth: bool = True) -> httpx.Client:
        token = self.info.get("api_token") if auth else None
        headers = {"Authorization": f"Bearer {token}"} if token else None
        return httpx.Client(base_url=self.base_url, timeout=timeout, trust_env=False, headers=headers)

    def session_client(self, timeout: float = 120.0) -> httpx.Client:
        """A browser-like client: logged in with the session cookie, CSRF header on every request."""
        return login_session(self.base_url, timeout=timeout)

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def log_tail(self, n: int = 60) -> str:
        with contextlib.suppress(OSError):
            return "\n".join(self.log.read_text(errors="replace").splitlines()[-n:])
        return ""


def server_env(root: Path, port: int, ui_dist: Path | None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    models = Path(env.get("HYDRA_MODELS_DIR") or Path.home() / "HydraNexus" / "models")
    env.update({
        "HYDRA_ROOT": str(root), "HYDRA_STATE_DIR": str(root / "state"), "HYDRA_DATA_DIR": str(root / "data"),
        "HYDRA_CACHE_DIR": str(root / "cache"), "HYDRA_MODELS_DIR": str(models),
        "HYDRA_API_HOST": "127.0.0.1", "HYDRA_API_PORT": str(port), "HYDRA_OFFLINE": "1",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HYDRA_SCHEDULER": "0", "HYDRA_ENRICH": "0",
        "HYDRA_LLM": "0", "HYDRA_SOCKET_GUARD": "1", "HYDRA_API_WARM": "0",
        "HF_HOME": str(root / "cache" / "huggingface"), "TORCH_HOME": str(root / "cache" / "torch"),
        "PYTHONUNBUFFERED": "1", "HYDRA_E2E_NETLOG": str(root / "netlog.jsonl"),
        # integrations + webhooks keep keys and endpoint config here, never in ~/.config/hydra
        "HYDRA_SECRETS_DIR": str(root / "secrets"),
        "HYDRA_AUTH": "1", "HYDRA_ADMIN_PASSWORD": E2E_ADMIN_PASSWORD,
    })
    env.setdefault("HYDRA_EXTRACT_WORKERS", "2")
    env.setdefault("HYDRA_TORCH_THREADS", "2")
    env.setdefault("HYDRA_OCR_WORKERS", "1")
    env.setdefault("OMP_NUM_THREADS", "2")
    env.setdefault("MKL_NUM_THREADS", "2")
    for k in ("HYDRA_UI_DIST", "HYDRA_ENRICH_CACHE", "HYDRA_LEXICON_DIR"):
        env.pop(k, None)
    if ui_dist is not None:
        env["HYDRA_UI_DIST"] = str(ui_dist)
    env.update(extra or {})
    return env


def start(root: Path, ui_dist: Path | None = None, analyse: bool = True, extra_env: dict[str, str] | None = None,
          analyse_timeout: float = 900.0) -> Server:
    from tests.e2e import corpus

    root = root.resolve()
    assert root != DEFAULT_STATE and DEFAULT_STATE not in root.parents, "e2e root must not be the real state"
    (root / "data").mkdir(parents=True, exist_ok=True)
    external = Path.home() / "HydraNexus" / "data" / "external"  # MISP/CERT/PSL snapshots, read-only
    if external.is_dir() and not (root / "data" / "external").exists():
        (root / "data" / "external").symlink_to(external)
    paths = corpus.build(root / "data" / "e2e")
    port = free_port()
    log = root / "server.log"
    proc = subprocess.Popen(  # noqa: S603 — our own interpreter
        [sys.executable, "-c", "from tests.e2e.netlog import install; install(); from hydra.cli import app; app()",
         "serve"],
        cwd=REPO, env=server_env(root, port, ui_dist, extra_env),
        stdout=log.open("wb"), stderr=subprocess.STDOUT, start_new_session=True)
    srv = Server(root=root, port=port, proc=proc, log=log, base=paths["base"], new=paths["new"],
                 base_zip=paths["base_zip"])
    try:
        wait_health(srv)
        srv.info.update(auth_bootstrap(srv.base_url))
        if analyse:
            t0 = time.monotonic()
            with srv.client() as c, srv.base_zip.open("rb") as fh:
                r = c.post("/v1/analyse", files={"files": ("base.zip", fh, "application/zip")})
                r.raise_for_status()
                job = r.json()
                assert job["mode"] == "corpus", job
                done = wait_job(c, f"/v1/analyse/{job['job_id']}", analyse_timeout)
            srv.info.update(analyse_job=job["job_id"], analyse_seconds=round(time.monotonic() - t0, 1),
                            analyse_status=done["job"]["status"])
    except BaseException:
        sys.stderr.write(srv.log_tail())
        srv.stop()
        raise
    srv.info.update(base_url=srv.base_url, port=port, pid=proc.pid, root=str(root),
                    state_dir=str(root / "state"), data_dir=str(root / "data"), base=str(srv.base),
                    new=str(srv.new), base_zip=str(srv.base_zip), log=str(log))
    return srv


class _LoggedIn(httpx.Client):
    """A client that already sent its login request: `with` must not try to open it a second time."""

    def __enter__(self) -> _LoggedIn:
        return self


def login_session(base_url: str, user: str = E2E_ADMIN_USER, password: str = E2E_ADMIN_PASSWORD,
                  timeout: float | httpx.Timeout = 120.0) -> httpx.Client:
    c = _LoggedIn(base_url=base_url, timeout=timeout, trust_env=False)
    r = c.post("/v1/auth/login", json={"username": user, "password": password})
    if r.status_code != 200:
        c.close()
        raise RuntimeError(f"e2e login failed: {r.status_code} {r.text[:200]}")
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def auth_bootstrap(base_url: str) -> dict[str, Any]:
    """Exercise the auth gate once and return {admin_user, admin_password, api_token} for server.json."""
    with httpx.Client(base_url=base_url, timeout=30, trust_env=False) as anon:
        r = anon.get("/v1/jobs")
        assert r.status_code == 401, f"auth gate open: GET /v1/jobs -> {r.status_code}"
        assert set(anon.get("/health").json()) <= {"status", "version", "auth"}, "/health leaks before login"
        r = anon.post("/v1/auth/login", json={"username": E2E_ADMIN_USER, "password": "wrong-password"})
        assert r.status_code == 401, f"wrong password -> {r.status_code}"
    with login_session(base_url) as c:
        csrf = c.headers.pop("X-CSRF-Token")
        r = c.post("/v1/auth/tokens", json={"name": "e2e harness"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf", f"CSRF not enforced: {r.status_code}"
        r = c.post("/v1/auth/tokens", json={"name": "e2e harness"}, headers={"X-CSRF-Token": csrf})
        r.raise_for_status()
        token = r.json()["token"]
    return {"admin_user": E2E_ADMIN_USER, "admin_password": E2E_ADMIN_PASSWORD, "api_token": token}


def wait_health(srv: Server, timeout: float = 120.0) -> None:
    t_end = time.monotonic() + timeout
    with srv.client(timeout=5) as c:
        while time.monotonic() < t_end:
            if srv.proc.poll() is not None:
                raise RuntimeError(f"API exited with {srv.proc.returncode}:\n{srv.log_tail()}")
            with contextlib.suppress(httpx.HTTPError):
                if c.get("/health").status_code == 200:
                    return
            time.sleep(0.3)
    raise TimeoutError("API did not become healthy")


def wait_job(c: httpx.Client, url: str, timeout: float = 600.0) -> dict[str, Any]:
    t_end = time.monotonic() + timeout
    body: dict[str, Any] = {}
    while time.monotonic() < t_end:
        r = c.get(url)
        r.raise_for_status()
        body = r.json()
        status = (body.get("job") or {}).get("status")
        if status in ("done", "failed"):
            if status == "failed":
                raise RuntimeError(f"job failed: {body['job'].get('error_code')} {body['job'].get('error')}")
            return body
        time.sleep(0.5)
    raise TimeoutError(f"job {url} not finished in {timeout}s: {body.get('job')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--ready", type=Path, required=True)
    ap.add_argument("--ui-dist", type=Path)
    ap.add_argument("--no-analyse", action="store_true")
    a = ap.parse_args()
    srv = start(a.root, ui_dist=a.ui_dist.resolve() if a.ui_dist else None, analyse=not a.no_analyse)
    a.ready.parent.mkdir(parents=True, exist_ok=True)
    a.ready.write_text(json.dumps(srv.info, indent=2))
    print(json.dumps(srv.info), flush=True)

    def _bye(*_: object) -> None:
        srv.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _bye)
    signal.signal(signal.SIGINT, _bye)
    while srv.proc.poll() is None:
        time.sleep(1)
    sys.exit(srv.proc.returncode or 1)


if __name__ == "__main__":
    main()
