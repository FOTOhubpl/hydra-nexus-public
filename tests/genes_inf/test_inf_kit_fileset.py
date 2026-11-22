"""G06 kit file-set overlap: synthetic saved pages with `_files/` folders (fictional .example hosts, invented ids)."""

from __future__ import annotations

import hashlib

from fixtures import PIXEL, _art, _synthetic_warninglists  # noqa: F401

from hydra.contracts import GeneStatus, Hypothesis, Incident
from hydra.entropy import build_rarity
from hydra.genome import base
from hydra.genome.dom import inf_dom
from hydra.genome.kitset import (
    BAND_GROUP,
    BAND_KIND,
    MEMBER_KIND,
    FileSet,
    content_key,
    fileset_overlap,
    is_ip_blocklist,
    minhash,
    norm_file_name,
)
from hydra.genome.webparse import norm_js
from hydra.synapse import Thresholds, build_genome, level, score_pair

TH = Thresholds(max_reasons=500)


def _blob(seed: str, n: int = 3000) -> bytes:
    out = b""
    i = 0
    while len(out) < n:
        out += hashlib.sha256(f"{seed}-{i}".encode()).digest()
        i += 1
    return out[:n]


def _js(seed: str, n: int = 40) -> bytes:
    body = "".join(f"function {seed}_f{i}(a,b){{return a*{i}+b-{seed!r}.length;}}\n" for i in range(n))
    return body.encode()


def _css(seed: str, n: int = 40) -> bytes:
    return "".join(f".{seed}-c{i}{{margin:{i}px;color:#{i:03d}}}\n" for i in range(n)).encode()


def _html(title: str, extra: str = "") -> str:
    return (
        f"<!DOCTYPE html><html lang='pl'><head><title>{title}</title>{extra}"
        "<link rel='stylesheet' href='./Page_files/landing-theme.css'>"
        "<script src='./Page_files/kit-core.js'></script></head><body><header>Portal</header>"
        "<div class='hero'><h1>Nowa platforma</h1></div><form action='/send.php'><input name='name'>"
        "<input name='phone'></form></body></html>"
    )


def _config(tag: str) -> bytes:
    return (
        f'var CFG={{endpoint:"https://exfil-{tag}.example/collect.php",mail:"ops-{tag}@exfil-{tag}.example",'
        f'tg:"55{tag.encode().hex()}12345:AAH{tag * 6}",ip:"203.0.113.{len(tag)}",phone:"+48 601 234 {len(tag):03d}"}};'
    ).encode()


def _blocklist(tag: str) -> bytes:
    return "\n".join(f"198.51.{i}.{len(tag)}" if i % 3 else f"192.0.2.{i}/32" for i in range(40)).encode()


def _kit_files(tag: str) -> dict[str, bytes]:
    """Kit A; `tag` changes only the exfil config and the IP blocklist."""
    return {
        "kit-core.js": _js("core"),
        "form-handler.js.pobrane": _js("form"),
        "countdown-widget.min.js": _js("countdown"),
        "landing-theme.css": _css("theme"),
        "testimonials.css": _css("testi"),
        "hero-portrait.jpg": _blob("hero"),
        "trust-badges.png": _blob("badges"),
        "config.js": _config(tag),
        "blocklist.txt": _blocklist(tag),
        "jquery-3.6.0.min.js.pobrane": b"/*jq*/" + b"x" * 3000,
    }


def _incident(root, iid: str, files: dict[str, bytes], html: str) -> base.ResolvedIncident:
    arts = [_art(root / iid, "Page.html", html.encode(), "landing_html", "text/html")]
    for name, data in files.items():
        arts.append(_art(root / iid, f"Page_files/{name}", data, "landing_asset"))
    inc = Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta for a in arts])
    return base.ResolvedIncident(inc, arts)


def _wp_files(theme: str) -> dict[str, bytes]:
    """WordPress-like page: core files identical across sites, theme files of its own."""
    return {
        "wp-emoji-release.min.js": _js("wpemoji"),
        "jquery.min.js": _js("jquery"),
        "jquery-migrate.min.js": _js("jqmigrate"),
        "style.min.css": _css("blocklib"),
        "style.css": _css(theme),
        f"{theme}-navigation.js": _js(theme + "nav"),
        f"{theme}-header.jpg": _blob(theme + "hdr"),
        f"{theme}-skip-link.js": _js(theme + "skip"),
        f"{theme}-footer.png": _blob(theme + "ftr"),
    }


