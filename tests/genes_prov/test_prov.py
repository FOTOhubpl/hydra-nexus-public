"""prov extractor on synthetic files built in the test (C2PA-signed JPEG, watermarked PNG, metadata chunks)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from hydra.contracts import Incident
from hydra.genome.base import ExtractContext, ResolvedIncident
from hydra.genome.prov import (
    EYE_TOL,
    FFHQ_EYES,
    SDV1_BYTES,
    SDXL_BITS,
    WM_MATCH_FRACTION,
    EyePair,
    ffhq_eye_offset,
    parse_manifest_store,
    prov,
    watermark_match,
)
from hydra.synapse import level
from hydra.vendor.imwatermark import bits_of_bytes, encode_bits
from tests.genes_prov.c2pa_synth import signed_jpeg
from tests.genes_vis.helpers import artifact, photo, save


def _inc(*arts) -> ResolvedIncident:
    return ResolvedIncident(Incident(id="syn-prov", source="synthetic", source_ref="t",
                                     artifacts=[a.meta for a in arts]), list(arts))


def _ctx(settings, tmp_path: Path) -> ExtractContext:
    return ExtractContext(settings=settings, work_dir=tmp_path / "work")


def _by_kind(genes):
    out: dict[str, list] = {}
    for g in genes:
        out.setdefault(g.kind, []).append(g)
    return out


def test_c2pa_ai_manifest_is_read(settings, tmp_path):
    p = signed_jpeg(tmp_path / "gen.jpg", ai=True, generator="SyntheticGen")
    res = prov(_inc(artifact(p, "ad_creative")), _ctx(settings, tmp_path))
    k = _by_kind(res.genes)
    assert k["c2pa_manifest"][0].value == "present"
    assert k["c2pa_claim_generator"][0].value == "SyntheticGen 1.0"
    assert "Synthetic Signer" in k["c2pa_signer"][0].value
    assert k["c2pa_ai_generated"][0].value == "trainedAlgorithmicMedia"
    assert "zaufanych" in k["c2pa_claim_generator"][0].evidence.note  # untrusted test certificate is reported
    assert all(g.evidence.artifact_id for g in k["c2pa_claim_generator"])


def test_c2pa_without_ai_assertion_and_absent(settings, tmp_path):
    p = signed_jpeg(tmp_path / "edit.jpg", ai=False)
    plain = save(photo(1), tmp_path / "plain.png")
    k = _by_kind(prov(_inc(artifact(p, "ad_creative"), artifact(plain, "landing_asset")),
                      _ctx(settings, tmp_path)).genes)
    assert "c2pa_ai_generated" not in k and k["c2pa_manifest"][0].value == "present"
    k2 = _by_kind(prov(_inc(artifact(plain, "ad_creative")), _ctx(settings, tmp_path)).genes)
    absent = k2["c2pa_manifest"][0]
    assert absent.value == "absent" and absent.status == "observed" and "autentyczności" in absent.evidence.note


def test_parse_manifest_store_minimal():
    store = {"active_manifest": "m1", "manifests": {"m1": {"claim_generator": "Tool/2",
             "assertions": [{"label": "c2pa.actions", "data": {"actions": [
                 {"action": "c2pa.created", "digitalSourceType":
                  "http://cv.iptc.org/newscodes/digitalsourcetype/compositeWithTrainedAlgorithmicMedia"}]}}]}},
             "validation_state": "Invalid"}
    f = parse_manifest_store(store)
    assert f["generator"] == "Tool/2" and f["ai_source"] == "compositeWithTrainedAlgorithmicMedia"
    assert f["validation"] == "Invalid" and f["signer"] is None


def _bgr(seed: int, size=(512, 512)) -> np.ndarray:
    """Textured synthetic image (blurred noise): DWT-DCT marks need texture, flat fields lose bits on rounding."""
    import cv2

    rng = np.random.default_rng(seed)
    return cv2.GaussianBlur((rng.random((size[1], size[0], 3)) * 255).astype(np.uint8), (5, 5), 0)


def test_watermark_sdxl_detected_and_clean_not(settings, tmp_path):
    marked = encode_bits(_bgr(3), SDXL_BITS)
    name, scores = watermark_match(marked)
    assert name == "sdxl_dwtdct" and scores["sdxl_dwtdct"] >= 0.95
    name0, scores0 = watermark_match(_bgr(4))
    assert name0 is None and scores0["sdxl_dwtdct"] < 0.9
    p = tmp_path / "wm.png"
    Image.fromarray(marked[:, :, ::-1]).save(p)
    clean = tmp_path / "clean.png"
    Image.fromarray(_bgr(5)[:, :, ::-1]).save(clean)
    k = _by_kind(prov(_inc(artifact(p, "ad_creative"), artifact(clean, "ad_creative")), _ctx(settings, tmp_path)).genes)
    assert k["wm_generator"][0].value == "sdxl_dwtdct"
    assert k["wm_check"][0].value == "detected"
    k2 = _by_kind(prov(_inc(artifact(clean, "ad_creative")), _ctx(settings, tmp_path)).genes)
    assert k2["wm_check"][0].value == "not_detected" and "nie wykryty" in k2["wm_check"][0].evidence.note


def test_watermark_sd_v1_bytes_detected():
    name, scores = watermark_match(encode_bits(_bgr(11), bits_of_bytes(SDV1_BYTES)))
    assert name == "sd_v1_dwtdct" and scores["sd_v1_dwtdct"] >= WM_MATCH_FRACTION


def test_small_images_watermark_unavailable(settings, tmp_path):
    small = save(photo(6, (120, 90)), tmp_path / "s.png")
    k = _by_kind(prov(_inc(artifact(small, "ad_creative")), _ctx(settings, tmp_path)).genes)
    assert k["wm_check"][0].status == "unavailable"


def test_png_generator_chunk(settings, tmp_path):
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", "a portrait, Steps: 20, Sampler: Euler")
    p = tmp_path / "a1111.png"
    photo(7).save(p, pnginfo=info)
    k = _by_kind(prov(_inc(artifact(p, "landing_asset")), _ctx(settings, tmp_path)).genes)
    assert k["ai_metadata_marker"][0].value == "png:parameters"


def test_ffhq_eye_offset_geometry():
    canon = EyePair(FFHQ_EYES[0], FFHQ_EYES[1], "test")
    assert ffhq_eye_offset(canon, 1024, 1024) == 0.0
    near = EyePair((0.38, 0.48), (0.62, 0.47), "test")
    assert ffhq_eye_offset(near, 512, 512) <= EYE_TOL
    off = EyePair((0.30, 0.40), (0.55, 0.42), "test")
    assert ffhq_eye_offset(off, 512, 512) > EYE_TOL
    assert ffhq_eye_offset(canon, 800, 400) is None  # not square: StyleGAN outputs are square


def test_no_media_not_applicable_and_genes_never_link(settings, tmp_path):
    html = tmp_path / "p.html"
    html.write_text("<html></html>")
    res = prov(_inc(artifact(html, "landing_html")), _ctx(settings, tmp_path))
    assert {g.status for g in res.genes} == {"not_applicable"}
    p = signed_jpeg(tmp_path / "gen.jpg")
    for g in prov(_inc(artifact(p, "ad_creative")), _ctx(settings, tmp_path)).genes:
        assert level(g) == "none", g.kind  # provenance genes add 0 linking bits


def test_avatar_check_unavailable_without_models(settings, tmp_path):
    sq = save(photo(8, (256, 256)), tmp_path / "avatar.png")
    k = _by_kind(prov(_inc(artifact(sq, "landing_asset")), _ctx(settings, tmp_path)).genes)
    assert k["avatar_geometry_check"][0].status == "unavailable"


@pytest.mark.parametrize("bad", [b"\xff\xd8\xff\xe0garbage", b""])
def test_corrupt_image_never_raises(settings, tmp_path, bad):
    p = tmp_path / "bad.jpg"
    p.write_bytes(bad)
    res = prov(_inc(artifact(p, "ad_creative", mime="image/jpeg")), _ctx(settings, tmp_path))
    assert res.genes  # summaries only, no exception


class _FakeLandmarker:
    """Stands in for the MediaPipe Face Landmarker: one face with iris centres at given positions."""

    def __init__(self, left, right):
        from types import SimpleNamespace

        pts = [SimpleNamespace(x=0.5, y=0.5) for _ in range(478)]
        pts[468] = SimpleNamespace(x=right[0], y=right[1])
        pts[473] = SimpleNamespace(x=left[0], y=left[1])
        self.result = SimpleNamespace(face_landmarks=[pts])

    def detect(self, _img):
        return self.result


def _mediapipe_image_or_skip() -> None:
    """mp.Image loads libEGL/libGLESv2 at construction on Linux (mediapipe 1.2.3 in uv.lock); headless hosts without
    libegl1/libgles2 raise OSError there. The landmarker path cannot run on such a host, so skip, do not fail."""
    mp = pytest.importorskip("mediapipe")
    try:
        mp.Image(image_format=mp.ImageFormat.SRGB, data=np.zeros((4, 4, 3), np.uint8))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"MediaPipe cannot create an mp.Image on this host: {type(exc).__name__}: {exc}")


def test_avatar_flag_via_landmarker_path(settings, tmp_path):
    _mediapipe_image_or_skip()
    sq = save(photo(9, (512, 512)), tmp_path / "avatar.png")
    ctx = _ctx(settings, tmp_path)
    ctx.models["mp_face_landmarker"] = _FakeLandmarker((0.377, 0.478), (0.623, 0.471))
    k = _by_kind(prov(_inc(artifact(sq, "landing_asset")), ctx).genes)
    flag = k["synth_avatar_eyes"][0]
    assert flag.family == "ID" and flag.quality <= 0.3 and "nie dowód" in flag.evidence.note
    assert k["avatar_geometry_check"][0].value == "flagged" and level(flag) == "none"
    ctx2 = _ctx(settings, tmp_path)
    ctx2.models["mp_face_landmarker"] = _FakeLandmarker((0.30, 0.42), (0.58, 0.40))
    k2 = _by_kind(prov(_inc(artifact(sq, "landing_asset")), ctx2).genes)
    assert "synth_avatar_eyes" not in k2 and k2["avatar_geometry_check"][0].value == "not_flagged"


def test_vectorised_decode_matches_upstream_loop_rule():
    import cv2
    import pywt

    from hydra.vendor.imwatermark import EmbedMaxDct

    e = EmbedMaxDct(wm_len=48)
    ca, _ = pywt.dwt2(cv2.cvtColor(encode_bits(_bgr(21, (256, 256)), SDXL_BITS), cv2.COLOR_BGR2YUV)[:, :, 1], "haar")
    loop: list[list[int]] = [[] for _ in range(48)]
    n = 0
    for i in range(ca.shape[0] // 4):
        for j in range(ca.shape[1] // 4):
            loop[n % 48].append(e.infer_dct_matrix(ca[i * 4:i * 4 + 4, j * 4:j * 4 + 4], 36))
            n += 1
    assert e.decode_frame(ca, 36, [[] for _ in range(48)]) == loop
