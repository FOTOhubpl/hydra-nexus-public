"""Media helpers and the local model runtime (model tests skip until weights are fetched)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hydra.media import extract_audio_wav16k, extract_frames, load_work_image
from hydra.ml.models import ModelUnavailable, cosine_topk, model_dir

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")


def test_work_image_applies_exif_rotation_and_downscales(tmp_path: Path):
    im = Image.new("RGB", (4000, 1000), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° CW on display
    p = tmp_path / "rot.jpg"
    im.save(p, exif=exif)
    w = load_work_image(p, max_side=2048)
    assert w.original_size == (1000, 4000)  # after transpose
    assert max(w.image.size) == 2048 and w.image.mode == "RGB"
    assert w.scale == pytest.approx(2048 / 4000)


def test_work_image_flattens_alpha_on_white(tmp_path: Path):
    p = tmp_path / "a.png"
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(p)
    assert load_work_image(p).image.getpixel((5, 5)) == (255, 255, 255)


@needs_ffmpeg
def test_video_frames_and_audio(tmp_path: Path):
    video = tmp_path / "t.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=3",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-shortest", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    frames = extract_frames(video, tmp_path / "frames", every_s=1.0)
    assert 2 <= len(frames) <= 4
    wav = extract_audio_wav16k(video, tmp_path / "a.wav")
    assert wav is not None and wav.stat().st_size > 16000


def test_cosine_topk_matches_bruteforce_and_excludes_self():
    rng = np.random.default_rng(0)
    x = rng.normal(size=(300, 32)).astype(np.float32)
    x /= np.linalg.norm(x, axis=1, keepdims=True)
    idx, val = cosine_topk(x, x, k=5, exclude_self=True, chunk=64)
    assert idx.shape == (300, 5)
    assert not np.any(idx == np.arange(300)[:, None])
    sims = x @ x.T
    np.fill_diagonal(sims, -2)
    np.testing.assert_allclose(val, -np.sort(-sims, axis=1)[:, :5], rtol=1e-5, atol=1e-5)


def _have(key: str) -> bool:
    try:
        model_dir(key)
        return True
    except ModelUnavailable:
        return False


@pytest.mark.skipif(not _have("dinov2-small"), reason="dinov2-small not fetched")
def test_dinov2_offline_embedding_is_normalised():
    from hydra.ml.models import load_image_embedder

    emb = load_image_embedder("dinov2-small")
    v = emb.embed([Image.new("RGB", (224, 224), (i * 40, 0, 0)) for i in range(3)])
    assert v.shape == (3, 384)
    np.testing.assert_allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-4)


@pytest.mark.skipif(not _have("e5-small"), reason="e5-small not fetched")
def test_e5_offline_embedding_polish():
    from hydra.ml.models import load_text_embedder

    emb = load_text_embedder("e5-small")
    v = emb.embed(["Bank jest wściekły", "Zainwestuj 1000 zł", "Pogoda w Bydgoszczy"])
    assert v.shape == (3, 384)
    np.testing.assert_allclose(np.linalg.norm(v, axis=1), 1.0, atol=1e-4)


@pytest.mark.skipif(not _have("rapidocr-v6"), reason="rapidocr-v6 not fetched")
@pytest.mark.parametrize("engine", ["rapidocr", "tesseract", "hybrid"])
def test_ocr_engines_read_polish_offline(engine: str, polish_line):
    import difflib

    from hydra.ml.ocr import ocr

    im = polish_line
    if engine != "rapidocr" and shutil.which("tesseract") is None:
        pytest.skip("tesseract missing")
    lines = ocr(im, engine=engine)
    text = " ".join(line.text for line in lines)
    assert lines and all(0 <= line.box[0] < line.box[2] <= im.width for line in lines)
    assert difflib.SequenceMatcher(None, text, "Zażółć gęślą jaźń 2026").ratio() >= 0.8, text
