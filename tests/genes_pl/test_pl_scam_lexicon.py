"""PL-SCAM public-source lexicons: integrity of the JSON assets and scheme detection on synthetic Polish text."""

from __future__ import annotations

import hashlib
import re

import pytest

from hydra.contracts import ArtifactRole, Incident, IncidentArtifact
from hydra.genome import nar as _nar  # noqa: F401
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident, run_all
from hydra.genome.lexicons import (
    INSTITUTION_BRANDS,
    MOTIFS,
    SCHEME_INFO,
    SCHEMES,
    UPPER_ONLY_BRANDS,
    compile_terms,
    public,
)
from hydra.genome.lexicons.persons import PERSON_SOURCES, PERSONS
from hydra.genome.ocr_pool import OcrPool
from hydra.genome.txt_entities import find_brands, find_persons
from hydra.synapse import gene_tier

NEW_SCHEMES = {"marketplace_phishing", "parcel_fee", "gov_refund", "bank_impersonation", "fake_shop",
               "account_takeover", "loan", "fake_charity"}


def test_every_public_term_has_a_resolvable_source_and_compiles():
    data = public.load("pl_scam_schemes")
    src = data["sources"]
    assert all(s["url"].startswith("https://") for s in src.values())
    for t in data["terms"]:
        assert t["source"] in src, t["id"]
        assert t["patterns"] and all(p == p.lower() for p in t["patterns"]), t["id"]
        assert not re.search(r"[ąćęłńóśźż]", " ".join(t["patterns"])), f"patterns must be folded: {t['id']}"
        for p in t["patterns"]:
            re.compile(p)
    assert {s["id"] for s in data["schemes"]} == NEW_SCHEMES
    for b in public.brands().values():
        assert all(s in src or s == "prior" for s in b["sources"]), b["id"]


def test_public_terms_are_merged_with_origin_and_source():
    assert NEW_SCHEMES <= set(SCHEMES) and NEW_SCHEMES <= set(SCHEME_INFO)
    fee = next(t for t in SCHEMES["parcel_fee"] if t.id == "fee")
    assert fee.origin == "public" and fee.source == "cert_2023"
    assert any(t.id == "state_company_investment" for t in MOTIFS)
    ids = {t.id for t in INSTITUTION_BRANDS}
    assert {"olx", "allegro", "vinted", "poczta_polska", "przelewy24", "santander"} <= ids
    assert {"dpd", "dhl", "olx", "nfz"} <= UPPER_ONLY_BRANDS
    compile_terms(tuple(t for v in SCHEMES.values() for t in v))


def test_existing_material_terms_are_not_widened():
    """Public terms never append patterns to an existing term id (material-confirmed terms stay as they were)."""
    inv = {t.id: t for t in SCHEMES["investment"]}
    assert all(t.origin != "public" for t in inv.values())


def test_documented_figures_carry_sources():
    names = {p.canonical: p for p in PERSONS}
    assert names["Wojciech Cejrowski"].origin == "documented"
    assert names["Jerzy Owsiak"].origin == "prior"  # already listed: keeps its entry, gains sources
    for who in ("Wojciech Cejrowski", "Jerzy Owsiak"):
        assert PERSON_SOURCES[who] and all(s["url"].startswith("https://") for s in PERSON_SOURCES[who])
    hits = find_persons("Wojciech Cejrowski poleca nową platformę. Recenzja Jerzego Owsiaka na YouTube.")
    assert {e.value for e in hits if e.kind == "person_named"} >= {"Wojciech Cejrowski", "Jerzy Owsiak"}


def test_short_brands_need_capitals():
    assert {e.value for e in find_brands("Twoja paczka DPD czeka, dopłata w OLX")} >= {"dpd", "olx"}
    assert "dpd" not in {e.value for e in find_brands("dpd")}


PAGES = {
    "marketplace_phishing": "Kupujący opłacił Twój przedmiot na OLX. Odbierz środki: kliknij, wybrał sposób dostawy "
                            "InPost. Podaj numer karty, data ważności, kod CVV2. Bezpieczna płatność OLX.",
    "parcel_fee": "InPost: Twoja paczka została wstrzymana z powodu niepełnego adresu. Dopłata 1,99 zł przez "
                  "bramkę płatności Przelewy24, inaczej przesyłka zostanie zwrócona do nadawcy.",
    "gov_refund": "e-Urząd Skarbowy: przysługuje Ci zwrot nadpłaty podatku 1 248 zł. Podaj numer PESEL i numer "
                  "dowodu osobistego, aby otrzymać zwrot środków.",
    "bank_impersonation": "PKO BP: wykryliśmy podejrzaną transakcję, Twoje konto zostało zablokowane. Przelej "
                          "środki na bezpieczne konto i zainstaluj AnyDesk, pracownik banku pomoże.",
    "fake_shop": "Likwidacja sklepu! Wszystko przecenione o 80%. Darmowa dostawa, Gwarancja zwrotu pieniędzy, "
                 "Bezpieczne zakupy. Dodaj do koszyka, Anna z Krakowa właśnie kupiła kurtkę.",
    "fake_charity": "Pilna zbiórka dla powodzian! WOŚP prosi o wsparcie, przekaż darowiznę przez eSkarbonkę.",
}


def _inc(tmp_path, text: str):
    data = f"<html lang='pl'><body><h1>Informacja</h1><p>{text}</p></body></html>".encode()
    p = tmp_path / "page.html"
    p.write_bytes(data)
    a = ResolvedArtifact(IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=ArtifactRole.LANDING_HTML,
                                          path_in_incident="page.html", mime="text/html", size=len(data)), p)
    return ResolvedIncident(Incident(id="pl1", source="t", source_ref="t", artifacts=[a.meta]), [a])


@pytest.mark.parametrize("scheme", sorted(PAGES))
def test_scheme_detected_on_synthetic_page(scheme, settings, tmp_path):
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(None, engine_fn=lambda im: [])})
    genes = run_all(_inc(tmp_path, PAGES[scheme]), ctx, only=["nar"]).genes
    schemes = {g.value for g in genes if g.kind == "scheme"}
    assert scheme in schemes, (scheme, schemes)
    g = next(g for g in genes if g.kind == "scheme" and g.value == scheme)
    assert g.family == "NAR" and gene_tier(g) == "kit"  # style evidence, never a campaign link


def test_investment_page_does_not_turn_into_bank_scheme(settings, tmp_path):
    text = ("Platforma inwestycyjna AI: minimalna wpłata 950 zł, zarabiaj 3 000 zł miesięcznie. Inwestycja w "
            "Baltic Pipe i akcje Orlenu. Doradca skontaktuje się z Tobą.")
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(None, engine_fn=lambda im: [])})
    genes = run_all(_inc(tmp_path, text), ctx, only=["nar"]).genes
    schemes = {g.value for g in genes if g.kind == "scheme"}
    assert "investment" in schemes and not schemes & {"bank_impersonation", "fake_shop", "parcel_fee"}
    motif = next(g for g in genes if g.kind == "motif" and g.value == "state_company_investment")
    assert "public:csirt_knf_2025" in motif.evidence.note
