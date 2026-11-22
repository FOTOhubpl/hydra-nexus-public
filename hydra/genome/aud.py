"""Audio genes: speech transcripts (ASR) and audio-track reuse fingerprints. Signals of reused voice-over or
soundtrack ("sygnały ponownego użycia głosu/ścieżki"); this module does NOT detect deepfakes or synthetic speech.

1. ASR (`transcribe`): faster-whisper (MIT code, MIT weights; training data not public) with language forced to
   Polish and the bundled Silero VAD. Weights load only from `$HYDRA_MODELS_DIR/faster-whisper-<size>`; when they
   are missing they are downloaded only with `HYDRA_MODEL_DOWNLOAD=1`, otherwise ASR is `unavailable` (unknown,
   never a penalty). Transcripts are cached per artifact sha256 under `<cache_dir>/asr/`. `asr_blocks` turns
   them into `TextBlock(channel="ad", source="asr")` so every TXT gene (MinHash, phrases, entities, phones, URLs)
   also runs on what is said in the video (see `txt_sources._collect`).
2. Audio fingerprint (`fingerprint`): own implementation of spectral landmark hashing (peak pairs in a log
   spectrogram, Wang 2003 idea; numpy/scipy only). Each artifact with an audio stream gets one MinHash signature
   over its landmark-hash set (`audio_fp_sig`) and LSH band values (`audio_fp_band`, candidate recall only). Both
   carry a boilerplate tier, so they never score as plain discrete matches; `synapse._audio_reuse` compares the
   signatures (containment of the landmark sets) and decides the level with rarity: a track held by more than
   `STOCK_AUDIO_MAX_DF` incidents is stock music / platform jingle (0 bits), a rare track with speech is a reused
   voice-over (campaign level, capped, never a link on its own), a rare track without detected speech is kit level.

Speaker embeddings (SpeechBrain ECAPA) are not shipped: see docs/genes/AUD.md for the licence decision.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from hydra.contracts import ArtifactRole, EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedArtifact, ResolvedIncident, register

log = logging.getLogger(__name__)

VERSION = 1
FP_VERSION = 1
SR = 16000
N_FFT = 1024                 # 64 ms window
HOP = 512                    # 32 ms hop
MAX_BIN = 256                # 0-4 kHz: speech/music core, survives low-bitrate codecs
PEAK_NEIGH = (21, 9)         # local-max neighbourhood (freq bins, frames)
PEAKS_PER_S = 30             # density cap (strongest peaks per second)
FAN_OUT = 8                  # target peaks paired with each anchor (next peaks in time)
MAX_DT = 63                  # frames (~2 s) between anchor and target
F_STEP, DT_STEP = 2, 2       # hash quantisation (measured on synthetic crops: containment 0.18 -> 0.24)
MIN_DB_OVER_MEDIAN = 10.0    # peaks must stand 10 dB over the clip's median level
MINHASH_PERM = 256
LSH_BANDS, LSH_ROWS = 128, 2  # candidate recall at Jaccard 0.1: 1-(1-0.01)^128 = 0.72; at 0.2: 0.99
MIN_HASHES = 150             # fewer unique landmark hashes: too little audio to fingerprint
MAX_AUDIO_S = 900.0          # 15 min per artifact
STOCK_AUDIO_MAX_DF = 2       # a track held by more incidents is stock music / template (used by synapse)
ASR_MODEL = "small"
ASR_MAX_NO_SPEECH = 0.6
ASR_MAX_COMPRESSION = 2.4    # Whisper repetition loops (hallucination on music/silence)
ASR_MIN_LOGPROB = -1.0

_PRIME = (1 << 61) - 1
_rng = np.random.default_rng(20261121)
_MH_A = _rng.integers(1, _PRIME, MINHASH_PERM, dtype=np.uint64)
_MH_B = _rng.integers(0, _PRIME, MINHASH_PERM, dtype=np.uint64)

AUDIO_ROLES = (ArtifactRole.AD_VIDEO, ArtifactRole.SCREEN_RECORDING, ArtifactRole.LANDING_ASSET,
               ArtifactRole.OTHER)
_AV_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".ts", ".avi", ".3gp", ".mp3", ".m4a", ".aac", ".wav", ".ogg",
           ".opus", ".flac"}


# ------------------------------------------------------------------------------------------ audio input


def is_audio_source(a: ResolvedArtifact) -> bool:
    if a.meta.role not in AUDIO_ROLES:
        return False
    mime = (a.meta.mime or "").lower()
    if mime.startswith(("video/", "audio/")):
        return not mime.endswith("mpegurl")
    if mime.startswith(("text/", "image/", "application/json")):
        return False
    return Path(a.meta.path_in_incident).suffix.lower() in _AV_EXT


def _cache_root(ctx: ExtractContext, sub: str) -> Path:
    root = ctx.settings.cache_dir or ctx.work_dir
    d = Path(root) / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_audio(a: ResolvedArtifact, ctx: ExtractContext) -> np.ndarray | None:
    """Mono 16 kHz float32 in [-1, 1] (first MAX_AUDIO_S), or None when the file has no audio stream.
    Raises RuntimeError when ffmpeg/ffprobe are missing (-> gene status unavailable)."""
    from scipy.io import wavfile

    from hydra.media import extract_audio_wav16k

    wav = ctx.work_dir / "audio" / f"{a.meta.artifact_id[:16]}.wav"
    if not wav.exists():
        if extract_audio_wav16k(a.path, wav) is None:
            return None
    sr, raw = wavfile.read(wav)
    x = raw.astype(np.float32)
    if np.issubdtype(raw.dtype, np.integer):
        x /= float(np.iinfo(raw.dtype).max) + 1.0
    if x.ndim > 1:
        x = x.mean(axis=1)
    if sr != SR:
        raise RuntimeError(f"unexpected sample rate {sr}")
    return x[: int(MAX_AUDIO_S * SR)]


# ------------------------------------------------------------------------------------------ fingerprint


@dataclass
class Fingerprint:
    hashes: np.ndarray     # uint32 landmark hashes (one per anchor-target pair)
    times: np.ndarray      # anchor frame index (HOP / SR seconds)
    duration_s: float
    n_peaks: int

    @property
    def unique(self) -> np.ndarray:
        return np.unique(self.hashes)


def spectral_peaks(x: np.ndarray, sr: int = SR) -> tuple[np.ndarray, np.ndarray]:
    """Constellation of local spectral maxima: (freq_bin, frame) arrays, sorted by frame then bin."""
    from scipy.ndimage import maximum_filter
    from scipy.signal import stft

    if x.size < N_FFT * 4:
        return np.zeros(0, np.int32), np.zeros(0, np.int32)
    _, _, z = stft(x, fs=sr, nperseg=N_FFT, noverlap=N_FFT - HOP, boundary=None, padded=False)
    s = 20.0 * np.log10(np.abs(z[:MAX_BIN]) + 1e-6)
    s[:2] = -120.0  # DC / rumble
    loc = maximum_filter(s, size=PEAK_NEIGH, mode="constant", cval=-200.0)
    floor = float(np.median(s)) + MIN_DB_OVER_MEDIAN
    f, t = np.nonzero((s == loc) & (s > floor) & (s > -80.0))
    if f.size == 0:
        return f.astype(np.int32), t.astype(np.int32)
    # density cap: strongest PEAKS_PER_S per second
    frames_per_s = sr / HOP
    sec = (t / frames_per_s).astype(np.int64)
    mag = s[f, t]
    order = np.lexsort((-mag, sec))
    sec_o = sec[order]
    first = np.searchsorted(sec_o, sec_o, side="left")
    rank = np.arange(order.size) - first
    keep = order[rank < PEAKS_PER_S]
    f, t = f[keep], t[keep]
    o = np.lexsort((f, t))
    return f[o].astype(np.int32), t[o].astype(np.int32)


def landmark_hashes(f: np.ndarray, t: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pair every peak with the next FAN_OUT peaks in time (1 <= dt <= MAX_DT frames).
    hash = f1/2 (7 bit) | f2/2 (7 bit) | dt/2 (5 bit). Time-shift invariant; the coarse frequency/time steps absorb
    the +-1 bin / frame jitter of a re-encoded copy or a crop that does not start on our frame grid."""
    hs, ts = [], []
    n = f.size
    for k in range(1, FAN_OUT + 1):
        if n <= k:
            break
        f1, f2 = f[:-k], f[k:]
        dt = t[k:] - t[:-k]
        ok = (dt >= 1) & (dt <= MAX_DT)
        q1 = (f1[ok] // F_STEP).astype(np.uint32) & 0x7F
        q2 = (f2[ok] // F_STEP).astype(np.uint32) & 0x7F
        h = (q1 << 12) | (q2 << 5) | ((dt[ok] // DT_STEP).astype(np.uint32) & 0x1F)
        hs.append(h)
        ts.append(t[:-k][ok])
    if not hs:
        return np.zeros(0, np.uint32), np.zeros(0, np.int32)
    return np.concatenate(hs), np.concatenate(ts).astype(np.int32)


def fingerprint(x: np.ndarray, sr: int = SR) -> Fingerprint:
    f, t = spectral_peaks(x, sr)
    h, ht = landmark_hashes(f, t)
    return Fingerprint(h, ht, round(x.size / sr, 2), int(f.size))


def minhash(values: np.ndarray) -> np.ndarray:
    """MINHASH_PERM-permutation MinHash of a uint32 set: h_i(x) = ((a_i x + b_i) mod 2^64) mod (2^61-1), low 32
    bits of the minimum. Chunked (bounded memory for 15 min of audio). Empty set -> all max."""
    if values.size == 0:
        return np.full(MINHASH_PERM, np.iinfo(np.uint32).max, dtype=np.uint32)
    best = np.full(MINHASH_PERM, np.iinfo(np.uint64).max, dtype=np.uint64)
    vals = values.astype(np.uint64)
    with np.errstate(over="ignore"):
        for s in range(0, vals.size, 4096):
            v = vals[None, s:s + 4096]
            hv = (_MH_A[:, None] * v + _MH_B[:, None]) % np.uint64(_PRIME)
            np.minimum(best, hv.min(axis=1), out=best)
    return (best & np.uint64(0xFFFFFFFF)).astype(np.uint32)


def lsh_bands(sig: np.ndarray) -> list[str]:
    out = []
    for b in range(LSH_BANDS):
        chunk = sig[b * LSH_ROWS:(b + 1) * LSH_ROWS].astype(">u4").tobytes()
        out.append(f"{b:02d}:{hashlib.sha1(chunk).hexdigest()[:12]}")
    return out


def jaccard(sa: np.ndarray, sb: np.ndarray) -> float:
    return float(np.mean(sa == sb)) if len(sa) == len(sb) and len(sa) else 0.0


def containment(j: float, na: int, nb: int) -> float:
    """|A ∩ B| / min(|A|, |B|) estimated from MinHash Jaccard and the set sizes (a cropped copy is contained)."""
    if j <= 0 or min(na, nb) <= 0:
        return 0.0
    inter = j * (na + nb) / (1.0 + j)
    return float(min(1.0, inter / min(na, nb)))


def parse_sig(value: str) -> tuple[int, np.ndarray] | None:
    try:
        n, hexsig = value.split(":", 1)
        return int(n), np.frombuffer(bytes.fromhex(hexsig), dtype=">u4").astype(np.uint32)
    except ValueError:
        return None


def cached_fingerprint(a: ResolvedArtifact, ctx: ExtractContext) -> tuple[np.ndarray, float, int] | None:
    """(unique landmark hashes, duration s, peaks) per artifact sha256, cached on disk. None = no audio stream."""
    p = _cache_root(ctx, "audio_fp") / f"{a.meta.artifact_id}.v{FP_VERSION}.npz"
    if p.exists():
        z = np.load(p)
        if int(z["no_audio"]):
            return None
        return z["hashes"].astype(np.uint32), float(z["duration"]), int(z["peaks"])
    x = load_audio(a, ctx)
    if x is None:
        np.savez(p, no_audio=1, hashes=np.zeros(0, np.uint32), duration=0.0, peaks=0)
        return None
    fp = fingerprint(x)
    u = fp.unique
    np.savez(p, no_audio=0, hashes=u, duration=fp.duration_s, peaks=fp.n_peaks)
    return u, fp.duration_s, fp.n_peaks


# ------------------------------------------------------------------------------------------ ASR


def _asr_model_dir(ctx: ExtractContext, size: str) -> Path | None:
    models = ctx.settings.models_dir
    if models is None:
        return None
    d = Path(models) / f"faster-whisper-{size}"
    if (d / "model.bin").exists():
        return d
    from hydra.netguard import model_download_allowed

    if model_download_allowed(ctx.settings):  # HYDRA_MODEL_DOWNLOAD=1 and not HYDRA_OFFLINE=1
        from faster_whisper import download_model

        d.mkdir(parents=True, exist_ok=True)
        download_model(size, output_dir=str(d))
        return d if (d / "model.bin").exists() else None
    return None


def _asr_model(ctx: ExtractContext):
    size = str(ctx.options.get("asr_model", ASR_MODEL))
    key = f"asr_whisper_{size}"
    if key in ctx.models:
        return ctx.models[key], size
    d = _asr_model_dir(ctx, size)
    model = None
    if d is not None:
        from faster_whisper import WhisperModel

        model = WhisperModel(str(d), device="cpu", compute_type="int8",
                             cpu_threads=int(ctx.options.get("asr_threads", 4)))
    ctx.models[key] = model
    return model, size


def transcribe(a: ResolvedArtifact, ctx: ExtractContext) -> dict | None:
    """{"model", "segments": [{start, end, text, avg_logprob, no_speech_prob}], "seconds"} or None when ASR is
    unavailable (no weights offline, disabled). {"segments": []} when the file has no audio stream."""
    if not ctx.options.get("asr", True):
        return None
    size = str(ctx.options.get("asr_model", ASR_MODEL))
    p = _cache_root(ctx, "asr") / f"{a.meta.artifact_id}.whisper-{size}.v{VERSION}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    model, size = _asr_model(ctx)
    if model is None:
        return None
    x = load_audio(a, ctx)
    out: dict = {"model": f"faster-whisper-{size}", "segments": [], "seconds": 0.0}
    if x is not None and x.size:
        t0 = time.perf_counter()
        segs, _info = model.transcribe(x, language="pl", vad_filter=True, beam_size=1,
                                       condition_on_previous_text=False)
        for s in segs:
            text = (s.text or "").strip()
            if not text or s.no_speech_prob > ASR_MAX_NO_SPEECH or s.compression_ratio > ASR_MAX_COMPRESSION \
                    or s.avg_logprob < ASR_MIN_LOGPROB:
                continue
            out["segments"].append({"start": round(float(s.start), 2), "end": round(float(s.end), 2), "text": text,
                                    "avg_logprob": round(float(s.avg_logprob), 3),
                                    "no_speech_prob": round(float(s.no_speech_prob), 3)})
        out["seconds"] = round(time.perf_counter() - t0, 2)
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def has_speech(tr: dict | None) -> bool | None:
    """True when ASR kept >= 5 words; None when ASR did not run (unknown)."""
    if tr is None:
        return None
    return sum(len(s["text"].split()) for s in tr.get("segments", [])) >= 5


def asr_blocks(inc: ResolvedIncident, ctx: ExtractContext, errors: list[tuple[str, str]] | None = None) -> list:
    """Transcripts as TXT blocks (channel 'ad', source 'asr'); evidence carries the timecode."""
    from hydra.genome.txt_norm import light
    from hydra.genome.txt_sources import Segment, TextBlock

    blocks = []
    for a in inc.artifacts:
        if not is_audio_source(a):
            continue
        try:
            tr = transcribe(a, ctx)
        except Exception as exc:  # noqa: BLE001 — one bad file never stops the incident
            if errors is not None:
                errors.append((a.meta.artifact_id, f"asr: {type(exc).__name__}: {exc}"[:300]))
            continue
        if not tr or not tr.get("segments"):
            continue
        segs = []
        for s in tr["segments"]:
            q = float(np.clip(np.exp(s["avg_logprob"]), 0.0, 1.0))
            segs.append(Segment(text=light(s["text"]), raw=s["text"], quality=round(q, 3),
                                evidence=EvidencePointer(artifact_id=a.meta.artifact_id, timecode_s=s["start"],
                                                         note=f"asr {tr['model']} {s['start']:.1f}-{s['end']:.1f} s"),
                                zone="ad", meta={"asr": True, "end_s": s["end"]}))
        blocks.append(TextBlock("ad", "asr", a.meta.artifact_id, segs, meta={"model": tr["model"]}))
    return blocks


# ------------------------------------------------------------------------------------------ extractors


def _gene(inc: ResolvedIncident, ex: str, family: Family, kind: str, value: str | None, ev: EvidencePointer,
          group: str | None = None, status: GeneStatus = GeneStatus.OBSERVED, quality: float = 1.0,
          prov: Provenance = Provenance.INF) -> Gene:
    return Gene(incident_id=inc.incident.id, family=family, kind=kind, value=value, status=status, provenance=prov,
                quality=round(max(0.0, min(1.0, quality)), 3), evidence=ev, extractor=f"{ex}@{VERSION}",
                dependency_group=group)


@register("aud_fp", family=Family.VIS, version=VERSION)
def aud_fp(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """One MinHash signature + LSH bands per artifact with an audio stream (see module docstring)."""
    ex = "aud_fp"
    genes: list[Gene] = []
    srcs = [a for a in inc.artifacts if is_audio_source(a)]
    if not srcs:
        return ExtractResult([_gene(inc, ex, Family.VIS, "audio_fp_sig", None,
                                    EvidencePointer(note="no audio/video artifact"),
                                    status=GeneStatus.NOT_APPLICABLE)])
    seen: set[str] = set()
    for a in srcs:
        aid = a.meta.artifact_id
        if aid in seen:
            continue
        seen.add(aid)
        try:
            fp = cached_fingerprint(a, ctx)
        except Exception as exc:  # noqa: BLE001
            genes.append(_gene(inc, ex, Family.VIS, "audio_fp_sig", None,
                               EvidencePointer(artifact_id=aid, note=f"{type(exc).__name__}: {exc}"[:300]),
                               status=GeneStatus.UNAVAILABLE))
            continue
        if fp is None:
            genes.append(_gene(inc, ex, Family.VIS, "audio_fp_sig", None,
                               EvidencePointer(artifact_id=aid, note="no audio stream"),
                               status=GeneStatus.NOT_APPLICABLE))
            continue
        u, dur, peaks = fp
        if u.size < MIN_HASHES:
            genes.append(_gene(inc, ex, Family.VIS, "audio_fp_sig", None,
                               EvidencePointer(artifact_id=aid, note=f"too little audio: {u.size} hashes, {dur} s"),
                               status=GeneStatus.NOT_APPLICABLE))
            continue
        sig = minhash(u)
        speech = None
        if ctx.options.get("aud_speech", True):
            try:
                speech = has_speech(transcribe(a, ctx))
            except Exception as exc:  # noqa: BLE001 — ASR failure leaves speech unknown, the fingerprint stays
                log.warning("asr failed for %s: %s", a.meta.path_in_incident, exc)
        sp = "unknown" if speech is None else ("1" if speech else "0")
        note = (f"hashes={u.size}; peaks={peaks}; duration={dur}s; speech={sp}; perm={MINHASH_PERM}; "
                f"file={Path(a.meta.path_in_incident).name}")[:300]
        ev = EvidencePointer(artifact_id=aid, timecode_s=0.0, note=note)
        genes.append(_gene(inc, ex, Family.VIS, "audio_fp_sig", f"{u.size}:" + sig.astype(">u4").tobytes().hex(),
                           ev, group="VIS:boilerplate:audio_fp"))
        for band in lsh_bands(sig):
            genes.append(_gene(inc, ex, Family.VIS, "audio_fp_band", band, ev, group="VIS:boilerplate:audio_fp"))
    return ExtractResult(genes)


@register("aud_asr", family=Family.TXT, version=VERSION)
def aud_asr(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    """ASR status per audio artifact (the transcript text itself feeds the TXT genes via txt_sources)."""
    ex = "aud_asr"
    genes: list[Gene] = []
    for a in inc.artifacts:
        if not is_audio_source(a):
            continue
        aid = a.meta.artifact_id
        try:
            tr = transcribe(a, ctx)
        except Exception as exc:  # noqa: BLE001
            genes.append(_gene(inc, ex, Family.TXT, "asr_transcript", None,
                               EvidencePointer(artifact_id=aid, note=f"{type(exc).__name__}: {exc}"[:300]),
                               status=GeneStatus.FAILED))
            continue
        if tr is None:
            genes.append(_gene(inc, ex, Family.TXT, "asr_transcript", None,
                               EvidencePointer(artifact_id=aid, note="ASR model not available offline "
                                               "(HYDRA_MODEL_DOWNLOAD=1 to fetch) or disabled"),
                               status=GeneStatus.UNAVAILABLE))
            continue
        segs = tr.get("segments", [])
        words = sum(len(s["text"].split()) for s in segs)
        status = GeneStatus.OBSERVED if segs else GeneStatus.NOT_APPLICABLE
        genes.append(_gene(inc, ex, Family.TXT, "asr_transcript", None,
                           EvidencePointer(artifact_id=aid, timecode_s=segs[0]["start"] if segs else None,
                                           note=f"{tr.get('model')}: {len(segs)} segments, {words} words, "
                                                f"{tr.get('seconds', 0)} s"),
                           group="TXT:boilerplate:asr_status", status=status))
    return ExtractResult(genes)
