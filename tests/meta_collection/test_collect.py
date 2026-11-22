"""META-COLLECT acceptance: collection, cursors, revisions, budget, failure handling (spec §11). SYNTHETIC data."""

from __future__ import annotations

import json
from datetime import date

import pytest

from hydra.meta import collector, contract, store
from hydra.meta import transport as tp
from tests.meta_collection.conftest import TOKEN, ad, configure, count, db_text, rows

TODAY = date(2026, 11, 22)


def cycle(online, writer, clock, **kw):
    return collector.run_cycle(online, writer, clock=clock, today=TODAY, **kw)


def test_contract_never_requests_undocumented_link_urls():
    from hydra.enrich import adlibrary as al

    assert "ad_creative_link_urls" not in contract.REQUIRED_FIELDS
    assert "ad_creative_link_urls" not in al.FIELDS
    assert contract.graph_version() == contract.GRAPH_VERSION_DEFAULT and al.GRAPH.endswith("/ads_archive")


def test_full_record_stored_with_provenance_and_queued(online, writer, clock, graph, token):
    configure(writer, "szybki zysk")
    graph.pages["szybki zysk"] = [[ad(1)]]
    r = cycle(online, writer, clock)
    assert (r.status, r.records, r.new_ads, r.new_revisions, r.requests) == ("ok", 1, 1, 1, 1)
    call = graph.graph_calls()[0]
    assert json.loads(call["ad_reached_countries"]) == ["PL"] and call["ad_type"] == "ALL"
    assert call["search_type"] == "KEYWORD_EXACT_PHRASE" and "languages" not in call  # no default pl filter
    assert set(call["fields"].split(",")) == set(contract.REQUIRED_FIELDS)
    [(content,)] = rows(online, "SELECT content FROM meta_revisions")
    c = json.loads(content)
    assert c["creative"]["source_ad_id"] == "8000000000001" and isinstance(c["creative"]["source_ad_id"], str)
    assert c["creative"]["landing_urls"] == [{"url": "https://lure-1.example/start", "provenance": "url_in_text",
                                              "field": "ad_creative_bodies"}]
    assert c["meta"]["display_domains"][0]["provenance"] == "link_caption"  # caption is not a landing URL
    assert c["meta"]["eu_transparency"]["eu_total_reach"]["status"] == "not_requested"  # never 0
    assert TOKEN not in json.dumps(c)
    assert count(online, "SELECT COUNT(*) FROM meta_outbox WHERE status='queued'") == 1
    assert count(online, "SELECT COUNT(*) FROM meta_ad_queries") == 1
    assert TOKEN not in db_text(online)


def test_optional_fields_absent_degrade_explicitly(online, writer, clock, graph, token):
    configure(writer, "a")
    writer.run(lambda c: store.set_state(c, "diagnostics", {"optional_fields": {"eu_total_reach": "available"}}, 0))
    graph.pages["a"] = [[ad(1)]]  # the row has no eu_total_reach
    cycle(online, writer, clock)
    assert "eu_total_reach" in graph.graph_calls()[0]["fields"]
    c = json.loads(rows(online, "SELECT content FROM meta_revisions")[0][0])
    eu = c["meta"]["eu_transparency"]
    assert eu["eu_total_reach"]["status"] == "unknown" and "value" not in eu["eu_total_reach"]
    assert eu["beneficiary_payers"]["status"] == "not_requested"


def test_single_token_provider_env_file_secrets_dir(online, writer, clock, graph, monkeypatch, tmp_path):
    from hydra.enrich import adlibrary as al
    from hydra.integrations import secrets
    from hydra.integrations.clients import meta_adlibrary as client

    configure(writer, "a")
    secrets.set_secret("meta-adlibrary", "META_AD_LIBRARY_TOKEN", "EAAfromSECRETSdirFILE123")  # HYDRA_SECRETS_DIR
    assert al._token() == client.token() == secrets.meta_token() == "EAAfromSECRETSdirFILE123"
    cycle(online, writer, clock)
    assert graph.graph_calls()[-1]["access_token"] == "EAAfromSECRETSdirFILE123"
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", "EAAfromENVvariable4567")
    assert al._token() == client.token() == "EAAfromENVvariable4567"
    monkeypatch.delenv("META_AD_LIBRARY_TOKEN")
    monkeypatch.delenv("HYDRA_SECRETS_DIR")
    f = tmp_path / "meta.env"
    f.write_text("META_AD_LIBRARY_TOKEN=EAAfromDEFAULTfile890\n")
    monkeypatch.setenv("HYDRA_META_ENV_FILE", str(f))
    assert al._token() == client.token() == secrets.meta_token() == "EAAfromDEFAULTfile890"
    assert secrets.meta_token_status()["source"] == "file"


