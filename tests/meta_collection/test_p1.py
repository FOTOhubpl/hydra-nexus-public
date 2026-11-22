"""META-COLLECT P1: bounded expansion, "nowa reklama, ten sam schemat" from stored analyses, evidence tasks.
SYNTHETIC data only."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import date

from hydra.db import connect
from hydra.meta import collector, scheme, store
from hydra.watch.rt.engine import load_fixture
from tests.meta_collection.conftest import ad, configure, count, rows
from tests.meta_collection.test_analysis import based, extractors, worker  # noqa: F401 — fixtures

TODAY = date(2026, 11, 22)


def test_expansion_is_bounded_explicit_and_shares_the_budget(online, writer, clock, graph, token):
    configure(writer, "zysk", expansion={"enabled": True, "max_depth": 1, "max_pages": 2})
    graph.pages["zysk"] = [[ad(1, page="111"), ad(2, page="222"), ad(3, page="333")]]
    collector.run_cycle(online, writer, clock=clock, today=TODAY)
    exp = rows(online, "SELECT kind, value, origin, depth, parent_ad_id FROM meta_queries WHERE origin='expansion' "
                       "ORDER BY value")
    assert exp == [("page_ids", "111", "expansion", 1, "8000000000001"),
                   ("page_ids", "222", "expansion", 1, "8000000000002")]  # max_pages = 2
    graph.pages["pages:111"] = [[ad(10, page="111")]]
    graph.pages["pages:222"] = [[ad(11, page="999")]]
    clock.advance(1800)
    collector.run_cycle(online, writer, clock=clock, today=TODAY)
    assert {c.get("search_page_ids") for c in graph.graph_calls()} >= {"111", "222"}
    # depth 2 never added (max_depth = 1): page 999 found through an expansion query stays a plain observation
    assert count(online, "SELECT COUNT(*) FROM meta_queries WHERE value='999'") == 0
    configure(writer, "zysk", expansion={"enabled": False})
    n = len(graph.calls)
    clock.advance(1800)
    collector.run_cycle(online, writer, clock=clock, today=TODAY)
    assert all(c.get("search_terms") == "zysk" for c in graph.calls[n:])  # switched off: no expansion queries


def test_scheme_view_from_meta_demo_replay(online, writer, clock, based):  # noqa: F811
    fx = load_fixture("meta-demo")
    for t in fx.ticks:
        collector.replay_body(online, writer, (fx.path / t["meta-adlibrary"]).read_bytes(), now=clock())
        clock.advance(60)
    assert worker(online, writer, clock, based).run_until_idle() >= 5
    with closing(connect(online.db_path, readonly=True)) as conn:
        s1 = scheme.compare(conn, "synthetic", "syn-meta-1")
        s3 = scheme.compare(conn, "synthetic", "syn-meta-3")
        a = store.analysis(conn, conn.execute("SELECT id FROM meta_analyses WHERE ad_id='syn-meta-3' ORDER BY id DESC "
                                              "LIMIT 1").fetchone()[0])
    v = s1["versions"][0]
    assert v["domain_change"] == {"removed": ["promo.zysk-orlen-demo.example"], "added": ["zysk-orlen-demo2.example"]}
    assert v["kept"]["phones"] == ["+48512000111"]
    # ad 3 (another advertiser) reuses ad 1's picture: shown side by side, not merged
    apart = {x["ad_id"]: x for x in s3["lookalike_not_merged"]}
    assert "syn-meta-1" in apart and apart["syn-meta-1"]["shared"]["media_sha256"]
    assert "nie są samodzielnym dowodem" in apart["syn-meta-1"]["why_not_merged_pl"]
    assert a["decision"] != "assign" and a["correlation"]["evidence_tasks"]
    assert s1["mode_label_pl"] == "DANE SYNTETYCZNE"
    assert json.dumps(s1)  # serialisable for the API
