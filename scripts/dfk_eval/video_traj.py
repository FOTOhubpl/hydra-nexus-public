"""Evaluation of the dfk_video statistic (DINOv2 token-trajectory curvature, ReStraV-style). Server only.

  calib (permissive; thresholds are chosen here): reals Wikimedia Commons videos (CC0 / CC BY / CC BY-SA, fetched by
        commons_videos.py), fakes Rapidata text-2-video-human-preferences-veo3 (Apache-2.0; Veo 2/3, Sora, Ray2,
        Pika, Hunyuan, Wan2.1, Gen-3 alpha);
  eval  (numbers only; GenVideo re-upload sshaiton/GenVideo-50k-shard, provenance of the reals unclear): real vs
        10 older generators.
Conditions: `orig` and `reencoded` (ffmpeg: height 540, 30 fps, libx264 CRF 30, the kind of platform re-encode a
screen recording goes through), applied to both classes. Aggregates only.

    PYTHONPATH=<checkout> python scripts/dfk_eval/video_traj.py --models ~/HydraNexus/models \
        --real data/commons_video --fake data/rapidata_veo3 --gv data/genvideo --out results/video_traj.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
import time
from multiprocessing import get_context
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import rate_at, summary  # noqa: E402

_MODEL = None
VIDEO_EXT = (".mp4", ".webm", ".mov", ".ogv", ".mkv")


def _init(models: str) -> None:
    global _MODEL
    import torch
    from transformers import AutoModel

    torch.set_num_threads(2)
    _MODEL = AutoModel.from_pretrained(Path(models) / "dinov2-small", local_files_only=True,
                                       use_safetensors=True).eval()


def reencode(src: Path, dst: Path) -> Path:
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-an", "-vf", "scale=-2:540,fps=30",
                    "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", str(dst)], check=True, timeout=300)
    return dst


def _stat(path: str) -> tuple[float | None, float | None]:
    from hydra.genome.dfk import trajectory_stats, video_windows

    out = []
    with tempfile.TemporaryDirectory() as td:
        for p in (Path(path), None):
            try:
                if p is None:
                    p = reencode(Path(path), Path(td) / "re.mp4")
                wins, _ = video_windows(p)
                st = [s["angle_mean"] for _t, fr in wins if (s := trajectory_stats(_MODEL, fr)) is not None]
                out.append(float(np.median(st)) if st else None)
            except Exception:  # noqa: BLE001
                out.append(None)
    return out[0], out[1]


def run(paths: list[str], models: str, workers: int) -> np.ndarray:
    with get_context("spawn").Pool(workers, initializer=_init, initargs=(models,)) as pool:
        return np.array(pool.map(_stat, paths, chunksize=1), dtype=object)


def genvideo_extract(gv: Path, dest: Path, per_gen: int, n_real: int, seed: int) -> list[tuple[str, str]]:
    rng = np.random.default_rng(seed)
    items: dict[str, list[tuple[tarfile.TarFile, str]]] = {}
    out = []
    for t in sorted(gv.glob("*.tar")):
        if t.stat().st_size < 1_000_000:
            continue
        tf = tarfile.open(t)
        names = {m.name: m for m in tf.getmembers()}
        for n, m in names.items():
            if n.endswith(".json"):
                j = json.loads(tf.extractfile(m).read())
                vid = n[:-5] + ".mp4"
                if vid in names:
                    items.setdefault(j.get("generator") or j["label"], []).append((tf, vid))
    dest.mkdir(parents=True, exist_ok=True)
    for gen, lst in items.items():
        k = n_real if gen == "real" else per_gen
        for i in rng.permutation(len(lst))[:k]:
            tf, vid = lst[i]
            p = dest / Path(vid).name
            if not p.exists():
                p.write_bytes(tf.extractfile(vid).read())
            out.append((gen, str(p)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True)
    ap.add_argument("--real", type=Path, required=True)
    ap.add_argument("--fake", type=Path, required=True)
    ap.add_argument("--gv", type=Path, required=True)
    ap.add_argument("--n-fake", type=int, default=200)
    ap.add_argument("--per-gen", type=int, default=15)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skip-eval", action="store_true", help="calibration set only")
    a = ap.parse_args()
    t0 = time.time()
    res: dict = {"date": time.strftime("%Y-%m-%d %H:%M %Z"), "statistic": "median over windows of the mean "
                 "turning angle (deg), dfk.trajectory_stats, dinov2-small, 2 s @ 12 fps, 4 windows",
                 "conditions": ["orig", "reencoded"]}
    man = a.real / "manifest.json"
    listed = {m["file"] for m in json.loads(man.read_text())} if man.exists() else None
    reals = sorted(str(p) for p in a.real.iterdir() if p.suffix.lower() in VIDEO_EXT
                   and (listed is None or p.name in listed))  # only files with licence + attribution recorded
    rng = np.random.default_rng(0)
    fk = sorted(str(p) for p in a.fake.iterdir() if p.suffix.lower() in VIDEO_EXT)
    fakes = [fk[i] for i in rng.permutation(len(fk))[: a.n_fake]]
    sr, sf_ = run(reals, a.models, a.workers), run(fakes, a.models, a.workers)
    calib: dict = {}
    for ci, c in enumerate(res["conditions"]):
        neg = np.array([v for v in sr[:, ci] if v is not None], float)
        pos = np.array([v for v in sf_[:, ci] if v is not None], float)
        calib[c] = summary(neg, pos)
        calib[c]["n_unmeasured"] = [int(sum(v is None for v in sr[:, ci])), int(sum(v is None for v in sf_[:, ci]))]
    neg_re = np.array([v for v in sr[:, 1] if v is not None], float)
    t_med, t_high = float(np.quantile(neg_re, 0.90)), float(np.quantile(neg_re, 0.99))
    calib["thresholds_from_reencoded_reals"] = {"t_medium_q90": t_med, "t_high_q99": t_high}
    gens = [Path(f).name.split("_")[1] for f in fakes]
    calib["fake_generators"] = {g: gens.count(g) for g in sorted(set(gens))}
    by = {}
    for g in sorted(set(gens)):
        pos = np.array([v for v, gg in zip(sf_[:, 1], gens, strict=True) if gg == g and v is not None], float)
        by[g] = {"n": len(pos), "auroc_reencoded": summary(neg_re, pos)["auroc"] if len(pos) else None}
    calib["by_generator_reencoded"] = by
    res["calib"] = calib
    print("calib", json.dumps({c: calib[c]["auroc"] for c in res["conditions"]}), t_med, t_high, flush=True)
    a.out.write_text(json.dumps(res, indent=1))
    if a.skip_eval:
        return 0
    with tempfile.TemporaryDirectory(dir=a.out.parent) as td:
        items = genvideo_extract(a.gv, Path(td), a.per_gen, 10 * a.per_gen, 3)
        se = run([p for _g, p in items], a.models, a.workers)
    gl = np.array([g for g, _p in items])
    ev: dict = {}
    for ci, c in enumerate(res["conditions"]):
        neg = np.array([v for v, g in zip(se[:, ci], gl, strict=True) if g == "real" and v is not None], float)
        pos = np.array([v for v, g in zip(se[:, ci], gl, strict=True) if g != "real" and v is not None], float)
        e = summary(neg, pos)
        e["fpr_at_t_high"], e["tpr_at_t_high"] = rate_at(neg, t_high), rate_at(pos, t_high)
        e["by_generator"] = {g: summary(neg, np.array([v for v, gg in zip(se[:, ci], gl, strict=True)
                                                       if gg == g and v is not None], float))["auroc"]
                             for g in sorted(set(gl) - {"real"})}
        ev[c] = e
    res["eval_genvideo"] = ev
    res["seconds"] = round(time.time() - t0, 1)
    print("eval", json.dumps({c: (ev[c]["auroc"], ev[c]["auroc_ci95"]) for c in res["conditions"]}), flush=True)
    a.out.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
