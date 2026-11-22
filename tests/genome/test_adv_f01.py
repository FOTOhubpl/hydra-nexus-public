"""ADV-F01 (HYDRA_ADV_F01=1): extraction-accuracy fixes for CR-F01-1..6. SYNTHETIC fixtures only.

Each test checks the flag-off behaviour (unchanged, the defect still reproduces) and the flag-on fix.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hydra.genome import adv_f01
from hydra.genome.operator_ids import find_accounts, find_messenger_handles
from hydra.genome.txt_entities import find_brands, find_hosts, find_persons, find_phones
from hydra.genome.webparse import Body
from tests.extract_accuracy import harness as H


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(adv_f01.FLAG, "1")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv(adv_f01.FLAG, "0")  # F01 is default-on since 2026-11-22


PHONES = ("Konsultant +420 601 126 480 lub +380 50 123 4567, Berlin 0049 151 234 567 89; "
          "PL (+48) 600 000 111, +48 512 908 364, tel. 733 112 909")


def test_versions(off, monkeypatch):
    assert adv_f01.version("txt_entities", 6) == 6 and adv_f01.version("inf_har", 3) == 3
    monkeypatch.setenv(adv_f01.FLAG, "1")
    assert adv_f01.version("txt_entities", 6) == 7 and adv_f01.version("inf_dom", 5) == 6
    assert adv_f01.version("inf_har", 3) == 4 and adv_f01.version("txt_lines", 1) == 2
    assert adv_f01.version("inf_url", 3) == 3


def test_foreign_phones_off_unchanged(off):
    v = [e.value for e in find_phones(PHONES)]
    assert "+48601126480" in v and "+48501234567" in v  # CR-F01-1 reproduces without the flag


def test_foreign_phones_on(on):
    v = [e.value for e in find_phones(PHONES)]
    assert v == ["+48600000111", "+48512908364", "+48733112909"]


def test_foreign_phone_context_only_foreign_cc():
    spans = adv_f01.foreign_spans("(+48) 601 222 333")
    assert not adv_f01.foreign_phone("(+48) 601 222 333", 6, 17, spans)
    t = "+44 20 7946 0958 i +49 30 123 456 789"
    assert adv_f01.foreign_phone(t, t.index("123"), t.index("123") + 11, adv_f01.foreign_spans(t))


def test_dom_phone_rx_foreign(on, monkeypatch):
    from hydra.genome.dom import _contact_genes
    from hydra.genome.webparse import GeneSink, pii_hash

    def run():
        sink = GeneSink("i1", "inf_dom@x")
        _contact_genes(sink, "Infolinia +420 601 126 480 oraz 604 873 215", None)
        return {g.value for g in sink.genes if g.kind == "entity_phone"}

    assert run() == {pii_hash("+48604873215")}
    monkeypatch.setenv(adv_f01.FLAG, "0")
    assert pii_hash("+48601126480") in run()


def test_iban_boundary_off_unchanged(off):
    assert [e.value for e in find_accounts("IBAN ES91 2100 0418 4502 0005 1332 Złośliwy")] == \
        ["ES9121000418450200051332Z"]


@pytest.mark.parametrize(("text", "want"), [
    ("IBAN ES91 2100 0418 4502 0005 1332 Złośliwy adres", "ES9121000418450200051332"),
    ("IBAN DE89 3704 0044 0532 0130 00 Łódź", "DE89370400440532013000"),
    ("konto PL61 1090 1014 0000 0712 1981 2874 1234 tytułem", "PL61109010140000071219812874"),
    ("konto PL61 1O90 1014 0000 0712 1981 2874", "PL61109010140000071219812874"),   # OCR O -> 0
    ("GB82 WEST 1234 5698 7654 32", "GB82WEST12345698765432"),                       # letters kept where allowed
])
def test_iban_on(on, text, want):
    acc = [e for e in find_accounts(text) if e.kind == "entity_iban"]
    assert [(e.value, e.quality) for e in acc] == [(want, 1.0)]


def test_iban_invalid_stays_invalid(on):
    acc = [e for e in find_accounts("Konto PL62 1090 1014 0000 0712 1981 2874 (stary)") if e.kind == "entity_iban"]
    assert len(acc) == 1 and acc[0].quality == 0.4  # no repair search: a wrong check digit stays a decoy


def test_persons(off, monkeypatch):
    assert find_persons("Wywiad z udziałem: Krzysztofa Stanowskiego") == []
    monkeypatch.setenv(adv_f01.FLAG, "1")
    assert [e.value for e in find_persons("Wywiad z udziałem: Krzysztofa Stanowskiego")] == ["Krzysztof Stanowski"]
    assert [e.value for e in find_persons("Rozmowa z Rafała Kowalczewskiego o lokatach")] == ["Rafał Kowalczewski"]
    assert adv_f01.lemma_person("Anny", "Kowalczewskiej") == ("Anna", "Kowalczewska")
    assert adv_f01.lemma_person("Jan", "Kowal") == ("Jan", "Kowal")


def test_brands(off, monkeypatch):
    t = "Jak informuje Onet, promocja Lidl. Fakt: Orange Polska i PayPal."
    assert [e.value for e in find_brands(t)] == []
    monkeypatch.setenv(adv_f01.FLAG, "1")
    v = [e.value for e in find_brands(t)]
    assert v == ["onet", "lidl", "orange", "paypal"]  # bare 'Fakt' (a common word) is not the newspaper
    assert [e.value for e in find_brands("nowy plan, gra w play")] == []


def test_telegram(off, monkeypatch):
    assert find_hosts("Napisz: t.me/abc_defgh") == []
    assert find_messenger_handles("Telegram: @abcd") == []
    monkeypatch.setenv(adv_f01.FLAG, "1")
    assert [e.value for e in find_hosts("Napisz: t.me/abc_defgh")] == ["t.me", "https://t.me/abc_defgh"]
    assert [e.value for e in find_messenger_handles("Telegram: @abcd")] == ["telegram:@abcd"]
    assert find_messenger_handles("pisz @abcd") == []  # still needs the Telegram context


# ------------------------------------------------------------------------------------------- redirects

def _req(url, entry, status=200, mime="text/html", rtype="document"):
    from urllib.parse import urlsplit

    return {"url": url, "host": urlsplit(url).hostname, "status": status, "mime": mime, "type": rtype,
            "entry": entry, "method": "GET"}


def test_body_hops_follow_and_loop():
    a, b, c = "https://a.example/x", "https://b.example/r", "https://c.example/land"
    h = SimpleNamespace(
        requests=[_req(a, 0, 302), _req(b, 1), _req(c, 2), _req(b, 3)],
        redirects=[{"kind": "http302", "from": a, "to": b, "entry": 0}],
        bodies=[Body(b, "text/html", b'<meta http-equiv="refresh" content="0;url=/land">' +
                     b'<script>location.href="https://c.example/land"</script>', 1),
                Body(c, "text/html", b"<script>window.location='https://b.example/r'</script>", 2)],
        doc_headers=[])
    hops = adv_f01.body_hops(h)
    # b -> /land is resolved against b (b.example/land is never requested: dropped); b -> c and the loop c -> b kept
    assert [(r["kind"], r["from"], r["to"]) for r in hops] == [("js_location", b, c), ("js_location", c, b)]
    chain = adv_f01.follow_chain(h.redirects + hops)
    assert [r["to"] for r in chain] == [b, c, b]  # the loop back is one hop, then it stops (each hop used once)


def test_unfollowed_js_target_is_not_a_hop():
    a, b = "https://a.example/", "https://b.example/"
    h = SimpleNamespace(requests=[_req(a, 0)], redirects=[], doc_headers=[],
                        bodies=[Body(a, "text/html", b"if(bot){location.href='https://b.example/'}", 0)])
    assert b not in {r["to"] for r in adv_f01.body_hops(h)}  # cloaking branch never taken: not a navigation


def test_refresh_header_hop():
    a, b = "https://a.example/", "https://b.example/z"
    h = SimpleNamespace(requests=[_req(a, 0), _req(b, 1)], redirects=[], bodies=[],
                        doc_headers=[(0, "a.example", [("Refresh", "0; url=https://b.example/z")])])
    assert [(r["kind"], r["to"]) for r in adv_f01.body_hops(h)] == [("http_refresh", b)]


def test_body_hops_linear_time():
    """3,000 requests x 1,500 script bodies with 5 unfollowed location targets each: indexed lookups (the first
    version scanned the remaining requests per target and took 56 s here)."""
    import time

    reqs = [_req(f"https://h{i % 50}.example/p{i}?q={i}", i, mime="application/javascript", rtype="script")
            for i in range(3000)]
    js = "".join(f"if(x){{location.href='https://t{j}.example/a{j}'}}" for j in range(5)).encode()
    h = SimpleNamespace(requests=reqs, redirects=[], doc_headers=[],
                        bodies=[Body(r["url"], "application/javascript", js, r["entry"]) for r in reqs[:1500]])
    t0 = time.perf_counter()
    assert adv_f01.body_hops(h) == []
    assert time.perf_counter() - t0 < 5.0


def test_har_meta_refresh_chain_product_path(tmp_path, settings, monkeypatch):
    g = H.Gen(4)
    cases = [H.har_case(tmp_path, g, i) for i in range(4)]

    def chains():
        out = {}
        for c in cases:
            r = H.run_case(c, settings)
            out[c.carrier, c.case_id] = (r["observed"]["inf_har"], c.truth["inf_har"])
        return out

    monkeypatch.setenv(adv_f01.FLAG, "1")
    for (carrier, _), (obs, truth) in chains().items():
        want = {v for k, v in truth if k == "redirect_chain"}
        got = {v for k, v in obs if k == "redirect_chain"}
        assert got == want, carrier


def test_extra_brands_do_not_shadow_lexicon(on):
    """An added brand must not fire on the name of a brand the shipped lexicon already has (other id)."""
    import json
    from pathlib import Path

    extra = {t.id for t, _ in adv_f01.extra_brands()}
    lex = json.loads((Path(adv_f01.__file__).parent / "lexicons/pl_brands.json").read_text("utf-8"))
    assert not extra & {b["id"] for b in lex["brands"]}
    for b in lex["brands"]:
        hits = {e.value for e in find_brands(f"Jak informuje {b['name']}, program jest legalny")}
        assert not hits & extra, (b["name"], hits & extra)