def test_no_token_and_expired_token_need_config_without_request_loop(online, writer, clock, graph, monkeypatch):
    configure(writer, "a")
    r = cycle(online, writer, clock)
    assert r.status == "needs_config" and graph.calls == []
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", TOKEN)
    graph.token_error = True
    r = cycle(online, writer, clock)
    assert r.status == "needs_config" and len(graph.calls) == 1
    for _ in range(3):
        assert cycle(online, writer, clock).status == "needs_config"
    assert len(graph.calls) == 1  # rejected token: no retries until a new token is saved
    monkeypatch.setenv("META_AD_LIBRARY_TOKEN", TOKEN + "new")
    graph.token_error = False
    assert cycle(online, writer, clock).status in ("ok", "empty") and len(graph.calls) == 2


def test_multipage_with_failure_resumes_without_loss(online, writer, clock, graph, token):
    configure(writer, "zysk", max_pages_per_query=5)
    graph.pages["zysk"] = [[ad(1), ad(2)], [ad(3)], [ad(4)]]
    graph.fail[("zysk", 1)] = [(500, {"error": {"code": 2, "message": f"boom access_token={TOKEN}"}}, {})]
    r = cycle(online, writer, clock)
    assert r.status == "partial" and r.records == 2
    q = store.list_queries.__wrapped__ if hasattr(store.list_queries, "__wrapped__") else None  # noqa: F841
    [(cur,)] = rows(online, "SELECT cursor FROM meta_queries")
    assert json.loads(cur)["live"]["after"] == "C1"
    clock.advance(1800)
    r2 = cycle(online, writer, clock)
    assert r2.status == "ok" and r2.records == 2
    assert graph.graph_calls()[-2]["after"] == "C1"  # resumed from the stored cursor, never paging.next
    assert count(online, "SELECT COUNT(*) FROM meta_ads") == 4
    assert TOKEN not in db_text(online)


def test_crash_before_commit_rereads_page_idempotently(online, writer, clock, graph, token, monkeypatch):
    configure(writer, "a")
    graph.pages["a"] = [[ad(1), ad(2)]]
    real = store.set_query_cursor

    def boom(*a, **k):
        raise RuntimeError("crash between observation write and cursor update")

    monkeypatch.setattr(store, "set_query_cursor", boom)
    with pytest.raises(RuntimeError):
        cycle(online, writer, clock)
    assert count(online, "SELECT COUNT(*) FROM meta_ads") == 0  # one transaction: nothing half-written
    monkeypatch.setattr(store, "set_query_cursor", real)
    cycle(online, writer, clock)
    cycle(online, writer, clock)  # the same page read again (new pass)
    assert count(online, "SELECT COUNT(*) FROM meta_ads") == 2
    assert count(online, "SELECT COUNT(*) FROM meta_revisions") == 2
    assert count(online, "SELECT COUNT(*) FROM meta_outbox") == 2
    assert count(online, "SELECT SUM(seen_count) FROM meta_ads") == 4


def test_same_ad_from_two_queries_one_revision_two_provenances(online, writer, clock, graph, token):
    configure(writer, "a", "b")
    graph.pages["a"] = [[ad(1)]]
    graph.pages["b"] = [[ad(1)]]
    cycle(online, writer, clock)
    assert count(online, "SELECT COUNT(*) FROM meta_revisions") == 1
    assert count(online, "SELECT COUNT(*) FROM meta_ad_queries") == 2
    assert count(online, "SELECT COUNT(*) FROM meta_outbox") == 1


def test_identical_text_different_ids_stay_separate(online, writer, clock, graph, token):
    configure(writer, "a")
    graph.pages["a"] = [[ad(1, body="Ten sam tekst"), ad(2, body="Ten sam tekst")]]
    cycle(online, writer, clock)
    assert count(online, "SELECT COUNT(*) FROM meta_ads") == 2
    assert count(online, "SELECT COUNT(*) FROM meta_outbox") == 2


