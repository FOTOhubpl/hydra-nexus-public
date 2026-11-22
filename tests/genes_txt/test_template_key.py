"""Identifier-masked text template key (GAPS_PAPERS G02). Synthetic inputs only."""

from __future__ import annotations

from hydra.genome.txt import template_key
from hydra.genome.txt_entities import find_amounts, find_hosts, find_phones

from .test_operator_ids import _score, ctx, settings  # noqa: F401  (fixtures)

AD_A = "Zarabiaj nawet 3 000 zł dziennie z nową platformą! Zadzwoń: +48 600 123 456 lub wejdź na zysk-alfa.top"
AD_B = "Zarabiaj nawet 4500 zł dziennie z nową platformą! Zadzwoń: +48 511 222 333 lub wejdź na beta-zysk.online/start"


def _key(t: str):
    return template_key(t, find_hosts(t) + find_phones(t) + find_amounts(t))


def test_same_text_other_identifiers_share_the_key():
    a, b = _key(AD_A), _key(AD_B)
    assert a and b and a[0] == b[0]
    assert "qtelq" in a[1] and "qurlq" in a[1] and "qkwotaq" in a[1]
    assert "600" not in a[1] and "zysk-alfa" not in a[1]


def test_url_only_or_unmasked_text_gives_no_key():
    assert _key("https://zysk-alfa.top/start") is None
    assert _key("Wejdź na zysk-alfa.top") is None  # fewer than 6 words besides the masks
    assert _key("To jest zwykłe zdanie bez żadnych identyfikatorów w treści") is None


def test_key_alone_does_not_link_but_adds_to_an_operator_gene(tmp_path, ctx):  # noqa: F811
    pe, genes = _score(tmp_path, ctx, AD_A, AD_B)
    kg = next(g for g in genes["a"] if g.kind == "text_template_key")
    assert kg.dependency_group == "TXT:phrase:template"
    assert any(r.kind == "text_template_key" for r in pe.score.reasons)
    assert pe.score.hypothesis != "same_campaign"
    wallet = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    pe2, _ = _score(tmp_path, ctx, AD_A + f" Portfel BTC: {wallet}", AD_B + f" Wpłać bitcoin na {wallet}")
    assert pe2.score.family_bits["TXT"] > pe.score.family_bits["TXT"]  # capped at 8 campaign bits (CR-OPGENES-5)
