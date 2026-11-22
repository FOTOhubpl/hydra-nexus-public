"""Video-PDQ temporal matching and the AV-sync suspicion signal, on synthetic OpenCV videos (no organiser data)."""

from __future__ import annotations

import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np
import pytest

from hydra.contracts import GeneStatus
from hydra.entropy import Rarity
from hydra.genome import vid_avsync, vid_seq, vis
from hydra.genome.base import ExtractContext
from hydra.synapse import build_genome, level, score_pair

from .helpers import artifact, incident, photo

FPS = 10
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


def scene_frames(seeds: list[int], seconds: float = 2.0, size: tuple[int, int] = (320, 240)) -> list[np.ndarray]:
    """Each seed = one 'shot' (a photo with a small moving square), `seconds` long at FPS."""
    import cv2

    out = []
    for s in seeds:
        base = cv2.cvtColor(np.asarray(photo(s, size)), cv2.COLOR_RGB2BGR)
        for k in range(int(seconds * FPS)):
            f = base.copy()
            x = 10 + 3 * k
            cv2.rectangle(f, (x, 10), (x + 12, 22), (255, 255, 255), -1)
            out.append(f)
    return out


def write_video(path: Path, frames: list[np.ndarray], crop: float = 0.0) -> Path:
    import cv2

    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (w, h))
    for f in frames:
        if crop:
            dx, dy = int(w * crop), int(h * crop)
            f = cv2.resize(f[dy : h - dy, dx : w - dx], (w, h))
        vw.write(f)
    vw.release()
    return path


def genome_of(inc_id: str, mp4: Path, settings, tmp_path: Path):
    inc = incident(inc_id, [artifact(mp4, "ad_video")])
    res = vis.vis(inc, ExtractContext(settings=settings, work_dir=tmp_path / f"work-{inc_id}"))
    vecs = {v.key: (v.model, v.data) for v in res.vectors}
    return build_genome(inc_id, res.genes, vecs)


def rarity(n: int) -> Rarity:
    return Rarity(n_incidents=n, postings={})


A_SEEDS = list(range(1, 11))                # 20 s, ten 2-second shots
B_SEEDS = [50, 51, 52] + A_SEEDS[2:8]       # 6 s intro, then A's shots 3..8: A t -> B t + 2 s


@needs_ffmpeg
def test_shifted_copy_matches_with_segment_and_offset(settings, tmp_path):
    ga = genome_of("v-a", write_video(tmp_path / "a.mp4", scene_frames(A_SEEDS)), settings, tmp_path)
    gb = genome_of("v-b", write_video(tmp_path / "b.mp4", scene_frames(B_SEEDS)), settings, tmp_path)
    best = vid_seq.match_genomes(ga, gb)
    assert best is not None
    seg = best[0]
    assert seg.frames >= 5
    assert seg.offset_s == pytest.approx(2.0, abs=1.0)
    assert 3.0 <= seg.start_a <= 5.0 and 13.0 <= seg.end_a <= 16.0
    assert 5.0 <= seg.start_b <= 7.0
    # unrelated footage: no segment
    gc = genome_of("v-c", write_video(tmp_path / "c.mp4", scene_frames(list(range(100, 110)))), settings, tmp_path)
    assert vid_seq.match_genomes(ga, gc) is None


@needs_ffmpeg
def test_segment_is_scored_and_explained(settings, tmp_path):
    ga = genome_of("v-a", write_video(tmp_path / "a.mp4", scene_frames(A_SEEDS)), settings, tmp_path)
    gb = genome_of("v-b", write_video(tmp_path / "b.mp4", scene_frames(B_SEEDS)), settings, tmp_path)
    vid_seq.annotate_footage({"v-a": ga, "v-b": gb})
    pe = score_pair(ga, gb, rarity(2))
    segs = [r for r in pe.score.reasons if r.kind == "video_segment"]
    assert segs and segs[0].bits > 0
    m = segs[0]
    assert "Wspólny fragment wideo" in m.explanation_pl and "przesunięcie" in m.explanation_pl
    pa, pb = vid_seq.parse_note(m.evidence_a.note), vid_seq.parse_note(m.evidence_b.note)
    assert pa and pb and pb["start"] - pa["start"] == pytest.approx(2.0, abs=1.0)
    assert m.evidence_a.timecode_s == pa["start"]


