"""Toll-fraud chain genes (hydra/genome/tollfraud.py), modelled on the link chain of CERT Polska, "Fałszywe reklamy
i złośliwe aplikacje — analiza operacji toll fraud" (2026-09-23). Synthetic inputs only: fictional packages
(com.fikcja.*), .example hosts, invented ids, certificate hashes and short-code keywords."""

from __future__ import annotations

import hashlib
import json

from fixtures import _art, _synthetic_warninglists  # noqa: F401

from hydra.contracts import ArtifactRole, Family, Incident
from hydra.entropy import build_rarity
from hydra.genome import base
from hydra.genome import dom as _dom  # noqa: F401  (registers inf_dom, inf_tollfraud)
from hydra.genome import har as _har_mod  # noqa: F401  (registers inf_url, inf_har)
from hydra.genome import txt as _txt  # noqa: F401  (registers txt_entities)
from hydra.genome.tollfraud import (
    KIND_PL_CERT,
    TOLL_KINDS,
    cloud_lb,
    find_adlibrary_ids,
    find_app_ids,
    find_billing_markers,
    find_cert_hashes,
    find_premium_sms,
    shared_cert_reason,
)
from hydra.genome.txt_entities import find_emails, find_hosts, refang
from hydra.genome.webparse import _BUNDLES
from hydra.synapse import build_genome, gene_tier, level, score_pair

PKG = "com.fikcja.player"
CERT_X = hashlib.sha256(b"synthetic-reused-certificate-der").hexdigest()
ALB = "alb-fikcja0c2d4e6f8a.us-east-1.alb.aliyuncsslbintl.com"


# ------------------------------------------------------------------------------------------------ unit: finders


def test_refang_keeps_original_offsets():
    t = "C2: hxxps://api[.]fikcja-c2-test[.]pl/x i kontakt op[at]fikcja-test[.]pl oraz zly-fikcja(.)pl"
    clean, om = refang(t)
    assert "https://api.fikcja-c2-test.pl/x" in clean and "op@fikcja-test.pl" in clean and om is not None
    hosts = {e.value: t[e.start:e.end] for e in find_hosts(t) if e.kind == "domain_in_text"}
    assert hosts["api.fikcja-c2-test.pl"] == "hxxps://api[.]fikcja-c2-test[.]pl/x"
    assert hosts["zly-fikcja.pl"] == "zly-fikcja(.)pl"
    urls = [e for e in find_hosts(t) if e.kind == "url_in_text"]
    assert urls[0].value == "https://api.fikcja-c2-test.pl/x"
    (mail,) = find_emails(t)
    assert mail.value == "op@fikcja-test.pl" and t[mail.start:mail.end] == "op[at]fikcja-test[.]pl"
    # plain text is untouched
    assert refang("zwykły tekst bez wskaźników") == ("zwykły tekst bez wskaźników", None)


def test_app_store_ids_and_tiers():
    vals = lambda t: {(e.kind, e.value, e.tier) for e in find_app_ids(t)}  # noqa: E731
    assert ("app_package_id", f"android:{PKG}", "operator") in vals(
        f"https://play.google.com/store/apps/details?id={PKG}&hl=pl")
    assert ("app_package_id", f"android:{PKG}", "operator") in vals(f"market://details?id={PKG}")
    assert ("app_package_id", f"android:{PKG}", "operator") in vals(
        f"intent://open#Intent;scheme=fikcja;package={PKG};end")
    assert ("app_package_id", f"android:{PKG}", "operator") in vals(
        f"hxxps://play[.]google[.]com/store/apps/details?id={PKG}")
    assert ("app_package_id", f"android:{PKG}", "operator") in vals(
        "https://r.fikcja.example/go?u=https%3A%2F%2Fplay.google.com%2Fstore%2Fapps%2Fdetails%3Fid%3D" + PKG)
    assert ("app_package_id", "ios:id1999999901", "operator") in vals(
        "https://apps.apple.com/pl/app/fikcja-player/id1999999901")
    assert ("app_developer_id", "play_dev:9000000000000001", "operator") in vals(
        "https://play.google.com/store/apps/dev?id=9000000000000001")
    assert ("app_developer_id", "play_dev_name:jan fikcyjny studio", "operator") in vals(
        "https://play.google.com/store/apps/developer?id=Jan+Fikcyjny+Studio")
    # in-app-browser escape to Chrome: boilerplate; an impersonated bank's real app: kit
    assert ("app_package_id", "android:com.android.chrome", "boilerplate") in vals(
        "intent://x#Intent;scheme=https;package=com.android.chrome;end")
    assert ("app_package_id", "android:pl.mbank", "kit") in vals(
        "https://play.google.com/store/apps/details?id=pl.mbank")


