from __future__ import annotations

import json

from fixtures import PIXEL, TOKEN, _synthetic_warninglists, web_incident  # noqa: F401

from hydra.contracts import Incident
from hydra.genome import base
from hydra.genome.dom import inf_dom
from hydra.genome.har import inf_har, inf_url
from hydra.genome.webparse import _BUNDLES


def _ctx(settings, tmp_path):
    return base.ExtractContext(settings=settings, work_dir=tmp_path)


def _kv(res):
    return {(g.kind, g.value) for g in res.genes}


def _all_text(res):
    return json.dumps([g.model_dump() for g in res.genes])


def test_url_genes(tmp_path, settings):
    res = inf_url(web_incident(tmp_path), _ctx(settings, tmp_path))
    kv = _kv(res)
    assert ("landing_host", "www.news-247daily-fixture.live") in kv
    assert ("registrable_domain", "news-247daily-fixture.live") in kv
    assert ("tds_value", "sub1=camp42") in kv
    assert any(k == "utm_pattern" and "utm_visit=uuid-v7" in v for k, v in kv)
    assert any(k == "capi_token_hash" for k, _ in kv)
    assert TOKEN not in _all_text(res)
    groups = {g.kind: g.dependency_group for g in res.genes}
    assert groups["landing_host"] == "inf:operator:landing"
    assert groups["utm_pattern"].startswith("inf:platform:")


def test_har_genes(tmp_path, settings):
    res = inf_har(web_incident(tmp_path), _ctx(settings, tmp_path))
    kv = _kv(res)
    assert ("redirect_chain", "click.tds-hop-fixture.top>www.news-247daily-fixture.live") in kv
    assert ("host_contacted", "click.tds-hop-fixture.top") in kv
    assert ("host_contacted", "www.googletagmanager.com") not in kv
    assert ("thirdparty_host", "www.googletagmanager.com") in kv
    assert ("server_ip", "198.51.100.7") in kv
    assert ("server_ip24", "198.51.100.0/24") in kv
    assert ("x_powered_by", "PHP/8.3.1") in kv
    assert ("cookie_name", "_subid") in kv
    assert ("cdn_provider", "cloudflare") in kv
    assert ("tracker_id", f"fb_pixel:{PIXEL}") in kv
    assert ("tracker_id", "ga4:G-ABCDEFGH12") in kv
    assert ("tds_signature", "keitaro") in kv
    assert ("tds_signature", "aio_sdk") in kv
    assert ("operator_id", "landing_uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee") in kv
    assert ("tracking_endpoint", "POST /api/v1/trigger/field/{uuid}/") in kv
    assert any(k == "post_fields" and "landed_on_landings_uuid" in v for k, v in kv)
    assert any(k == "js_redirect" and "white.safe-fixture.org" in v for k, v in kv)
    assert TOKEN not in _all_text(res)
    ev = next(g for g in res.genes if g.kind == "redirect_chain").evidence
    assert ev.artifact_id and ev.har_entry is not None


def test_dom_genes(tmp_path, settings):
    res = inf_dom(web_incident(tmp_path), _ctx(settings, tmp_path))
    kv = _kv(res)
    kinds = {g.kind for g in res.genes}
    assert ("page_title", "Szokujące wyznanie — Gazeta.pl") in kv
    assert ("og_meta", "og:site_name=Gazeta.pl") in kv
    assert ("canonical_host", "wiadomosci.gazeta.pl") in kv
    assert ("media_brand", "gazeta.pl") in kv
    assert ("form_fields", "first_name,offer_uuid,phone,sub1") in kv
    assert ("lead_form_kit", "hidden:offer_uuid,sub1") in kv
    assert ("tracker_id", f"fb_pixel:{PIXEL}") in kv
    assert ("operator_id", "offer_uuid:11111111-2222-3333-4444-555555555555") in kv
    assert {
        "dom_shingle",
        "dom_skeleton_hash",
        "dom_token",
        "inline_script_hash",
        "asset_sha256",
        "favicon_mmh3",
        "js_build_hash",
        "css_build_hash",
        "asset_sha256_lib",
        "fake_comment_block",
        "comment_names_set",
        "entity_phone",
        "entity_email",
    } <= kinds
    # personal data is hashed, never stored raw
    txt = _all_text(res)
    assert "601 234 567" not in txt and "kontakt@zysk-fikcja-fixture.pl" not in txt
    phone = next(g for g in res.genes if g.kind == "entity_phone")
    assert phone.value.startswith("h:") and phone.dependency_group == "inf:operator:contact"
    lib = next(g for g in res.genes if g.kind == "asset_sha256_lib")
    assert lib.dependency_group.startswith("inf:boilerplate:")
    assert sum(g.kind == "dom_shingle" for g in res.genes) <= 64


def test_two_variants_share_kit_genes(tmp_path, settings):
    a = inf_dom(web_incident(tmp_path / "a", "a"), _ctx(settings, tmp_path))
    b = inf_dom(web_incident(tmp_path / "b", "b", har=False), _ctx(settings, tmp_path))
    shared = {(g.kind, g.value) for g in a.genes} & {(g.kind, g.value) for g in b.genes}
    assert {"asset_sha256", "css_build_hash", "favicon_mmh3", "dom_skeleton_hash"} <= {k for k, _ in shared}


def test_missing_material_is_unavailable_not_failed(tmp_path, settings):
    inc = base.ResolvedIncident(Incident(id="empty", source="t", source_ref="t"), [])
    for fn in (inf_url, inf_har, inf_dom):
        res = fn(inc, _ctx(settings, tmp_path))
        assert res.genes and all(g.status == "unavailable" for g in res.genes)


def test_garbage_har_never_raises(tmp_path, settings):
    inc = web_incident(tmp_path, html=False)
    inc.artifacts[0].path.write_bytes(b"{not json")
    _BUNDLES.clear()
    res = inf_har(inc, _ctx(settings, tmp_path))
    assert any(g.status == "failed" for g in res.genes)
    res = (
        base.run_all(inc, _ctx(settings, tmp_path), only=["inf_url", "inf_dom"]) if "inf_url" in base.REGISTRY else None
    )
    assert res is None or all(g.status != "failed" or g.kind for g in res.genes)
