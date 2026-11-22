"""End-to-end TXT / NAR / META extractors on a synthetic incident (fake OCR engine, no organiser data)."""

from __future__ import annotations

import hashlib
import json

import pytest
from PIL import Image

from hydra.contracts import ArtifactRole, Incident, IncidentArtifact
from hydra.genome import meta as _meta  # noqa: F401  (registers extractors)
from hydra.genome import nar as _nar  # noqa: F401
from hydra.genome import txt as _txt  # noqa: F401
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident, run_all
from hydra.genome.ocr_pool import OcrPool
from hydra.ml.ocr import OcrLine

from .test_gtxt_sources import HTML

AD_LINES = [("Fikcyjny Doradca", (20, 10, 300, 40)), ("Sponsorowane", (20, 45, 200, 70)),
            ("PILNE: prezes aresztowany! Wywiad usunięty", (20, 300, 580, 360)),
            ("Dowiedz się więcej", (20, 600, 250, 630)), ("Wejdź na nova-kapital.top", (20, 640, 400, 670))]


def fake_engine(im):
    return [OcrLine(t, b, 0.95, "fake") for t, b in AD_LINES]


def _art(tmp_path, name, role, data: bytes, mime):
    p = tmp_path / name
    p.write_bytes(data)
    meta = IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=role, path_in_incident=name,
                            mime=mime, size=len(data))
    return ResolvedArtifact(meta, p)


@pytest.fixture
def incident(tmp_path):
    img = tmp_path / "_img.png"
    Image.new("RGB", (600, 700), (255, 255, 255)).save(img)
    info = {"id": "1234567890123456_2", "title": "Czytaj", "extractor": "facebook:ads", "playlist_id":
            "1234567890123456", "playlist_uploader": "Fikcyjny Doradca", "playlist_uploader_id": "99887766",
            "webpage_url": "https://www.facebook.com/ads/library/?id=1234567890123456", "epoch": 1773484200,
            "n_entries": 3, "description": "Program rządowy: zarabiaj 3 000 zł miesięcznie bez ryzyka"}
    arts = [
        _art(tmp_path, "Zrzut ekranu 2025-03-14 101500.png", ArtifactRole.AD_CREATIVE, img.read_bytes(), "image/png"),
        _art(tmp_path, "landing.html", ArtifactRole.LANDING_HTML, HTML.encode(), "text/html"),
        _art(tmp_path, "ad.info.json", ArtifactRole.PLATFORM_METADATA, json.dumps(info).encode(), "application/json"),
    ]
    inc = Incident(id="syn-1", source="test", source_ref="t", artifacts=[a.meta for a in arts])
    return ResolvedIncident(inc, arts)


def _run(incident, settings, tmp_path):
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "work",
                         models={"ocr_pool": OcrPool(settings.cache_dir, engine_fn=fake_engine)})
    names = ["txt_lines", "txt_phrases", "txt_entities", "txt_embed", "nar", "meta"]
    return run_all(incident, ctx, only=names)