def test_ad_library_ids_from_urls_use_meta_value_form():
    ents = find_adlibrary_ids("https://www.facebook.com/ads/library/?active_status=all&country=PL&id=900000000000001"
                              " oraz https://www.facebook.com/ads/library/?view_all_page_id=610000000000001"
                              "&amp;country=PL")
    got = {(e.kind, e.value) for e in ents}
    assert got == {("platform_ad_id", "900000000000001"), ("platform_page_id", "610000000000001")}
    assert all(e.tier == "operator" for e in ents)
    assert not find_adlibrary_ids("https://www.facebook.com/fikcja.strona?id=123456789")


def test_premium_sms_keyword_vs_code_only():
    e = {(x.kind, x.value, x.tier) for x in find_premium_sms("Wyślij SMS o treści FIK 12345 na numer 92599")}
    assert e == {("premium_sms", "92599:FIK #", "operator"), ("premium_sms_code", "92599", "platform")}
    e = {(x.kind, x.value) for x in find_premium_sms("SMS na numer 79999 o treści FIKCJAPLAY")}
    assert ("premium_sms", "79999:FIKCJAPLAY") in e
    e = {(x.kind, x.value) for x in find_premium_sms("wyślij START na 7999 aby aktywować")}
    assert ("premium_sms", "7999:START") in e
    only = {(x.kind, x.value) for x in find_premium_sms("Wyślij SMS na numer 92599")}
    assert only == {("premium_sms_code", "92599")}
    uri = {(x.kind, x.value) for x in find_premium_sms('<a href="sms:92599?body=FIK%2012345">aktywuj</a>')}
    assert ("premium_sms", "92599:FIK #") in uri
    # not premium: ordinary mobile numbers, prices, postal codes
    assert not find_premium_sms("Zadzwoń 601 234 567, kod pocztowy 00-950, cena 92 zł")
    assert not find_premium_sms("SMS na numer 60123")  # 6xxxx is not a premium short code (assumed rule)
    assert not find_premium_sms("wyślij formularz na 7000 osób")  # no SMS word, lowercase keyword


def test_carrier_billing_markers_are_kit():
    ms = find_billing_markers("Subskrypcja 17 zł co 7 dni. Płatność przez operatora, kwota doliczona do rachunku "
                              "telefonu. Usługa premium.")
    assert {m.value for m in ms} >= {"recurring_subscription", "operator_payment", "added_to_phone_bill",
                                     "premium_service"}
    assert all(m.tier == "kit" for m in ms)


def test_cloud_lb_is_platform_and_instance_operator():
    assert cloud_lb(ALB) == ("aliyun_alb", "us-east-1", "alb-fikcja0c2d4e6f8a")
    assert cloud_lb("fikcja-app-1234567890.eu-central-1.elb.amazonaws.com")[0] == "aws_elb"
    assert cloud_lb("d1fikcja000000.cloudfront.net") == ("aws_cloudfront", "-", "d1fikcja000000")
    assert cloud_lb("fikcja.global.ssl.fastly.net") == ("fastly", "-", None)  # shared front door: no instance
    assert cloud_lb("api.fikcja.example") is None
    assert TOLL_KINDS["cloud_lb_host"][0] == "inf:platform:cloud_lb"
    assert TOLL_KINDS["cloud_lb_name"][0] == "inf:operator:cloud_lb_name"


