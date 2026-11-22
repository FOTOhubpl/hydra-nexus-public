"""GALLERY: reference base (faces, logos) built from the operator's own imported material. Synthetic only: random
128-d face vectors stand in for SFace output (no real faces), a drawn "XQZ" mark stands in for a brand logo."""

from __future__ import annotations

import json

import numpy as np
import pytest

from hydra import reference_build as rb
from hydra.contracts import EvidencePointer, Family, Gene
from hydra.genome import ident, logo
from hydra.genome.base import ExtractContext, ExtractResult, Vector

from .helpers import artifact, incident, photo, save
from .test_ident_logo import _logo_img


@pytest.fixture(autouse=True)
def _no_env_ref(monkeypatch):
    monkeypatch.delenv("HYDRA_VIS_REFERENCE_DIR", raising=False)


def _unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=128).astype(np.float32)
    return v / np.linalg.norm(v)


PEOPLE = {"Anna Testowa": _unit(1), "Jan Syntetyczny": _unit(2)}


def _fake_ident(inc, ctx):
    """Per incident: one gated face named by the material (alt) -> what ident@ emits for a material-named face."""
    res = ExtractResult()
    for k, a in enumerate(inc.artifacts):
        name = a.meta.path_in_incident.split("/")[-1].split(".")[0].replace("_", " ")
        if name not in PEOPLE:
            continue
        key = f"{inc.incident.id}|{a.meta.artifact_id}|face|{k}"
        v = PEOPLE[name] + 0.05 * _unit(100 + k + len(inc.incident.id))
        res.vectors.append(Vector(key=key, model="sface", data=(v / np.linalg.norm(v)).astype(np.float32)))
        res.genes.append(Gene(incident_id=inc.incident.id, family=Family.VIS, kind="face", vector_ref=key,
                              quality=0.9, extractor="ident@1",
                              evidence=EvidencePointer(artifact_id=a.meta.artifact_id, note="det=0.95; gated=1")))
        res.genes.append(Gene(incident_id=inc.incident.id, family=Family.VIS, kind="person_named", value=name,
                              extractor="ident@1", evidence=EvidencePointer(
                                  artifact_id=a.meta.artifact_id, note=f"alt={name}; face={key}; faces=1")))
    return res


def _corpus(tmp_path):
    """Two incidents: each a landing page with a named face photo and the same 'logo XQZ News' mark."""
    root = tmp_path / "corpus"
    logo_png = None
    for i, person in enumerate(["Anna_Testowa", "Anna_Testowa", "Jan_Syntetyczny"]):
        inc = root / f"{i + 1}"
        assets = inc / "index_files"
        save(photo(20 + i), assets / f"{person}.png")
        logo_png = save(_logo_img(), assets / "mark.png")
        (inc / "index.html").write_text(
            f'<html><body><img src="index_files/{person}.png" alt="{person.replace("_", " ")}">'
            '<img src="index_files/mark.png" alt="logo XQZ News"></body></html>', encoding="utf-8")
    return root, logo_png


def test_build_from_folder_then_status_then_loaders(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ident, "ident", _fake_ident)
    (settings.data_dir / "nask" / "reference").mkdir(parents=True)
    (settings.data_dir / "nask" / "reference" / "persons.json").write_text(
        json.dumps({"public_figures": ["Anna Testowa"]}))
    root, _ = _corpus(tmp_path)
    fp0 = rb.fingerprint(settings)
    res = rb.build(settings, root)
    assert res["ok"] and res["incidents"] == 3
    assert res["faces"]["entries"] == 2 and res["faces"]["public_figures"] == 1
    assert res["logos"]["templates"] == 1 and res["logos"]["brands"] == ["XQZ News"]
    assert "przelicz analizę" in res["message_pl"]
    ref = settings.state_dir / "reference"
    assert rb.reference_dir(settings) == ref and (ref / "faces" / "gallery.json").is_file()
    g = json.loads((ref / "faces" / "gallery.json").read_text())
    roles = {e["name"]: e["role"] for e in g["entries"]}
    assert roles == {"Anna Testowa": "public_figure", "Jan Syntetyczny": "persona"}  # roles only from persons.json
    st = rb.status(settings)
    assert st["face_entries"] == 2 and st["logo_templates"] == 1
    assert st["references"]["face_gallery"] and st["references"]["logo_templates"]
    assert not any("galeria twarzy" in n or "wzorce logo" in n for n in st["references"]["notes_pl"])
    assert "vectors" not in json.dumps(st)  # status never exposes biometrics
    assert rb.fingerprint(settings) != fp0

    # re-analyse path: the extractors' own loaders find the built base in <state>/reference
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w")
    gal = ident.load_gallery(ctx)
    i, sim = gal.match(PEOPLE["Anna Testowa"])
    assert gal.names[i] == "Anna Testowa" and sim > 0.9
    scene = photo(9, (900, 600))
    scene.paste(_logo_img().convert("RGB").resize((300, 120)), (520, 40))
    out = logo.logo(incident("t", [artifact(save(scene, tmp_path / "ad.png"), "ad_creative")]), ctx)
    assert [gn.value for gn in out.genes if "source=orb" in (gn.evidence.note or "")] == ["XQZ News"]


def test_logo_needs_two_incidents(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(ident, "ident", _fake_ident)
    root, _ = _corpus(tmp_path)
    for sub in ("2", "3"):
        (root / sub / "index_files" / "mark.png").unlink()
    res = rb.build_logos(settings, root)
    assert res["templates"] == 0 and "nie znaleziono" in res["message_pl"]


def test_empty_corpus_polish_message(settings, tmp_path):
    (tmp_path / "empty").mkdir()
    res = rb.build(settings, tmp_path / "empty")
    assert res["ok"] is False and res["message_pl"].startswith("Brak materiału")
    res = rb.build(settings, None)  # empty state: no DB
    assert res["ok"] is False and "zaimportuj" in res["message_pl"]


def test_reference_fingerprint_keys_only_ident_and_logo(settings, tmp_path, monkeypatch):
    from hydra import pipeline
    from hydra.genome import base

    base.load_family_modules()
    inc = incident("t", [artifact(save(photo(1), tmp_path / "a.png"), "ad_creative")]).incident
    names = [n for n in ("ident", "logo", "vis") if n in base.REGISTRY]
    v0 = pipeline.extract_versions(settings, names, None)
    fp0 = {n: pipeline._extractor_fp(inc, n, v0) for n in names}
    monkeypatch.setattr(ident, "ident", _fake_ident)
    rb.build(settings, _corpus(tmp_path)[0])
    v1 = pipeline.extract_versions(settings, names, None)
    fp1 = {n: pipeline._extractor_fp(inc, n, v1) for n in names}
    assert fp0["ident"] != fp1["ident"] and fp0["logo"] != fp1["logo"]
    if "vis" in names:
        assert fp0["vis"] == fp1["vis"]


def test_build_route_is_admin_only():
    from hydra.api_auth import required_role

    assert required_role("POST", "/v1/reference/build") == "admin"
    assert required_role("GET", "/v1/reference/status") == "viewer"
    assert required_role("POST", "/v1/reference/reanalyse") == "analyst"
