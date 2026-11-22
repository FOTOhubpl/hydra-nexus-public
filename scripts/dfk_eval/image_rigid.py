"""Evaluation of the dfk_image statistic (RIGID-style representation instability, dinov2-small). Server only.

Two sets, two capture conditions each (applied identically to reals and fakes):
  calib (permissive; thresholds are chosen here): reals OpenImages v7 validation (CC BY 2.0), fakes Community
        Forensics train parquet shards (CC BY 4.0: commercial + systematic generators);
  eval  (CC BY-NC-SA 4.0, numbers only): Community Forensics Eval (21 generators, paired reals RAISE/COCO/FFHQ/LAION).
Conditions: `orig` (decoded file as is) and `laundered` (short side 512 -> JPEG q75 -> x0.75 -> JPEG q70, an
approximation of a re-shared screenshot; it also removes the PNG-vs-JPEG format bias between the classes).
Writes aggregate JSON only (scores per image are kept in memory).

    PYTHONPATH=<checkout> python scripts/dfk_eval/image_rigid.py --models ~/HydraNexus/models \
        --oi data/openimages/img --cc data/commfor_cc --ev data/commfor --out results/image_rigid.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from multiprocessing import get_context
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import rate_at, summary  # noqa: E402

_MODEL = None


def launder(im):
    from PIL import Image

    im = im.convert("RGB")
    s = 512 / min(im.size)
    im = im.resize((max(1, round(im.width * s)), max(1, round(im.height * s))), Image.Resampling.BICUBIC)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=75)
    im = Image.open(io.BytesIO(b.getvalue())).convert("RGB")
    im = im.resize((round(im.width * 0.75), round(im.height * 0.75)), Image.Resampling.BILINEAR)
    b = io.BytesIO()
    im.save(b, "JPEG", quality=70)
    return Image.open(io.BytesIO(b.getvalue())).convert("RGB")


_LAM = 0.05


def _init(models: str, backbone: str = "dinov2-small", lam: float = 0.05) -> None:
    global _MODEL, _LAM
    import torch
    from transformers import AutoModel

    torch.set_num_threads(2)
    _LAM = lam
    _MODEL = AutoModel.from_pretrained(Path(models) / backbone, local_files_only=True,
                                       use_safetensors=True).eval()


def _score(blobs: list[bytes]) -> list[tuple[float, float]]:
    from PIL import Image

    from hydra.genome.dfk import instability_scores, to_pixels

    origs, lds = [], []
    for b in blobs:
        im = Image.open(io.BytesIO(b))
        im.draft("RGB", (1024, 1024))
        im = im.convert("RGB")
        origs.append(np.asarray(im))
        lds.append(np.asarray(launder(im)))
    a = instability_scores(_MODEL, to_pixels(origs), lam=_LAM)
    b = instability_scores(_MODEL, to_pixels(lds), lam=_LAM)
    return list(zip(a.tolist(), b.tolist(), strict=True))


def parquet_rows(files: list[Path], per_file: int, seed: int, label: int | None = None):
    """Yield (meta, image bytes) sampled per file (rows with matching label when given)."""
    import pyarrow.parquet as pq

    rng = np.random.default_rng(seed)
    for f in files:
        pf = pq.ParquetFile(f)
        meta = pf.read(columns=["label", "model_name", "architecture"] if "architecture" in pf.schema_arrow.names
                       else ["label", "model_name"]).to_pylist()
        idx = [i for i, m in enumerate(meta) if label is None or int(m["label"]) == label]
        pick = set(rng.permutation(idx)[:per_file].tolist())
        if not pick:
            continue
        off = 0
        for rg in range(pf.num_row_groups):
            n = pf.metadata.row_group(rg).num_rows
            want = [i - off for i in range(off, off + n) if i in pick]
            if want:
                t = pf.read_row_group(rg, columns=["image_data"]).column(0)
                for j in want:
                    yield meta[off + j], t[j].as_py()
            off += n


ARGS: tuple = ()


def run(blobs: list[bytes], models: str, workers: int) -> np.ndarray:
    chunks = [blobs[i : i + 8] for i in range(0, len(blobs), 8)]
    with get_context("spawn").Pool(workers, initializer=_init, initargs=(models, *ARGS)) as pool:
        out = [s for part in pool.map(_score, chunks) for s in part]
    return np.array(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--oi", type=Path, required=True)
    ap.add_argument("--cc", type=Path, required=True)
    ap.add_argument("--ev", type=Path, required=True)
    ap.add_argument("--n-calib", type=int, default=600)
    ap.add_argument("--per-shard", type=int, default=40)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--backbone", default="dinov2-small")
    ap.add_argument("--lam", type=float, default=0.05)
    a = ap.parse_args()
    global ARGS
    ARGS = (a.backbone, a.lam)
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    t0 = time.time()
    res: dict = {"date": time.strftime("%Y-%m-%d %H:%M %Z"), "statistic": f"dfk.instability_scores ({a.backbone}, "
                 f"lambda {a.lam}, 4 draws)", "conditions": ["orig", "laundered"]}
    # calibration set (permissive)
    oi = sorted(a.oi.glob("*.jpg"))[: a.n_calib]
    reals = [p.read_bytes() for p in oi]
    cc_files = sorted(a.cc.glob("*.parquet"))
    fakes_meta, fakes = [], []
    for m, b in parquet_rows(cc_files, a.n_calib // max(1, len(cc_files)) + 1, 1, label=1):
        fakes_meta.append(m)
        fakes.append(b)
    sr, sf_ = run(reals, a.models, a.workers), run(fakes, a.models, a.workers)
    calib = {}
    for ci, cond in enumerate(res["conditions"]):
        calib[cond] = summary(sr[:, ci], sf_[:, ci])
    thr_med = float(np.quantile(sr[:, 1], 0.90))
    thr_high = float(np.quantile(sr[:, 1], 0.99))
    calib["thresholds_from_laundered_reals"] = {"t_medium_q90": thr_med, "t_high_q99": thr_high}
    calib["fake_generators"] = sorted({str(m["model_name"]) for m in fakes_meta})[:50]
    calib["n_fake_generators"] = len({str(m["model_name"]) for m in fakes_meta})
    res["calib"] = calib
    print("calib", json.dumps({c: calib[c]["auroc"] for c in res["conditions"]}), thr_med, thr_high, flush=True)
    # evaluation set (NC, numbers only)
    ev_meta, ev_blobs = [], []
    for m, b in parquet_rows(sorted(a.ev.glob("*.parquet")), a.per_shard, 2):
        ev_meta.append(m)
        ev_blobs.append(b)
    se = run(ev_blobs, a.models, a.workers)
    lab = np.array([int(m["label"]) for m in ev_meta])
    arch = np.array([str(m.get("architecture") or "?") for m in ev_meta])
    ev: dict = {}
    for ci, cond in enumerate(res["conditions"]):
        neg, pos = se[lab == 0, ci], se[lab == 1, ci]
        e = summary(neg, pos)
        e["fpr_at_t_high"] = rate_at(neg, thr_high)
        e["tpr_at_t_high"] = rate_at(pos, thr_high)
        e["fpr_at_t_medium"] = rate_at(neg, thr_med)
        e["tpr_at_t_medium"] = rate_at(pos, thr_med)
        e["by_architecture"] = {g: {"n": int((arch == g).sum()), "auroc_vs_all_reals": summary(neg, se[(lab == 1) & (
            arch == g), ci])["auroc"]} for g in sorted(set(arch[lab == 1]))}
        ev[cond] = e
    res["eval_commfor_eval_nc"] = ev
    res["seconds"] = round(time.time() - t0, 1)
    print("eval", json.dumps({c: (ev[c]["auroc"], ev[c]["auroc_ci95"]) for c in res["conditions"]}), flush=True)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
