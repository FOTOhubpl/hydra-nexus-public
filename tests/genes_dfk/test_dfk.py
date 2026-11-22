"""DFK suspicion genes on synthetic inputs only (tiny random backbones built in the test, generated media).

No pinned weights are needed: the DINOv2 statistic is tested with a 2-layer random Dinov2Model, the AudioSeal
detector with random initialisation. Real-weight behaviour is measured on the server (docs/genes/DFK.md).
"""

from __future__ import annotations

import os
import pickle
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest
import torch

from hydra.contracts import GeneStatus, Incident
from hydra.genome import dfk
from hydra.genome.base import ExtractContext, ResolvedIncident
from hydra.synapse import level
from hydra.vendor import audioseal as vas
from tests.genes_vis.helpers import artifact, photo, save

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


@pytest.fixture(scope="module")
def tiny_dino():
    from transformers import Dinov2Config, Dinov2Model

    torch.manual_seed(0)
    cfg = Dinov2Config(hidden_size=32, num_hidden_layers=2, num_attention_heads=2, intermediate_size=64,
                       image_size=224, patch_size=14)
    return Dinov2Model(cfg).eval()


def _inc(*arts) -> ResolvedIncident:
    return ResolvedIncident(Incident(id="syn-dfk", source="synthetic", source_ref="t",
                                     artifacts=[a.meta for a in arts]), list(arts))


def _ctx(settings, tmp_path: Path, **models) -> ExtractContext:
    return ExtractContext(settings=settings, work_dir=tmp_path / "work", models=dict(models),
                          options={"dfk_image": True, "dfk_video": True})


# ------------------------------------------------------------------------------------------ image statistic


def test_instability_is_deterministic_and_zero_without_noise(tiny_dino):
    px = dfk.to_pixels([np.asarray(photo(s)) for s in range(3)])
    assert px.shape == (3, 3, 224, 224) and px.dtype == np.float32
    a = dfk.instability_scores(tiny_dino, px)
    b = dfk.instability_scores(tiny_dino, px)
    assert np.allclose(a, b) and np.all(a >= 0) and np.all(a <= 2)
    assert np.allclose(dfk.instability_scores(tiny_dino, px, lam=0.0), 0.0, atol=1e-6)
    # stronger perturbation moves the embedding further
    assert dfk.instability_scores(tiny_dino, px, lam=0.5).mean() > a.mean()


def test_band_edges():
    assert dfk.band(0.5, 0.2, 0.4) == "high"
    assert dfk.band(0.3, 0.2, 0.4) == "medium"
    assert dfk.band(0.1, 0.2, 0.4) == "low"
    assert dfk.IMG_T_HIGH > dfk.IMG_T_MEDIUM > 0 and dfk.VID_T_HIGH > dfk.VID_T_MEDIUM > 0


def test_image_extractor_genes_are_label_tier(settings, tmp_path, tiny_dino):
    arts = [artifact(save(photo(s, (320, 240)), tmp_path / f"p{s}.jpg", quality=90), "ad_creative") for s in range(3)]
    tiny = artifact(save(photo(9).resize((40, 40)), tmp_path / "tiny.png"), "ad_creative")
    res = dfk.dfk_image(_inc(*arts, tiny), _ctx(settings, tmp_path, dfk_dinov2=tiny_dino))
    per = [g for g in res.genes if g.kind == "synthetic_image_suspicion"]
    summ = [g for g in res.genes if g.kind == "synthetic_image_check"]
    assert len(per) == 3 and len(summ) == 1  # the 40 px image is skipped
    assert all(g.value in ("low", "medium", "high") and g.quality <= 0.2 for g in per)
    assert summ[0].value in ("flagged", "not_flagged") and "sprawdzono 3" in summ[0].evidence.note
    for g in res.genes:
        assert level(g) == "none" and g.dependency_group == dfk.GROUP_IMAGE
        assert "deepfake" not in (g.evidence.note or "").replace("wykrywanie deepfake'ów", "")
    assert all(dfk.WORDING in g.evidence.note and g.evidence.artifact_id for g in per)


def test_image_extractor_off_by_default(settings, tmp_path, tiny_dino, monkeypatch):
    monkeypatch.delenv("HYDRA_DFK_IMAGE", raising=False)
    p = artifact(save(photo(1), tmp_path / "a.png"), "ad_creative")
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w", models={"dfk_dinov2": tiny_dino})
    res = dfk.dfk_image(_inc(p), ctx)
    assert [(g.kind, g.status) for g in res.genes] == [("synthetic_image_check", GeneStatus.UNAVAILABLE)]
    assert "HYDRA_DFK_IMAGE" in res.genes[0].evidence.note


