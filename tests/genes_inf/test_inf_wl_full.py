"""Complete MISP warninglist coverage (ADOPT_PLAN item 6): every list type, label effect, phone regex lists, and the
acceptance test that a pair sharing only a special-use / sinkhole / bank / reserved-phone value gets 0 campaign
bits. Synthetic fixture only (tests/genes_inf/wl_fixture/)."""

from __future__ import annotations

import logging

import pytest
from fixtures import _synthetic_warninglists  # noqa: F401

from hydra.contracts import Gene
from hydra.enrich import warninglists as wl
from hydra.entropy import build_rarity
from hydra.genome.operator_ids import phone_profile
from hydra.genome.webparse import GeneSink, pii_hash
from hydra.synapse import build_genome, level, score_pair

UPSTREAM_TYPES = {"cidr", "hostname", "string", "substring", "regex"}


@pytest.fixture(autouse=True)
def _fresh_caches():
    wl.cache_clear()
    phone_profile.cache_clear()
    yield
    wl.cache_clear()
    phone_profile.cache_clear()


# ------------------------------------------------------------------------------------------- mapping
def test_every_upstream_list_has_an_explicit_effect():
    assert len(wl.EFFECT) == wl.UPSTREAM_LIST_COUNT == 231
    assert set(wl.EFFECT.values()) == {"boilerplate", "platform", "parked_domain", "label", "info", "none"}
    unused = {n for n, e in wl.EFFECT.items() if e == "none"}
    assert unused == set(wl.REASON) and all(wl.REASON[n].strip() for n in unused)
    for name in ("rfc1918", "rfc5735", "rfc6598", "ipv6-linklocal", "multicast", "rfc6761", "phone_numbers"):
        assert wl.EFFECT[name] == "boilerplate", name
    assert wl.EFFECT["sinkholes"] == wl.EFFECT["bank-website"] == "label"
    for name in ("alibaba-cloud", "huawei-cloud", "tencent-cloud", "vpn-ipv4", "tor-exit-nodes",
                 "icloud-private-relay"):
        assert wl.EFFECT[name] in ("platform", "boilerplate"), name
    for big in ("cisco_top1m", "cloudflare-top1m", "tranco", "netapi-top1m", "google-chrome-crux-1million",
                "windows-binary-hashes"):
        assert wl.EFFECT[big] == "none", big
    assert set(wl.LABEL_SUB) <= {n for n, e in wl.EFFECT.items() if e == "label"}


def test_summary_counts_by_effect_and_type():
    s = wl.summary()
    assert s["available"] and s["loaded"] == 19 and s["mapped"] == 206
    assert set(s["by_type"]) == UPSTREAM_TYPES
    assert s["by_effect"]["label"] == 3 and s["by_effect"]["boilerplate"] == 9
    assert s["regex_skipped"] == {"phone_numbers": 2}
    assert s["regex_patterns"]["phone"] == 4 and s["regex_patterns"]["email"] == 1
    line = wl.report_line()
    assert line.startswith("MISP warninglists: 19 lists loaded (206 mapped, commit ")


def test_summary_without_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("HYDRA_MISP_WL_DIR", str(tmp_path / "none"))
    assert wl.summary()["available"] is False
    assert wl.report_line().startswith("MISP warninglists: not loaded")
    assert wl.fingerprint().startswith("none:0:")


# ------------------------------------------------------------------------------------------- list types
def test_cidr_lists():
    assert wl.classify("server_ip", "10.1.2.3") == wl.Hit("boilerplate", "rfc1918")
    assert wl.classify("dns_ip24", "192.168.7.0/24") == wl.Hit("boilerplate", "rfc1918")
    assert wl.classify("server_ip", "91.208.99.77") == wl.Hit("label", "sinkholes")
    assert wl.classify("dns_ip", "45.83.64.2") == wl.Hit("label", "sinkholes")
    assert wl.classify("dns_ip", "45.83.64.4") is None
    assert wl.classify("server_ip", "146.70.77.9") == wl.Hit("platform", "vpn-ipv4")
    assert wl.classify("linked_host", "10.1.2.3") is None  # an IP list never applies to host kinds


def test_hostname_lists_suffix_and_special_use_tlds():
    assert wl.classify("linked_host", "www.synthbank-fixture.pl") == wl.Hit("label", "bank-website")
    assert wl.classify("registrable_domain", "bankfixture.com") == wl.Hit("label", "bank-website")
    assert wl.classify("linked_host", "notsynthbank-fixture.pl") is None
    assert wl.classify("ns_host", "ns1.lure.invalid") == wl.Hit("boilerplate", "rfc6761")
    assert wl.classify("linked_host", "localhost") == wl.Hit("boilerplate", "rfc6761")
    assert wl.classify("server_ip", "91.208.99.77").effect == "label"  # host list does not leak into IP kinds


def test_public_suffix_tenant_is_not_zeroed():
    """Google's list names web.app; a tenant's own subdomain under it is demoted to platform, not zeroed."""
    assert wl.classify("linked_host", "web.app") == wl.Hit("boilerplate", "google")
    assert wl.classify("landing_host", "evil-lure.web.app") == wl.Hit("platform", "google")
    assert wl.classify("landing_host", "cdn.googlefixture.com") == wl.Hit("boilerplate", "google")


def test_string_lists_are_exact():
    assert wl.classify("tls_cert_sha256", "AB" * 32) == wl.Hit("boilerplate", "mozilla-CA")
    assert wl.classify("asset_sha256", "ab" * 32) is None  # matching_attributes: x509 only, not file hashes
    assert wl.classify("landing_host", "pl") == wl.Hit("boilerplate", "tlds")
    assert wl.classify("landing_host", "shop.pl") is None  # a TLD string never swallows its domains


