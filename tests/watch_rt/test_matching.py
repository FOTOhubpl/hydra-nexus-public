"""Indicator normalisation, exact-match index, strength, rarity bits, campaign-derived watchlist (synthetic DB)."""

from __future__ import annotations

import json
import math

import pytest

from hydra.events import utcnow
from hydra.genome.webparse import pii_hash
from hydra.watch.rt import indicators as ind
from hydra.watch.rt import store
from hydra.watch.rt.watchlist import Entry, Index, derive, rarity_bits, strength

from .conftest import T0

IBAN = "PL61 1090 1014 0000 0712 1981 2874"


@pytest.mark.parametrize(("kind", "raw", "key"), [
    ("host", "WWW.Example-Shop.COM.", "www.example-shop.com"),
    ("host", "https://Login.Bank-Demo.example:8443/x", "login.bank-demo.example"),
    ("host", "promo[.]demo[.]example", "promo.demo.example"),
    ("host", "10.0.0.1", None),
    ("host", "not a host", None),
    ("domain", "login.sub.example.co.uk", "example.co.uk"),
    ("domain", "a.b.zysk-demo.example", "zysk-demo.example"),
    ("url", "HTTPS://Ex.Example:443/a?b=1#frag", "https://ex.example/a?b=1"),
    ("url", "hxxp://ex[.]example", "http://ex.example/"),
    ("url", "ftp://ex.example/", None),
    ("ip", "[2001:db8::1]", "2001:db8::1"),
    ("phone", "+48 512 000 111", "+48512000111"),
    ("phone", "512-000-111", "+48512000111"),
    ("phone", "0048 512 000 111", "+48512000111"),
    ("phone", "12", None),
    ("iban", IBAN, pii_hash(IBAN.replace(" ", ""))),
    ("iban", "XX00", None),
    ("wallet", "0x52908400098527886E0F7030069857D2E4169EE7", "0x52908400098527886E0F7030069857D2E4169EE7"),
    ("wallet", "short", None),
    ("tracker", "g-demo12345", "G-DEMO12345"),
    ("keyword", "Zysk-Orlen", "zysk-orlen"),
    ("keyword", "a b", None),
])
def test_normalise(kind, raw, key):
    assert ind.normalise(kind, raw) == key


def test_hashed_gene_values_stay_hash_keys():
    """Genes store IBANs (always) and inf_dom phones as the salted hash `h:<hex>`: the key is the hash itself, never
    digits extracted from the hex (regression: 'h:327a...' used to become the phone '+327768780674')."""
    hp = pii_hash("+48512000111")
    assert ind.normalise("phone", hp) == hp and ind.normalise("phone", hp.upper()) == hp
    hi = pii_hash(IBAN.replace(" ", ""))
    assert ind.normalise("iban", hi) == hi and ind.normalise("iban", hi.upper()) == hi
    assert ind.normalise("phone", "h:zz") is None and ind.normalise("iban", "h:") is None
    assert "skrót" in ind.display("phone", hp) and "skrót" in ind.display("iban", hi)


def test_extraction_from_text_and_url():
    pairs = ind.from_text(f"Zadzwoń +48 512 000 111, G-ABC12345, 0x52908400098527886E0F7030069857D2E4169EE7, {IBAN}")
    kinds = {k for k, _ in pairs}
    assert {"phone", "tracker", "wallet", "iban"} <= kinds
    assert ("iban", pii_hash(IBAN.replace(" ", ""))) in pairs  # never the clear IBAN
    assert not any(IBAN.replace(" ", "") in v for _, v in pairs)
    u = ind.from_url("https://Promo.Zysk-Demo.example/start?x=1")
    assert u == [("url", "https://promo.zysk-demo.example/start?x=1"), ("host", "promo.zysk-demo.example"),
                 ("domain", "zysk-demo.example")]
    assert ind.from_host("10.1.2.3") == [("ip", "10.1.2.3")]


def _index(entries: list[tuple[str, str, str]]) -> Index:
    idx = Index()
    for i, (kind, value, origin) in enumerate(entries, 1):
        idx.by_key[(kind, ind.normalise(kind, value))].append(
            Entry(i, kind, ind.normalise(kind, value), origin, "", None, None, [], None, None))
    return idx