def test_image_extractor_unknown_states(settings, tmp_path):
    none = dfk.dfk_image(_inc(), _ctx(settings, tmp_path))
    assert [g.status for g in none.genes] == [GeneStatus.NOT_APPLICABLE]
    p = artifact(save(photo(1), tmp_path / "a.png"), "ad_creative")
    res = dfk.dfk_image(_inc(p), _ctx(settings, tmp_path))  # empty models dir: dinov2-small missing
    assert [(g.kind, g.status) for g in res.genes] == [("synthetic_image_check", GeneStatus.UNAVAILABLE)]


# ------------------------------------------------------------------------------------------ video statistic


def _pan(n: int = 24, seed: int = 0, step: int = 6) -> np.ndarray:
    big = np.asarray(photo(seed, (224 + n * step, 224)))
    return np.stack([big[:, k * step : k * step + 224] for k in range(n)])


def test_trajectory_stats_rejects_cuts_and_stills(tiny_dino):
    st = dfk.trajectory_stats(tiny_dino, _pan())
    assert st is not None and 0.0 <= st["angle_mean"] <= 180.0 and st["step_median"] > 0
    still = np.repeat(_pan(1), 24, axis=0)
    assert dfk.trajectory_stats(tiny_dino, still) is None
    cut = np.concatenate([_pan(12, 0, 1), _pan(12, 5, 1)])
    assert dfk.trajectory_stats(tiny_dino, cut) is None
    assert dfk.trajectory_stats(tiny_dino, _pan(3)) is None


