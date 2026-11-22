from __future__ import annotations

from hydra.genome.ocr_pool import OcrPool, chrome_crop
from hydra.ml.ocr import OcrLine


def test_chrome_crop_only_for_phone_screens():
    assert chrome_crop(1170, 2532) == (0, 152, 1170, 2405)
    assert chrome_crop(537, 677) is None


def test_boxes_mapped_back_to_original_coordinates(tmp_path):
    from PIL import Image

    p = tmp_path / "phone.png"
    Image.new("RGB", (1170, 2532), (255, 255, 255)).save(p)
    pool = OcrPool(tmp_path / "cache", engine_fn=lambda im: [OcrLine("Tekst", (10, 0, 110, 20), 0.9, "fake")])
    (r,) = pool.run([(p, None)])
    # work image is scaled to 2048 max side; crop offset is added back
    assert r.crop == (0, 152, 1170, 2405)
    x0, y0, x1, y1 = r.lines[0].box
    assert abs(y0 - 152) <= 2 and abs(x0 - 12) <= 2
    (r2,) = pool.run([(p, None)])
    assert r2.cached and r2.lines[0].box == r.lines[0].box


def test_failed_image_is_reported(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"nope")
    (r,) = OcrPool(None, engine_fn=lambda im: []).run([(p, None)])
    assert not r.ok and r.error
