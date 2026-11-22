"""G13 lure-domain facts: brand mark off the brand's domains, registrar registration window (synthetic only)."""

from __future__ import annotations

from datetime import date, timedelta

from fixtures import _art, _synthetic_warninglists, web_incident  # noqa: F401
from test_inf_enrich import cache, certpl  # noqa: F401

from hydra.contracts import GeneStatus, Incident
from hydra.enrich import Enricher, client
from hydra.entropy import build_rarity
from hydra.genome import base
from hydra.genome.dom import inf_dom
from hydra.genome.inf import inf_enrich
from hydra.genome.lure import emit_registrar_window, registrar_scale, registrar_window_values
from hydra.genome.webparse import GeneSink
from hydra.synapse import Thresholds, build_genome, score_pair


def _page(root, iid: str, url: str | None, title: str = "Pilne: TVN24 ujawnia") -> base.ResolvedIncident:
    saved = f"<!-- saved from url=({len(url):04d}){url} -->\n" if url else ""
    html = (f"{saved}<html><head><title>{title}</title><meta property='og:site_name' content='TVN24'></head>"
            "<body><header><img class='logo' alt='TVN24'></header><p>Treść</p></body></html>")
    a = _art(root / iid, "Page.html", html.encode(), "landing_html", "text/html")
    return base.ResolvedIncident(Incident(id=iid, source="t", source_ref=iid, artifacts=[a.meta]), [a])


def _ctx(settings, tmp_path):
    return base.ExtractContext(settings=settings, work_dir=tmp_path)


def _offdomain(genes):
    return [g for g in genes if g.kind == "brand_offdomain"]


def test_tvn24_mark_off_domain_emits_fact(tmp_path, settings):
    genes = inf_dom(_page(tmp_path, "off", "https://tvn24-news.example/artykul/1"), _ctx(settings, tmp_path)).genes
    off = _offdomain(genes)
    assert [g.value for g in off] == ["TVN24"]
    g = off[0]
    assert g.dependency_group == "inf:boilerplate:brand_offdomain"  # explanation fact, 0 bits
    assert "znak TVN24 na domenie spoza TVN24" in g.evidence.note and "tvn24-news.example" in g.evidence.note
    assert g.evidence.artifact_id and g.evidence.dom_path in ("title", "og", "masthead")


def test_tvn24_mark_on_official_domain_is_silent(tmp_path, settings):
    for i, url in enumerate(("https://tvn24.pl/polska/1", "https://fakty.tvn24.pl/x", "https://www.tvn.pl/")):
        genes = inf_dom(_page(tmp_path, f"on{i}", url), _ctx(settings, tmp_path)).genes
        assert not _offdomain(genes), url


def test_unknown_landing_host_is_unavailable(tmp_path, settings):
    genes = inf_dom(_page(tmp_path, "nohost", None), _ctx(settings, tmp_path)).genes
    off = _offdomain(genes)
    assert off and all(g.status == GeneStatus.UNAVAILABLE and g.value is None for g in off)


def test_window_values_match_within_two_days_only():
    base_day = date(2026, 10, 1)
    a = set(registrar_window_values("Example Registrar", base_day))
    shared = [len(a & set(registrar_window_values("Example Registrar", base_day + timedelta(days=k))))
              for k in range(7)]
    assert shared == [3, 2, 1, 0, 0, 0, 0]
    assert not a & set(registrar_window_values("Other Registrar", base_day))
    assert registrar_scale("NameCheap, Inc.") == "large" and registrar_scale("GoDaddy.com, LLC") == "large"
    assert registrar_scale("Example Registrar") == "small"


def _reg_genes(iid: str, registrar: str, day: date):
    sink = GeneSink(iid, "t@1")
    emit_registrar_window(sink, registrar, day, domain=f"{iid}.example", key=None)
    return sink.genes


def _score(registrar: str):
    genes = {"x": _reg_genes("x", registrar, date(2026, 10, 1)), "y": _reg_genes("y", registrar, date(2026, 10, 2))}
    for i in range(6):  # background: other registrars and dates
        genes[f"bg{i}"] = _reg_genes(f"bg{i}", f"Registrar {i}", date(2026, 3, 1) + timedelta(days=9 * i))
    rarity = build_rarity(genes, {})
    gen = {k: build_genome(k, v, {}) for k, v in genes.items()}
    return score_pair(gen["x"], gen["y"], rarity, Thresholds(max_reasons=100))


def test_small_registrar_one_day_apart_matches():
    pe = _score("Example Registrar")
    win = [r for r in pe.score.reasons if r.kind == "registrar_window"]
    assert len(win) == 2 and all(r.bits > 0 for r in win)
    assert pe.campaign_bits > 0


def test_large_registrar_gives_zero_bits():
    pe = _score("NameCheap, Inc.")
    assert not [r for r in pe.score.reasons if r.kind == "registrar_window"]
    assert pe.campaign_bits == 0 and pe.kit_bits == 0


def test_inf_enrich_emits_registrar_window(tmp_path, settings, cache, certpl, monkeypatch):  # noqa: F811
    monkeypatch.setattr(client, "http_json", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    enr = Enricher(cache_dir=cache, live=False)
    inc = web_incident(tmp_path / "inc", "lure-enrich")  # own id: the bundle cache is keyed by incident id
    res = inf_enrich(inc, base.ExtractContext(settings=settings, work_dir=tmp_path, enrich=enr))
    win = [g for g in res.genes if g.kind == "registrar_window"]
    assert {g.value for g in win} == set(registrar_window_values("Example Registrar", date(2026, 10, 1)))
    assert all(g.dependency_group == "inf:operator:registration" and g.evidence.enrichment_key for g in win)