def test_certificate_fingerprint_in_text_and_shared_certs():
    (e,) = find_cert_hashes(f"Certificate SHA-256: {CERT_X}\nSubject CN: fikcja-wiki.example")
    assert e.value == CERT_X and e.tier == "operator"
    colon = ":".join(CERT_X[i:i + 2] for i in range(0, 64, 2)).upper()
    assert find_cert_hashes(f"odcisk certyfikatu TLS {colon}")[0].value == CERT_X
    assert not find_cert_hashes(f"plik reklamy o SHA-256 {CERT_X}")  # a file hash, not a certificate
    assert shared_cert_reason("x.fikcja.example", {"sans": ["*.cloudfront.net"]})
    assert shared_cert_reason("x.fikcja.example", {"sans": [f"h{i}.example" for i in range(60)]})
    assert shared_cert_reason("api.fikcja.example", {"sans": ["api.fikcja.example"]}) is None


def test_polish_labels_cover_new_kinds():
    assert set(KIND_PL_CERT) >= set(TOLL_KINDS) | {"platform_page_id"}


# ------------------------------------------------------------------------------------------------ chain (CERT)


def _har(entries: list[dict], title: str) -> bytes:
    return json.dumps({"log": {"version": "1.2", "creator": {"name": "WebInspector"}, "pages": [{"title": title}],
                               "entries": entries}}).encode()


def _entry(url: str, *, mime="text/html", text="", ip="203.0.113.10", rtype="document", tls=None) -> dict:
    e = {"startedDateTime": "2026-11-21T08:00:00.000Z", "time": 5, "_resourceType": rtype, "serverIPAddress": ip,
         "request": {"method": "GET", "url": url, "headers": []},
         "response": {"status": 200, "headers": [], "redirectURL": "",
                      "content": {"mimeType": mime, "size": len(text), "text": text}}}
    if tls:
        e["_tls"] = tls
    return e


def _creative(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + hashlib.sha256(tag.encode()).digest() * 40


def _incident(root, iid: str, *, creative: bytes | None = None, html: str | None = None,
              har: bytes | None = None) -> base.ResolvedIncident:
    arts = []
    if creative is not None:
        arts.append(_art(root, f"{iid}/reklama.png", creative, ArtifactRole.AD_CREATIVE, "image/png"))
    if html is not None:
        arts.append(_art(root, f"{iid}/strona.html", html.encode(), ArtifactRole.LANDING_HTML, "text/html"))
    if har is not None:
        arts.append(_art(root, f"{iid}/siec.har", har, ArtifactRole.HAR, "application/json"))
    return base.ResolvedIncident(Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta for a in arts]),
                                 arts)


