"""Portability guards: every registry model is pinned in models.lock.json; assign-batch item discovery."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fetch_models():
    spec = importlib.util.spec_from_file_location("fetch_models", ROOT / "scripts" / "fetch_models.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_every_default_model_is_locked_with_sha256():
    fm = _fetch_models()
    lock = json.loads((ROOT / "models.lock.json").read_text())["entries"]
    for key, spec in fm.REGISTRY.items():
        if not spec["default"]:
            continue
        assert key in lock, f"{key} missing from models.lock.json"
        assert lock[key]["revision"] == spec["revision"]
        assert lock[key]["files"] and all(len(f["sha256"]) == 64 for f in lock[key]["files"])


def test_runtime_model_paths_match_registry_keys():
    from hydra.genome import aud, prov, vid_avsync

    fm = _fetch_models()
    assert f"faster-whisper-{aud.ASR_MODEL}" in fm.REGISTRY
    assert vid_avsync.LANDMARKER_DIR in fm.REGISTRY
    assert prov.MP_MODELS[0] == f"{vid_avsync.LANDMARKER_DIR}/{vid_avsync.LANDMARKER_FILE}"
    url_files = fm.REGISTRY[vid_avsync.LANDMARKER_DIR]["files"]
    assert url_files[0]["sha256"] == vid_avsync.LANDMARKER_SHA256


def test_batch_items_skip_hidden(tmp_path):
    from hydra.cli_task import _batch_items

    (tmp_path / "inc-a").mkdir()
    (tmp_path / "inc-b.zip").write_bytes(b"PK")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    (tmp_path / "__MACOSX").mkdir()
    assert [p.name for p in _batch_items(tmp_path)] == ["inc-a", "inc-b.zip"]