def test_extractors_end_to_end(incident, settings, tmp_path):
    res = _run(incident, settings, tmp_path)
    g = {(x.family, x.kind, x.value) for x in res.genes}
    kinds = {(x.family, x.kind) for x in res.genes}
    assert not [x for x in res.genes if x.status == "failed"], [x for x in res.genes if x.status == "failed"]
    # TXT
    assert ("TXT", "headline", "prezes aresztowany po wywiadzie na zywo") in g
    assert ("TXT", "ocr_line", "pilne prezes aresztowany wywiad usuniety") in g
    assert ("TXT", "domain_in_text", "nova-kapital.top") in g
    assert ("TXT", "entity_price", "950 PLN") in g
    assert ("TXT", "entity_phone", "+48511222333") in g
    assert ("TXT", "lang_mismatch", "en->pl") in g
    assert {("TXT", "phrase"), ("TXT", "minhash_band"), ("TXT", "minhash_sig"), ("TXT", "charsig_page")} <= kinds
    assert ("TXT", "commenter_name", "jan kowalski") in g
    ocr = next(x for x in res.genes if x.kind == "ocr_line" and x.value.startswith("pilne"))
    assert ocr.evidence.bbox == (20.0, 300.0, 560.0, 60.0) and ocr.evidence.artifact_id
    # embeddings: models dir is empty in tests -> unavailable, never failed
    emb = [x for x in res.genes if x.kind.startswith("emb_")]
    assert emb and all(x.status == "unavailable" for x in emb)
    # NAR
    assert ("NAR", "scheme", "investment") in g
    assert ("NAR", "motif", "arrest") in g and ("NAR", "motif", "banned_interview") in g
    assert ("NAR", "cta", "learn_more") in g and ("NAR", "motif", "government_program") in g
    # META
    assert ("META", "platform_ad_id", "1234567890123456") in g
    assert ("META", "platform_page_name", "fikcyjny doradca") in g
    assert ("META", "capture_date", "2025-03-14") in g
    # vectors referenced by genes exist
    refs = {x.vector_ref for x in res.genes if x.vector_ref and x.status == "observed"}
    assert refs and refs <= {v.key for v in res.vectors}
    # discrete genes are unique per (kind, value)
    seen = [(x.kind, x.value) for x in res.genes if x.value]
    assert len(seen) == len(set(seen))


def test_ocr_is_cached_by_sha(incident, settings, tmp_path):
    calls = []

    def counting(im):
        calls.append(1)
        return fake_engine(im)

    from hydra.genome import txt_sources

    txt_sources._MEMO.clear()
    for _ in range(2):
        txt_sources._MEMO.clear()
        ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                             models={"ocr_pool": OcrPool(settings.cache_dir, engine_fn=counting)})
        txt_sources.collect(incident, ctx)
    assert len(calls) == 1
    assert list((settings.cache_dir / "ocr").rglob("*.json"))


def test_empty_incident_is_not_applicable(settings, tmp_path):
    inc = ResolvedIncident(Incident(id="empty", source="t", source_ref="t"), [])
    ctx = ExtractContext(settings=settings, work_dir=tmp_path)
    res = run_all(inc, ctx, only=["txt_lines", "nar", "meta"])
    assert [(x.kind, x.status) for x in res.genes] == [("text", "not_applicable")]


def test_broken_inputs_never_raise(settings, tmp_path):
    bad = [_art(tmp_path, "x.png", ArtifactRole.AD_CREATIVE, b"not an image", "image/png"),
           _art(tmp_path, "x.html", ArtifactRole.LANDING_HTML, b"\xff\xfe<<<", "text/html"),
           _art(tmp_path, "x.info.json", ArtifactRole.PLATFORM_METADATA, b"{broken", "application/json")]
    inc = ResolvedIncident(Incident(id="bad", source="t", source_ref="t", artifacts=[a.meta for a in bad]), bad)
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(settings.cache_dir, engine_fn=fake_engine)})
    res = run_all(inc, ctx, only=["txt_lines", "txt_phrases", "txt_entities", "nar", "meta"])
    assert all(x.extractor for x in res.genes)
    assert any(x.status == "failed" for x in res.genes)  # the broken image / json are reported, not raised


def test_dependency_groups_follow_tier_convention(incident, settings, tmp_path):
    res = _run(incident, settings, tmp_path)
    tiers = {"operator", "platform", "kit", "boilerplate", "asset", "persona", "phrase", "label"}
    for g in res.genes:
        fam, tier, name = g.dependency_group.split(":", 2)
        assert fam == g.family and tier in tiers and name
    by = {(g.kind, g.dependency_group.split(":")[1]) for g in res.genes}
    assert ("entity_phone", "operator") in by and ("commenter_name", "boilerplate") in by
    assert ("scheme", "kit") in by and ("platform_ad_id", "operator") in by
