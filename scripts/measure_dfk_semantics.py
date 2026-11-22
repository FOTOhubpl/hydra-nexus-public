"""Synthetic re-measurement for CR-ASTRA-RES-2 (AV-sync bands) and CR-ASTRA-RES-3 (AudioSeal per-window false alerts).

Synthetic inputs only, generated here (no organiser data, no network). Prints one JSON document on stdout.

    uv run python scripts/measure_dfk_semantics.py [--audioseal PATH] [--minutes 30]

Part A (AV): mouth-opening / speech-envelope series at 10 fps for scenarios that the AV heuristic is known to confuse
(matched speech, dubbing, off-screen narration, cuts, no face) scored with `hydra.genome.vid_avsync`. Run it once on
the current code and once with PYTHONPATH pointing at an older export to compare implementations. These arrays are
NOT physical face/audio measurements: the bands remain uncalibrated on real media.

Part B (AudioSeal): the real pinned detector on unmarked synthetic audio (harmonic 'voice + music' tracks from
tests/genome/test_aud.py, sine tones, white/pink noise, silence-with-hiss, optional macOS `say` speech). Counts
windows, local intervals and clip-level decisions that fire on audio that carries no mark.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.append(str(REPO))  # PYTHONPATH first, so an older export can be measured

FPS = 10
N = 300  # 30 s


def speech_env(rng: np.random.Generator, n: int = N) -> np.ndarray:
    """Syllable bumps (~4/s) grouped into 1-3 s phrases separated by 0.3-1 s pauses, over a small noise floor."""
    e = np.abs(rng.normal(0, 0.02, n))
    k = 0
    while k < n:
        phrase = int(rng.uniform(1, 3) * FPS)
        end = min(n, k + phrase)
        j = k
        while j < end:
            w = int(rng.integers(2, 4))
            e[j:j + w] += rng.uniform(0.5, 1.0) * np.hanning(w + 2)[1:-1][: max(0, min(w, n - j))]
            j += w + int(rng.integers(0, 2))
        k = end + int(rng.uniform(0.3, 1.0) * FPS)
    return e


def mouth_of(env: np.ndarray, rng: np.random.Generator, lag: int = 0, noise: float = 0.15) -> np.ndarray:
    m = np.convolve(env, np.ones(2) / 2, mode="same")
    m = np.roll(m, -lag)  # mouth leads audio by `lag` frames
    return m + rng.normal(0, noise * m.std(), len(m))


def scenarios(seed: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(seed)
    env = speech_env(rng)
    other = speech_env(rng)
    out = {
        "matched": (mouth_of(env, rng), env),
        "matched_lag2": (mouth_of(env, rng, lag=2), env),
        "dubbing": (mouth_of(other, rng), env),
    }
    # off-screen narration: the presenter is on screen with a closed mouth while a narrator speaks
    closed = 0.02 + np.abs(rng.normal(0, 0.005, N))
    out["narration_face_closed"] = (closed, env)
    # off-screen narration with B-roll: a face in only 20% of the frames
    m = mouth_of(other, rng)
    m[np.arange(N) % 10 >= 2] = np.nan
    out["narration_broll"] = (m, env)
    # cuts every 3 s between the speaking presenter and a listening person
    m = mouth_of(env, rng)
    cut = (np.arange(N) // 30) % 2 == 1
    m[cut] = closed[cut]
    out["cuts_speaker_listener"] = (m, env)
    out["no_face"] = (np.full(N, np.nan), env)
    return out


def astra_probe_case() -> dict:
    from hydra.genome.vid_avsync import av_sync_score, suspicion_level

    envelope = np.zeros(200)
    envelope[:40] = 1 + 0.2 * np.sin(np.arange(40))
    a = envelope.copy()
    b = envelope.copy()
    b[40:] = 100 * np.random.default_rng(42).normal(size=160)
    sa, sb = av_sync_score(a, envelope), av_sync_score(b, envelope)
    return {"original": {**sa, "band": suspicion_level(sa)},
            "changed_outside_gate": {**sb, "band": suspicion_level(sb)}}


def part_av(seeds: int) -> dict:
    from hydra.genome.vid_avsync import R_HIGH, R_LOW, av_sync_score, suspicion_level

    rows: dict[str, dict] = {}
    for seed in range(seeds):
        for name, (m, e) in scenarios(seed).items():
            s = av_sync_score(m, e)
            r = rows.setdefault(name, {"r": [], "bands": {}})
            if s["r"] is not None:
                r["r"].append(s["r"])
            band = str(suspicion_level(s))
            r["bands"][band] = r["bands"].get(band, 0) + 1
    summary = {}
    for name, r in rows.items():
        v = np.array(r["r"]) if r["r"] else np.array([np.nan])
        summary[name] = {"n": seeds, "r_q10": round(float(np.nanquantile(v, 0.1)), 3),
                         "r_median": round(float(np.nanmedian(v)), 3),
                         "r_q90": round(float(np.nanquantile(v, 0.9)), 3), "bands": dict(sorted(r["bands"].items()))}
    return {"thresholds": {"R_LOW": R_LOW, "R_HIGH": R_HIGH}, "scenarios": summary, "astra_probe": astra_probe_case()}


def _pink(rng: np.random.Generator, n: int) -> np.ndarray:
    f = np.fft.rfft(rng.normal(size=n))
    f /= np.sqrt(np.maximum(1, np.arange(len(f))))
    x = np.fft.irfft(f, n)
    return x / np.abs(x).max() * 0.5


def _say(text: str, path: Path) -> np.ndarray | None:
    if not (shutil.which("say") and shutil.which("ffmpeg")):
        return None
    aiff = path.with_suffix(".aiff")
    subprocess.run(["say", "-o", str(aiff), text], check=True, capture_output=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", str(path)],
                   check=True, capture_output=True)
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768.0


def unmarked_clips(minutes: float, tmp: Path) -> list[tuple[str, np.ndarray]]:
    from tests.genome.test_aud import degrade, track

    sr, clips, total = 16000, [], 0.0
    rng = np.random.default_rng(7)
    t = np.arange(20 * sr) / sr
    gens = [
        ("track", lambda k: track(100 + k, 20.0)),
        ("track_degraded", lambda k: degrade(track(200 + k, 20.0), k, snr_db=15.0)),
        ("sine", lambda k: (0.2 * np.sin(2 * np.pi * (110 + 37 * k) * t)).astype(np.float32)),
        ("white_noise", lambda k: (0.1 * rng.normal(size=20 * sr)).astype(np.float32)),
        ("pink_noise", lambda k: _pink(rng, 20 * sr).astype(np.float32)),
        ("hiss", lambda k: (0.002 * rng.normal(size=20 * sr)).astype(np.float32)),
    ]
    k = 0
    while total < minutes * 60:
        name, g = gens[k % len(gens)]
        x = g(k // len(gens))
        clips.append((name, x))
        total += len(x) / sr
        k += 1
    phrases = ["Zainwestuj dziś i zarabiaj pięć tysięcy złotych dziennie bez wychodzenia z domu.",
               "This is a synthetic narration used only to measure false alerts of a watermark detector."]
    for i, p in enumerate(phrases):
        x = _say(p * 3, tmp / f"say{i}.wav")
        if x is not None:
            clips.append(("say_tts", x))
    return clips


def part_audioseal(path: Path, minutes: float) -> dict:
    from hydra.genome import dfk
    from hydra.vendor.audioseal import load_detector

    det = load_detector(path)
    with tempfile.TemporaryDirectory(prefix="dfk-sem-") as d:
        clips = unmarked_clips(minutes, Path(d))
    per: dict[str, dict] = {}
    for name, x in clips:
        mask = dfk._marked_mask(det, x, 16000, dfk.AS_CHUNK_S, dfk.AS_MAX_S)
        w = 16000
        n = len(mask) // w
        shares = [float(mask[k * w:(k + 1) * w].mean()) for k in range(n)]
        row = per.setdefault(name, {"clips": 0, "seconds": 0.0, "windows": 0, "windows_marked": 0,
                                    "intervals_1win": 0, "intervals_2win": 0, "clips_global": 0,
                                    "max_window_share": 0.0, "max_fraction": 0.0})
        row["clips"] += 1
        row["seconds"] += round(len(mask) / 16000, 2)
        row["windows"] += n
        row["windows_marked"] += sum(s > dfk.AS_WIN_FRAC for s in shares)
        row["intervals_1win"] += len(dfk.marked_intervals(mask, min_windows=1))
        row["intervals_2win"] += len(dfk.marked_intervals(mask, min_windows=2))
        row["clips_global"] += int(float(mask.mean()) > dfk.AS_THRESHOLD)
        row["max_window_share"] = round(max(row["max_window_share"], max(shares, default=0.0)), 4)
        row["max_fraction"] = round(max(row["max_fraction"], float(mask.mean()) if len(mask) else 0.0), 4)
    tot = {k: sum(r[k] for r in per.values()) for k in ("clips", "windows", "windows_marked", "intervals_1win",
                                                         "intervals_2win", "clips_global")}
    nw = max(1, tot["windows"])
    tot["window_fp_upper95"] = round(1 - math.pow(0.05, 1 / nw), 5) if tot["windows_marked"] == 0 else None
    return {"detector": str(path.name), "win_s": dfk.AS_WIN_S, "win_frac": dfk.AS_WIN_FRAC,
            "min_windows": dfk.AS_MIN_WINDOWS, "per_generator": per, "total": tot}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audioseal", type=Path, default=None)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--seeds", type=int, default=50)
    ap.add_argument("--skip-audio", action="store_true")
    a = ap.parse_args()
    out = {"data": "synthetic only", "av": part_av(a.seeds)}
    if not a.skip_audio and a.audioseal and a.audioseal.is_file():
        out["audioseal"] = part_audioseal(a.audioseal, a.minutes)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