def test_popularity_rankings_match_exact_host_only():
    assert wl.classify("linked_host", "popularsite-fixture.pl") == wl.Hit("platform", "tranco10k")
    assert wl.classify("landing_host", "lure.popularsite-fixture.pl") is None


def test_substring_lists_ip_and_host_entries():
    assert wl.classify("server_ip", "52.168.52.134") == wl.Hit("label", "microsoft-attack-simulator")
    assert wl.classify("linked_host", "portal.simfixture.net") == wl.Hit("label", "microsoft-attack-simulator")
    assert wl.classify("entity_email_domain", "gmail.com") == wl.Hit("platform", "personal-email-domains")


def test_regex_email_list():
    assert wl.classify("entity_email", "Abuse@shop-fixture.pl") == wl.Hit("platform", "common-contact-emails")
    assert wl.classify("entity_email", "jan@gmail.com") == wl.Hit("platform", "personal-email-domains")
    assert wl.classify("entity_email", "jan@shop-fixture.pl") is None
    assert wl.classify("entity_email", pii_hash("abuse@shop-fixture.pl")) is None  # hashed values never match


def test_invalid_and_overlong_regex_entries_are_skipped_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="hydra.enrich.warninglists"):
        assert wl.available()
    msgs = [r.getMessage() for r in caplog.records if "phone_numbers" in r.getMessage()]
    assert len(msgs) == 1 and "2 regex entries skipped" in msgs[0]


# ------------------------------------------------------------------------------------------- phones
@pytest.mark.parametrize("number", ["+447700900123", "00447700900123", "447700900123", "+12125550123",
                                    "+33999123456", "+46313900612"])
def test_classify_phone_reserved_forms(number):
    assert wl.classify_phone(number) == wl.Hit("boilerplate", "phone_numbers")
    assert wl.classify("entity_phone", number) == wl.Hit("boilerplate", "phone_numbers")


@pytest.mark.parametrize("number", ["+48221234567", "+447700901123", "+919875550123", "+4479009001234", "h:abc"])
def test_classify_phone_ordinary_numbers(number):
    # +919875550123: the US pattern's national part (NNN55501NN) must not fire for another country code
    assert wl.classify_phone(number) is None


def test_phone_profile_reserved_number_is_boilerplate():
    prof = phone_profile("+447700900123")
    assert prof is not None and prof.tier == "boilerplate" and "phone_numbers" in prof.reason
    assert phone_profile("+48601234567").tier == "operator"


# ------------------------------------------------------------------------------------------- GeneSink
def test_sink_label_effect_and_only_lowering():
    s = GeneSink("i", "x@1")
    s.add("server_ip", "91.208.99.77", None)
    s.add("linked_host", "www.synthbank-fixture.pl", None)
    s.add("registrable_domain", "bankfixture.com", "inf:boilerplate:landing")  # already zero: unchanged
    s.add("landing_host", "evil-lure.web.app", None)
    g = {x.value: x for x in s.genes}
    sink = g["91.208.99.77"]
    assert sink.dependency_group == "inf:label:sinkhole" and sink.quality <= 0.1
    assert "[warninglist:sinkholes label]" in sink.evidence.note and level(sink) == "none"
    assert g["www.synthbank-fixture.pl"].dependency_group == "inf:label:bank_website"
    assert g["bankfixture.com"].dependency_group == "inf:boilerplate:landing"
    assert "warninglist:bank-website" in g["bankfixture.com"].evidence.note
    assert g["evil-lure.web.app"].dependency_group == "inf:platform:landing"


# ------------------------------------------------------------------------------------------- acceptance
def _incident(iid: str, k: int, shared: list[tuple[str, str, str | None]]) -> list[Gene]:
    s = GeneSink(iid, "inf_fixture@1")
    s.add("landing_host", f"lure{k}-fixture.pl", None)
    s.add("server_ip", f"45.83.{100 + k}.20", None)
    s.add("tracker_id", f"G-FIXTURE{k:04d}", None)
    for kind, value, group in shared:
        s.add(kind, value, group)
    return s.genes


def _pair(shared: list[tuple[str, str, str | None]]):
    genes = {f"i{k}": _incident(f"i{k}", k, shared if k < 2 else []) for k in range(10)}
    rarity = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}
    return score_pair(gen["i0"], gen["i1"], rarity)


def _phone(number: str) -> tuple[str, str, str]:
    prof = phone_profile(number)
    return "entity_phone", pii_hash(prof.e164), f"inf:{prof.tier}:contact"  # as hydra.genome.dom does


@pytest.mark.parametrize(
    "shared",
    [
        [("server_ip", "10.20.30.40", None), ("dns_ip", "10.20.30.40", None)],
        [("server_ip", "91.208.99.77", None), ("dns_ip", "91.208.99.77", None)],
        [("linked_host", "www.synthbank-fixture.pl", None), ("registrable_domain", "synthbank-fixture.pl", None)],
        "reserved_phone",
    ],
    ids=["rfc1918", "sinkhole", "bank", "reserved_phone"],
)
def test_pair_sharing_only_a_warninglisted_value_gets_zero_campaign_bits(shared):
    if shared == "reserved_phone":
        shared = [_phone("+447700900123")]
    pe = _pair(shared)
    assert pe.campaign_bits == 0


def test_control_pair_sharing_an_operator_value_links():
    """Same construction with a shared ordinary IP / phone: campaign bits > 0, so the zero above is the lists."""
    assert _pair([("server_ip", "45.83.99.20", None)]).campaign_bits > 0
    assert _pair([_phone("+48601234567")]).campaign_bits > 0