@needs_ffmpeg
def test_common_footage_is_boilerplate(settings, tmp_path):
    """The same clip in many incidents (stock / news footage) is shown but gives 0 bits."""
    genomes = {}
    for k in range(4):
        seeds = [200 + k] + A_SEEDS[:6]
        genomes[f"v-{k}"] = genome_of(f"v-{k}", write_video(tmp_path / f"{k}.mp4", scene_frames(seeds)), settings,
                                      tmp_path)
    info = vid_seq.annotate_footage(genomes)
    assert info["common_frames"] > 0
    best = vid_seq.match_genomes(genomes["v-0"], genomes["v-1"])
    assert best is not None and best[0].common
    pe = score_pair(genomes["v-0"], genomes["v-1"], rarity(4))
    segs = [r for r in pe.score.reasons if r.kind == "video_segment"]
    assert segs and segs[0].bits == 0 and "stockowy" in segs[0].explanation_pl
    assert not any(r.kind == "visual_near_duplicate" for r in pe.score.reasons)  # frames excluded as common


def _frames(pdqs: list[np.ndarray], vecs: list[np.ndarray | None], ts: list[float], art: str) -> list:
    return [vid_seq.Frame(f"{art}:{i}", art, t, p, v) for i, (p, v, t) in enumerate(zip(pdqs, vecs, ts, strict=True))]


def test_cropped_copy_matches_by_embedding_when_pdq_breaks():
    """2% crop breaks PDQ (SPEC_ERRATA: distance 34) but DINOv2 stays >= 0.95: the run is still found."""
    rng = np.random.default_rng(0)
    n = 8
    pa = [rng.integers(0, 256, 32, dtype=np.uint8) for _ in range(n)]
    flip = np.zeros(256, np.uint8)
    flip[:60] = 1  # 60 of 256 bits differ: above the PDQ threshold 31, far below random (128)
    pb = [np.bitwise_xor(p, np.packbits(rng.permutation(flip))) for p in pa]
    va = [v / np.linalg.norm(v) for v in rng.normal(size=(n, 16))]
    vb = [(v + 0.02 * rng.normal(size=16)) for v in va]
    vb = [v / np.linalg.norm(v) for v in vb]
    fa = _frames(pa, va, [float(t) for t in range(n)], "A")
    fb = _frames(pb, vb, [t + 5.0 for t in range(n)], "B")
    seg = vid_seq.match_sequences(fa, fb)
    assert seg is not None and seg.frames == n and seg.offset_s == pytest.approx(5.0)
    assert seg.min_hamming is not None and seg.min_hamming > 31 and seg.mean_cos >= 0.95
    unrelated = _frames([rng.integers(0, 256, 32, dtype=np.uint8) for _ in range(n)], vb, [float(t) for t in range(n)],
                        "C")
    assert vid_seq.match_sequences(fa, unrelated) is None  # cosine alone never makes a run


def test_out_of_order_frames_do_not_form_a_run():
    rng = np.random.default_rng(1)
    p = [rng.integers(0, 256, 32, dtype=np.uint8) for _ in range(6)]
    fa = _frames(p, [None] * 6, [0, 1, 2, 3, 4, 5], "A")
    fb = _frames(p[::-1], [None] * 6, [0, 1, 2, 3, 4, 5], "B")  # reversed edit: every offset differs
    seg = vid_seq.match_sequences(fa, fb)
    assert seg is None or seg.frames <= 2


# ------------------------------------------------------------------------------------------ AV-sync


def test_av_sync_score_separates_synced_from_unrelated():
    rng = np.random.default_rng(2)
    n = 300  # 30 s at 10 fps
    syll = np.clip(np.sin(np.arange(n) * 2 * np.pi * 4 / FPS) + 0.6 * rng.normal(size=n), 0, None)
    env = syll + 0.05 * rng.random(n)
    mouth = 0.02 + 0.05 * syll + 0.005 * rng.normal(size=n)
    s = vid_avsync.av_sync_score(mouth, env)
    assert s["r"] > 0.5 and abs(s["lag_s"]) <= 0.1 and vid_avsync.suspicion_level(s) == "low"
    other = np.clip(rng.normal(size=n), 0, None)
    s2 = vid_avsync.av_sync_score(0.02 + 0.05 * other, env)
    assert vid_avsync.suspicion_level(s2) in ("medium", "high")
    no_face = vid_avsync.av_sync_score(np.full(n, np.nan), env)
    assert vid_avsync.suspicion_level(no_face) is None
    txt = vid_avsync.describe("high", {**s2, "r": 0.05, "lag_s": 0.3})
    assert txt.startswith("podejrzenie niespójności ruchu ust i dźwięku") and "deepfake" not in txt.lower()