def _mp4(path: Path, seconds: int = 6, audio: bool = False) -> Path:
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=25:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    subprocess.run([*cmd, "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


@needs_ffmpeg
def test_video_windows_and_extractor(settings, tmp_path, tiny_dino):
    mp4 = _mp4(tmp_path / "v.mp4")
    wins, dur = dfk.video_windows(mp4)
    assert 5.5 < dur < 6.5 and len(wins) == dfk.VID_WINDOWS
    assert all(fr.shape[1:] == (224, 224, 3) and len(fr) == 24 for _s, fr in wins)
    res = dfk.dfk_video(_inc(artifact(mp4, "ad_video")), _ctx(settings, tmp_path, dfk_dinov2=tiny_dino))
    kinds = {g.kind: g for g in res.genes}
    assert set(kinds) == {"synthetic_video_suspicion", "synthetic_video_check"}
    assert all(level(g) == "none" and g.dependency_group == dfk.GROUP_VIDEO for g in res.genes)
    g = kinds["synthetic_video_suspicion"]
    if g.status == GeneStatus.OBSERVED:
        assert g.value in ("low", "medium", "high") and g.evidence.timecode_s is not None
    else:  # testsrc2 may be judged a still/cut window by the random backbone: unknown, never a penalty
        assert g.status == GeneStatus.NOT_APPLICABLE


@needs_ffmpeg
def test_video_extractor_off_by_default(settings, tmp_path, tiny_dino, monkeypatch):
    monkeypatch.delenv("HYDRA_DFK_VIDEO", raising=False)
    mp4 = _mp4(tmp_path / "v.mp4", seconds=3)
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w", models={"dfk_dinov2": tiny_dino})
    res = dfk.dfk_video(_inc(artifact(mp4, "ad_video")), ctx)
    assert [(g.kind, g.status) for g in res.genes] == [("synthetic_video_check", GeneStatus.UNAVAILABLE)]
    assert "HYDRA_DFK_VIDEO" in res.genes[0].evidence.note


def test_video_extractor_without_video_or_model(settings, tmp_path):
    assert dfk.dfk_video(_inc(), _ctx(settings, tmp_path)).genes[0].status == GeneStatus.NOT_APPLICABLE


# ------------------------------------------------------------------------------------------ AudioSeal


def test_fold_weight_norm_is_exact():
    torch.manual_seed(1)
    plain = vas.build_detector().eval()
    sd = plain.state_dict()
    old_style = {}
    for k, v in sd.items():
        if k.endswith("conv.conv.weight"):
            base = k[: -len(".weight")]
            g = torch.linalg.vector_norm(v.reshape(v.shape[0], -1), dim=1).reshape(-1, *([1] * (v.dim() - 1)))
            old_style[base + ".weight_g"] = g
            old_style[base + ".weight_v"] = v * 3.0  # any positive scale: the norm cancels it
        else:
            old_style[k] = v
    folded = vas.fold_weight_norm(old_style)
    assert set(folded) == set(sd)
    other = vas.build_detector().eval()
    other.load_state_dict(folded, strict=True)
    x = torch.randn(1, 1, 8000) * 0.1
    with torch.no_grad():
        assert torch.allclose(plain(x)[0], other(x)[0], atol=1e-5)


def test_detector_shapes_and_score():
    det = vas.build_detector().eval()
    with torch.no_grad():
        probs, msg = det(torch.zeros(1, 1, 16000))
    assert probs.shape == (1, 2, 16000) and msg.shape == (1, vas.NBITS)
    s = dfk.audioseal_score(det, np.zeros(40000, np.float32), chunk_s=1.0)
    assert 0.0 <= s <= 1.0
    assert dfk.audioseal_score(det, np.zeros(0, np.float32)) == 0.0


class _Evil:
    def __reduce__(self):
        return (os.system, ("touch " + str(_Evil.marker),))


def test_restricted_unpickler_never_runs_code(tmp_path):
    _Evil.marker = tmp_path / "pwned"
    sd = {"detector.1.bias": torch.zeros(3)}
    p = tmp_path / "ck.pth"
    torch.save({"xp.cfg": _Evil(), "model": sd}, p)
    got = vas.load_checkpoint_restricted(p)
    assert not _Evil.marker.exists()
    assert torch.equal(got["detector.1.bias"], sd["detector.1.bias"])
    bad = tmp_path / "bad.pth"
    torch.save({"model": _Evil()}, bad)
    with pytest.raises(ValueError):
        vas.load_checkpoint_restricted(bad)
    assert not _Evil.marker.exists()
    # plain pickle of the same payload would run it: the guard is the allow-list, not the file format
    assert b"system" in pickle.dumps(_Evil())


def test_convert_roundtrip(tmp_path):
    torch.manual_seed(2)
    plain = vas.build_detector().eval()
    src = tmp_path / "det.pth"
    torch.save({"xp.cfg": {"seanet": {}}, "model": plain.state_dict()}, src)
    out = vas.convert_checkpoint(src, tmp_path / "det.safetensors")
    det = vas.load_detector(out)
    x = torch.randn(1, 1, 4000) * 0.1
    with torch.no_grad():
        assert torch.allclose(plain(x)[0], det(x)[0], atol=1e-6)


def test_converted_metadata_order_is_canonical(tmp_path):
    """safetensors writes __metadata__ in HashMap order; the pinned sha256 needs one fixed order."""
    from safetensors.torch import load_file, save_file

    meta = {"source": "facebook/audioseal detector_base.pth", "converted_by": "hydra.vendor.audioseal"}
    f = tmp_path / "t.safetensors"
    save_file({"w": torch.ones(3)}, str(f), metadata=dict(reversed(meta.items())))
    head = f.read_bytes()
    vas._canonical_metadata_order(f, meta)
    body = f.read_bytes()
    assert len(body) == len(head) and b'"__metadata__":{"source":' in body[:200]
    assert torch.equal(load_file(str(f))["w"], torch.ones(3))


def _wav(path: Path, seconds: float = 3.0) -> Path:
    t = np.arange(int(16000 * seconds)) / 16000
    x = (0.2 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(x.tobytes())
    return path


@needs_ffmpeg
def test_audio_wm_extractor(settings, tmp_path):
    wav = artifact(_wav(tmp_path / "a.wav"), "ad_video", mime="audio/wav")
    det = vas.build_detector().eval()
    res = dfk.dfk_audio_wm(_inc(wav), _ctx(settings, tmp_path, dfk_audioseal=det))
    summ = [g for g in res.genes if g.kind == "wm_audio_check"]
    assert len(summ) == 1 and summ[0].status == GeneStatus.OBSERVED
    assert summ[0].value in ("detected", "not_detected") and "brak znaku nie świadczy" in summ[0].evidence.note
    assert all(level(g) == "none" for g in res.genes)


def test_audio_wm_unavailable_and_not_applicable(settings, tmp_path, monkeypatch):
    monkeypatch.delenv("HYDRA_MODEL_DOWNLOAD", raising=False)
    wav = artifact(_wav(tmp_path / "a.wav"), "ad_video", mime="audio/wav")
    res = dfk.dfk_audio_wm(_inc(wav), _ctx(settings, tmp_path))
    assert [(g.kind, g.status) for g in res.genes] == [("wm_audio_check", GeneStatus.UNAVAILABLE)]
    assert "HYDRA_MODEL_DOWNLOAD" in res.genes[0].evidence.note
    img = artifact(save(photo(1), tmp_path / "x.png"), "ad_creative")
    assert dfk.dfk_audio_wm(_inc(img), _ctx(settings, tmp_path)).genes[0].status == GeneStatus.NOT_APPLICABLE


def test_registered_and_pinned():
    from hydra.genome import base

    base.load_family_modules()
    assert {"dfk_image", "dfk_video", "dfk_audio_wm"} <= set(base.REGISTRY)
    assert len(dfk.AS_SRC_SHA256) == 64 and len(dfk.AS_SHA256) == 64
    assert "3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba" in dfk.AS_SRC_URL
