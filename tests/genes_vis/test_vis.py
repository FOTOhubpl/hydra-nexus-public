from __future__ import annotations

import io
import shutil

import numpy as np
import pytest
from PIL import Image, ImageOps

from hydra.genome import vis, vis_media
from hydra.genome.base import ExtractContext

from .helpers import artifact, feed_screenshot, incident, photo, save


def _jpeg(im: Image.Image, q: int = 70) -> Image.Image:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=q)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def test_pdq_canonical_is_flip_invariant_and_robust_to_resize():
    im = photo(1)
    pdq, canon, q = vis.pdq_hashes(im)
    assert q >= 50 and len(pdq) == 64
    _, canon_flip, _ = vis.pdq_hashes(ImageOps.mirror(im))
    assert vis.hex_hamming(canon_flip, canon) <= 31  # same variant wins; a few bits of resampling noise
    pdq_flip, _, _ = vis.pdq_hashes(ImageOps.mirror(im))
    assert vis.hex_hamming(pdq_flip, pdq) > 70  # the plain hash does not survive a flip
    pdq_small, _, _ = vis.pdq_hashes(_jpeg(im.resize((240, 160))))
    assert vis.hex_hamming(pdq, pdq_small) <= 31
    other, _, _ = vis.pdq_hashes(photo(2))
    assert vis.hex_hamming(pdq, other) > 70


def test_phash_and_tiles_survive_overlay_edit():
    a = photo(3)
    b = a.copy()
    b.paste((255, 0, 0), (0, 0, 120, 80))  # a sticker over one corner tile
    assert vis.hex_hamming(vis.phash_hex(a), vis.phash_hex(b)) <= 14
    assert vis.tiles_match(vis.phash_tiles_hex(a), vis.phash_tiles_hex(b)) >= 12
    assert vis.tiles_match(vis.phash_tiles_hex(a), vis.phash_tiles_hex(photo(4))) <= 6


@pytest.mark.parametrize("w,h,role,name,expected", [
    (1, 1, "landing_asset", "delivered", "pixel"),
    (48, 48, "landing_asset", "fav.png", "icon"),
    (300, 93, "landing_asset", "logo.4b5c6d7e.png", "logo"),
    (1170, 2532, "feed_screenshot", "IMG_1.PNG", "phone_screenshot"),
    (508, 695, "ad_creative", "Zrzut ekranu.png", "creative"),
    (100, 100, "landing_asset", "ava-coment1.jpeg", "avatar"),
    (1600, 893, "landing_asset", "01_hero.webp", "page_image"),
    (1249, 2281, "landing_screenshot", "screenshot.png", "page_screenshot"),
])
def test_classify_image(w, h, role, name, expected):
    assert vis_media.classify_image(w, h, role, name) == expected


def test_boilerplate_names_do_not_fire_on_hex_names():
    assert vis_media.is_boilerplate("page_image", "likes.png")
    assert vis_media.is_boilerplate("page_image", "visa-mastercard.svg")
    assert not vis_media.is_boilerplate("page_image", "c0ffee00c0ffee00c0ffee00c0ffee00.4b5c6d7e.jpg")
    assert not vis_media.is_boilerplate("page_image", "news1.webp")


def test_photo_region_found_in_feed_screenshot():
    ad = photo(5, (600, 400))
    shot = feed_screenshot(ad)
    crop, top = vis_media.crop_phone_chrome(shot)
    assert top == round(shot.height * vis_media.PHONE_TOP_CHROME)
    regions = vis_media.photo_regions(crop)
    assert regions, "the ad photo must be found"
    x0, y0, x1, y1 = regions[0]
    ad_top = int(0.28 * shot.height) - top
    ad_h = int(shot.width * ad.height / ad.width)
    assert abs(y0 - ad_top) <= 0.03 * shot.height and abs(y1 - (ad_top + ad_h)) <= 0.03 * shot.height
    assert x1 - x0 >= 0.9 * shot.width
    # the region is a near-duplicate of the bare creative, the whole screenshot is not
    region_pdq, _, _ = vis.pdq_hashes(crop.crop(regions[0]))
    ad_pdq, _, _ = vis.pdq_hashes(ad)
    shot_pdq, _, _ = vis.pdq_hashes(shot)
    assert vis.hex_hamming(region_pdq, ad_pdq) <= 31 < vis.hex_hamming(shot_pdq, ad_pdq)


def test_embed_view_letterboxes_tall_images():
    v = vis.embed_view(Image.new("RGB", (500, 1500), (10, 200, 10)))
    assert abs(v.width - v.height) <= 1 and min(v.size) <= 256


