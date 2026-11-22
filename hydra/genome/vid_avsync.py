"""AV-sync suspicion: does mouth movement follow the speech envelope? (weights-free heuristic, our own code)

Per video artifact with an audio stream and a visible face:
1. frames at AV_FPS (10 fps) over the first AV_MAX_S seconds; MediaPipe Face Landmarker (Apache-2.0 code and
   model) gives the inner-lip gap (landmarks 13/14) normalised by face height (10/152) = mouth opening series;
2. audio: mono 16 kHz WAV, RMS per video frame = speech envelope;
3. gate = frames with a face AND audio RMS above 20% of its 95th percentile; both series high-passed (minus a 1 s
   moving average computed over gated frames only);
4. score = maximum Pearson correlation within +-0.5 s over pairs (mouth[i], audio[i+lag]) whose frames both pass the
   gate, and its lag. Samples outside the gate cannot change the score (CR-ASTRA-RES-2, tested).
Bands low / medium / high are UNCALIBRATED: thresholds were re-checked only on synthetic series
(scripts/measure_dfk_semantics.py, docs/genes/DFK.md section 7), never on labelled real media.

Output: one gene `av_sync_suspicion` (VIS, dependency group `VIS:label:av_sync` = label tier, 0 bits, never a
link rule) with value low / medium / high, quality from face coverage and speech duration, and a Polish note.
The wording is always "podejrzenie niespójności ruchu ust i dźwięku": voice-over, dubbing, B-roll and music beds
give the same signal; thresholds are not calibrated on labelled data. It is NOT a deepfake detector.

The landmarker model is a separate file ($HYDRA_MODELS_DIR/mediapipe-face-landmarker/face_landmarker.task); when
it is missing the gene is `unavailable`. It is fetched only with HYDRA_MODEL_DOWNLOAD=1 (`fetch_landmarker`).
"""

from __future__ import annotations

import hashlib
import logging
import wave
from pathlib import Path

import numpy as np

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register

log = logging.getLogger(__name__)

EXTRACTOR = "vid_avsync@1"
KIND = "av_sync_suspicion"
GROUP = "VIS:label:av_sync"
AV_FPS = 10
AV_MAX_S = 60.0
MAX_LAG_S = 0.5
MIN_FACE_FRAC = 0.5     # face in >= 50% of the frames
MIN_SPEECH_S = 3.0      # audio above the speech gate for >= 3 s (with a face)
R_LOW = 0.30            # r >= R_LOW near lag 0: mouth follows the audio (low suspicion)
R_HIGH = 0.10           # r < R_HIGH: no relation (high suspicion)
LAG_OK_S = 0.25

LANDMARKER_DIR = "mediapipe-face-landmarker"
LANDMARKER_FILE = "face_landmarker.task"
LANDMARKER_URL = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/"
                  "face_landmarker.task")
LANDMARKER_SHA256 = "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff"
WORDING = "podejrzenie niespójności ruchu ust i dźwięku"
LEVEL_PL = {"low": "niskie", "medium": "średnie", "high": "wysokie"}


# ------------------------------------------------------------------------------------------ signal


def rms_envelope(samples: np.ndarray, sr: int, fps: float, n_frames: int) -> np.ndarray:
    """RMS of the audio per video frame (window = 1/fps)."""
    hop = int(round(sr / fps))
    x = samples.astype(np.float32)
    out = np.zeros(n_frames, np.float32)
    for k in range(n_frames):
        w = x[k * hop : (k + 1) * hop]
        out[k] = float(np.sqrt(np.mean(w * w))) if len(w) else 0.0
    return out


def _highpass_masked(x: np.ndarray, mask: np.ndarray, win: int) -> np.ndarray:
    """x minus its moving average over `win` frames, where the average uses only frames in `mask`.

    Frames outside the mask get 0 and never influence any other frame (CR-ASTRA-RES-2)."""
    w = mask.astype(np.float64)
    v = np.where(mask, np.nan_to_num(x, nan=0.0), 0.0)
    k = np.ones(win)
    lo, hi = win // 2, win - 1 - win // 2
    s = np.convolve(np.pad(v, (lo, hi)), k, mode="valid")
    c = np.convolve(np.pad(w, (lo, hi)), k, mode="valid")
    return np.where(mask & (c > 0), v - s / np.maximum(c, 1.0), 0.0)