def test_changed_creative_is_a_new_revision_and_last_seen_is_not(online, writer, clock, graph, token):
    configure(writer, "a")
    graph.pages["a"] = [[ad(1, body="wersja 1")]]
    cycle(online, writer, clock)
    clock.advance(3600)
    graph.pages["a"] = [[ad(1, body="wersja 1", ad_delivery_stop_time="2026-11-25", publisher_platforms=["instagram"])]]
    cycle(online, writer, clock)
    assert count(online, "SELECT COUNT(*) FROM meta_revisions") == 1  # metadata only: no revision, no extraction
    assert count(online, "SELECT COUNT(*) FROM meta_outbox") == 1
    c = json.loads(rows(online, "SELECT content FROM meta_revisions")[0][0])
    assert c["meta"]["source_delivery_stop"] == "2026-11-25"
    graph.pages["a"] = [[ad(1, body="wersja 2: https://new-domain.example/")]]
    cycle(online, writer, clock)
    assert rows(online, "SELECT rev FROM meta_revisions ORDER BY rev") == [(1,), (2,)]
    assert count(online, "SELECT COUNT(*) FROM meta_outbox") == 2
    assert rows(online, "SELECT revisions, seen_count FROM meta_ads") == [(2, 3)]


def test_own_budget_poll_now_and_retry_share_one_limit(online, writer, clock, graph, token):
    configure(writer, "a", "b", "c", budget_per_h=3, max_pages_per_query=2)
    for t in "abc":
        graph.pages[t] = [[ad(ord(t))], [ad(ord(t) + 100)]]
    r = cycle(online, writer, clock)
    assert r.status == "deferred_budget" and len(graph.calls) == 3
    assert "budżet" in r.message_pl and "nie oznacza braku reklam" in r.message_pl
    r = cycle(online, writer, clock, trigger="poll_now")
    assert r.status == "deferred_budget" and len(graph.calls) == 3  # poll-now: same budget, no request
    v = tp.budget_view(online, 3, clock())
    assert v["used_60m"] == 3 and v["remaining"] == 0 and v["deferred_until"]
    clock.advance(3601)
    r = cycle(online, writer, clock)
    assert len(graph.calls) > 3
    # rotation: the query that hit the budget is served next, the first query did not eat the whole pool
    terms = [c.get("search_terms") for c in graph.graph_calls()]
    assert terms[0] == terms[1] != terms[2] and terms[3] == terms[2]


def test_rate_limit_613_and_retry_after_defer_instead_of_looping(online, writer, clock, graph, token):
    configure(writer, "a")
    graph.fail[("a", 0)] = [(400, {"error": {"code": 613, "message": "rate"}}, {"retry-after": "120"})]
    graph.pages["a"] = [[ad(1)]]
    r = cycle(online, writer, clock)
    assert r.status == "rate_limited" and len(graph.calls) == 1
    assert cycle(online, writer, clock).status == "rate_limited" and len(graph.calls) == 1
    clock.advance(121)
    assert cycle(online, writer, clock).status == "ok" and len(graph.calls) == 2
    graph.headers = {"x-business-use-case-usage": json.dumps({"1": [{"type": "ads_archive", "call_count": 12,
                                                                     "estimated_time_to_regain_access": 0}]})}
    cycle(online, writer, clock)
    assert tp.load_state(online)["last_usage"]["x-business-use-case-usage"]["1"][0]["call_count"] == 12


def test_offline_makes_no_connection(tmp_path, graph, token):
    from hydra.db import Writer
    from tests.meta_collection.conftest import make_settings

    off = make_settings(tmp_path, offline=True)
    w = Writer(off.db_path)
    try:
        configure(w, "a")
        r = collector.run_cycle(off, w)
        assert r.status == "offline" and graph.calls == []
    finally:
        w.close()


def test_partial_response_is_not_ok(online, writer, clock, graph, token):
    configure(writer, "a")
    graph.pages["a"] = [[ad(1), {"page_name": "bez id"}]]
    r = cycle(online, writer, clock)
    assert r.status == "partial" and r.records == 1
    assert "bez identyfikatora" in r.queries[0]["reason_pl"]


