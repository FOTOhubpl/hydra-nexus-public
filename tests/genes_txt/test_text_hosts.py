"""GAP-AUDIT B7: a host read by OCR from a screenshot links by domain to an incident with that landing host.
Synthetic inputs only (fake OCR engine)."""

from __future__ import annotations

import hashlib

from PIL import Image

from hydra.contracts import ArtifactRole, Incident, IncidentArtifact
from hydra.entropy import build_rarity
from hydra.genome import har as _har  # noqa: F401  (registers inf_url)
from hydra.genome import txt as _txt  # noqa: F401  (registers inf_text_hosts)
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident, run_all
from hydra.genome.ocr_pool import OcrPool
from hydra.ml.ocr import OcrLine
from hydra.synapse import build_genome, score_pair

LINES = {"ad": [("Sponsorowane", (20, 10, 200, 40)), ("Nowa platforma inwestycyjna dla Polaków", (20, 100, 580, 140)),
                ("Wejdź na nova-kapital-fikcja.top", (20, 300, 500, 340)), ("Czytaj w onet.pl", (20, 400, 300, 430)),
                ("Komunikat zus.pl", (20, 450, 300, 480))]}


def _fake_engine(im):
    return [OcrLine(t, b, 0.95, "fake") for t, b in LINES["ad"]]


def _art(root, rel, data: bytes, role, mime):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    meta = IncidentArtifact(artifact_id=hashlib.sha256(data + rel.encode()).hexdigest(), role=role,
                            path_in_incident=rel, mime=mime, size=len(data))
    return ResolvedArtifact(meta, p)


def _inc(iid, arts):
    return ResolvedIncident(Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta for a in arts]), arts)


def test_ocr_host_becomes_landing_host_and_links(settings, tmp_path):
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(settings.cache_dir, engine_fn=_fake_engine)})
    img = tmp_path / "ad.png"
    Image.new("RGB", (600, 700), (255, 255, 255)).save(img)
    shot = _inc("shot", [_art(tmp_path, "shot/ad.png", img.read_bytes(), ArtifactRole.AD_CREATIVE, "image/png")])
    genes = {"shot": run_all(shot, ctx, only=["inf_text_hosts"]).genes}
    lh = {g.value: g for g in genes["shot"] if g.kind == "landing_host"}
    assert set(lh) == {"nova-kapital-fikcja.top"}  # impersonated media / institution domains skipped
    g = lh["nova-kapital-fikcja.top"]
    assert g.family == "INF" and g.provenance == "INF" and g.quality <= 0.8 and g.evidence.bbox is not None
    assert "ocr" in g.evidence.note
    url = _inc("site", [_art(tmp_path, "site/url.txt", b"https://nova-kapital-fikcja.top/start\n", ArtifactRole.URL,
                             "text/plain")])
    genes["site"] = run_all(url, ctx, only=["inf_url"]).genes
    for k in range(10):
        f = _inc(f"f{k}", [_art(tmp_path, f"f{k}/url.txt", f"https://inna-strona-{k}.example.pl/\n".encode(),
                                ArtifactRole.URL, "text/plain")])
        genes[f"f{k}"] = run_all(f, ctx, only=["inf_url"]).genes
    rarity = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    pe = score_pair(gen["shot"], gen["site"], rarity)
    assert pe.campaign_bits >= 10 and pe.score.hypothesis == "same_campaign"
    assert any(r.kind == "landing_host" for r in pe.score.reasons)


def test_same_landing_genes_as_the_screenshot_branch_and_skip_when_own_landing(settings, tmp_path):
    """Screenshot mode follows the OCR URL as a URL input (inf_url); file mode must give the same landing genes."""
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(settings.cache_dir, engine_fn=_fake_engine)})
    img = tmp_path / "ad.png"
    Image.new("RGB", (600, 700), (255, 255, 255)).save(img)
    shot = _inc("shot2", [_art(tmp_path, "shot2/ad.png", img.read_bytes(), ArtifactRole.AD_CREATIVE, "image/png")])
    file_mode = {(g.kind, g.value) for g in run_all(shot, ctx, only=["inf_text_hosts"]).genes if g.value}
    url = _inc("url2", [_art(tmp_path, "url2/url.txt", b"https://nova-kapital-fikcja.top/\n", ArtifactRole.URL,
                             "text/plain")])
    shot_mode = {(g.kind, g.value) for g in run_all(url, ctx, only=["inf_url"]).genes if g.value}
    landing = {"landing_host", "registrable_domain", "entry_url"}
    assert {x for x in file_mode if x[0] in landing} == {x for x in shot_mode if x[0] in landing}
    both = _inc("both", shot.artifacts + url.artifacts)
    genes = run_all(both, ctx, only=["inf_text_hosts"]).genes
    assert [g.status for g in genes] == ["not_applicable"]  # own landing URL: OCR hosts stay TXT evidence


def test_ip_like_dotted_numbers_are_not_phones():
    from hydra.genome.txt_entities import find_phones

    assert find_phones("Serwer IP 51.164.145.168 działa") == []
    assert find_phones("adres 192.168.100.200") == []
    assert find_phones("164.145.168") == []
    assert [e.value for e in find_phones("tel. 600.123.456")] == ["+48600123456"]
    assert [e.value for e in find_phones("zadzwoń 600 123 456")] == ["+48600123456"]