def test_exact_lookup_per_kind_and_strength():
    idx = _index([("host", "promo.demo.example", "manual"), ("domain", "other-demo.example", "manual"),
                  ("url", "https://pay-demo.test/pay/", "manual"), ("phone", "+48 512 000 111", "manual"),
                  ("iban", IBAN, "manual"), ("wallet", "0x52908400098527886E0F7030069857D2E4169EE7", "manual"),
                  ("tracker", "G-DEMO12345", "manual"), ("ip", "198.51.100.7", "manual"),
                  ("phone", pii_hash("+48600100300"), "campaign")])
    cases = {
        "host": ind.from_host("promo.demo.example"),
        "domain": ind.from_host("login.other-demo.example"),
        "url": ind.from_url("https://pay-demo.test/pay/"),
        "phone": ind.from_text("tel. 512 000 111"),
        "iban": ind.from_text(IBAN),
        "wallet": ind.from_text("send to 0x52908400098527886E0F7030069857D2E4169EE7"),
        "tracker": ind.from_text("gtag G-DEMO12345"),
        "ip": ind.from_host("198.51.100.7"),
    }
    for kind, pairs in cases.items():
        hits = idx.match(pairs)
        assert [h[0] for h in hits] == [kind], kind
        assert strength(kind, hits[0][2]) == ("weak" if kind in ("domain", "ip") else "strong")
    # a plain feed phone matches a hashed (inf_dom) campaign phone
    hits = idx.match(ind.from_text("Zadzwoń: +48 600 100 300"))
    assert len(hits) == 1 and hits[0][2].origin == "campaign" and hits[0][1] == "+48600100300"
    # near misses never match (exact lookup only)
    assert idx.match(ind.from_host("promo.demo.example.evil.example")) == []
    assert idx.match(ind.from_url("https://pay-demo.test/pay/x")) == []
    assert idx.match(ind.from_text("tel. 512 000 112")) == []


def test_rarity_bits():
    assert rarity_bits(2, 9) == round(math.log2(10 / 3), 2)
    assert rarity_bits(None, 9) is None and rarity_bits(1, 0) is None


# ------------------------------------------------------------------ campaign-derived watchlist (synthetic DB)
RUN = "run-syn-1"


def _seed(conn) -> None:
    ts = utcnow()
    conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES (?, 'analyse', '{}',"
                 " '{}', ?, ?)", (RUN, ts, ts))
    for i in range(1, 6):
        conn.execute("INSERT INTO incidents (id, source, source_ref, created_at) VALUES (?, 'test', ?, ?)",
                     (f"I{i}", f"ref{i}", ts))
    for cid, label in (("C1", "Kampania syntetyczna 1"), ("C2", "Kampania syntetyczna 2")):
        conn.execute("INSERT INTO campaigns (id, run_id, label, detail) VALUES (?, ?, ?, '{}')", (cid, RUN, label))
    for cid, iid in (("C1", "I1"), ("C1", "I2"), ("C2", "I3"), ("C2", "I4")):
        conn.execute("INSERT INTO campaign_members (run_id, campaign_id, incident_id) VALUES (?, ?, ?)",
                     (RUN, cid, iid))
    genes = [
        ("I1", "landing_host", "Promo.Op-Alpha.example", "inf:operator:landing"),
        ("I2", "landing_host", "promo.op-alpha.example", "inf:operator:landing"),
        ("I5", "landing_host", "promo.op-alpha.example", "inf:operator:landing"),  # not a campaign member: df only
        ("I1", "entity_phone", "+48 600 100 200", "txt:operator:contact"),
        ("I3", "entity_phone", pii_hash("+48600100300"), "inf:operator:contact"),
        ("I2", "entity_phone", "+48 800 100 100", "txt:hotline:contact"),  # no operator tier: excluded
        ("I3", "tracker_id", "G-OPALPHA1", "inf:operator:tracker"),
        ("I4", "landing_host", "facebook.com", "inf:operator:landing"),  # platform host: excluded
        ("I4", "entity_iban", pii_hash(IBAN.replace(" ", "")), "inf:operator:contact"),
        ("I4", "registrable_domain", "op-beta.example", "inf:operator:domain"),
    ]
    for iid, kind, value, dep in genes:
        conn.execute("INSERT INTO genes (incident_id, family, kind, value, status, provenance, extractor, "
                     "dependency_group, created_at) VALUES (?, 'INF', ?, ?, 'observed', 'OBS', 'test', ?, ?)",
                     (iid, kind, value, dep, ts))


