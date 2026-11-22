"""BONUS-AUTO: search rules (keywords, date of appearance, other rules), periodic re-check summary, new material
linked to EXISTING campaigns (Meta assignments and WATCH-RT matches), the overview API and the synthetic replay
probe. SYNTHETIC base corpus and ads (.example), test extractors only, no network."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.db import connect
from hydra.meta import analysis, auto, collector, store
from hydra.watch.rt.engine import load_fixture
from tests.meta_collection.conftest import T0, ad, configure, count
from tests.meta_collection.test_analysis import based, extractors, worker  # noqa: F401  (fixtures)

M = "/v1/watch/rt/meta"


def _conn(s):
    return closing(connect(s.db_path, readonly=True))


# ------------------------------------------------------------------------------------------------ rules
def test_rules_roundtrip_and_validation(online, writer):
    cfg = configure(writer, "zarabiaj", date_from="2026-11-01", date_to="2026-11-30", platforms=["instagram",
                                                                                                    "FACEBOOK"],
                    rules={"keywords": ["lokata życia"], "celebrities": ["Jan Fikcyjny"],
                           "page_name_patterns": [r"inwest\w*\s+demo"], "same_text_min_pages": 4,
                           "alert_min_priority": "high"})
    assert cfg["date_from"] == "2026-11-01" and cfg["date_to"] == "2026-11-30"
    assert cfg["platforms"] == ["FACEBOOK", "INSTAGRAM"]
    assert cfg["rules"]["page_name_patterns"] == [r"inwest\w*\s+demo"] and cfg["rules"]["same_text_min_pages"] == 4
    for bad in ({"date_from": "2026-13-01"}, {"date_from": "2026-12-01", "date_to": "2026-11-01"},
                {"platforms": ["MYSPACE"]}, {"rules": {"page_name_patterns": ["(unclosed"]}},
                {"rules": {"alert_min_priority": "urgent"}}, {"rules": {"same_text_min_pages": 1}}):
        with pytest.raises(ValueError):
            writer.run(lambda c, b=bad: store.put_config(c, {**b}, T0))
    cfg = writer.run(lambda c: store.put_config(c, {"date_from": "", "date_to": ""}, T0))
    assert cfg["date_from"] is None and cfg["date_to"] is None
    assert cfg["rules"]["keywords"] == ["lokata życia"]  # partial patch keeps the other rules


def test_date_and_platform_rules_reach_the_graph_query(online, writer, clock, graph, token):
    configure(writer, "zarabiaj", date_from="2026-11-20", date_to="2026-11-30", platforms=["FACEBOOK"],
              date_window_days=7)
    graph.pages["zarabiaj"] = [[ad(1)]]
    r = collector.run_cycle(online, writer, clock=clock, today=date(2026, 11, 22))
    assert r.status == "ok"
    [call] = graph.graph_calls()
    # rolling window = today - 7 = 2026-11-15; the operator floor 2026-11-20 is later, so it wins
    assert call["ad_delivery_date_min"] == "2026-11-20"
    assert call["ad_delivery_date_max"] == "2026-11-30"
    assert json.loads(call["publisher_platforms"]) == ["FACEBOOK"]
    assert call["search_terms"] == "zarabiaj"


def test_local_rules_in_triage():
    content = {"creative": {"source_ad_id": "1", "source_page_id": "9", "source_page_name": "Inwestycje Demo PL",
                            "creative_bodies": ["Lokata życia z Janem Fikcyjnym, tylko dziś."],
                            "creative_link_titles": [], "creative_link_captions": [], "creative_link_descriptions": [],
                            "landing_urls": []}, "meta": {}}
    plain = analysis.triage(content, {}, set(), [])
    assert not any(r["code"] == "page_name_pattern" for r in plain["reasons"])
    rules = {"keywords": ["lokata życia"], "celebrities": ["Jan Fikcyjny"], "page_name_patterns": [r"inwest\w*\s+demo"]}
    t = analysis.triage(content, {}, set(), [], rules)
    codes = {r["code"]: r for r in t["reasons"]}
    assert "page_name_pattern" in codes and "reguła operatora" in codes["page_name_pattern"]["source"]
    assert any("lokata życia" in r["description_pl"] and "reguła operatora" in r["source"] for r in t["reasons"])
    # same text on many pages: threshold is configurable (default 3 pages = this + 2 others)
    assert any(r["code"] == "same_text_many_pages" for r in analysis.triage(content, {}, set(), ["2", "3"])["reasons"])
    assert not any(r["code"] == "same_text_many_pages" for r in analysis.triage(
        content, {}, set(), ["2", "3"], {"same_text_min_pages": 4})["reasons"])


# ------------------------------------------------------------------------------------------------ cycles
def test_cycle_summary_new_changed_unchanged_ended(online, writer, clock, graph, token):
    configure(writer, "zarabiaj")
    graph.pages["zarabiaj"] = [[ad(1), ad(2), ad(3)]]
    collector.run_cycle(online, writer, clock=clock, today=date(2026, 11, 22))
    clock.advance(1800)
    graph.pages["zarabiaj"] = [[ad(1), ad(2, body="Zmieniona oferta https://lure-2b.example/x"),
                                ad(3, ad_delivery_stop_time="2026-11-22"), ad(4)]]
    collector.run_cycle(online, writer, clock=clock, today=date(2026, 11, 22))
    with _conn(online) as conn:
        c2, c1 = auto.cycles(conn, "live")[:2]
    assert (c1["new_ads"], c1["changed_ads"], c1["ads_seen"]) == (3, 0, 3)
    assert (c2["new_ads"], c2["changed_ads"], c2["unchanged_ads"], c2["ended_ads"]) == (1, 1, 2, 1)
    assert c2["trigger"] == "schedule" and c2["finished_at"] > c1["finished_at"]


# ------------------------------------------------------------------------------------------------ campaign links
@pytest.fixture
def probe(monkeypatch):
    """The test corpus has no landing-host genes (its extractor says `host`), so the probe is given explicitly: the
    indicators of synthetic campaign A of tests.core.test_pipeline.corpus."""
    def fake(conn):
        cid = conn.execute("SELECT campaign_id FROM campaign_members WHERE incident_id='nask-1' ORDER BY run_id DESC "
                           "LIMIT 1").fetchone()[0]
        return {"campaign_id": cid, "run_id": None, "hosts": ["evil-a.example"], "phones": ["+48 700 100 100"],
                "urls": ["https://evil-a.example/start?pixel=1234567890123456"]}

    monkeypatch.setattr(auto, "campaign_probe", fake)


def test_replay_probe_assigns_to_existing_campaign_and_is_listed(online, writer, clock, based, probe):  # noqa: F811
    fx = load_fixture("auto-search-demo")
    body0 = (fx.path / fx.ticks[0]["meta-adlibrary"]).read_bytes()
    events = []
    r = collector.replay_body(online, writer, body0, now=clock())
    assert r.mode == "synthetic" and r.new_ads == 3
    w = worker(online, writer, clock, based)
    w.emit = lambda t, d: events.append((t, d))
    assert w.run_until_idle() == 3
    with _conn(online) as conn:
        links = auto.meta_links(conn, mode="synthetic")
        cid = conn.execute("SELECT campaign_id FROM campaign_members WHERE incident_id='nask-1'").fetchone()[0]
        ov = auto.overview(conn, "synthetic")
        cl = auto.campaign_links(conn, cid)
    assert [x["ad_id"] for x in links] == ["syn-auto-1"]
    lk = links[0]
    assert lk["campaign_id"] == cid and lk["mode_label_pl"] == "DANE SYNTETYCZNE" and lk["evidence"]
    assert lk["incident_id"].startswith("metaad-syn-") and lk["first_observed_at"]
    assert cl["meta_ads"][0]["ad_id"] == "syn-auto-1"
    # the stream event carries the decision (top bar: "nowa reklama powiązana z kampanią")
    ev = [d for t, d in events if t == "meta.analysis" and d.get("decision") == "assign"]
    assert ev and ev[0]["campaign_id"] == cid and ev[0]["ad_id"] == "syn-auto-1"
    # overview numbers are the DB counters
    assert ov["totals"]["assigned"] == count(online, "SELECT COUNT(*) FROM meta_analyses WHERE mode='synthetic' AND "
                                                     "decision='assign'") == 1
    assert ov["totals"]["linked_campaigns"] == 1
    assert sum(ov["triage"].values()) == 3 and ov["cycles"][0]["new_ads"] == 3
    # second tick: 1 new, 1 changed, 1 ended, probe unchanged
    body1 = (fx.path / fx.ticks[1]["meta-adlibrary"]).read_bytes()
    r2 = collector.replay_body(online, writer, body1, now=clock() + 60)
    assert (r2.new_ads, r2.new_revisions) == (1, 2)
    with _conn(online) as conn:
        c = auto.cycles(conn, "synthetic")[0]
    assert (c["new_ads"], c["changed_ads"], c["unchanged_ads"], c["ended_ads"]) == (1, 1, 2, 1)


def test_probe_rows_dropped_without_a_base_campaign(online, writer, clock):
    fx = load_fixture("auto-search-demo")
    r = collector.replay_body(online, writer, (fx.path / fx.ticks[0]["meta-adlibrary"]).read_bytes(), now=clock())
    assert r.new_ads == 2  # no analysed campaign: the probe row is skipped, never shown with a placeholder text
    assert count(online, "SELECT COUNT(*) FROM meta_ads WHERE ad_id='syn-auto-1'") == 0


def test_probe_row_text():
    row = auto.probe_row({"id": "x", "hydra_campaign_probe": True},
                         {"campaign_id": "C1", "hosts": ["a.example", "www.a.example"], "phones": ["+48700100100"],
                          "urls": []})
    assert "hydra_campaign_probe" not in row and "https://a.example/" in row["ad_creative_bodies"][0]
    assert "SYNTETYCZNA" in row["ad_creative_bodies"][0] and "+48700100100" in row["ad_creative_bodies"][0]


def test_campaign_probe_from_member_genes():
    import sqlite3

    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE runs (id TEXT, kind TEXT, created_at TEXT);
        CREATE TABLE campaigns (id TEXT, run_id TEXT, label TEXT);
        CREATE TABLE campaign_members (run_id TEXT, campaign_id TEXT, incident_id TEXT);
        CREATE TABLE genes (incident_id TEXT, kind TEXT, value TEXT, dependency_group TEXT, status TEXT);
        INSERT INTO runs VALUES ('r1', 'analyse', '2026-11-22');
        INSERT INTO campaign_members VALUES ('r1', 'C-big', 'i1'), ('r1', 'C-big', 'i2'), ('r1', 'C-one', 'i3');
        INSERT INTO genes VALUES ('i1', 'landing_host', 'shop-a.example', 'inf:operator:landing', 'observed'),
          ('i2', 'landing_host', 'shop-a.example', 'inf:operator:landing', 'observed'),
          ('i1', 'entity_phone', '+48600200300', 'TXT:operator:contact', 'observed'),
          ('i1', 'entity_phone', 'h:0123', 'inf:operator:contact', 'observed'),
          ('i1', 'phrase', 'przelew na pl27114020040000300201355387 teraz', 'TXT:phrase:phrase_ad', 'observed'),
          ('i1', 'headline', 'buty wyprzedaz', 'TXT:phrase:headline', 'observed'),
          ('i1', 'tracker_id', 'fb_pixel:987654321012345', 'inf:operator:tracker', 'observed'),
          ('i3', 'landing_host', 'single.example', 'inf:operator:landing', 'observed');
    """)
    p = auto.campaign_probe(c)
    assert p["campaign_id"] == "C-big" and p["hosts"] == ["shop-a.example"] and p["phones"] == ["+48600200300"]
    body = auto.probe_row({"id": "x", "hydra_campaign_probe": True}, p)["ad_creative_bodies"][0]
    for part in ("SYNTETYCZNA", "https://shop-a.example/", "+48600200300", "PL27114020040000300201355387",
                 "Buty wyprzedaz", "987654321012345"):
        assert part in body
    assert "h:0123" not in body