def test_extractor_genes_without_models(settings, tmp_path):
    ad = photo(6, (600, 400))
    arts = [
        artifact(save(ad, tmp_path / "in" / "ad.png"), "ad_creative"),
        artifact(save(feed_screenshot(ad), tmp_path / "in" / "IMG_1.PNG"), "feed_screenshot"),
        artifact(save(Image.new("RGB", (1, 1)), tmp_path / "in" / "px.gif"), "landing_asset", "image/gif"),
        artifact(save(Image.new("RGB", (80, 80), (0, 90, 255)), tmp_path / "in" / "likes.png"), "landing_asset"),
    ]
    broken = tmp_path / "in" / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    arts.append(artifact(broken, "landing_asset", "image/jpeg"))
    inc = incident("t-1", arts)
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "work")
    res = vis.vis(inc, ctx)  # never raises
    kinds = [(g.kind, g.status) for g in res.genes]
    assert ("dinov2", "unavailable") in kinds  # empty models dir -> unknown, not a crash
    assert ("pdq", "failed") in kinds          # the broken file
    classes = {g.value for g in res.genes if g.kind == "image_class"}
    assert {"creative", "phone_screenshot", "pixel"} <= classes
    pdq = [g for g in res.genes if g.kind == "pdq"and g.status == "observed"]
    groups = {g.dependency_group for g in pdq}
    assert "vis_app_ui" in groups  # whole phone screen
    regions = [g for g in pdq if "kind=region" in (g.evidence.note or "")]
    assert regions and regions[0].evidence.bbox is not None
    ad_g = next(g for g in pdq if "file=ad.png" in g.evidence.note)
    assert min(vis.hex_hamming(ad_g.value, r.value) for r in regions) <= 31
    assert not any(g.kind == "pdq" and "px.gif" in (g.evidence.note or "") for g in res.genes)


def test_background_normalisation():
    rng = np.random.default_rng(0)
    base = rng.normal(size=16)
    v = np.stack([base + 0.1 * rng.normal(size=16) for _ in range(5)] + [rng.normal(size=16) for _ in range(5)])
    v = (v / np.linalg.norm(v, axis=1, keepdims=True)).astype(np.float32)
    bg = vis.background_stats(v, k=3)
    assert bg[:5].mean() > bg[5:].mean()
    assert vis.normalised_similarity(0.9, 0.8, 0.8) == pytest.approx(0.1)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg missing")
def test_video_frames_one_fps_with_timecodes_and_dedup(settings, tmp_path):
    import subprocess

    mp4 = tmp_path / "rec.mp4"
    # 6 s: 3 s static red, then 3 s moving test pattern
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=320x240:d=3",
                    "-f", "lavfi", "-i", "testsrc=s=320x240:d=3:r=25",
                    "-filter_complex", "[0:v]fps=25[a];[a][1:v]concat=n=2:v=1[o]", "-map", "[o]",
                    "-pix_fmt", "yuv420p", str(mp4)], check=True, capture_output=True)
    frames = vis_media.video_frames(mp4, tmp_path / "frames")
    one_fps = [f for f in frames if not f[2]]
    assert [t for _, t, _ in one_fps][:6] == [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    kept = vis_media.select_frames(frames)
    assert 1 <= len(kept) < len(frames)  # the static red frames collapse
    inc = incident("t-v", [artifact(mp4, "screen_recording")])
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "work")
    res = vis.vis(inc, ctx)
    tcs = {g.evidence.timecode_s for g in res.genes if g.kind == "pdq"}
    assert tcs and all(t is not None for t in tcs)


def test_same_asset_orb_with_crop_and_mirror():
    a = photo(11, (800, 600))
    from PIL import ImageDraw

    d = ImageDraw.Draw(a)
    rng = np.random.default_rng(3)
    for _ in range(300):  # texture so ORB has keypoints
        x, y = int(rng.integers(0, 790)), int(rng.integers(0, 590))
        d.rectangle((x, y, x + 6, y + 6), fill=tuple(int(c) for c in rng.integers(0, 255, 3)))
    crop = a.crop((80, 60, 720, 540)).resize((480, 360))
    r = vis.same_asset(a, crop)
    assert r["same_asset"] and not r["mirrored"]
    m = vis.same_asset(ImageOps.mirror(a), crop)
    assert m["mirrored"] and m["inliers"] >= 120
    other = vis.same_asset(a, photo(12, (800, 600)))
    assert not other["same_asset"]


def test_odd_formats_never_crash(settings, tmp_path):
    base = photo(13, (300, 200))
    files = [
        save(base.convert("CMYK"), tmp_path / "cmyk.jpg"),
        save(Image.fromarray(np.asarray(base.convert("L")).astype(np.uint16) * 256), tmp_path / "g16.png"),
        save(base.convert("P"), tmp_path / "pal.png", transparency=0),
        save(base.convert("RGBA"), tmp_path / "rgba.webp"),
    ]
    gif = tmp_path / "anim.gif"
    base.save(gif, save_all=True, append_images=[photo(14, (300, 200))], duration=100)
    files.append(gif)
    bad_mp4 = tmp_path / "bad.mp4"
    bad_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 200)
    arts = [artifact(p, "landing_asset") for p in files]
    arts[-1].meta.mime = "image/gif"
    arts.append(artifact(bad_mp4, "screen_recording"))
    res = vis.vis(incident("t-odd", arts), ExtractContext(settings=settings, work_dir=tmp_path / "w"))
    observed = {g.evidence.artifact_id for g in res.genes if g.kind == "pdq" and g.status == "observed"}
    assert observed == {a.meta.artifact_id for a in arts[:-1]}
    failed = [g for g in res.genes if g.status == "failed"]
    assert [g.evidence.artifact_id for g in failed] == [arts[-1].meta.artifact_id]
