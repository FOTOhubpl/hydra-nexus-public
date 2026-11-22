from __future__ import annotations

import json

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from hydra.genome import ident, logo
from hydra.genome.base import ExtractContext

from .helpers import artifact, incident, photo, save


def test_names_in_text_needs_a_known_first_name():
    assert ident.names_in_text("Marek Zieliński odwiedza Szpital Kliniczny") == ["Marek Zieliński"]
    assert ident.names_in_text("Zrzut ekranu Bank Pekao — przelew") == []
    assert ident.names_in_text("Miejsce zdarzenia na Alejach Ujazdowskich w Warszawie") == []
    assert ident.names_in_text("Agnieszka Nowak-Wrona i Donald Kwiatek") == ["Agnieszka Nowak-Wrona",
                                                                                  "Donald Kwiatek"]
    # bare surnames only through the reference base (public figures), also in ASCII file names
    sur = {"wróblewsk": "Adam Wróblewski", "kalinowsk": "Monika Kalinowska"}
    assert ident.names_in_text("03 wroblewsk", sur) == ["Adam Wróblewski"]
    assert ident.names_in_text("Kadr — Wróblewski wstaje, Kalinowska próbuje", sur) == ["Adam Wróblewski",
                                                                                     "Monika Kalinowska"]
    assert ident.names_in_text("Monika Kalinowska w studio", sur) == ["Monika Kalinowska"]


def test_alt_text_map_and_asset_keys(tmp_path):
    html = tmp_path / "page.html"
    html.write_text('<html><body><img src="./Page_files/02_persona_.webp" alt="Monika Kalinowska w studio">'
                    '<figure><img src="ava/M1.png"><figcaption>Jan Nowak, ekspert</figcaption></figure>'
                    '<img src="x.png" alt=""></body></html>', encoding="utf-8")
    inc = incident("t", [artifact(html, "landing_html")])
    m = ident.alt_text_map(inc)
    assert m["02_persona_.webp"] == ["Monika Kalinowska w studio"]
    assert ident._asset_key("resources/image/M1_7d8e9f00.png") == "m1.png"
    assert m["m1.png"] == ["Jan Nowak, ekspert"]
    assert "x.png" not in m


def test_face_quality_gate():
    q, gated = ident.face_quality(0.93, 150, 0.1, 400)
    assert gated and q > 0.8
    q2, gated2 = ident.face_quality(0.7, 30, 0.6, 10)
    assert not gated2 and q2 < q


def _face(vec, t, q=0.5):
    from hydra.genome.vis_media import VisualUnit

    u = VisualUnit(uid=f"u:{t}", artifact=None, idx=1, kind="frame", image=Image.new("RGB", (8, 8)),  # type: ignore[arg-type]
                   scale=1.0, offset=(0, 0), source_size=(8, 8), image_class="frame", name="v", timecode_s=t)
    v = np.asarray(vec, np.float32)
    return ident.Face(u, (0, 0, 4, 4), 0.9, 100, 0.1, 100, v / np.linalg.norm(v), True, q)


def test_tracks_collapse_one_talking_head_but_not_two_people():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=128), rng.normal(size=128)
    faces = [_face(a + 0.2 * rng.normal(size=128), t) for t in range(10)]
    faces += [_face(b + 0.2 * rng.normal(size=128), t + 0.5) for t in range(5)]
    tracks = ident.build_tracks(faces)
    assert sorted(len(t.faces) for t in tracks) == [5, 10]
    assert abs(float(np.linalg.norm(tracks[0].vector())) - 1) < 1e-5