def _page(title: str, body: str) -> str:
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def _chain(tmp_path) -> dict[str, base.ResolvedIncident]:
    play = f"https://play.google.com/store/apps/details?id={PKG}"
    incs = {
        # A and B: two ads with the identical creative file; A's landing sends to the Play package
        "A": _incident(tmp_path, "A", creative=_creative("pdf-expired"),
                       html=_page("Aktualizacja PDF", f'<p>Twoja aplikacja PDF wygasła.</p><a href="{play}">'
                                  "Aktualizuj</a>")),
        "B": _incident(tmp_path, "B", creative=_creative("pdf-expired")),
        # C: another creative and landing domain, same package; its API endpoint presents certificate X
        "C": _incident(tmp_path, "C", creative=_creative("messenger-promo"), har=_har([
            _entry("https://promo-c.fikcja-landing.example/start", text=(
                f'<html><body><h1>Nowy komunikator</h1><a href="market://details?id={PKG}">Pobierz</a></body></html>')),
            _entry("https://api.c2-fikcja.example/v1/rules", mime="application/json", text="{}", rtype="xhr",
                   ip="198.51.100.20", tls={"sha256": CERT_X, "sans": ["fikcja-wiki.example"], "verified": False,
                                            "issuer": "CN=Synthetic CA"}),
        ], "https://promo-c.fikcja-landing.example/start")),
        # D: an installed-app capture: the C2 host is an Aliyun-ALB-like name presenting the same certificate X
        "D": _incident(tmp_path, "D", har=_har([
            _entry(f"https://{ALB}/asnv", mime="application/json", text='{"task":"sms"}', rtype="xhr",
                   ip="198.51.100.77", tls={"sha256": CERT_X, "sans": ["fikcja-wiki.example"], "verified": False,
                                            "issuer": "CN=Synthetic CA"}),
        ], f"https://{ALB}/asnv")),
    }
    # fillers: unrelated ads with their own creative, package, landing host and certificate
    for k in range(8):
        cert = hashlib.sha256(f"filler-cert-{k}".encode()).hexdigest()
        incs[f"f{k}"] = _incident(tmp_path, f"f{k}", creative=_creative(f"filler-{k}"), har=_har([
            _entry(f"https://shop{k}.filler-fikcja.example/", text=(
                f'<html><body><p>Oferta {k}</p><a href="https://play.google.com/store/apps/details?id='
                f'com.filler.app{k}">App</a></body></html>'), ip=f"192.0.2.{10 + k}",
                tls={"sha256": cert, "sans": [f"shop{k}.filler-fikcja.example"], "verified": True}),
        ], f"https://shop{k}.filler-fikcja.example/"))
    return incs


def test_cert_polska_chain_links_ads_apps_and_c2(tmp_path, settings):
    import hydra.genome.artifact  # noqa: F401  (registers artifact_sha256)

    ctx = base.ExtractContext(settings=settings, work_dir=tmp_path / "work")
    only = ["artifact_sha256", "inf_url", "inf_har", "inf_dom", "inf_tollfraud"]
    genes = {}
    for iid, inc in _chain(tmp_path).items():
        _BUNDLES.clear()
        genes[iid] = base.run_all(inc, ctx, only=only).genes
    pkg = [g for g in genes["A"] if g.kind == "app_package_id"]
    assert pkg and pkg[0].value == f"android:{PKG}" and pkg[0].dependency_group == "inf:operator:app_package"
    assert gene_tier(pkg[0]) == "operator" and level(pkg[0]) == "campaign"
    lb = {g.kind: g for g in genes["D"] if g.kind.startswith("cloud_lb")}
    assert lb["cloud_lb_host"].value == "aliyun_alb:us-east-1" and level(lb["cloud_lb_host"]) == "kit"
    assert lb["cloud_lb_name"].value == "aliyun_alb:alb-fikcja0c2d4e6f8a"
    cert = next(g for g in genes["D"] if g.kind == "tls_cert_sha256")
    assert cert.value == CERT_X and cert.dependency_group == "inf:operator:cert"

    rarity = build_rarity(genes, {})
    gen = {i: build_genome(i, gs, {}) for i, gs in genes.items()}

    def pair(x, y):
        pe = score_pair(gen[x], gen[y], rarity)
        return pe, [r.explanation_pl for r in pe.score.reasons]

    ab, ab_lines = pair("A", "B")
    assert ab.score.hypothesis == "same_campaign"
    assert any(r.kind == "artifact_sha256" and r.family == Family.VIS for r in ab.score.reasons)

    ac, ac_lines = pair("A", "C")
    assert ac.score.hypothesis == "same_campaign" and ac.operator_bits >= 8
    line = next(r for r in ac.score.reasons if r.kind == "app_package_id")
    assert PKG in line.explanation_pl and line.bits > 8

    cd, cd_lines = pair("C", "D")
    assert cd.score.hypothesis == "same_campaign"
    line = next(r for r in cd.score.reasons if r.kind == "tls_cert_sha256")
    assert CERT_X in line.explanation_pl

    # no direct evidence between B (creative only) and D (C2 capture only); fillers stay unlinked
    bd, _ = pair("B", "D")
    assert bd.score.hypothesis != "same_campaign"
    for k in range(8):
        assert pair("A", f"f{k}")[0].score.hypothesis != "same_campaign"
        assert pair("C", f"f{k}")[0].score.hypothesis != "same_campaign"
    print("\nA-B", ab.score.hypothesis, ab_lines[:2], "\nA-C", ac.score.hypothesis, ac_lines[:2],
          "\nC-D", cd.score.hypothesis, cd_lines[:2], "\nB-D", bd.score.hypothesis)


