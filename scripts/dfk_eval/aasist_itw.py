"""EVAL-ONLY measurement of AASIST-L (clovaai/aasist, MIT code, bundled weights trained on ASVspoof 2019 LA, ODC-By) on
In-the-Wild (CC BY-SA 4.0). Server only; the AASIST code is imported from a server clone, nothing is shipped.

Protocol as in the AASIST repo: first 64,600 samples at 16 kHz (repeat-padded when shorter), score = logit of the
bona fide class (index 1); here spoof score = -score. Reports AUROC (spoof positive) with a bootstrap CI and the EER.

    venv-eval/bin/python scripts/dfk_eval/aasist_itw.py --repo repos/aasist --itw data/itw/release_in_the_wild \
        --n 600 --out results/aasist_itw.json
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import summary  # noqa: E402

NB = 64600


def eer(neg: np.ndarray, pos: np.ndarray) -> float:
    thr = np.unique(np.concatenate([neg, pos]))
    fpr = np.array([(neg >= t).mean() for t in thr])
    fnr = np.array([(pos < t).mean() for t in thr])
    i = int(np.argmin(np.abs(fpr - fnr)))
    return float((fpr[i] + fnr[i]) / 2)


def pad(x: np.ndarray) -> np.ndarray:
    if len(x) >= NB:
        return x[:NB]
    return np.tile(x, NB // max(1, len(x)) + 1)[:NB]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--itw", type=Path, required=True)
    ap.add_argument("--n", type=int, default=600)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    torch.set_num_threads(8)
    sys.path.insert(0, str(a.repo))
    from models.AASIST import Model  # noqa: E402  (server clone, MIT)

    cfg = json.loads((a.repo / "config" / "AASIST-L.conf").read_text())["model_config"]
    model = Model(cfg)
    model.load_state_dict(torch.load(a.repo / "models" / "weights" / "AASIST-L.pth", map_location="cpu",
                                     weights_only=True))
    model.eval()
    rows = list(csv.DictReader(open(a.itw / "meta.csv")))
    rng = np.random.default_rng(0)
    by = {lab: [r for r in rows if r["label"] == lab] for lab in ("bona-fide", "spoof")}
    scores: dict[str, list[float]] = {"bona-fide": [], "spoof": []}
    t0 = time.time()
    with torch.no_grad():
        for lab, lst in by.items():
            for i in rng.permutation(len(lst))[: a.n // 2]:
                x, sr = sf.read(a.itw / lst[i]["file"], dtype="float32")
                if x.ndim > 1:
                    x = x.mean(1)
                _, out = model(torch.from_numpy(pad(x))[None])
                scores[lab].append(-float(out[0, 1]))
    neg, pos = np.array(scores["bona-fide"]), np.array(scores["spoof"])
    res = {"date": time.strftime("%Y-%m-%d %H:%M %Z"), "model": "AASIST-L (clovaai/aasist @ a04c986)",
           "data": "In-the-Wild, random balanced subset", "itw_summary": summary(neg, pos),
           "eer": round(eer(neg, pos), 4),
           "seconds": round(time.time() - t0, 1)}
    print(json.dumps({k: res[k] for k in ("eer",)}), res["itw_summary"]["auroc"], res["itw_summary"]["auroc_ci95"])
    a.out.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
