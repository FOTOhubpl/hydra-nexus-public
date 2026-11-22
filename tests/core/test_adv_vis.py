"""ADV-VIS: pair geometry (ORB/RootSIFT + MAGSAC++), crop rule, HSV colour gene. Synthetic fixtures only."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageOps

from hydra import vis_colour, vis_geom
from hydra.synapse import score_pair
from tests.adversarial.helpers import corpus, image_unit

FLAGS = ("HYDRA_ADV_GEOM", "HYDRA_ADV_CROP", "HYDRA_ADV_HSV")


def _creative(seed: int, size=(480, 320)) -> Image.Image:
    """Textured synthetic creative: many small shapes give ORB enough corners."""
    rng = np.random.default_rng(seed)
    im = Image.new("RGB", size, tuple(int(c) for c in rng.integers(150, 240, 3)))
    d = ImageDraw.Draw(im)
    w, h = size
    for _ in range(260):
        x, y = int(rng.integers(0, w - 20)), int(rng.integers(0, h - 20))
        s = int(rng.integers(6, 28))
        fill = tuple(int(c) for c in rng.integers(0, 255, 3))
        if rng.random() < 0.5:
            d.rectangle((x, y, x + s, y + s // 2 + 3), fill=fill)
        else:
            d.ellipse((x, y, x + s, y + s), fill=fill)
    return im


def _crop(im: Image.Image, fx=(0.08, 0.03, 0.10, 0.05), scale=0.8) -> Image.Image:
    w, h = im.size
    c = im.crop((int(w * fx[0]), int(h * fx[1]), w - int(w * fx[2]), h - int(h * fx[3])))
    return c.resize((int(c.width * scale), int(c.height * scale)))


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    from hydra.config import Settings

    for f in FLAGS:
        monkeypatch.delenv(f, raising=False)
    monkeypatch.setenv("HYDRA_ADV_CROP", "0")  # crop is default-on since 2026-11-22; tests start from all-off
    vis_geom._MEMO.clear()
    vis_colour._MEMO.clear()
    vis_geom._SPENT[0] = 0
    monkeypatch.setattr(vis_geom, "_settings", lambda: Settings(state_dir=tmp_path))
    monkeypatch.setattr(vis_geom, "_locate", lambda artifact_id, settings=None: tmp_path)
    yield
    vis_geom._MEMO.clear()


def test_verify_crop_resize_is_same_asset_and_crop_match():
    a = _creative(1)
    res = vis_geom.verify(_crop(a), a)
    assert res["same_asset"] and res["crop_match"], res
    assert res["inliers"] >= vis_geom.MIN_INLIERS
    assert res["anisotropy"] <= vis_geom.CROP_ANISO
    assert 0.7 < res["scale"] < 1.6
    assert len(res["points"]) <= vis_geom.MAX_POINTS and len(res["homography"]) == 3


def test_verify_unrelated_images_and_mirror():
    a, b = _creative(1), _creative(2)
    assert not vis_geom.verify(a, b)["same_asset"]
    m = vis_geom.verify(ImageOps.mirror(a), a)
    assert m["same_asset"] and m["mirrored"] and not m["crop_match"]  # a flip is not a crop


def test_verify_is_deterministic():
    a = _creative(3)
    c = _crop(a)
    r1, r2 = vis_geom.verify(c, a), vis_geom.verify(c, a)
    assert r1 == r2


def _pair(img_a, img_b, cos=0.9, monkeypatch=None):
    """Two incidents with one creative unit each; DINOv2 replaced by a controlled-cosine synthetic vector."""
    from hydra.genome.vis import phash_hex, unit_hash_values

    rows, vectors, images = {}, {}, {}
    va = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    vb = np.array([cos, float(np.sqrt(1 - cos * cos)), 0.0, 0.0], dtype=np.float32)
    for iid, image, v in (("a", img_a, va), ("b", img_b, vb)):
        rows[iid], refs = image_unit(iid, iid, phash_hex(image), vector=v)
        for kind, (value, quality, _note) in unit_hash_values(image).items():
            if kind != "phash":
                rows[iid].append(rows[iid][0].model_copy(update={"kind": kind, "value": value, "quality": quality}))
        vectors.update(refs)
        images[hashlib.sha256(f"{iid}:{iid}".encode()).hexdigest()] = image
    if monkeypatch is not None:
        monkeypatch.setattr(vis_geom, "load_unit", lambda ref, settings=None, root=None: images.get(ref[0]))
    genomes, rarity = corpus(rows, vectors)
    return score_pair(genomes["a"], genomes["b"], rarity)


def _kinds(pe) -> set[str]:
    return {m.kind for m in pe.score.reasons}


def test_flags_off_no_geometry_is_run(monkeypatch):
    a = _creative(5)
    calls = []
    monkeypatch.setattr(vis_geom, "cached_verify", lambda *x, **k: calls.append(x))
    pe = _pair(_crop(a, fx=(0.12, 0.0, 0.0, 0.12)), a, monkeypatch=monkeypatch)
    assert not calls
    assert "geom_same_asset" not in _kinds(pe)


def test_crop_flag_turns_a_verified_crop_into_a_near_duplicate(monkeypatch):
    a = _creative(5)
    c = _crop(a, fx=(0.12, 0.0, 0.0, 0.12))
    off = _pair(c, a, monkeypatch=monkeypatch)
    assert "visual_near_duplicate" not in _kinds(off)  # the 64 % sub-windows miss this crop
    monkeypatch.setenv("HYDRA_ADV_CROP", "1")
    vis_geom._MEMO.clear()
    on = _pair(c, a, monkeypatch=monkeypatch)
    assert "visual_near_duplicate" in _kinds(on)
    m = next(m for m in on.score.reasons if m.kind == "visual_near_duplicate")
    assert "geometrycznie" in m.explanation_pl
    assert on.campaign_bits > off.campaign_bits


def test_crop_flag_never_matches_different_images_or_low_cosine(monkeypatch):
    monkeypatch.setenv("HYDRA_ADV_CROP", "1")
    for k in range(3):
        pe = _pair(_creative(100 + k), _creative(200 + k), cos=0.97, monkeypatch=monkeypatch)
        assert "visual_near_duplicate" not in _kinds(pe), k
    a = _creative(9)
    pe = _pair(_crop(a), a, cos=0.5, monkeypatch=monkeypatch)  # below GEOM_COS_MIN: never verified
    assert "visual_near_duplicate" not in _kinds(pe)


def test_geom_flag_is_asset_tier_only(monkeypatch):
    a = _creative(5)
    c = _crop(a, fx=(0.12, 0.0, 0.0, 0.12))
    off = _pair(c, a, monkeypatch=monkeypatch)
    monkeypatch.setenv("HYDRA_ADV_GEOM", "1")
    on = _pair(c, a, monkeypatch=monkeypatch)
    assert "geom_same_asset" in _kinds(on)
    assert on.campaign_bits == off.campaign_bits  # 0 campaign bits
    assert on.asset_bits >= off.asset_bits


def test_cached_verify_writes_and_reuses_disk_cache(monkeypatch, tmp_path):
    a = _creative(11)
    imgs = {"x": _crop(a), "y": a}
    monkeypatch.setattr(vis_geom, "load_unit", lambda ref, settings=None, root=None: imgs[ref[0]])
    r1 = vis_geom.cached_verify(("x", None), ("y", None))
    assert r1 and r1["same_asset"]
    assert len(list((tmp_path / "work" / "vis_geom").glob("*.json"))) == 1
    vis_geom._MEMO.clear()
    monkeypatch.setattr(vis_geom, "verify", lambda *x, **k: pytest.fail("cache miss"))
    assert vis_geom.cached_verify(("x", None), ("y", None))["inliers"] == r1["inliers"]


def test_budget_bounds_the_cost(monkeypatch):
    monkeypatch.setattr(vis_geom, "load_unit", lambda ref, settings=None, root=None: _creative(1, (64, 64)))
    vis_geom._SPENT[0] = vis_geom.BUDGET
    assert vis_geom.cached_verify(("p", None), ("q", None)) is None


# ------------------------------------------------------------------------------------------ HSV colour gene


def test_hsv_never_changes_extraction(monkeypatch):
    from hydra.genome.vis import unit_hash_values

    a = _creative(1)
    off = unit_hash_values(a)
    monkeypatch.setenv("HYDRA_ADV_HSV", "1")
    assert unit_hash_values(a) == off  # scoring-time feature: gene cache keys and stored genes unchanged
    assert len(vis_colour.hsv_hist_hex(a)) == 2 * 8 * 4 * 4


def test_hsv_intersection_survives_crop_but_not_recolour():
    a = _creative(1)
    ha = vis_colour.hsv_hist(a)
    hc = vis_colour.hsv_hist(_crop(a))
    rec = Image.merge("RGB", a.split()[::-1])  # channel swap = recolour
    hr = vis_colour.hsv_hist(rec)
    assert abs(ha.sum() - 1.0) < 1e-9
    assert vis_colour.intersection(ha, hc) >= vis_colour.HSV_MIN
    assert vis_colour.intersection(ha, hr) < vis_colour.HSV_MIN


def test_hsv_reason_is_weak_asset_tier(monkeypatch):
    monkeypatch.setenv("HYDRA_ADV_HSV", "1")
    a = _creative(5)
    c = _crop(a, fx=(0.12, 0.0, 0.0, 0.12))
    on = _pair(c, a, cos=0.9, monkeypatch=monkeypatch)
    hsv = [m for m in on.score.reasons if m.kind == "hsv_colour"]
    assert hsv and hsv[0].bits == vis_colour.HSV_BITS
    monkeypatch.delenv("HYDRA_ADV_HSV")
    off = _pair(c, a, cos=0.9, monkeypatch=monkeypatch)
    assert on.campaign_bits == off.campaign_bits


# ------------------------------------------------------------------------------------------ API helper


def test_norm_points_maps_unit_pixels_to_artifact_coordinates():
    from hydra.api_geom import _norm_points

    res = {"points": [[50.0, 25.0, 10.0, 10.0]], "size_a": [100, 50], "size_b": [20, 20]}
    out = _norm_points(res, ("a", (100.0, 50.0, 200.0, 100.0)), ("b", None), (400, 200), (20, 20))
    assert out == [[0.5, 0.5, 0.5, 0.5]]


def test_score_key_part_tracks_flags(monkeypatch):
    assert vis_geom.score_key_part() == ""
    monkeypatch.setenv("HYDRA_ADV_CROP", "1")
    assert vis_geom.score_key_part() == "vis_geom@1:crop"
    monkeypatch.setenv("HYDRA_ADV_GEOM", "1")
    assert vis_geom.score_key_part() == "vis_geom@1:geom+crop"


def test_locate_finds_the_artifact_in_any_connected_state(monkeypatch, tmp_path):
    """The eval harness and the API pass explicit Settings: the blob is found through the DBs this process opened."""
    from hydra import db
    from hydra.config import Settings
    from hydra.storage import ContentStore

    monkeypatch.undo()  # real _locate / _settings
    other, env_state = tmp_path / "eval-shard", tmp_path / "env-state"
    digest = ContentStore(other / "cas").put_bytes(b"not an image")
    monkeypatch.setattr(vis_geom, "_settings", lambda: Settings(state_dir=env_state))
    assert vis_geom._locate(digest) is None
    db.connect(other / "hydra.sqlite").close()
    assert vis_geom._locate(digest) == other
    assert vis_geom._locate(digest, Settings(state_dir=env_state)) == other
    assert vis_geom.cached_verify((digest, None), (digest, None)) is None  # undecodable: unknown, no evidence
    assert not (env_state / "work").exists()  # never writes into a state that does not hold the blob


def test_crop_is_default_on(monkeypatch):
    monkeypatch.delenv("HYDRA_ADV_CROP", raising=False)
    assert vis_geom.crop_on()
    monkeypatch.setenv("HYDRA_ADV_CROP", "0")
    assert not vis_geom.crop_on()
