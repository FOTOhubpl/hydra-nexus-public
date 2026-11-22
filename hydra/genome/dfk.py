"""DFK: low-weight "suspicion of manipulation" signals (sygnały podejrzenia manipulacji). Never deepfake detection.

Three extractors, all label tier (dependency groups `*:label:*`, synapse.level() = "none", 0 linking bits). They feed
the "Nadużycie wizerunku" analyst card (hydra/likeness.py) only. Survey, licences and measurements:
docs/research/DEEPFAKE_2026.md and docs/genes/DFK.md.

1. `dfk_audio_wm` (META, on) - AudioSeal watermark present? Vendored detector (hydra/vendor/audioseal, MIT code, MIT
   weights trained on VoxPopuli, CC0), pinned in models.lock.json as a converted safetensors file. A positive means
   "audio carries an AudioSeal mark" (a tool that embeds it processed the audio; the mark can be embedded in real
   speech, so it says nothing about the content kind); absence proves nothing, because most commercial voice-cloning
   services do not embed AudioSeal. Headline `wm_audio` = clip-majority rule (> AS_THRESHOLD of samples, unchanged);
   `wm_audio_local` reports marked intervals and their share when the global rule is not met (CR-ASTRA-RES-3).
2. `dfk_image` (VIS, OFF by default: HYDRA_DFK_IMAGE=1) - representation instability of still images, training-free.
   Idea from RIGID (He et al. 2024, IBM/RIGID, Apache-2.0; behaviour re-implemented, no code copied): real photos
   keep their DINOv2 embedding under a tiny Gaussian pixel perturbation better than generated images. Score = 1 -
   mean cosine between the CLS embedding of the image and of DRAWS noisy copies (sigma NOISE_LAMBDA in normalised
   pixel units), pinned Apache-2.0 dinov2-small backbone. Higher = less stable = more typical of generators.
3. `dfk_video` (VIS, OFF by default: HYDRA_DFK_VIDEO=1) - temporal trajectory curvature, training-free. Idea from
   ReStraV (Interno et al., NeurIPS 2025, ChristianInterno/ReStraV, Apache-2.0; re-implemented, no code copied,
   their trained MLP is not used): natural video traces a straighter path in DINOv2 token space than generated
   video. Statistic = mean turning angle (degrees) between consecutive displacement vectors of the full token set
   over 2 s windows at 12 fps; windows with a cut or a still picture are skipped; video value = median over windows.

Thresholds for 2 and 3 were set on permissively licensed data only (docs/genes/DFK.md section 3). Evaluation on
non-commercial sets (Community Forensics Eval, GenVideo) produced numbers only; there both statistics were at or
below chance, which is why they are off by default. Every gene note says the score is a weak suspicion signal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedArtifact, ResolvedIncident, register

log = logging.getLogger(__name__)

VERSION = 1
GROUP_IMAGE = "VIS:label:synthetic_image"
GROUP_VIDEO = "VIS:label:synthetic_video"
GROUP_WM = "META:label:provenance"
WORDING = "sygnał podejrzenia manipulacji"
LEVEL_PL = {"low": "niski", "medium": "średni", "high": "podwyższony"}
CAVEAT = ("słaby sygnał bez kalibracji na reklamach; zrzut ekranu, rekompresja i nakładki interfejsu zmieniają wynik;"
          " to nie jest wykrywanie deepfake'ów ani dowód powiązania")

DINO_KEY = "dinov2-small"
MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
SIDE = 224

# --- image (RIGID-style). Thresholds: docs/genes/DFK.md section 3 (permissive calibration set, laundered).
NOISE_LAMBDA = 0.05
DRAWS = 4
NOISE_SEED = 20261121
IMG_T_MEDIUM = 0.0185   # q90 of laundered permissive reals (dinov2-small), DFK.md section 3
IMG_T_HIGH = 0.0441     # q99 of the same
IMG_MIN_SIDE = 96
IMG_MAX = 30
IMG_ROLES = {"ad_creative", "landing_asset", "landing_screenshot", "feed_screenshot", "other"}
IMG_QUALITY = 0.2

# --- video (ReStraV-style). Thresholds: docs/genes/DFK.md section 3.
VID_FPS = 12
VID_WIN_S = 2.0
VID_WINDOWS = 4
VID_MAX_S = 120.0
VID_CUT_RATIO = 4.0      # a step > 4x the window median = a cut (window skipped)
VID_STATIC_COS = 0.995   # consecutive frames this similar on average = still picture (window skipped)
VID_T_MEDIUM = 101.4     # q90 of re-encoded permissive reals (77 Commons videos), DFK.md section 3
VID_T_HIGH = 105.7       # q99 of the same (with n = 77 this is close to the maximum)
VID_QUALITY = 0.2

# --- audio watermark (AudioSeal)
AS_DIR = "audioseal-detector"
AS_FILE = "detector_base.safetensors"
AS_SRC_URL = ("https://huggingface.co/facebook/audioseal/resolve/3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba/"
              "detector_base.pth")
AS_SRC_SHA256 = "8a78e8a83584113523e161fc599fcab10fd0e94c04d2eb9d2fa1e9ec91ab69d9"
AS_SHA256 = "449efba12e01c194ac526c0c7dfd062bfe9fe5a6799273d13d78e174926cf1f9"
AS_THRESHOLD = 0.5       # fraction of samples with p(marked) > 0.5 (upstream detect_watermark rule)
AS_MAX_S = 300.0
AS_CHUNK_S = 30.0
# Local (per-window) reporting, CR-ASTRA-RES-3: 1 s windows, a window is marked when > 50% of its samples are, an
# interval needs >= 2 consecutive marked windows. False alerts measured on synthetic unmarked audio with the real
# detector: docs/genes/DFK.md section 8 (scripts/measure_dfk_semantics.py). Reported, never counted, never linking.
AS_WIN_S = 1.0
AS_WIN_FRAC = 0.5
AS_MIN_WINDOWS = 2


# ------------------------------------------------------------------------------------------ backbone


def _dino(ctx: ExtractContext):
    """dinov2-small (transformers, local files only), shared across incidents in ctx.models."""
    key = "dfk_dinov2"
    if key not in ctx.models:
        from transformers import AutoModel

        from hydra.ml.models import model_dir

        path = model_dir(DINO_KEY, ctx.settings)
        ctx.models[key] = AutoModel.from_pretrained(path, local_files_only=True, use_safetensors=True).eval()
    return ctx.models[key]


def to_pixels(rgb_images: list[np.ndarray]) -> np.ndarray:
    """uint8 HxWx3 arrays -> normalised float32 N x 3 x 224 x 224 (bicubic resize, no crop, as RIGID)."""
    from PIL import Image

    out = np.empty((len(rgb_images), 3, SIDE, SIDE), np.float32)
    for i, a in enumerate(rgb_images):
        im = Image.fromarray(a).convert("RGB")
        if im.size != (SIDE, SIDE):
            im = im.resize((SIDE, SIDE), Image.Resampling.BICUBIC)
        x = (np.asarray(im, np.float32) / 255.0 - MEAN) / STD
        out[i] = x.transpose(2, 0, 1)
    return out


def instability_scores(model: Any, pixels: np.ndarray, lam: float = NOISE_LAMBDA, draws: int = DRAWS,
                       seed: int = NOISE_SEED, batch: int = 16) -> np.ndarray:
    """1 - mean cos(CLS(x), CLS(x + lam * eps_k)) over `draws` fixed noise tensors (same for every image)."""
    import torch

    gen = torch.Generator().manual_seed(seed)
    noise = torch.randn((draws, 3, SIDE, SIDE), generator=gen)
    scores = np.zeros(len(pixels), np.float64)
    with torch.inference_mode():
        for i in range(0, len(pixels), batch):
            x = torch.from_numpy(pixels[i : i + batch])
            n = len(x)
            xs = torch.cat([x] + [x + lam * noise[k] for k in range(draws)])
            cls = model(pixel_values=xs).last_hidden_state[:, 0].float()
            cls = torch.nn.functional.normalize(cls, dim=-1)
            base = cls[:n]
            sims = torch.stack([(base * cls[n * (k + 1) : n * (k + 2)]).sum(-1) for k in range(draws)])
            scores[i : i + n] = (1.0 - sims.mean(0)).double().numpy()
    return scores


def trajectory_stats(model: Any, frames: np.ndarray) -> dict[str, float] | None:
    """Mean turning angle (deg) and step statistics of the DINOv2 token trajectory of one window of frames.

    `frames`: T x H x W x 3 uint8. Returns None for windows with a cut or a still picture."""
    import torch

    if len(frames) < 4:
        return None
    with torch.inference_mode():
        z = model(pixel_values=torch.from_numpy(to_pixels(list(frames)))).last_hidden_state.float()
    z = z.reshape(len(frames), -1)
    delta = z[1:] - z[:-1]
    d = delta.norm(dim=-1)
    med = float(d.median())
    zn = torch.nn.functional.normalize(z, dim=-1)
    frame_cos = float((zn[1:] * zn[:-1]).sum(-1).mean())
    if med <= 1e-6 or frame_cos >= VID_STATIC_COS:
        return None
    if float(d.max()) > VID_CUT_RATIO * med:
        return None
    cos = torch.nn.functional.cosine_similarity(delta[:-1], delta[1:], dim=-1).clamp(-1, 1)
    theta = torch.rad2deg(torch.acos(cos))
    return {"angle_mean": float(theta.mean()), "angle_std": float(theta.std()), "step_median": med,
            "frame_cos": round(frame_cos, 4)}


def video_windows(path: Path, n_windows: int = VID_WINDOWS, fps: int = VID_FPS, win_s: float = VID_WIN_S,
                  max_s: float = VID_MAX_S) -> tuple[list[tuple[float, np.ndarray]], float]:
    """Up to n_windows evenly spaced windows of win_s seconds at fps, decoded by ffmpeg to 224x224 RGB."""
    from hydra.media import _ffmpeg, probe

    info = probe(path)
    dur = float((info.get("format") or {}).get("duration") or 0.0)
    if dur < win_s + 0.5:
        return [], dur
    span = min(dur, max_s) - win_s
    starts = [span * (k + 0.5) / n_windows for k in range(n_windows)] if span > 0 else [0.0]
    out = []
    n = int(round(win_s * fps))
    for s in starts:
        proc = subprocess.run(
            [_ffmpeg(), "-v", "error", "-ss", f"{s:.3f}", "-i", str(path), "-t", f"{win_s:.3f}", "-an",
             "-vf", f"fps={fps},scale={SIDE}:{SIDE}:flags=bicubic", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            check=True, capture_output=True, timeout=120)
        buf = np.frombuffer(proc.stdout, np.uint8)
        k = len(buf) // (SIDE * SIDE * 3)
        if k >= 4:
            out.append((round(s, 2), buf[: k * SIDE * SIDE * 3].reshape(k, SIDE, SIDE, 3)[:n]))
    return out, dur


def band(score: float, t_medium: float, t_high: float) -> str:
    return "high" if score >= t_high else "medium" if score >= t_medium else "low"


# ------------------------------------------------------------------------------------------ AudioSeal


def audioseal_path(ctx: ExtractContext) -> Path | None:
    md = ctx.settings.models_dir
    if md is None:
        return None
    p = Path(md) / AS_DIR / AS_FILE
    if p.is_file():
        return p
    from hydra.netguard import model_download_allowed

    if model_download_allowed():  # HYDRA_MODEL_DOWNLOAD=1 and not HYDRA_OFFLINE=1 (kill switch, CR-ASTRA-SEC-8)
        return fetch_audioseal(Path(md))
    return None


def fetch_audioseal(models_dir: Path) -> Path:
    """Download the pinned upstream detector .pth (sha256-checked), convert it to safetensors with the allow-list
    unpickler, check the result's sha256, delete the .pth. Only called with HYDRA_MODEL_DOWNLOAD=1."""
    import urllib.request

    from hydra.netguard import require_online
    from hydra.vendor.audioseal import convert_checkpoint

    require_online("model download (AudioSeal detector)")

    dest = models_dir / AS_DIR / AS_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = dest.with_name("detector_base.pth.part")
    req = urllib.request.Request(AS_SRC_URL, headers={"User-Agent": "hydra-nexus-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 — pinned https URL, explicit opt-in
        data = r.read()
    if hashlib.sha256(data).hexdigest() != AS_SRC_SHA256:
        raise RuntimeError("AudioSeal detector_base.pth sha256 mismatch")
    src.write_bytes(data)
    try:
        convert_checkpoint(src, dest)
    finally:
        src.unlink(missing_ok=True)
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    if digest != AS_SHA256:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"converted AudioSeal detector sha256 mismatch: {digest}")
    return dest


def audioseal_score(detector: Any, samples: np.ndarray, sr: int = 16000, chunk_s: float = AS_CHUNK_S,
                    max_s: float = AS_MAX_S) -> float:
    """Fraction of 16 kHz samples with p(marked) > 0.5 over the first max_s seconds (upstream decision rule)."""
    return audioseal_profile(detector, samples, sr, chunk_s, max_s)["fraction"]


def _marked_mask(detector: Any, samples: np.ndarray, sr: int, chunk_s: float, max_s: float) -> np.ndarray:
    """Per-sample p(marked) > 0.5 over the first max_s seconds; chunks shorter than 0.1 s are dropped (as before)."""
    import torch

    x = np.asarray(samples, np.float32)[: int(max_s * sr)]
    parts, step = [], int(chunk_s * sr)
    with torch.inference_mode():
        for i in range(0, len(x), step):
            c = torch.from_numpy(np.ascontiguousarray(x[i : i + step]))[None, None]
            if c.shape[-1] < 1600:
                continue
            probs, _ = detector(c)
            parts.append((probs[0, 1] > 0.5).numpy())
    return np.concatenate(parts) if parts else np.zeros(0, bool)


def marked_intervals(mask: np.ndarray, sr: int = 16000, win_s: float = AS_WIN_S, win_frac: float = AS_WIN_FRAC,
                     min_windows: int = AS_MIN_WINDOWS) -> list[tuple[float, float]]:
    """Runs of >= min_windows consecutive windows (win_s each, full windows only) whose marked-sample share exceeds
    win_frac, as (start_s, end_s). Local reporting for CR-ASTRA-RES-3; the clip-level decision stays global."""
    w = int(win_s * sr)
    n = len(mask) // w if w else 0
    flags = [float(mask[k * w : (k + 1) * w].mean()) > win_frac for k in range(n)]
    out, k = [], 0
    while k < n:
        if not flags[k]:
            k += 1
            continue
        j = k
        while j < n and flags[j]:
            j += 1
        if j - k >= min_windows:
            out.append((round(k * win_s, 2), round(j * win_s, 2)))
        k = j
    return out


def audioseal_profile(detector: Any, samples: np.ndarray, sr: int = 16000, chunk_s: float = AS_CHUNK_S,
                      max_s: float = AS_MAX_S) -> dict[str, Any]:
    """Checked duration, global marked fraction (headline, AS_THRESHOLD) and supported marked intervals, separately."""
    mask = _marked_mask(detector, samples, sr, chunk_s, max_s)
    iv = marked_intervals(mask, sr)
    checked = len(mask) / sr
    marked_s = sum(b - a for a, b in iv)
    return {"checked_s": round(checked, 2), "fraction": float(mask.mean()) if len(mask) else 0.0,
            "intervals": iv, "marked_share": round(marked_s / checked, 4) if checked else 0.0}


# ------------------------------------------------------------------------------------------ genes


def _gene(inc_id: str, family: Family, kind: str, group: str, status: GeneStatus, note: str,
          art: str | None = None, value: str | None = None, quality: float = 1.0,
          timecode: float | None = None) -> Gene:
    return Gene(incident_id=inc_id, family=family, kind=kind, value=value, quality=round(quality, 3), status=status,
                provenance=Provenance.INF, extractor=f"{kind_extractor(kind)}@{VERSION}", dependency_group=group,
                evidence=EvidencePointer(artifact_id=art, timecode_s=timecode, note=note[:300]))


def kind_extractor(kind: str) -> str:
    return {"synthetic_image_suspicion": "dfk_image", "synthetic_image_check": "dfk_image",
            "synthetic_video_suspicion": "dfk_video", "synthetic_video_check": "dfk_video",
            "wm_audio": "dfk_audio_wm", "wm_audio_local": "dfk_audio_wm", "wm_audio_check": "dfk_audio_wm"}[kind]


def _images(inc: ResolvedIncident) -> list[ResolvedArtifact]:
    out, seen = [], set()
    for a in inc.artifacts:
        mime = (a.meta.mime or "").lower()
        if a.meta.artifact_id in seen or a.meta.role not in IMG_ROLES or not mime.startswith("image/"):
            continue
        if mime.startswith(("image/svg", "image/gif", "image/x-icon", "image/vnd.microsoft.icon")):
            continue
        seen.add(a.meta.artifact_id)
        out.append(a)
    return out[:IMG_MAX]


def image_enabled(ctx: ExtractContext) -> bool:
    """Off by default (docs/genes/DFK.md section 4: AUROC 0.50 on laundered Community Forensics Eval, and the "high"
    flag fired on more real than generated images there). Opt in with HYDRA_DFK_IMAGE=1 or option dfk_image."""
    return bool(ctx.options.get("dfk_image")) or os.environ.get("HYDRA_DFK_IMAGE") == "1"


@register("dfk_image", family=Family.VIS, version=VERSION)
def dfk_image(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    from PIL import Image

    from hydra.ml.models import ModelUnavailable

    iid = inc.incident.id
    if not image_enabled(ctx):
        return ExtractResult([_gene(iid, Family.VIS, "synthetic_image_check", GROUP_IMAGE, GeneStatus.UNAVAILABLE,
                                    "wyłączone domyślnie: na obrazach po rekompresji pomiar był bliski losowemu "
                                    "(docs/genes/DFK.md); włączenie HYDRA_DFK_IMAGE=1")])
    files = _images(inc)
    if not files:
        return ExtractResult([_gene(iid, Family.VIS, "synthetic_image_check", GROUP_IMAGE, GeneStatus.NOT_APPLICABLE,
                                    "brak obrazów do sprawdzenia")])
    try:
        model = _dino(ctx)
    except (ModelUnavailable, OSError) as exc:
        return ExtractResult([_gene(iid, Family.VIS, "synthetic_image_check", GROUP_IMAGE, GeneStatus.UNAVAILABLE,
                                    f"{DINO_KEY} unavailable: {exc}")])
    arrays, kept = [], []
    for a in files:
        try:
            with Image.open(a.path) as im:
                if min(im.size) < IMG_MIN_SIDE:
                    continue
                im.draft("RGB", (2 * SIDE, 2 * SIDE))
                arrays.append(np.asarray(im.convert("RGB")))
                kept.append(a)
        except Exception as exc:  # noqa: BLE001 — unreadable image: skipped, never a penalty
            log.debug("dfk_image skip %s: %s", a.meta.path_in_incident, exc)
    if not kept:
        return ExtractResult([_gene(iid, Family.VIS, "synthetic_image_check", GROUP_IMAGE, GeneStatus.NOT_APPLICABLE,
                                    f"brak obrazów >= {IMG_MIN_SIDE} px")])
    scores = instability_scores(model, to_pixels(arrays))
    genes = []
    for a, s in zip(kept, scores, strict=True):
        lvl = band(float(s), IMG_T_MEDIUM, IMG_T_HIGH)
        genes.append(_gene(iid, Family.VIS, "synthetic_image_suspicion", GROUP_IMAGE, GeneStatus.OBSERVED,
                           f"{WORDING} (obraz generowany?): {LEVEL_PL[lvl]}; niestabilność reprezentacji "
                           f"{s:.4f} (progi {IMG_T_MEDIUM:.4f} / {IMG_T_HIGH:.4f}); {CAVEAT}",
                           art=a.meta.artifact_id, value=lvl, quality=IMG_QUALITY))
    n_high = sum(g.value == "high" for g in genes)
    genes.append(_gene(iid, Family.VIS, "synthetic_image_check", GROUP_IMAGE, GeneStatus.OBSERVED,
                       f"sprawdzono {len(kept)} obrazów, podwyższony sygnał: {n_high}",
                       value="flagged" if n_high else "not_flagged"))
    return ExtractResult(genes)


def video_enabled(ctx: ExtractContext) -> bool:
    """Off by default (docs/genes/DFK.md section 4.2: AUROC 0.65 on the permissive calibration set but 0.31-0.40,
    i.e. inverted, on GenVideo; the direction depends on the real set). Opt in: HYDRA_DFK_VIDEO=1 or option."""
    return bool(ctx.options.get("dfk_video")) or os.environ.get("HYDRA_DFK_VIDEO") == "1"


@register("dfk_video", family=Family.VIS, version=VERSION)
def dfk_video(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    from hydra.genome.vis_media import VIDEO_ROLES, is_video
    from hydra.ml.models import ModelUnavailable

    iid = inc.incident.id
    if not video_enabled(ctx):
        return ExtractResult([_gene(iid, Family.VIS, "synthetic_video_check", GROUP_VIDEO, GeneStatus.UNAVAILABLE,
                                    "wyłączone domyślnie: kierunek sygnału zależy od zbioru (na jednym zbiorze "
                                    "odwrócony, docs/genes/DFK.md); włączenie HYDRA_DFK_VIDEO=1")])
    videos = list({a.meta.artifact_id: a for a in inc.artifacts if is_video(a) and a.meta.role in VIDEO_ROLES}
                  .values())
    if not videos:
        return ExtractResult([_gene(iid, Family.VIS, "synthetic_video_check", GROUP_VIDEO, GeneStatus.NOT_APPLICABLE,
                                    "brak nagrań wideo")])
    try:
        model = _dino(ctx)
    except (ModelUnavailable, OSError) as exc:
        return ExtractResult([_gene(iid, Family.VIS, "synthetic_video_check", GROUP_VIDEO, GeneStatus.UNAVAILABLE,
                                    f"{DINO_KEY} unavailable: {exc}")])
    genes: list[Gene] = []
    for a in videos:
        aid = a.meta.artifact_id
        try:
            wins, _dur = video_windows(a.path)
            stats = [(s, st) for s, fr in wins if (st := trajectory_stats(model, fr)) is not None]
        except Exception as exc:  # noqa: BLE001 — one bad file never stops the incident
            log.warning("dfk_video failed for %s: %s", a.meta.path_in_incident, exc)
            genes.append(_gene(iid, Family.VIS, "synthetic_video_suspicion", GROUP_VIDEO, GeneStatus.FAILED,
                               f"{type(exc).__name__}: {exc}", art=aid))
            continue
        if not stats:
            genes.append(_gene(iid, Family.VIS, "synthetic_video_suspicion", GROUP_VIDEO, GeneStatus.NOT_APPLICABLE,
                               f"brak okien bez cięć i ruchu ({len(wins)} okien po {VID_WIN_S:.0f} s)", art=aid))
            continue
        angle = float(np.median([st["angle_mean"] for _s, st in stats]))
        lvl = band(angle, VID_T_MEDIUM, VID_T_HIGH)
        start = stats[int(np.argmax([st["angle_mean"] for _s, st in stats]))][0]
        genes.append(_gene(iid, Family.VIS, "synthetic_video_suspicion", GROUP_VIDEO, GeneStatus.OBSERVED,
                           f"{WORDING} (wideo generowane?): {LEVEL_PL[lvl]}; krzywizna trajektorii {angle:.1f}° "
                           f"(mediana {len(stats)} okien, progi {VID_T_MEDIUM:.1f}° / {VID_T_HIGH:.1f}°); {CAVEAT}",
                           art=aid, value=lvl, quality=VID_QUALITY * len(stats) / VID_WINDOWS, timecode=start))
    obs = [g for g in genes if g.status == GeneStatus.OBSERVED]
    n_high = sum(g.value == "high" for g in obs)
    if obs:
        genes.append(_gene(iid, Family.VIS, "synthetic_video_check", GROUP_VIDEO, GeneStatus.OBSERVED,
                           f"sprawdzono {len(obs)} nagrań, podwyższony sygnał: {n_high}",
                           value="flagged" if n_high else "not_flagged"))
    else:
        genes.append(_gene(iid, Family.VIS, "synthetic_video_check", GROUP_VIDEO, GeneStatus.NOT_APPLICABLE,
                           "żadne nagranie nie dało okien do pomiaru"))
    return ExtractResult(genes)


def _audioseal(ctx: ExtractContext):
    key = "dfk_audioseal"
    if key not in ctx.models:
        p = audioseal_path(ctx)
        if p is None:
            return None
        from hydra.vendor.audioseal import load_detector

        ctx.models[key] = load_detector(p)
    return ctx.models[key]


@register("dfk_audio_wm", family=Family.META, version=VERSION)
def dfk_audio_wm(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    from hydra.genome.vid_avsync import read_wav
    from hydra.media import extract_audio_wav16k

    iid = inc.incident.id
    media = list({a.meta.artifact_id: a for a in inc.artifacts
                  if (a.meta.mime or "").lower().startswith(("video/", "audio/"))}.values())
    if not media:
        return ExtractResult([_gene(iid, Family.META, "wm_audio_check", GROUP_WM, GeneStatus.NOT_APPLICABLE,
                                    "brak nagrań z dźwiękiem")])
    try:
        det = _audioseal(ctx)
    except Exception as exc:  # noqa: BLE001 — fetch/convert failure: unknown, not a penalty
        return ExtractResult([_gene(iid, Family.META, "wm_audio_check", GROUP_WM, GeneStatus.UNAVAILABLE,
                                    f"AudioSeal: {type(exc).__name__}: {exc}")])
    if det is None:
        return ExtractResult([_gene(iid, Family.META, "wm_audio_check", GROUP_WM, GeneStatus.UNAVAILABLE,
                                    f"AudioSeal {AS_FILE} missing in $HYDRA_MODELS_DIR/{AS_DIR} "
                                    "(fetch with HYDRA_MODEL_DOWNLOAD=1 or scripts/fetch_models.py)")])
    genes: list[Gene] = []
    checked, scores = 0, {}
    for a in media:
        aid = a.meta.artifact_id
        try:
            wav = extract_audio_wav16k(a.path, ctx.work_dir / "dfk_audio" / aid[:16] / "audio.wav")
            if wav is None:
                continue
            samples, sr = read_wav(wav)
            if sr != 16000 or len(samples) < sr:
                continue
            prof = audioseal_profile(det, samples, sr)
        except Exception as exc:  # noqa: BLE001
            log.warning("dfk_audio_wm failed for %s: %s", a.meta.path_in_incident, exc)
            continue
        checked += 1
        s = prof["fraction"]
        scores[aid] = {"f": round(s, 4), "s": prof["checked_s"], "share": prof["marked_share"],
                       "iv": prof["intervals"][:5]}
        if s > AS_THRESHOLD:  # headline: clip-majority rule, unchanged
            genes.append(_gene(iid, Family.META, "wm_audio", GROUP_WM, GeneStatus.OBSERVED,
                               f"wykryto znak AudioSeal w ścieżce dźwiękowej {a.meta.path_in_incident}: {100 * s:.0f}% "
                               f"próbek z {prof['checked_s']:.0f} s powyżej progu; narzędzie osadzające AudioSeal "
                               "przetworzyło dźwięk (znak można osadzić też w prawdziwym głosie; to fakt o pliku, nie "
                               "dowód generowania ani oszustwa)", art=aid, value="audioseal"))
        elif prof["intervals"]:  # local mark below the global threshold: reported, not the headline decision
            iv = ", ".join(f"{a0:.0f}–{b0:.0f} s" for a0, b0 in prof["intervals"][:5])
            genes.append(_gene(iid, Family.META, "wm_audio_local", GROUP_WM, GeneStatus.OBSERVED,
                               f"znak AudioSeal tylko we fragmentach {a.meta.path_in_incident}: {iv} "
                               f"({100 * prof['marked_share']:.0f}% z {prof['checked_s']:.0f} s); poniżej progu "
                               f"całego nagrania ({100 * AS_THRESHOLD:.0f}%); fakt o pliku, nie dowód generowania",
                               art=aid, value="audioseal_partial", quality=0.5,
                               timecode=float(prof["intervals"][0][0])))
    if not checked:
        genes.append(_gene(iid, Family.META, "wm_audio_check", GROUP_WM, GeneStatus.NOT_APPLICABLE,
                           "brak ścieżek dźwiękowych >= 1 s"))
        return ExtractResult(genes)
    n_hit = sum(g.kind == "wm_audio" for g in genes)
    n_local = sum(g.kind == "wm_audio_local" for g in genes)
    note = json.dumps({k[:12]: v for k, v in scores.items()})
    genes.append(_gene(iid, Family.META, "wm_audio_check", GROUP_WM, GeneStatus.OBSERVED,
                       f"sprawdzono {checked} ścieżek, znak AudioSeal w całym nagraniu: {n_hit}, tylko we "
                       f"fragmentach: {n_local}; brak znaku nie świadczy o autentyczności; wyniki {note}"[:300],
                       value="detected" if n_hit else "not_detected"))
    return ExtractResult(genes)
