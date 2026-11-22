"""SCAMLINKS genes: ad-platform campaign ids, shop-builder fingerprint, product path, random-letter domains.

Synthetic values only (made-up hosts under .test / .example and made-up ids)."""

from __future__ import annotations

import json

from fixtures import _art, _synthetic_warninglists  # noqa: F401

from hydra.contracts import Incident
from hydra.genome import base
from hydra.genome.adshop import inf_adshop, product_path, shop_from_params
from hydra.genome.base import ResolvedIncident
from hydra.genome.domain_pl import analyse_host, random_letters
from hydra.genome.har import inf_url
from hydra.genome.webparse import (
    KIND_PL_SCAMLINKS,
    TIERS,
    ad_campaign_ids,
    ad_label_pl,
    dependency_group,
)

GADS = ("https://qwrtzpkx.test/products/12345678901234567890123456?utm_origin=shopline&utm_campaign=777001"
        "&utm_content=55500011122&adset_id=900000000001&ad_id=900000000002&aatid=1234567890&gad_source=1"
        "&gad_campaignid=55500011122&gclid=Cj0KCQsyntheticclickid")


def _ctx(settings, tmp_path):
    return base.ExtractContext(settings=settings, work_dir=tmp_path)


def _url_incident(root, url, iid="s-1"):
    a = _art(root, f"{iid}/url.txt", url.encode(), "url", "text/plain")
    return ResolvedIncident(Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta]), [a])


def test_ad_campaign_ids_google():
    from urllib.parse import parse_qsl, urlsplit

    plats, ids = ad_campaign_ids(parse_qsl(urlsplit(GADS).query))
    assert plats == {"google_ads": "gad_source"}
    vals = {v for v, _, _ in ids}
    # gad_campaignid and utm_content carry the same id: one value, the parameter name is not part of it
    assert vals == {"google_ads:55500011122", "google_ads:777001", "google_ads:900000000001",
                    "google_ads:900000000002", "google_ads:1234567890"}
    assert not any("Cj0KCQ" in v for v in vals)  # per-click id never becomes a value


def test_generic_names_need_an_ad_platform():
    plats, ids = ad_campaign_ids([("campaign_id", "123456"), ("utm_content", "99999999")])
    assert plats == {} and ids == []  # TDS / affiliate campaign_id: stays a tds_value
    plats, ids = ad_campaign_ids([("ad_id", "123456789"), ("ad_id2", "{{ad.id}}")])
    assert ids == [("ads:123456789", "ad_id", "ad")]  # ad-platform-specific name: kept, platform unknown
    _, ids = ad_campaign_ids([("fbclid", "x1"), ("utm_campaign", "{{campaign.id}}"), ("utm_id", "120200000000001")])
    assert ids == [("meta_ads:120200000000001", "utm_id", "campaign")]


def test_tiers_and_polish_labels():
    assert dependency_group("ad_campaign_id") == "inf:operator:ad_campaign"
    assert dependency_group("ad_platform") == "inf:platform:ad_platform"
    assert dependency_group("shop_platform") == "inf:platform:shop_builder"
    assert dependency_group("product_path_skeleton") == "inf:kit:shop_product_path"
    assert set(KIND_PL_SCAMLINKS) <= set(TIERS)
    assert ad_label_pl("google_ads:55500011122") == "Ten sam identyfikator kampanii Google Ads"