def av_sync_score(mouth: np.ndarray, env: np.ndarray, fps: float = AV_FPS, max_lag_s: float = MAX_LAG_S
                  ) -> dict:
    """Max normalised cross-correlation of mouth opening vs audio envelope within +-max_lag_s.

    `mouth`: NaN where no face. Returns r, lag_s (positive = audio after mouth), face_frac, speech_s."""
    n = min(len(mouth), len(env))
    mouth, env = np.asarray(mouth[:n], np.float64), np.asarray(env[:n], np.float64)
    face = ~np.isnan(mouth)
    out = {"r": None, "lag_s": None, "face_frac": round(float(face.mean()) if n else 0.0, 3), "speech_s": 0.0,
           "frames": int(n)}
    if n < fps * 2 or face.sum() < fps * 2:
        return out
    gate = max(1e-6, 0.2 * float(np.percentile(env, 95)))
    speech = env > gate
    valid = speech & face  # the documented mask: frames with a face AND audio above the speech gate
    out["speech_s"] = round(float(valid.sum()) / fps, 2)
    win = max(3, int(fps))
    # high-pass over gated frames only, so samples outside the gate never leak into the correlated values
    hm, he = _highpass_masked(mouth, valid, win), _highpass_masked(env, valid, win)
    best_r, best_lag = -2.0, 0
    lag_max = int(round(max_lag_s * fps))
    for lag in range(-lag_max, lag_max + 1):
        # pair mouth[i] with audio[i + lag]; both frames must pass the gate under this shift
        if lag >= 0:
            a, b, v = hm[: n - lag], he[lag:], valid[: n - lag] & valid[lag:]
        else:
            a, b, v = hm[-lag:], he[: n + lag], valid[-lag:] & valid[: n + lag]
        a, b = a[v], b[v]
        if len(a) < fps * 2 or a.std() < 1e-9 or b.std() < 1e-9:
            continue
        r = float(np.corrcoef(a, b)[0, 1])
        if r > best_r:
            best_r, best_lag = r, lag
    if best_r > -2.0:
        out["r"], out["lag_s"] = round(best_r, 3), round(best_lag / fps, 2)
    return out


def suspicion_level(s: dict) -> str | None:
    """low / medium / high, or None when the evidence is insufficient (no face, too little speech)."""
    if s.get("r") is None or s["face_frac"] < MIN_FACE_FRAC or s["speech_s"] < MIN_SPEECH_S:
        return None
    r, lag = s["r"], abs(s["lag_s"] or 0.0)
    if r >= R_LOW and lag <= LAG_OK_S:
        return "low"
    if r < R_HIGH:
        return "high"
    return "medium"


def quality_of(s: dict) -> float:
    return round(min(1.0, s["face_frac"]) * min(1.0, s["speech_s"] / 10.0), 3)


# ------------------------------------------------------------------------------------------ media


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr, n, ch = w.getframerate(), w.getnframes(), w.getnchannels()
        raw = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        raw = raw.reshape(-1, ch).mean(axis=1)
    return raw, sr


def landmarker_path(ctx: ExtractContext) -> Path | None:
    md = ctx.settings.models_dir
    if md is None:
        return None
    p = Path(md) / LANDMARKER_DIR / LANDMARKER_FILE
    if p.is_file():
        return p
    from hydra.netguard import model_download_allowed

    if model_download_allowed(ctx.settings):  # HYDRA_MODEL_DOWNLOAD=1 and not HYDRA_OFFLINE=1
        return fetch_landmarker(Path(md))
    return None


def fetch_landmarker(models_dir: Path) -> Path:
    """Download the pinned Face Landmarker bundle (only called with HYDRA_MODEL_DOWNLOAD=1); sha256-verified."""
    import urllib.request

    from hydra.netguard import require_online

    require_online("model download (Face Landmarker)")
    dest = models_dir / LANDMARKER_DIR / LANDMARKER_FILE
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(LANDMARKER_URL, headers={"User-Agent": "hydra-nexus-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — pinned https URL, explicit opt-in
        data = r.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != LANDMARKER_SHA256:
        raise RuntimeError(f"face_landmarker.task sha256 mismatch: {digest}")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


class MouthTracker:
    """MediaPipe Face Landmarker (VIDEO mode) → mouth opening per frame (NaN without a face)."""

    def __init__(self, model: Path):
        from mediapipe.tasks.python import BaseOptions, vision

        opts = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(model)),
                                            running_mode=vision.RunningMode.VIDEO, num_faces=1)
        self._lm = vision.FaceLandmarker.create_from_options(opts)

    def series(self, frames: list[np.ndarray], fps: float) -> np.ndarray:
        import mediapipe as mp

        out = np.full(len(frames), np.nan)
        for k, rgb in enumerate(frames):
            img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
            res = self._lm.detect_for_video(img, int(k * 1000 / fps))
            if not res.face_landmarks:
                continue
            p = res.face_landmarks[0]
            face_h = abs(p[152].y - p[10].y)
            if face_h > 1e-3:
                out[k] = abs(p[14].y - p[13].y) / face_h
        return out

    def close(self) -> None:
        self._lm.close()