def test_derive_campaign_watchlist(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    eng.writer.run(_seed)
    with eng.reader() as conn:
        entries = derive(conn)
    got = {(e["kind"], e["value"], e["campaign_id"]) for e in entries}
    assert got == {
        ("host", "promo.op-alpha.example", "C1"),
        ("phone", "+48600100200", "C1"),
        ("phone", pii_hash("+48600100300"), "C2"),
        ("tracker", "G-OPALPHA1", "C2"),
        ("iban", pii_hash(IBAN.replace(" ", "")), "C2"),
        ("domain", "op-beta.example", "C2"),
    }
    host = next(e for e in entries if e["kind"] == "host")
    assert host["df"] == 3 and host["n_incidents"] == 5 and host["incident_ids"] == ["I1", "I2"]
    assert host["label"] == "Kampania syntetyczna 1" and host["run_id"] == RUN

    res = eng.rebuild_watchlist()
    assert res["added"] == 6 and res["removed"] == 0
    assert eng.rebuild_watchlist()["added"] == 0  # idempotent
    # a manual and a campaign-origin keyword: only the manual one is ever sent to crt.sh
    eng.writer.run(lambda c: store.replace_campaign_entries(c, [
        *[{**e, "incident_ids": e["incident_ids"]} for e in entries],
        {"kind": "keyword", "value": "op-alpha", "display": "op-alpha", "campaign_id": "C1", "run_id": RUN,
         "label": "x", "incident_ids": ["I1"], "df": 1, "n_incidents": 5}], T0))
    eng.add_entry("keyword", "Zysk-Demo", None)
    assert eng.index.keywords == ["zysk-demo"]

    # feed items through the engine: matches carry campaign, strength and informational bits
    lines = [{"RegisterPositionId": "m-1", "DomainAddress": "promo.op-alpha.example", "ActionType": "block",
              "ActionTime": "2026-11-22T03:00:00"},
             {"RegisterPositionId": "m-2", "DomainAddress": "shop.op-beta.example", "ActionType": "block",
              "ActionTime": "2026-11-22T03:00:00"},
             {"RegisterPositionId": "m-3", "DomainAddress": "facebook.com", "ActionType": "block",
              "ActionTime": "2026-11-22T03:00:00"}]
    eng.fetcher.bodies["cert-pl"] = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()
    eng.set_source("cert-pl", enabled=True)
    run = eng.poll_source("cert-pl")
    assert run["new"] == 3 and run["matched"] == 2
    with eng.reader() as conn:
        ms = {m["indicator_kind"]: m for m in store.list_matches(conn)}
    assert set(ms) == {"host", "domain"}
    assert ms["host"]["campaign_id"] == "C1" and ms["host"]["strength"] == "strong"
    assert ms["host"]["bits"] == round(math.log2((5 + 1) / (3 + 1)), 2) and ms["host"]["df"] == 3
    assert ms["host"]["incident_ids"] == ["I1", "I2"] and ms["host"]["origin"] == "campaign"
    assert ms["domain"]["campaign_id"] == "C2" and ms["domain"]["strength"] == "weak"
    assert ms["domain"]["bits"] == round(math.log2(6 / 2), 2)
    # operator identifiers in free text hit the campaign entries (hashed phone + hashed IBAN)
    hits = eng.index.match(ind.from_text(f"Zadzwoń +48 600 100 300 albo przelew {IBAN}, gtag G-OPALPHA1"))
    assert {(h[0], h[2].campaign_id) for h in hits} == {("phone", "C2"), ("iban", "C2"), ("tracker", "C2")}


def test_crtsh_receives_manual_keywords_only(online, fetch, make_engine):
    fetch.bodies["crtsh"] = b"[]"
    eng = make_engine(online, fetch)
    eng.writer.run(lambda c: store.replace_campaign_entries(c, [
        {"kind": "keyword", "value": "secret-campaign", "display": "secret-campaign", "campaign_id": "C1",
         "run_id": RUN, "label": None, "incident_ids": [], "df": 1, "n_incidents": 1}], T0))
    eng.reload_index()
    eng.set_source("crtsh", enabled=True)
    run = eng.poll_source("crtsh")
    assert run["error_pl"] is None and fetch.count("crtsh") == 0  # no manual keyword: nothing sent
    eng.add_entry("keyword", "zysk-demo", None)
    eng.clock.advance(120)
    eng.poll_source("crtsh")
    urls = [u for c, _, u in fetch.calls if c == "crtsh"]
    assert len(urls) == 1 and "zysk-demo" in urls[0] and "secret-campaign" not in urls[0]