def test_inf_url_landing(tmp_path, settings):
    res = inf_url(_url_incident(tmp_path, GADS), _ctx(settings, tmp_path))
    kv = {(g.kind, g.value): g for g in res.genes}
    assert ("ad_campaign_id", "google_ads:55500011122") in kv
    assert kv[("ad_campaign_id", "google_ads:55500011122")].dependency_group == "inf:operator:ad_campaign"
    assert "Google Ads" in kv[("ad_campaign_id", "google_ads:55500011122")].evidence.note
    assert ("ad_platform", "google_ads") in kv
    assert kv[("ad_platform", "google_ads")].dependency_group.startswith("inf:platform:")
    # ad params are one observation: no duplicate tds_value for utm_campaign / utm_content
    assert not any(k == "tds_value" and v.startswith(("utm_campaign=", "utm_content=")) for k, v in kv)
    assert ("product_path_skeleton", "/products/{num26}") in kv
    assert ("shop_product_id", "shopline:12345678901234567890123456") in kv
    assert kv[("shop_product_id", "shopline:12345678901234567890123456")].dependency_group == \
        "inf:operator:shop_store"
    assert not any("Cj0KCQ" in (g.value or "") for g in res.genes)


def test_tds_values_unchanged_without_ad_platform(tmp_path, settings):
    res = inf_url(_url_incident(tmp_path, "https://lure-fixture.test/a?sub1=camp42&campaign_id=123456"),
                  _ctx(settings, tmp_path))
    kv = {(g.kind, g.value) for g in res.genes}
    assert ("tds_value", "sub1=camp42") in kv and ("tds_value", "campaign_id=123456") in kv
    assert not any(k == "ad_campaign_id" for k, _ in kv)


def test_shop_platform_from_markers(tmp_path, settings):
    html = ("<html><head><meta name='generator' content='Shopify'>"
            "<script src='https://cdn.shopify.com/s/files/app.js'></script></head><body class='product'>"
            "<a href='https://shop-fixture.test/x?fbclid=abc&ad_id=120000000000042'>x</a>"
            "<script>/* sdk list: shopline shoplazza woocommerce prestashop magento */</script></body></html>")
    har = {"log": {"version": "1.2", "creator": {"name": "t"}, "pages": [], "entries": [{
        "startedDateTime": "2026-11-21T10:00:00Z", "time": 1,
        "request": {"method": "GET", "url": "https://img.myshopline.com/a.png?x=1", "headers": [], "queryString": []},
        "response": {"status": 200, "headers": [], "content": {"size": 0, "mimeType": "image/png"}},
        "serverIPAddress": "203.0.113.9"}]}}
    arts = [_art(tmp_path, "Page.html", html.encode(), "landing_html", "text/html"),
            _art(tmp_path, "cap.har", json.dumps(har).encode(), "har", "application/json")]
    inc = ResolvedIncident(Incident(id="s-2", source="test", source_ref="s-2", artifacts=[a.meta for a in arts]), arts)
    res = inf_adshop(inc, _ctx(settings, tmp_path))
    shops = {g.value for g in res.genes if g.kind == "shop_platform"}
    # builder names in a bundled SDK list are not evidence; hosts, generator and code markers are
    assert shops == {"shopify", "shopline"}
    assert all(g.dependency_group == "inf:platform:shop_builder" for g in res.genes if g.kind == "shop_platform")
    kv = {(g.kind, g.value) for g in res.genes}
    assert ("ad_campaign_id", "meta_ads:120000000000042") in kv  # from a link in the saved page


def test_shop_params_and_product_path():
    assert shop_from_params([("utm_origin", "Shopline"), ("utm_source", "shopline")]) == [
        ("shopline", "utm_origin=shopline")]
    assert shop_from_params([("utm_origin", "newsletter")]) == []
    assert product_path("/products/t-shirt-blue", None) == (None, None)
    assert product_path("/en/products/1234567890123", None) == ("/products/{num13}", "shop:1234567890123")
    assert product_path("/products/red-dress", "shopify") == ("/products/red-dress", None)


def test_random_letter_domains():
    assert random_letters("xkqtrbwe") and random_letters("qwrtzpkx")
    for word in ("sprzedam", "szczecin", "wszystko", "allegro", "facebook", "strength", "instrukcja", "mbank"):
        assert not random_letters(word), word
    f = analyse_host("shop.qwrtzpkx.com")
    assert f is not None and "random_letters" in f.shapes
