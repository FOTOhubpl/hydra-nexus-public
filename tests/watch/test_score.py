"""Stage A name score: weights, reasons and the clean-room design choices (synthetic names only)."""

from __future__ import annotations

import pytest

from hydra.watch import score as S
from hydra.watch.families import FamilyIndex
from hydra.watch.names import defang, family_key, fold, osa_distance, parts, template
from hydra.watch.score import A_CUT, W, score_host


def codes(host: str, **kw) -> dict[str, float]:
    return {r.code: r.bits for r in score_host(host, **kw).reasons}


def test_weights_documented_and_signed():
    assert W["official_domain"] < 0
    assert all(v >= 0 for k, v in W.items() if k != "official_domain")
    assert A_CUT == 6.0
    assert W["brand_subdomain"] == 4.0 and W["family"] == 3.0 and W["campaign_token"] == 6.0


def test_brand_in_subdomain_of_foreign_domain():
    c = codes("allegro.przyklad-oferta123456.sbs")
    assert c["brand_subdomain"] == 4.0
    assert c["tld_prior"] == 2.0 and c["digit_run"] == 1.0 and c["bait"] == 1.0
    assert score_host("allegro.przyklad-oferta123456.sbs").bits == pytest.approx(8.0)


def test_official_domain_is_negative_and_stops():
    s = score_host("www.allegro.pl")
    assert [r.code for r in s.reasons] == ["official_domain"] and s.bits < 0
    assert score_host("moje.olx.pl").bits < 0


def test_brand_combo_and_foreign_suffix():
    assert "brand_combo" in codes("pkobp-weryfikacja.com")
    assert codes("vinted.sbs")["brand_foreign_suffix"] == 3.0


def test_typo_only_for_long_tokens():
    assert codes("inpots-paczka.com")["brand_typo"] == 4.0          # transposition (Damerau)
    assert "brand_typo" in codes("santnader-logowanie.icu")
    # short brands never match fuzzily or as free substrings
    assert codes("dingo.com") == {} and codes("pingwin.com") == {}
    assert "brand_typo" not in codes("olz.com")


def test_short_brand_whole_token_only():
    assert "brand_combo" in codes("olx-pl.top")
    assert codes("folxa.com") == {}


def test_homoglyph_folding():
    host = "аllegro-pl.com"  # Cyrillic 'а'
    s = score_host(host)
    c = {r.code for r in s.reasons}
    assert "homoglyph" in c and "brand_combo" in c
    # the punycode host is not treated as the official allegro.pl
    assert "official_domain" not in c
    assert fold("ąłę")[0] == "ale" and fold("ąłę")[1] is False


def test_fake_suffix_hyphens_depth():
    c = codes("ing.pl-zwrot.com.xyz")
    assert c["fake_suffix"] == 1.5 and c["deep_name"] == 1.0
    assert codes("a-b-c-d.com")["many_hyphens"] == 1.0
    assert codes("a-b-c-d-e-f.com")["many_hyphens"] == 1.5


def test_bait_cap():
    c = score_host("oferta-zwrot-paczka-doplata-bonus.com")
    assert sum(r.bits for r in c.reasons if r.code == "bait") == S.CAPS["bait"]


def test_family_reason_needs_three_listed_domains():
    fam = FamilyIndex()
    for i in range(3):
        fam.add("2026-11-21T10:00:00+00:00", "oferta-#.sbs", f"oferta-{1000 + i}.sbs", None)
    assert codes("oferta-5555.sbs", families=fam)["family"] == 3.0
    for i in range(10):
        fam.add("2026-11-21T10:00:00+00:00", "oferta-#.sbs", f"oferta-{2000 + i}.sbs", None)
    assert codes("oferta-5555.sbs", families=fam)["family"] == 4.0  # cap
    assert "family" not in codes("zupelnie-inna.sbs", families=fam)


def test_listed_label_learned_from_feed():
    fam = FamilyIndex()
    for i in range(5):
        fam.add("2026-11-21T10:00:00+00:00", f"x{i}.sbs", f"x{i}.sbs", None, ("biletownia",))
    assert codes("biletownia.nowa123.sbs", families=fam)["listed_label"] == 2.0


class _Camp:
    def skeleton_campaigns(self, sk):
        return ["C-1"] if sk == "L4-D2L5.xyz" else []

    def token_campaigns(self, t):
        return ["C-2"] if t == "zyskomat" else []

    def domain_campaigns(self, host, reg):
        return ["C-3"] if reg == "znana-domena.com" else []

    def lure_labels(self):
        return {"zdrowielinkq": ["C-4"]}

    def family_campaigns(self, fam):
        return ["C-5"] if fam == "?-offertanr#.cyou" else []


def test_campaign_genes():
    c = score_host("panel.zyskomat-pro.com", campaigns=_Camp())
    assert c.campaign_hints == ["C-2"] and any(r.code == "campaign_token" for r in c.reasons)
    assert "campaign_domain" in codes("www.znana-domena.com", campaigns=_Camp())
    assert "campaign_domain_typo" in codes("zdrowielinkk.pro", campaigns=_Camp())
    fam = score_host("olx.pk-offertanr92792.cyou", campaigns=_Camp())
    assert "C-5" in fam.campaign_hints and any(r.code == "campaign_family" for r in fam.reasons)
    # skeleton: full weight while the stream history is small
    assert codes("shop-12abcde.xyz", campaigns=_Camp())["campaign_skeleton"] == 6.0


def test_skeleton_rarity_scaling():
    fam = FamilyIndex()
    for i in range(1000):
        fam.add("2026-11-21T10:00:00+00:00", f"f{i}", f"d{i}.com", "L4-D2L5.xyz" if i < 500 else "L9.com")
    # a shape seen in half of the stream carries ~0 bits
    assert "campaign_skeleton" not in codes("shop-12abcde.xyz", families=fam, campaigns=_Camp())


def test_names_helpers():
    assert osa_distance("allegro", "allergo") == 1
    assert osa_distance("mbank", "santander") == 3  # early exit: limit + 1
    assert template("dk-fk12gc") == "?-fk#gc"
    assert template("pk-offertanr92792") == template("qp-offertanr72683") == "?-offertanr#"
    assert template("pl-secure") == "pl-secure"  # no digit run: not a generated name
    assert family_key(parts("allegro.innpozycja543412.sbs")) == "innpozycja#.sbs"
    assert family_key(parts("tenant-1.pages.dev")).endswith("pages.dev")
    assert defang("https://allegro.pl-3942.lat/a.b") == "hxxps://allegro[.]pl-3942[.]lat/a.b"
    assert defang("olx.pl") == "olx[.]pl"
