"""Keep ASTRA test resources isolated from all persisted HYDRA data."""

import builtins
import io
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app


@pytest.fixture(autouse=True)
def isolated_resources(monkeypatch, settings):
    root = Path.home() / "HydraNexus"
    attempts = []

    def guarded(original):
        def opened(file, *args, **kwargs):
            if not isinstance(file, int):
                path = Path(os.fsdecode(file)).resolve()
                if path.is_relative_to(root):
                    parts = path.relative_to(root).parts
                    if parts and (parts[0] in {"models", "cache"} or parts[0].startswith("state")
                                  or parts[:2] == ("data", "nask")):
                        attempts.append(str(path))
                        raise RuntimeError(f"Forbidden persisted resource: {path}")
            return original(file, *args, **kwargs)
        return opened

    for module in (builtins, io, os):
        monkeypatch.setattr(module, "open", guarded(module.open))
    for key, value in {
        "HYDRA_DATA_DIR": settings.data_dir, "HYDRA_MODELS_DIR": settings.models_dir,
        "HYDRA_CACHE_DIR": settings.cache_dir, "HYDRA_STATE_DIR": settings.state_dir,
        "HYDRA_ENRICH_CACHE": settings.enrich_cache,
        "HYDRA_OFFLINE": "1", "HYDRA_ENRICH": "0", "HYDRA_LLM": "0",
        "HYDRA_LEXICON_DIR": "none", "HYDRA_MODEL_DOWNLOAD": "0",
    }.items():
        monkeypatch.setenv(key, str(value))
    yield
    if attempts:
        raise RuntimeError(f"Forbidden resource access attempted: {attempts}")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings), raise_server_exceptions=False) as http:
        yield http