def read_frames(path: Path, out_dir: Path, fps: float = AV_FPS, max_s: float = AV_MAX_S,
                max_side: int = 480) -> list[np.ndarray]:
    from PIL import Image

    from hydra.media import extract_frames

    jpgs = extract_frames(path, out_dir, every_s=1.0 / fps, max_side=max_side, max_frames=int(max_s * fps))
    out = []
    for p in jpgs:
        with Image.open(p) as im:
            out.append(np.asarray(im.convert("RGB")))
    return out


# ------------------------------------------------------------------------------------------ extractor


def _gene(inc_id: str, status: GeneStatus, art: str | None, note: str, value: str | None = None,
          quality: float = 1.0) -> Gene:
    return Gene(incident_id=inc_id, family=Family.VIS, kind=KIND, value=value, quality=quality, status=status,
                provenance=Provenance.INF, extractor=EXTRACTOR, dependency_group=GROUP,
                evidence=EvidencePointer(artifact_id=art, timecode_s=0.0 if value else None, note=note[:300]))


def describe(level: str, s: dict) -> str:
    return (f"{WORDING}: {LEVEL_PL[level]} (r={s['r']:.2f}, przesunięcie {s['lag_s']:+.2f} s, twarz w"
            f" {100 * s['face_frac']:.0f}% klatek, mowa {s['speech_s']:.0f} s); heurystyka bez kalibracji,"
            f" lektor, dubbing i przebitki dają ten sam sygnał; nie jest dowodem powiązania")


@register("vid_avsync", family=Family.VIS, version=1)
def vid_avsync(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    from hydra.genome.vis_media import VIDEO_ROLES, is_video
    from hydra.media import extract_audio_wav16k

    inc_id = inc.incident.id
    videos = [a for a in inc.artifacts if is_video(a) and a.meta.role in VIDEO_ROLES]
    if not videos:
        return ExtractResult([_gene(inc_id, GeneStatus.NOT_APPLICABLE, None, "no video artifacts")])
    model = None
    try:
        model = landmarker_path(ctx)
    except Exception as exc:  # noqa: BLE001 — download failure: unknown, not a penalty
        return ExtractResult([_gene(inc_id, GeneStatus.UNAVAILABLE, None, f"landmarker fetch: {exc}")])
    if model is None:
        return ExtractResult([_gene(inc_id, GeneStatus.UNAVAILABLE, None,
                                    f"MediaPipe {LANDMARKER_FILE} missing in $HYDRA_MODELS_DIR/{LANDMARKER_DIR}"
                                    " (fetch with HYDRA_MODEL_DOWNLOAD=1)")])
    genes: list[Gene] = []
    seen: set[str] = set()
    for a in videos:
        aid = a.meta.artifact_id
        if aid in seen:
            continue
        seen.add(aid)
        try:
            wav = extract_audio_wav16k(a.path, ctx.work_dir / "avsync" / aid[:16] / "audio.wav")
            if wav is None:
                genes.append(_gene(inc_id, GeneStatus.NOT_APPLICABLE, aid, "no audio stream"))
                continue
            frames = read_frames(a.path, ctx.work_dir / "avsync" / aid[:16] / "frames")
            samples, sr = read_wav(wav)
            tracker = MouthTracker(model)
            try:
                mouth = tracker.series(frames, AV_FPS)
            finally:
                tracker.close()
            s = av_sync_score(mouth, rms_envelope(samples, sr, AV_FPS, len(frames)))
            lvl = suspicion_level(s)
            if lvl is None:
                genes.append(_gene(inc_id, GeneStatus.NOT_APPLICABLE, aid,
                                   f"insufficient: face {100 * s['face_frac']:.0f}% of frames, speech with face"
                                   f" {s['speech_s']} s, r={s['r']}"))
                continue
            genes.append(_gene(inc_id, GeneStatus.OBSERVED, aid, describe(lvl, s), value=lvl, quality=quality_of(s)))
        except Exception as exc:  # noqa: BLE001 — one bad file never stops the incident
            log.warning("av-sync failed for %s: %s", a.meta.path_in_incident, exc)
            genes.append(_gene(inc_id, GeneStatus.FAILED, aid, f"{type(exc).__name__}: {exc}"))
    return ExtractResult(genes)