def _ctx(settings, tmp_path):
    return base.ExtractContext(settings=settings, work_dir=tmp_path)


WP_GEN = "<meta name='generator' content='WordPress 6.4.2'>"


def _corpus(tmp_path, settings, extra: dict[str, base.ResolvedIncident], *, wp_background: int = 0):
    incs = dict(extra)
    for i in range(6):  # unrelated background kits
        files = {f"bg{i}-file{j}.js": _js(f"bg{i}x{j}") for j in range(5)}
        incs[f"bg-{i}"] = _incident(tmp_path, f"bg-{i}", files, _html(f"Tło {i}"))
    for i in range(wp_background):  # other WordPress sites: CMS core files and the generator are common
        incs[f"wp-bg-{i}"] = _incident(tmp_path, f"wp-bg-{i}", _wp_files(f"theme{i}"), _html(f"Strona {i}", WP_GEN))
    genes = {iid: inf_dom(inc, _ctx(settings, tmp_path)).genes for iid, inc in incs.items()}
    rarity = build_rarity(genes, {})
    genomes = {iid: build_genome(iid, g, {}) for iid, g in genes.items()}
    return genes, rarity, genomes


def _bands(genes):
    return {g.value for g in genes if g.kind == BAND_KIND and g.value}


def _fileset_reasons(pe):
    return [r for r in pe.score.reasons if r.kind == BAND_KIND]


# ------------------------------------------------------------------------------------------- unit level


def test_norm_js_masks_deployment_values():
    a = 'x("https://exfil-a.example/c.php","a@exfil-a.example","203.0.113.5","2001:db8::1","+48 601 234 567")'
    b = 'x("https://other.example/z","b@other.example","198.51.100.9","2001:db8:0:1::7","+48 700 100 200")'
    assert norm_js(a) == norm_js(b)
    assert norm_js("var t='12:30:45';") == "vart='12:30:45';"  # not an address


def test_name_normalisation_and_blocklist():
    assert norm_file_name("countdown-widget.min.js.pobrane") == "countdown-widget.js"
    assert norm_file_name("app.3f9a8b2c.js?ver=6.4") == "app.js"
    assert norm_file_name("hero-portrait(1).jpg") == "hero-portrait.jpg"
    assert norm_file_name("slider-2.1.0.css") == "slider.css"
    assert is_ip_blocklist(_blocklist("a").decode())
    assert not is_ip_blocklist(_js("core").decode())
    assert content_key("blocklist.txt", _blocklist("a")) is None


def test_minhash_tracks_jaccard():
    base_set = {f"e{i}" for i in range(40)}
    near = (base_set - {"e0", "e1"}) | {"x0", "x1"}  # J = 38/42
    far = {f"e{i}" for i in range(10)} | {f"y{i}" for i in range(30)}  # J = 10/70
    sa, sn, sf = minhash(base_set), minhash(near), minhash(far)
    agree = lambda x, y: sum(p == q for p, q in zip(x, y, strict=True)) / len(x)  # noqa: E731
    assert agree(sa, sn) > 0.75 and agree(sa, sf) < 0.35


# ------------------------------------------------------------------------------------------- acceptance


def test_a_kit_copy_shares_kit_bits_but_no_campaign_link(tmp_path, settings):
    a = _incident(tmp_path, "kit-a", _kit_files("a"), _html("Inwestuj teraz"))
    b = _incident(tmp_path, "kit-a2", _kit_files("bb"), _html("Inwestuj teraz"))
    genes, rarity, genomes = _corpus(tmp_path, settings, {"kit-a": a, "kit-a2": b})
    ga, gb = genes["kit-a"], genes["kit-a2"]
    band = next(g for g in ga if g.kind == BAND_KIND)
    assert band.dependency_group == BAND_GROUP and level(band) == "kit"
    assert "files=8" in (band.evidence.note or "") and "ip_blocklist=1" in band.evidence.note
    assert len(_bands(ga) & _bands(gb)) >= 1
    ov = fileset_overlap(ga, gb)
    assert ov is not None and ov["n_a"] == ov["n_b"] == 8
    assert ov["jaccard"] >= 0.80 and ov["containment"] >= 0.75
    assert {"kit-core.js", "hero-portrait.jpg", "countdown-widget.js"} <= set(ov["shared"])
    assert "blocklist.txt" not in ov["shared"] and not any("jquery" in s for s in ov["shared"])
    pe = score_pair(genomes["kit-a"], genomes["kit-a2"], rarity, TH)
    reasons = _fileset_reasons(pe)
    assert reasons and all(r.bits > 0 for r in reasons)
    assert pe.kit_bits >= 6.0
    assert pe.campaign_bits < 10.0
    assert pe.score.hypothesis == Hypothesis.SHARED_TEMPLATE
    # member genes are evidence only: 0 bits
    assert not [r for r in pe.score.reasons if r.kind == MEMBER_KIND]