def test_shared_cdn_certificate_is_boilerplate(tmp_path, settings):
    ctx = base.ExtractContext(settings=settings, work_dir=tmp_path / "work")
    cdn = hashlib.sha256(b"cdn-cert").hexdigest()
    _BUNDLES.clear()
    inc = _incident(tmp_path, "cdn", har=_har([_entry("https://x.fikcja-cdn.example/", tls={
        "sha256": cdn, "sans": ["*.cloudfront.net", "x.fikcja-cdn.example"]})], "https://x.fikcja-cdn.example/"))
    g = next(g for g in base.run_all(inc, ctx, only=["inf_har"]).genes if g.kind == "tls_cert_sha256")
    assert g.dependency_group == "inf:boilerplate:cert" and level(g) == "none"


def test_txt_and_inf_values_match_for_package_and_premium_sms(tmp_path, settings):
    """A pasted (defanged) report and a saved page give identical values, so they match across families."""
    from hydra.contracts import IncidentArtifact

    ctx = base.ExtractContext(settings=settings, work_dir=tmp_path / "work")
    text = (f"Reklama prowadziła do hxxps://play[.]google[.]com/store/apps/details?id={PKG}. "
            "Wyślij SMS o treści FIK 12345 na numer 92599. Płatność przez operatora.")
    p = tmp_path / "raport.txt"
    p.write_text(text, "utf-8")
    meta = IncidentArtifact(artifact_id=hashlib.sha256(p.read_bytes()).hexdigest(), role=ArtifactRole.TEXT,
                            path_in_incident="raport.txt", mime="text/plain", size=p.stat().st_size)
    tg = base.run_all(base.ResolvedIncident(Incident(id="t", source="test", source_ref="t"),
                                            [base.ResolvedArtifact(meta, p)]), ctx, only=["txt_entities"]).genes
    tv = {(g.kind, g.value): g for g in tg}
    assert ("app_package_id", f"android:{PKG}") in tv and ("premium_sms", "92599:FIK #") in tv
    assert tv[("app_package_id", f"android:{PKG}")].dependency_group == "TXT:operator:app_package"
    assert tv[("premium_sms_code", "92599")].dependency_group == "TXT:platform:premium_sms_code"
    assert tv[("carrier_billing_marker", "operator_payment")].dependency_group == "TXT:kit:carrier_billing"
    ev = tv[("app_package_id", f"android:{PKG}")].evidence
    assert ev.char_range is not None and "play[.]google[.]com" in text[ev.char_range[0]:ev.char_range[1]]

    _BUNDLES.clear()
    page = _page("Strona", f'<a href="https://play.google.com/store/apps/details?id={PKG}">x</a>'
                           "<p>Wyślij SMS o treści FIK 12345 na numer 92599</p>")
    ig = base.run_all(_incident(tmp_path, "p", html=page), ctx, only=["inf_tollfraud"]).genes
    iv = {(g.kind, g.value) for g in ig}
    assert {("app_package_id", f"android:{PKG}"), ("premium_sms", "92599:FIK #"),
            ("premium_sms_code", "92599")} <= iv