def test_cursor_loop_detected(online, writer, clock, graph, token):
    from hydra.integrations import http

    configure(writer, "a", max_pages_per_query=5)
    graph.pages["a"] = [[ad(1)], [ad(2)]]

    def looping(method, url, headers, body, timeout, max_bytes):
        st, h, b = graph(method, url, headers, body, timeout, max_bytes)
        d = json.loads(b)
        d["paging"] = {"cursors": {"after": "C1"}, "next": "https://graph.facebook.com/x"}  # same cursor forever
        return st, h, json.dumps(d).encode()

    http.set_transport(looping)
    r = cycle(online, writer, clock)
    assert len(graph.calls) == 2 and r.status == "partial" and r.records == 2
    assert "Zapętlony" in r.queries[0]["reason_pl"]


def test_replay_and_live_spaces_are_separate(online, writer, clock, graph, token):
    configure(writer, "a")
    graph.pages["a"] = [[ad(1)]]
    cycle(online, writer, clock)
    body = json.dumps({"format": collector.PAGES_FORMAT, "synthetic": True,
                       "pages": [{"query": {"kind": "keyword", "value": "a"}, "page": 0,
                                  "data": {"data": [ad(1, body="inna treść syntetyczna")]}}]}).encode()
    collector.replay_body(online, writer, body, now=clock())
    cycle(online, writer, clock)
    assert rows(online, "SELECT mode, revisions FROM meta_ads ORDER BY mode") == [("live", 1), ("synthetic", 1)]
    from contextlib import closing

    from hydra.db import connect

    with closing(connect(online.db_path, readonly=True)) as conn:
        assert store.counters(conn, "live")["unique_ads"] == 1 and store.counters(conn, "synthetic")["unique_ads"] == 1
        assert [q["origin"] for q in store.config(conn)["queries"]].count("replay") == 1


def test_recording_spans_pages_and_queries_and_replays_offline(online, writer, clock, graph, token, monkeypatch):
    monkeypatch.setenv("HYDRA_WATCH_RECORD", "1")
    configure(writer, "a", "b", max_pages_per_query=3)
    graph.pages["a"] = [[ad(1)], [ad(2)]]
    graph.pages["b"] = [[ad(3)]]
    r = cycle(online, writer, clock, recorder=collector.recorder_for(online))
    assert r.records == 3
    rec = online.state_dir / "watch" / "rt" / "recordings"
    files = sorted(rec.rglob("*-meta-adlibrary.json"))
    assert len(files) == 1
    text = files[0].read_text()
    assert TOKEN not in text and "access_token" not in text and '"next"' not in text
    man = json.loads((files[0].parent / "manifest.json").read_text())
    assert man["synthetic"] is False
    res = collector.replay_body(online, writer, files[0].read_bytes(), now=clock())
    assert res.mode == "recorded" and res.records == 3
    assert count(online, "SELECT COUNT(*) FROM meta_ads WHERE mode='recorded'") == 3


def test_scheduler_delegates_to_the_single_collector(online, writer, clock, graph, token):
    from hydra import scheduler

    configure(writer, "a")
    graph.pages["a"] = [[ad(1)]]
    out = scheduler.JOBS["adlibrary_pull"](online, writer, scheduler.load_watchlist(online))
    assert out["delegated"] is True and out["new_revisions"] == 1
    assert count(online, "SELECT COUNT(*) FROM meta_polls WHERE trigger='scheduler'") == 1


def test_diagnostics_layers_id_ok_but_importer_contract_rejected(online, writer, clock, graph, token):
    from hydra.integrations.clients import meta_adlibrary as client
    from hydra.meta import diagnose

    graph.reject_fields = {"ad_creation_time"}  # `fields=id` works, the importer's field set does not
    d = diagnose.run(online, writer, clock=clock)
    st = {x["id"]: x["status"] for x in d["layers"]}
    assert st == {"token": "pass", "query_pl_all": "pass", "required_fields": "fail", "optional_fields": "not_run",
                  "normalise_store": "not_run"} and d["ok"] is False
    ok, status, msg = client.test(online, writer)
    assert (ok, status) == (False, "error") and msg.startswith("Import niesprawny")
    graph.reject_fields = set()
    graph.pages["inwestycja"] = [[ad(1)]]
    d = diagnose.run(online, writer, clock=clock)
    st = {x["id"]: x["status"] for x in d["layers"]}
    assert d["ok"] is True and st["normalise_store"] == "pass" and st["optional_fields"] == "pass"
    graph.pages["inwestycja"] = [[]]
    d = diagnose.run(online, writer, clock=clock)
    assert {x["id"]: x["status"] for x in d["layers"]}["normalise_store"] == "skipped"  # empty 200 is not a pass
    assert TOKEN not in json.dumps(d)
