"""PL-SCAM domain-naming genes, KNF / MF warning-list labels and the snapshot parsers (synthetic inputs only)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from hydra.contracts import ArtifactRole, Incident, IncidentArtifact
from hydra.enrich import public_warnings
from hydra.genome import domain_pl
from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident, run_all
from hydra.genome.ocr_pool import OcrPool
from hydra.synapse import gene_tier, level

ROOT = Path(__file__).resolve().parents[2]


def _fetcher():
    path = ROOT / "scripts" / "fetch_public_warnings.py"
    spec = importlib.util.spec_from_file_location("fetch_public_warnings", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    """Tiny KNF / MF snapshots produced by the real parsers from synthetic raw responses."""
    f = _fetcher()
    knf_html = ("<h2>Zawiadomienia o podejrzeniu popełnienia przestępstwa z art. 178 ustawy o obrocie instrumentami "
                "finansowymi</h2><table><thead><tr><th>Lp.</th><th>Nazwa</th></tr></thead><tbody>"
                "<tr><td>1<!-- -->.</td><td>Przykład Capital Ltd, platforma www.przyklad-trade.example.com oraz "
                "https://inna-platforma.top/</td><td>brak danych</td><td>Prokuratura Okręgowa</td><td>-</td>"
                "<td>Zawiadomienie złożone przez UKNF.</td></tr>"
                "<tr><td>2.</td><td>Jan K. (osoba fizyczna) sp. z o.o. S.A., źródło beta.companieshouse.gov.uk</td>"
                "<td>brak</td><td>-</td><td>-</td><td>-</td></tr></tbody></table>").encode()
    mf_xml = (b'<?xml version="1.0" encoding="utf-8"?><Rejestr xmlns="http://www.hazard.mf.gov.pl/2017/03/21/">'
              b'<PozycjaRejestru Lp="7"><AdresDomeny>www.kasyno-przyklad.sbs</AdresDomeny>'
              b'<DataWpisu>2026-11-20T13:56:55</DataWpisu></PozycjaRejestru></Rejestr>')
    knf_raw, mf_raw = tmp_path / "knf.html", tmp_path / "mf.xml"
    knf_raw.write_bytes(knf_html)
    mf_raw.write_bytes(mf_xml)
    out = tmp_path / "external"
    assert f.main(["--out", str(out), "--from-file", f"knf={knf_raw}", "--from-file", f"mf={mf_raw}"]) == 0
    monkeypatch.setenv("HYDRA_KNF_DIR", str(out / "knf"))
    monkeypatch.setenv("HYDRA_MF_HAZARD_DIR", str(out / "mf_hazard"))
    public_warnings._knf.cache_clear()
    public_warnings._mf.cache_clear()
    yield out
    public_warnings._knf.cache_clear()
    public_warnings._mf.cache_clear()


def test_parsers_extract_domains_and_skip_references(snapshots):
    knf = json.loads((snapshots / "knf" / "knf_warnings.json").read_text())
    assert knf[0]["domains"] == ["przyklad-trade.example.com", "inna-platforma.top"]
    assert knf[0]["category"].startswith("Zawiadomienia") and knf[0]["lp"] == 1
    assert knf[1]["domains"] == []  # "z o.o", "S.A." and a cited government register are not domains
    meta = json.loads((snapshots / "knf" / "SNAPSHOT.json").read_text())
    assert meta["entries"] == 2 and len(meta["sha256"]) == 64 and meta["domains"] == 2
    mf = json.loads((snapshots / "mf_hazard" / "register.json").read_text())
    assert mf == [{"lp": 7, "domain": "www.kasyno-przyklad.sbs", "date_added": "2026-11-20T13:56:55"}]


def test_lookup_levels_and_missing_snapshot(snapshots, monkeypatch):
    assert public_warnings.available() == {"knf": True, "mf_hazard": True}
    h = public_warnings.lookup("https://www.inna-platforma.top/rejestracja")
    assert h["knf"][0]["level"] == "host"
    assert public_warnings.lookup("login.inna-platforma.top")["knf"][0]["level"] == "registrable"
    assert public_warnings.lookup("kasyno-przyklad.sbs")["mf_hazard"]["level"] == "host"
    assert public_warnings.lookup("example.org") == {"knf": [], "mf_hazard": None, "knf_snapshot": h["knf_snapshot"],
                                                     "mf_snapshot": h["mf_snapshot"]}
    monkeypatch.setenv("HYDRA_KNF_DIR", str(snapshots / "nothing"))
    public_warnings._knf.cache_clear()
    assert public_warnings.available()["knf"] is False  # not checked, never "not listed"
    assert public_warnings.lookup("inna-platforma.top")["knf"] == []


@pytest.mark.parametrize("host,brand,shapes", [
    ("allegrolokalnie.pl-oferta5815015105198474.cfd", "allegro",
     {"brand_subdomain_foreign", "pl_prefix", "word_longdigits"}),  # cert.pl/posts/2025/09/analiza-adresow-stron/
    ("olx-dostawa-pl.sbs", "olx", {"pl_prefix", "brand_service"}),
    ("www-account-booking.com", "booking", {"embedded_tld"}),
    ("ogloszezenie-olx.click", "olx", {"brand_service", "brand_typo"}),
])
def test_lookalike_shapes(host, brand, shapes):
    f = domain_pl.analyse_host(host)
    assert brand in {b for b, _ in f.brands}
    assert shapes <= set(f.shapes)


@pytest.mark.parametrize("host", ["allegro.pl", "pkobp.pl", "www.olx.pl", "sklep.example.pl", "xpkoyy.shop",
                                  "spokojny-dom.pl"])
def test_official_and_unrelated_hosts_are_not_lookalikes(host):
    assert domain_pl.analyse_host(host).brands == []


def test_tiers_never_make_campaign_links():
    genes = domain_pl.domain_genes("x", {"inpost-paczka24.cyou": ("landing", None)})
    kinds = {g.kind: g for g in genes}
    assert {"pl_domain_brand_lookalike", "pl_domain_template", "pl_domain_tld_risk", "pl_domain_shape",
            "pl_domain_service_word"} <= set(kinds)
    assert gene_tier(kinds["pl_domain_brand_lookalike"]) == "kit" and level(kinds["pl_domain_template"]) == "kit"
    for k in ("pl_domain_tld_risk", "pl_domain_shape", "pl_domain_service_word"):
        assert level(kinds[k]) == "none", k
    assert "log2 ratio 6.84" in kinds["pl_domain_tld_risk"].evidence.note


def test_extractor_on_synthetic_landing(snapshots, settings, tmp_path):
    html = ("<html lang='pl'><body><h1>Odbierz środki</h1><p>Przejdź na olx-dostawa-pl.sbs lub "
            "www.inna-platforma.top oraz kasyno-przyklad.sbs</p></body></html>").encode()
    p = tmp_path / "page.html"
    p.write_bytes(html)
    a = ResolvedArtifact(IncidentArtifact(artifact_id=hashlib.sha256(html).hexdigest(), role=ArtifactRole.LANDING_HTML,
                                          path_in_incident="page.html", mime="text/html", size=len(html)), p)
    inc = ResolvedIncident(Incident(id="d1", source="t", source_ref="t", artifacts=[a.meta]), [a])
    ctx = ExtractContext(settings=settings, work_dir=tmp_path / "w",
                         models={"ocr_pool": OcrPool(None, engine_fn=lambda im: [])})
    genes = run_all(inc, ctx, only=["inf_domain_pl"]).genes
    by = {(g.kind, g.value): g for g in genes}
    assert ("pl_domain_brand_lookalike", "olx") in by
    knf = by[("knf_warning_listed", "inna-platforma.top")]
    assert level(knf) == "none" and "Lp. 1" in knf.evidence.note and "Jan" not in knf.evidence.note
    assert level(by[("mf_hazard_listed", "kasyno-przyklad.sbs")]) == "none"
    assert all(g.family == "INF" and g.extractor == "inf_domain_pl@2" for g in genes)
