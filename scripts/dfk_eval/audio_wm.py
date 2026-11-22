"""AudioSeal detector evaluation (server only, eval venv with the upstream `audioseal` package for embedding).

Positives: VoxPopuli PL test clips (CC0) watermarked with the upstream AudioSeal generator (MIT) and a random 16-bit
message. Negatives: the same clips unmarked, plus In-the-Wild clips (CC BY-SA 4.0; bona fide and spoofed public-figure
speech, none expected to carry AudioSeal). Conditions applied to both classes with ffmpeg: clean, AAC 64 kbit/s,
MP3 32 kbit/s, 16->44.1->16 kHz resample + AAC 128 kbit/s at 44.1 kHz (screen-recording-like), 20 dB white noise.
Score = fraction of samples with p(marked) > 0.5 (upstream `detect_watermark` rule), our vendored detector.
Prints and writes aggregate JSON only.

    PYTHONPATH=<checkout> venv-eval/bin/python scripts/dfk_eval/audio_wm.py --vox data/voxpopuli_pl/test.parquet \
        --itw data/itw/release_in_the_wild --det models/audioseal/detector_base.safetensors --out results/audio_wm.json
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import rate_at, summary  # noqa: E402

from hydra.genome.dfk import audioseal_score  # noqa: E402
from hydra.vendor.audioseal import load_detector  # noqa: E402

SR = 16000


def ffmpeg_roundtrip(x: np.ndarray, args_enc: list[str], ext: str, tmp: Path, pre_sr: int | None = None) -> np.ndarray:
    src, mid = tmp / "src.wav", tmp / f"mid.{ext}"
    sf.write(src, x, SR, subtype="PCM_16")
    enc = ["ffmpeg", "-v", "error", "-y", "-i", str(src)]
    if pre_sr:
        enc += ["-ar", str(pre_sr)]
    subprocess.run(enc + args_enc + [str(mid)], check=True)
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(mid), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(out, np.float32).copy()


CONDITIONS = {
    "clean": None,
    "aac64": (["-c:a", "aac", "-b:a", "64k"], "m4a", None),
    "mp3_32": (["-c:a", "libmp3lame", "-b:a", "32k"], "mp3", None),
    "screenrec_aac128_44k": (["-c:a", "aac", "-b:a", "128k"], "m4a", 44100),
}


def apply(name: str, x: np.ndarray, tmp: Path, rng: np.random.Generator) -> np.ndarray:
    if name == "noise20db":
        p = float(np.mean(x ** 2)) + 1e-12
        return (x + rng.normal(0, np.sqrt(p / 100.0), x.shape)).astype(np.float32)
    spec = CONDITIONS[name]
    if spec is None:
        return x
    return ffmpeg_roundtrip(x, spec[0], spec[1], tmp, spec[2])


def load_vox(path: Path, n: int, seed: int) -> list[np.ndarray]:
    import pyarrow.parquet as pq

    t = pq.read_table(path, columns=["audio"]).to_pylist()
    rng = np.random.default_rng(seed)
    out = []
    for i in rng.permutation(len(t)):
        x, sr = sf.read(io.BytesIO(t[i]["audio"]["bytes"]), dtype="float32")
        if x.ndim > 1:
            x = x.mean(1)
        if sr != SR or len(x) < 2 * SR:
            continue
        out.append(x[: 10 * SR])
        if len(out) >= n:
            break
    return out


def load_itw(root: Path, n: int, seed: int) -> list[np.ndarray]:
    files = sorted(root.glob("*.wav"))
    rng = np.random.default_rng(seed)
    out = []
    for i in rng.permutation(len(files))[: n * 2]:
        x, sr = sf.read(files[i], dtype="float32")
        if x.ndim > 1:
            x = x.mean(1)
        if sr != SR:
            import torchaudio.functional as AF
            x = AF.resample(torch.from_numpy(x), sr, SR).numpy()
        if len(x) >= 2 * SR:
            out.append(x[: 10 * SR])
        if len(out) >= n:
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vox", type=Path, required=True)
    ap.add_argument("--itw", type=Path)
    ap.add_argument("--det", type=Path, required=True)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--check-upstream", action="store_true")
    ap.add_argument("--conditions", default=",".join([*CONDITIONS, "noise20db"]))
    a = ap.parse_args()
    torch.set_num_threads(2)
    from audioseal import AudioSeal

    gen = AudioSeal.load_generator("audioseal_wm_16bits")
    det = load_detector(a.det)
    rng = np.random.default_rng(0)
    vox = load_vox(a.vox, a.n, 0)
    itw = load_itw(a.itw, a.n, 1) if a.itw else []
    marked = []
    with torch.no_grad():
        for x in vox:
            t = torch.from_numpy(x)[None, None]
            msg = torch.randint(0, 2, (1, 16))
            marked.append((t + gen.get_watermark(t, SR, message=msg)).squeeze().numpy().astype(np.float32))
    res: dict = {"date": time.strftime("%Y-%m-%d %H:%M %Z"), "n_vox": len(vox), "n_itw": len(itw),
                 "score": "fraction of samples with p(marked) > 0.5 (vendored detector, converted safetensors)",
                 "conditions": {}}
    if a.check_upstream:
        up = AudioSeal.load_detector("audioseal_detector_16bits")
        diffs = []
        with torch.no_grad():
            for x in marked[:20] + vox[:20]:
                t = torch.from_numpy(x)[None, None]
                p_up, _ = up.detect_watermark(t, SR)
                diffs.append(abs(float(p_up) - audioseal_score(det, x)))
        res["max_abs_diff_vs_upstream"] = float(max(diffs))
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        for cond in a.conditions.split(","):
            t0 = time.time()
            pos = [audioseal_score(det, apply(cond, x, tmp, rng)) for x in marked]
            neg_v = [audioseal_score(det, apply(cond, x, tmp, rng)) for x in vox]
            neg_i = [audioseal_score(det, apply(cond, x, tmp, rng)) for x in itw]
            neg = neg_v + neg_i
            s = summary(np.array(neg), np.array(pos))
            s["fpr_at_0.5"] = rate_at(neg, 0.5)
            s["fpr_itw_at_0.5"] = rate_at(neg_i, 0.5) if neg_i else None
            s["tpr_at_0.5"] = rate_at(pos, 0.5)
            s["seconds"] = round(time.time() - t0, 1)
            res["conditions"][cond] = s
            print(cond, json.dumps({k: s[k] for k in ("auroc", "auroc_ci95", "tpr_at_0.5", "fpr_at_0.5")}), flush=True)
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_text(json.dumps(res, indent=1))  # after every condition: a crash keeps what was measured
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
