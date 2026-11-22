"""Two analyse runs in separate processes with different PYTHONHASHSEED give byte-identical campaign/group JSON."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = r"""
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
from hydra.config import Settings
from hydra.genome import base
from tests.core import test_pipeline as tp
from hydra.contracts import Family
base.register("t_page", family=Family.INF)(tp.t_page)
base.register("t_vec", family=Family.VIS)(tp.t_vec)
from hydra.pipeline import analyse
from tests.conftest import png_bytes
root = Path(sys.argv[1])
s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
             state_dir=root / "state", workers=1)
s.ensure_dirs()
ds = tp.corpus(Path(sys.argv[2]), png_bytes)
res = analyse([ds], s, only=["t_page", "t_vec"])
print(json.dumps({"campaigns": [c.model_dump() for c in res.clustering.campaigns],
                  "groups": [g.model_dump() for g in res.clustering.groups],
                  "pairs": {f"{a}|{b}": pe.score.model_dump() for (a, b), pe in sorted(res.pairs.items())}},
                 sort_keys=True, default=str))
"""


def _run(tmp: Path, seed: str) -> str:
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run([sys.executable, "-c", SCRIPT, str(tmp / f"state{seed}"), str(tmp / f"src{seed}"), str(repo)],
                         capture_output=True, text=True, env=env, cwd=repo, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_campaign_and_group_json_is_identical_across_processes(tmp_path):
    a, b = _run(tmp_path, "1"), _run(tmp_path, "4242")
    da, db = json.loads(a), json.loads(b)
    assert da["campaigns"] == db["campaigns"] and da["groups"] == db["groups"]
    assert da["pairs"] == db["pairs"]
