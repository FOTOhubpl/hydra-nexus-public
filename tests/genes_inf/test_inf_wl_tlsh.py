from __future__ import annotations

import random

from fixtures import _synthetic_warninglists, web_incident  # noqa: F401

from hydra.enrich import warninglists as wl
from hydra.genome import base
from hydra.genome.dom import inf_dom
from hydra.genome.fuzzy import best_distance, pair_bits, tlsh_value
from hydra.genome.webparse import GeneSink


def test_warninglist_matchers():
    assert wl.classify("server_ip", "104.18.5.9") == wl.Hit("boilerplate", "cloudflare")
    assert wl.classify("dns_ip24", "104.18.5.0/24").effect == "boilerplate"
    assert wl.classify("dns_ip24", "2606:4700:3030::/48").effect == "boilerplate"
    assert wl.classify("dns_ip", "1.1.1.1").list_name == "public-dns-v4"
    assert wl.classify("dns_ip", "5.9.1.2").effect == "info"
    assert wl.classify("dns_ip", "198.51.100.1") is None
    assert wl.classify("ns_host", "ns1.parkingcrew.net").effect == "parked_domain"
    assert wl.classify("linked_host", "bit.ly").effect == "platform"
    assert wl.classify("entity_email_domain", "gmail.com").effect == "platform"
    assert (
        wl.classify("asset_sha256", "E3B0C44298FC1C149AFBF4C8996FB92427AE41E4649B934CA495991B7852B855").effect
        == "boilerplate"
    )
    assert wl.classify("page_title", "bit.ly") is None


def test_sink_only_lowers_tiers():
    s = GeneSink("i", "x@1")
    s.add("server_ip", "104.21.0.1", None)  # operator -> boilerplate (cloudflare)
    s.add("server_ip", "5.9.1.2", None)  # hoster: note only, stays operator
    s.add("ns_host", "ns1.parkingcrew.net", None)  # parked label
    s.add("mx_host", "eforward1.registrar-servers.com", None)  # parking-NS list but MX: platform
    s.add("linked_host", "bit.ly", None)  # operator -> platform
    s.add("cdn_provider", "cloudflare", None)  # not a looked-up kind
    g = {x.value: x for x in s.genes}
    assert g["104.21.0.1"].dependency_group == "inf:boilerplate:ip" and g["104.21.0.1"].quality <= 0.1
    assert "warninglist:cloudflare" in g["104.21.0.1"].evidence.note
    assert g["5.9.1.2"].dependency_group == "inf:operator:ip" and "hetzner" in g["5.9.1.2"].evidence.note
    assert g["ns1.parkingcrew.net"].dependency_group == "inf:label:parked_domain"
    assert g["eforward1.registrar-servers.com"].dependency_group == "inf:platform:mx"
    assert g["bit.ly"].dependency_group == "inf:platform:linked_hosts"
    assert g["cloudflare"].dependency_group == "inf:boilerplate:cdn"


def test_no_snapshot_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setenv("HYDRA_MISP_WL_DIR", str(tmp_path / "none"))
    assert wl.classify("server_ip", "104.21.0.1") is None


def _js(seed: int, n: int = 3000) -> str:
    r = random.Random(seed)
    words = ["var", "function", "return", "if", "else", "for", "const", "let", "document", "window", "fetch"]
    return " ".join(f"{r.choice(words)}_{r.randint(0, 999)}" for _ in range(n // 8))


def test_tlsh_distance_and_bits():
    a = _js(1).encode()
    edited = a.replace(b"var_", b"vaz_", 3)
    other = _js(2).encode()
    ha, he, ho = tlsh_value(a), tlsh_value(edited), tlsh_value(other)
    assert ha and he and ho and ha.startswith("T1")
    assert best_distance([ha], [he]) < 30 < best_distance([ha], [ho])
    bg = sorted([5, 20, 150, 180, 200, 220, 250, 260, 300, 310, 350, 400])
    assert pair_bits(0, bg) > pair_bits(25, bg) > 0
    assert pair_bits(120, bg) == 0.0  # outside the near band
    assert tlsh_value(b"a" * 300) is None  # TNULL: too uniform


def test_tlsh_genes_from_dom(tmp_path, settings):
    inc = web_incident(tmp_path)
    js = next(a for a in inc.artifacts if a.meta.path_in_incident.endswith("bundle.app.js.pobrane"))
    js.path.write_text(_js(7, 6000))
    css = next(a for a in inc.artifacts if a.meta.path_in_incident.endswith("main.css"))
    css.path.write_bytes(b"." * 2000)
    res = inf_dom(inc, base.ExtractContext(settings=settings, work_dir=tmp_path))
    by = {}
    for g in res.genes:
        by.setdefault(g.kind, []).append(g)
    assert by["tlsh_js"][0].value.startswith("T1")
    assert all(
        g.dependency_group == "inf:kit:fuzzy_build" for k in ("tlsh_js", "tlsh_html_skeleton") for g in by.get(k, [])
    )
    # a uniform CSS body: TLSH refuses it -> not_applicable, not failed
    assert [g.status for g in by["tlsh_css"]] == ["not_applicable"]