def test_b_kit_copy_with_shared_operator_id_is_same_campaign(tmp_path, settings):
    pixel = f"<script>fbq('init', '{PIXEL}');fbq('track','PageView');</script>"
    a = _incident(tmp_path, "op-a", _kit_files("a"), _html("Inwestuj teraz", pixel))
    b = _incident(tmp_path, "op-a2", _kit_files("bb"), _html("Inwestuj teraz", pixel))
    genes, rarity, genomes = _corpus(tmp_path, settings, {"op-a": a, "op-a2": b})
    tr = next(g for g in genes["op-a"] if g.kind == "tracker_id")
    assert tr.dependency_group == "inf:operator:tracker"
    pe = score_pair(genomes["op-a"], genomes["op-a2"], rarity, TH)
    assert _fileset_reasons(pe)
    assert pe.score.hypothesis == Hypothesis.SAME_CAMPAIGN


def test_c_unrelated_wordpress_pages_no_link(tmp_path, settings):
    c = _incident(tmp_path, "wp-c", _wp_files("astra"), _html("Blog o ogrodzie", WP_GEN))
    d = _incident(tmp_path, "wp-d", _wp_files("kadence"), _html("Przepisy kuchenne", WP_GEN))
    genes, rarity, genomes = _corpus(tmp_path, settings, {"wp-c": c, "wp-d": d}, wp_background=6)
    ov = fileset_overlap(genes["wp-c"], genes["wp-d"])
    assert ov is not None and ov["jaccard"] < 0.2
    assert not any(s.startswith("wp-emoji") for s in ov["shared"])  # CMS core skipped
    assert not (_bands(genes["wp-c"]) & _bands(genes["wp-d"]))
    pe = score_pair(genomes["wp-c"], genomes["wp-d"], rarity, TH)
    assert not _fileset_reasons(pe)
    assert pe.score.hypothesis == Hypothesis.INDEPENDENT


def test_d_inline_page_is_not_applicable(tmp_path, settings):
    inline = (
        "<html><head><title>Jedna strona</title><style>.a{color:red}</style>"
        "<script>var x=1;</script></head><body><p>Treść</p></body></html>"
    )
    p = _incident(tmp_path, "inline-1", {}, inline)
    q = _incident(tmp_path, "inline-2", {}, inline.replace("Jedna", "Inna"))
    few = _incident(tmp_path, "few-1", {"kit-core.js": _js("core"), "hero.jpg": _blob("h")}, _html("Mało"))
    genes, rarity, genomes = _corpus(tmp_path, settings, {"inline-1": p, "inline-2": q, "few-1": few})
    for iid in ("inline-1", "inline-2", "few-1"):
        bands = [g for g in genes[iid] if g.kind == BAND_KIND]
        assert len(bands) == 1 and bands[0].status == GeneStatus.NOT_APPLICABLE and bands[0].value is None
    assert "2 first-party files < 4" in next(g for g in genes["few-1"] if g.kind == BAND_KIND).evidence.note
    assert fileset_overlap(genes["inline-1"], genes["inline-2"]) is None
    pe = score_pair(genomes["inline-1"], genomes["inline-2"], rarity, TH)
    assert not _fileset_reasons(pe)
    assert pe.score.hypothesis != Hypothesis.SAME_CAMPAIGN


def test_fileset_add_skips_fonts_libs_and_cdn_helpers():
    fs = FileSet("k", "Page_files", None)
    fs.add("font-awesome.woff2", _blob("f"), lib=False)
    fs.add("email-decode.min.js", _js("cf"), lib=False, path="cdn-cgi/scripts/email-decode.min.js")
    fs.add("bootstrap.min.js", _js("bs"), lib=True)
    fs.add("pixel.gif", b"GIF89a", lib=False)
    fs.add("kit-core.js", _js("core"), lib=False)
    assert fs.n_files == 1 and fs.names == {"n:kit-core.js"}
    assert fs.skipped == {"font": 1, "cms_or_cdn_helper": 1, "library": 1, "tiny": 1}
