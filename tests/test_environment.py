"""Environment smoke tests — generic, no product logic. They pass identically on macOS and Linux."""

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _env_after_sourcing(root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HYDRA_", "HF_", "TRANSFORMERS_"))}
    # env.sh creates directories while being sourced. Keep that side effect in this test's scratch root.
    env["HYDRA_ROOT"] = str(root)
    env.update(extra or {})
    out = subprocess.run(
        ["bash", "-c", ". scripts/env.sh && env -0"],
        cwd=REPO, env=env, check=True, capture_output=True, text=True,
    ).stdout
    return dict(item.split("=", 1) for item in out.split("\0") if "=" in item)


def test_python_is_311():
    assert sys.version_info[:2] == (3, 11)


def test_env_is_offline_by_default(tmp_path):
    env = _env_after_sourcing(tmp_path)
    assert env["HYDRA_OFFLINE"] == "1"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert env["HF_HUB_DISABLE_TELEMETRY"] == "1"


def test_heavy_dirs_live_outside_the_repo(tmp_path):
    env = _env_after_sourcing(tmp_path)
    for key in ("HYDRA_DATA_DIR", "HYDRA_MODELS_DIR", "HYDRA_CACHE_DIR", "HYDRA_STATE_DIR", "HF_HOME"):
        assert not Path(env[key]).resolve().is_relative_to(REPO), key


def test_api_binds_loopback_only(tmp_path):
    assert _env_after_sourcing(tmp_path)["HYDRA_API_HOST"] == "127.0.0.1"