def test_gallery_match_and_aliases(settings, tmp_path, monkeypatch):
    rng = np.random.default_rng(1)
    v1, v2 = rng.normal(size=128), rng.normal(size=128)
    ref = tmp_path / "ref"
    (ref / "faces").mkdir(parents=True)
    (ref / "faces" / "gallery.json").write_text(json.dumps({"entries": [
        {"name": "Anna Testowa", "role": "public_figure", "surname_stems": ["testow"], "vectors": [v1.tolist()]},
        {"name": "Ewa Persona", "role": "persona", "aliases": ["Ewa Persona", "Ola Persona"],
         "vectors": [v2.tolist()]}]}))
    monkeypatch.setenv("HYDRA_VIS_REFERENCE_DIR", str(ref))
    ctx = ExtractContext(settings=settings, work_dir=tmp_path)
    g = ident.load_gallery(ctx)
    assert g is not None and g.surnames == {"testow": "Anna Testowa"}
    i, sim = g.match((v1 / np.linalg.norm(v1)).astype(np.float32))
    assert g.names[i] == "Anna Testowa" and sim > 0.99
    assert g.aliases[1] == ["Ewa Persona", "Ola Persona"]


def test_ident_without_models_is_unavailable_not_a_crash(settings, tmp_path):
    inc = incident("t", [artifact(save(photo(1), tmp_path / "a.png"), "ad_creative")])
    res = ident.ident(inc, ExtractContext(settings=settings, work_dir=tmp_path / "w"))
    assert [(g.kind, g.status) for g in res.genes] == [("face", "unavailable")]


def _logo_img() -> Image.Image:
    im = Image.new("L", (400, 160), 255)
    d = ImageDraw.Draw(im)
    # Pillow's bundled scalable font (Aileron, Pillow >= 10.1), identical on macOS and Linux. A system font path
    # (macOS Arial Bold) made the template platform-dependent: on Linux the fallback was the tiny bitmap font,
    # leaving too few ORB keypoints on the text for the >= 15-inlier gate.
    font = ImageFont.load_default(size=90)
    d.rectangle((5, 5, 150, 150), fill=0)
    d.ellipse((30, 30, 125, 125), fill=255)
    d.polygon([(60, 40), (120, 78), (60, 116)], fill=0)
    d.text((165, 25), "XQZ", fill=0, font=font)
    for k in range(0, 400, 37):
        d.line((k, 152, k + 20, 158), fill=0, width=3)
    return im


def test_logo_orb_template_found_with_bbox(settings, tmp_path, monkeypatch):
    ref = tmp_path / "ref" / "logos"
    ref.mkdir(parents=True)
    _logo_img().save(ref / "xqz.png")
    (ref / "logos.json").write_text(json.dumps({"templates": {"xqz.png": {"brand": "XQZ News", "kind": "media"}}}))
    monkeypatch.setenv("HYDRA_VIS_REFERENCE_DIR", str(tmp_path / "ref"))
    scene = photo(9, (900, 600))
    scene.paste(_logo_img().convert("RGB").resize((300, 120)), (520, 40))
    arts = [artifact(save(scene, tmp_path / "ad.png"), "ad_creative"),
            artifact(save(photo(10, (900, 600)), tmp_path / "other.png"), "ad_creative")]
    svg = tmp_path / "logo-gazeta.svg"
    svg.write_text("<svg/>")
    arts.append(artifact(svg, "landing_asset", "image/svg+xml"))
    res = logo.logo(incident("t", arts), ExtractContext(settings=settings, work_dir=tmp_path / "w"))
    orb = [g for g in res.genes if g.value == "XQZ News"]
    assert len(orb) == 1 and orb[0].evidence.artifact_id == arts[0].meta.artifact_id
    x, y, w, h = orb[0].evidence.bbox
    assert abs(x - 520) < 40 and abs(y - 40) < 40 and abs(w - 300) < 60
    assert orb[0].dependency_group == "vis_brand"
    fn = [g for g in res.genes if g.value == "Gazeta.pl"]
    assert fn and "filename" in fn[0].evidence.note


def test_brand_lexicon_boundaries():
    assert [b for b, _ in logo.brands_in_text("tvn24_logo")] == ["TVN24"]
    assert {b for b, _ in logo.brands_in_text("shot-pekao")} == {"Bank Pekao"}
    assert logo.brands_in_text("training") == []
    assert ("Google", "platform") in logo.brands_in_text("googlelogo_color_68x28dp")
    assert logo._group("platform") == "vis_boilerplate"
