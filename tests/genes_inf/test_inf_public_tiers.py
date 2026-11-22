"""Tier fixes moved upstream from the public-corpus run (PUBLIC_CORPUS.md, 2026-11-21)."""

from __future__ import annotations

import json

from fixtures import _synthetic_warninglists  # noqa: F401

from hydra.enrich import warninglists as wl
from hydra.enrich.client import Enricher
from hydra.enrich.domains import digit_skeleton, is_platform, shape_is_lexical
from hydra.genome.har import _edge, url_genes
from hydra.genome.inf import ns_tier
from hydra.genome.webparse import GeneSink


def _groups(sink):
    return {(g.kind, g.value): g.dependency_group for g in sink.genes}


def test_shape_only_skeleton_is_boilerplate_and_digit_skeleton_is_operator():
    s = GeneSink("i", "x@1")
    url_genes(s, "https://randomword.com/", "t", None)
    url_genes(s, "https://allegrolokalnie.pe-7492104.sbs/oferta/123", "t", None)
    g = _groups(s)
    assert g[("url_label_skeleton", "L10.com")] == "inf:boilerplate:url_shape"
    assert g[("url_label_path_skeleton", "L10.com/")] == "inf:boilerplate:url_shape"
    assert g[("url_label_skeleton", "L15.L2-D7.sbs")] == "inf:kit:url_shape"
    assert g[("url_digit_skeleton", "allegrolokalnie.pe-#.sbs")] == "inf:operator:url_skeleton"
    assert ("url_digit_skeleton", "randomword.com") not in g  # no digits: equals the host, not emitted
    assert not shape_is_lexical("L9.com") and not shape_is_lexical("L3.L9.com") and shape_is_lexical("L4-D2L5.xyz")
    assert digit_skeleton("www.shop77.example.co.uk") is None or "#" in digit_skeleton("www.shop77.example.co.uk")


def test_private_and_bogon_ips_are_never_operator():
    s = GeneSink("i", "x@1")
    for kind, v in (
        ("dns_ip", "fd10:aec2:5dae::1"),
        ("dns_ip24", "fd10:aec2:5dae::/48"),
        ("server_ip", "10.0.0.5"),
        ("server_ip", "100.64.1.1"),
        ("dns_ip", "127.0.0.1"),
        ("dns_ip", "169.254.1.1"),
        ("dns_ip", "192.0.2.10"),
    ):
        s.add(kind, v, None)
    s.add("dns_ip", "45.83.100.20", None)
    g = _groups(s)
    assert all(v == "inf:boilerplate:private_ip" for (k, val), v in g.items() if val != "45.83.100.20")
    assert g[("dns_ip", "45.83.100.20")] == "inf:operator:ip"


def test_edge_asn_ips(tmp_path):
    root = tmp_path / "dns"
    root.mkdir()
    for ip, asn in (("34.120.1.2", 15169), ("45.83.100.20", 64500)):
        q = ".".join(reversed(ip.split("."))) + ".origin.asn.cymru.com_TXT"
        (root / f"{q}.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "fetched_at": "2026-11-21T00:00:00+00:00",
                    "data": {"answers": [f'"{asn} | x/24 | US | arin | 2010"']},
                }
            )
        )
    enr = Enricher(cache_dir=tmp_path, live=False)
    assert _edge(enr, "34.120.1.2") is True
    assert _edge(enr, "45.83.100.20") is False
    assert _edge(enr, "10.0.0.1") is False and _edge(None, "34.120.1.2") is False


def test_registrar_default_ns_is_platform():
    assert ns_tier("ina1.registrar.eu") == "platform"
    assert ns_tier("ns1.cyberfolks.pl") == "platform"
    assert ns_tier("dns1.registrar-servers.com") == "platform"
    assert ns_tier("kurt.ns.cloudflare.com") == "boilerplate"
    assert ns_tier("ns1.shady-dns-fixture.net") is None


def test_site_builder_tenants_are_platform():
    for h in (
        "shop.lovable.app",
        "x.cfolks.pl",
        "a.vercel.app",
        "a.netlify.app",
        "a.web.app",
        "a.firebaseapp.com",
        "a.github.io",
        "a.pages.dev",
        "a.wixsite.com",
        "a.weebly.com",
        "a.blogspot.com",
        "sites.google.com",
        "a.herokuapp.com",
        "a.glitch.me",
        "a.repl.co",
        "a.framer.website",
    ):
        assert is_platform(h), h
    assert not is_platform("lure-fixture.live") and not is_platform("45.83.100.20")


def test_warninglist_dir_logs_when_missing(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("HYDRA_MISP_WL_DIR", str(tmp_path / "missing"))
    wl._load.cache_clear()
    with caplog.at_level("WARNING"):
        assert wl.classify("server_ip", "104.21.0.1") is None
    assert "not found" in caplog.text


def test_warninglist_dir_falls_back_to_newest_snapshot(monkeypatch, tmp_path):
    monkeypatch.delenv("HYDRA_MISP_WL_DIR")
    snap = tmp_path / "external" / "misp-warninglists" / "abc123" / "lists" / "x"
    snap.mkdir(parents=True)
    monkeypatch.setattr(wl, "_data_dir", lambda _k: tmp_path)
    assert wl.snapshot_dir() == tmp_path / "external" / "misp-warninglists" / "abc123"