def test_alert_threshold_rule(online, writer, clock, graph, token, based):  # noqa: F811
    configure(writer, "zysk")
    graph.pages["zysk"] = [[ad(1, body="Wygrana! Odbierz nagrodę teraz https://x-1.example/a"),
                            ad(2, body="Ogrodnictwo https://ogrod.example")]]
    collector.run_cycle(online, writer, clock=clock, today=date(2026, 11, 22))
    worker(online, writer, clock, based).run_until_idle()
    with _conn(online) as conn:
        pr = dict(conn.execute("SELECT ad_id, triage_priority FROM meta_analyses").fetchall())
        n_med = store.counters(conn, "live")["alerts_open"]
    writer.run(lambda c: store.put_config(c, {"rules": {"alert_min_priority": "low"}}, T0))
    with _conn(online) as conn:
        n_low = store.counters(conn, "live")["alerts_open"]
        alerts_low = store.list_ads(conn, "live", view="alerts")["ads"]
    expect_low = sum(1 for p in pr.values() if p in ("low", "medium", "high"))
    expect_med = sum(1 for p in pr.values() if p in ("medium", "high"))
    assert (n_low, n_med) == (expect_low, expect_med)
    assert len(alerts_low) == n_low


# ------------------------------------------------------------------------------------------------ API
def test_api_auto_overview_and_campaign_links(online, monkeypatch):
    with TestClient(create_app(online)) as c:
        ov = c.get(f"{M}/auto", params={"mode": "synthetic"})
        assert ov.status_code == 200, ov.text
        d = ov.json()
        assert d["mode_label_pl"] == "DANE SYNTETYCZNE" and d["linked_ads"] == [] and "budget" in d
        assert d["rules"]["local_keywords_default"] > 0 and "collector" in d
        lk = c.get(f"{M}/campaigns/C-1/links", params={"run": "r1"})
        assert lk.status_code == 200 and lk.json()["meta_ads"] == [] and lk.json()["watch_matches"] == []
        put = c.put(f"{M}/config", json={"date_from": "2026-11-01", "platforms": ["INSTAGRAM"],
                                         "rules": {"page_name_patterns": ["demo"], "alert_min_priority": "high"}})
        assert put.status_code == 200, put.text
        assert put.json()["rules"]["alert_min_priority"] == "high" and put.json()["platforms"] == ["INSTAGRAM"]
        bad = c.put(f"{M}/config", json={"rules": {"page_name_patterns": ["(x"]}})
        assert bad.status_code == 422
