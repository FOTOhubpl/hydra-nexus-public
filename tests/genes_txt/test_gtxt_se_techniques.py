"""G12 named social-engineering techniques and the funds-recovery scheme (synthetic Polish pages, invented text)."""

from __future__ import annotations

import hashlib

from hydra.contracts import ArtifactRole, EvidencePointer, Family, Gene, Hypothesis, Incident, IncidentArtifact
from hydra.entropy import build_rarity
from hydra.genome import nar as _nar  # noqa: F401
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident, run_all
from hydra.genome.lexicons import SE_TECHNIQUES
from hydra.genome.ocr_pool import OcrPool
from hydra.synapse import build_genome, level, score_pair

LURE = """<html lang="pl"><head><title>Pilne: wywiad usunięty</title></head><body>
<h1>Znany biznesmen poleca platformę. Wywiad usunięty po emisji, zobacz zanim usuną</h1>
<p>Platforma pod nadzorem KNF. Lekarze w szoku, banki są wściekłe. Tylko dziś: liczba pozostałych miejsc 7.</p>
<p>Tysiące Polaków już zarabia. Bonus powitalny w prezencie. Nie czekaj, działaj teraz, bo stracisz szansę.</p>
<p>Odpowiedz 5 min temu. Lubię to.</p></body></html>"""

RECOVERY = """<html lang="pl"><head><title>Kancelaria zwrotów</title></head><body>
<h1>Straciłeś pieniądze na fałszywej platformie? Odzyskamy Twoje środki!</h1>
<p>Nasza kancelaria pomogła wielu osobom, które padły ofiarą oszustów. Zwrot środków w 30 dni.</p>
</body></html>"""

PLAIN = """<html lang="pl"><head><title>Przepis na sernik</title></head><body>
<h1>Sernik babci</h1><p>Ser twarogowy, jajka, cukier i masło wymieszać, piec w 170 stopniach.</p></body></html>"""


def _inc(tmp_path, iid: str, html: str) -> ResolvedIncident:
    data = html.encode()
    p = tmp_path / f"{iid}.html"
    p.write_bytes(data)
    a = ResolvedArtifact(IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=ArtifactRole.LANDING_HTML,
                                          path_in_incident=f"{iid}/page.html", mime="text/html", size=len(data)), p)
    return ResolvedIncident(Incident(id=iid, source="t", source_ref=iid, artifacts=[a.meta]), [a])


def _nar_genes(inc, settings, tmp_path):
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(None, engine_fn=lambda im: [])})
    return run_all(inc, ctx, only=["nar"]).genes


def test_every_technique_gene_has_char_range_evidence(settings, tmp_path):
    genes = _nar_genes(_inc(tmp_path, "lure", LURE), settings, tmp_path)
    tech = {g.value: g for g in genes if g.kind == "se_technique"}
    assert set(tech) == set(SE_TECHNIQUES)  # all seven named techniques found in the synthetic lure
    for g in tech.values():
        assert g.family == Family.NAR and g.dependency_group == "NAR:kit:se_technique"
        assert g.evidence.char_range is not None and g.evidence.char_range[1] > g.evidence.char_range[0]
        assert g.evidence.artifact_id and "sources:" in (g.evidence.note or "")
        assert level(g) == "kit"
    assert "presja czasu" in tech["urgency"].evidence.note


def test_plain_text_has_no_techniques(settings, tmp_path):
    genes = _nar_genes(_inc(tmp_path, "plain", PLAIN), settings, tmp_path)
    assert not [g for g in genes if g.kind == "se_technique"]


def test_recovery_text_is_the_funds_recovery_scheme(settings, tmp_path):
    genes = _nar_genes(_inc(tmp_path, "rec", RECOVERY), settings, tmp_path)
    schemes = sorted((g for g in genes if g.kind == "scheme"), key=lambda g: -g.quality)
    assert schemes and schemes[0].value == "funds_recovery"
    assert "recover_promise" in schemes[0].evidence.note
    assert "authority" in {g.value for g in genes if g.kind == "se_technique"}  # kancelaria


def _tracker(iid: str) -> Gene:
    return Gene(incident_id=iid, family=Family.INF, kind="tracker_id", value="fb_pixel:900000000000001",
                dependency_group="inf:operator:tracker", evidence=EvidencePointer(note="synthetic"), extractor="t@1")


def test_nar_techniques_add_no_campaign_bits(settings, tmp_path):
    """Regression: hypotheses and campaign bits identical with and without the se_technique genes."""
    pages = {"a": LURE, "b": LURE.replace("biznesmen", "aktor"), "c": RECOVERY, "d": PLAIN,
             "e": PLAIN.replace("Sernik", "Szarlotka"), "f": RECOVERY.replace("30", "14")}
    after = {iid: _nar_genes(_inc(tmp_path, iid, html), settings, tmp_path) for iid, html in pages.items()}
    assert any(g.kind == "se_technique" for g in after["a"])
    for iid in ("a", "b"):  # a/b also share an operator id: same_campaign must stay
        after[iid] = after[iid] + [_tracker(iid)]
    before = {iid: [g for g in gs if g.kind != "se_technique"] for iid, gs in after.items()}
    results = {}
    for label, genes in (("before", before), ("after", after)):
        rarity = build_rarity(genes, {})
        gen = {iid: build_genome(iid, g, {}) for iid, g in genes.items()}
        results[label] = {(x, y): score_pair(gen[x], gen[y], rarity) for x in pages for y in pages if x < y}
    for pair, pe in results["after"].items():
        pb = results["before"][pair]
        assert pe.score.hypothesis == pb.score.hypothesis, pair
        assert pe.campaign_bits == pb.campaign_bits, pair
        assert pe.campaign_by_family.get("NAR", 0.0) == 0.0
    assert results["after"][("a", "b")].score.hypothesis == Hypothesis.SAME_CAMPAIGN
