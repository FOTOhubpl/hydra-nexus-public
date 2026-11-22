"""GAPS_PAPERS G05: cloaking taxonomy flags and the cloaking-implementation fingerprint (synthetic landers)."""

from __future__ import annotations

import pytest
from fixtures import _art, _synthetic_warninglists  # noqa: F401

from hydra.contracts import Incident
from hydra.genome.base import ExtractContext, ResolvedIncident, load_family_modules, run_all
from hydra.genome.webparse import CLOAK_RX, Scan, scan_text
from hydra.synapse import level

load_family_modules()

MOUSE_A = """<html><body><h1>Oferta</h1><script>
var moved = false;
document.addEventListener('mousemove', function () {
  if (moved) { return; }
  moved = true;
  if (navigator.webdriver) { return; }
  window.location.href = 'https://lure-a-fixture.live/offer?sub1=1';
});
</script></body></html>"""
# same gate, renamed variables, different redirect URL
MOUSE_B = (MOUSE_A.replace("moved", "seenUser").replace("https://lure-a-fixture.live/offer?sub1=1",
                                                          "https://zupelnie-inna-fixture.pl/x/y?z=9"))
TIMER = """<html><body><script>
var t0 = 1;
setTimeout(function () { if (navigator.userAgent.indexOf('Googlebot') > -1) { return; }
  window.location.replace('https://lure-c-fixture.live/'); }, 4000);
</script></body></html>"""
ANALYTICS = """<html><body><script>
var payload = {ref: document.referrer, page: location.pathname, ts: Date.now()};
navigator.sendBeacon('/collect', JSON.stringify(payload));
</script></body></html>"""


def _flags(txt: str) -> set[str]:
    acc = Scan()
    scan_text(txt, "t", acc)
    return set(acc.cloak)


@pytest.mark.parametrize("name,code", [
    ("mouse_gate", "document.addEventListener('mousemove', go)"),
    ("alert_gate", "alert('Gratulacje!'); window.location = '/win'"),
    ("click_gate", "btn.addEventListener('click', function(){ location.href = '/go' })"),
    ("cookie_check", "if (document.cookie.indexOf('seen=1') < 0) { x() }"),
    ("ua_check", "if (/Googlebot|bingbot/i.test(navigator.userAgent)) { stay() }"),
    ("timer_gate", "setTimeout(function(){ location.href='/next' }, 3000)"),
    ("random_gate", "if (Math.random() < 0.3) { go() }"),
    ("captcha_gate", '<div class="g-recaptcha" data-sitekey="x"></div>'),
    ("random_path_redirect", "location.href = '/' + Math.random().toString(36).slice(2)"),
    ("ip_geo_lookup", "fetch('http://www.geoplugin.net/json.gp')"),
    ("referrer_check", "if (document.referrer.indexOf('facebook') === -1) { white() }"),
])
def test_new_flags(name, code):
    assert name in CLOAK_RX and name in _flags(code)


def test_analytics_referrer_read_is_not_a_check():
    assert "referrer_check" not in _flags(ANALYTICS)


def _impl(txt: str) -> set[str]:
    acc = Scan()
    scan_text(txt, "t", acc)
    return set(acc.cloak_impl)


def test_same_gate_renamed_shares_impl_different_gate_does_not():
    a, b, c = _impl(MOUSE_A), _impl(MOUSE_B), _impl(TIMER)
    assert a and a == b
    assert c and not (c & a)
    assert _impl(ANALYTICS) == set()


def test_library_bodies_do_not_give_impl():
    acc = Scan()
    scan_text(MOUSE_A, "lib", acc, redirects=False)  # third-party / boilerplate host body
    assert acc.cloak and not acc.cloak_impl


def test_cloak_impl_gene_is_kit_tier(settings, tmp_path):
    art = _art(tmp_path, "Page.html", MOUSE_A.encode(), "landing_html", "text/html")
    inc = ResolvedIncident(Incident(id="ck-1", source="t", source_ref="ck-1", artifacts=[art.meta]), [art])
    genes = run_all(inc, ExtractContext(settings=settings, work_dir=tmp_path), only=["inf_dom"]).genes
    impl = [g for g in genes if g.kind == "cloak_impl"]
    assert len(impl) == 1 and impl[0].dependency_group == "inf:kit:cloak_impl" and level(impl[0]) == "kit"
    flags = {g.value for g in genes if g.kind == "cloak_feature"}
    assert {"mouse_gate", "webdriver_check"} <= flags