def test_rms_envelope_follows_bursts():
    sr = 16000
    x = np.zeros(sr * 2, np.float32)
    x[sr // 2 : sr] = 0.5 * np.sin(np.arange(sr // 2) * 2 * np.pi * 220 / sr)
    env = vid_avsync.rms_envelope(x, sr, FPS, 20)
    assert env[5:10].min() > 0.3 and env[:5].max() < 1e-6


def test_av_sync_gene_is_label_tier_and_never_links():
    g = vid_avsync._gene("x", GeneStatus.OBSERVED, "a" * 64, "n", value="high", quality=0.5)
    assert level(g) == "none" and g.dependency_group == "VIS:label:av_sync"


@needs_ffmpeg
def test_av_sync_extractor_degrades_without_model_or_audio(settings, tmp_path, monkeypatch):
    monkeypatch.delenv("HYDRA_MODEL_DOWNLOAD", raising=False)
    mp4 = write_video(tmp_path / "a.mp4", scene_frames([1, 2]))
    inc = incident("v-av", [artifact(mp4, "ad_video")])
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w")
    res = vid_avsync.vid_avsync(inc, ctx)
    assert [g.status for g in res.genes] == ["unavailable"]
    model = Path(os.environ.get("HYDRA_MODELS_DIR", "~/HydraNexus/models")).expanduser()
    if not (model / vid_avsync.LANDMARKER_DIR / vid_avsync.LANDMARKER_FILE).is_file():
        pytest.skip("face landmarker not fetched")
    ctx2 = ExtractContext(settings=settings.model_copy(update={"models_dir": model}), work_dir=tmp_path / "w2")
    res2 = vid_avsync.vid_avsync(inc, ctx2)
    assert [(g.status, g.evidence.note) for g in res2.genes] == [("not_applicable", "no audio stream")]
    # with audio but no face: not applicable (insufficient), never a verdict
    wav = tmp_path / "tone.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((3000 * np.sin(np.arange(16000 * 4) * 2 * np.pi * 220 / 16000)).astype("<i2").tobytes())
    av = tmp_path / "av.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4), "-i", str(wav), "-c:v", "copy", "-c:a", "aac",
                    "-shortest", str(av)], check=True, capture_output=True)
    res3 = vid_avsync.vid_avsync(incident("v-av2", [artifact(av, "ad_video")]), ctx2)
    assert [g.status for g in res3.genes] == ["not_applicable"] and "insufficient" in res3.genes[0].evidence.note


def test_diff_api_exposes_segments_with_both_time_ranges():
    import json
    import sqlite3

    import hydra.api  # noqa: F401 — router modules import each other through hydra.api
    from hydra.api_visual import _video_segments

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE genes (incident_id TEXT, evidence TEXT)")
    for t in (0.0, 9.0, 18.0):
        conn.execute("INSERT INTO genes VALUES (?, ?)", ("a", json.dumps({"artifact_id": "x", "timecode_s": t})))
    seg = vid_seq.Segment("x", "y", 4.0, 14.0, 6.0, 16.0, 2.0, 7, 0, 0.99, 1)
    reasons = [{"kind": "video_segment", "bits": 12.0, "explanation_pl": "e",
                "evidence_a": {"artifact_id": "x", "note": seg.note("a")},
                "evidence_b": {"artifact_id": "y", "note": seg.note("b")}}, {"kind": "pdq"}]
    out = _video_segments(conn, "a", "b", reasons)
    assert len(out) == 1
    s = out[0]
    assert (s["start_a"], s["end_a"], s["start_b"], s["end_b"], s["offset_s"], s["frames"]) == (4, 14, 6, 16, 2, 7)
    assert s["length_a"] == 18.0 and s["length_b"] is None
