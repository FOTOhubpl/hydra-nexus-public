"""META-COLLECT in WATCH-RT (real http.request path: audit, redaction, budget) and the `/v1/watch/rt/meta/*` API
with roles and CSRF (HYDRA_AUTH=1 harness from tests/auth). SYNTHETIC data, no network."""

from __future__ import annotations

import json
import random
from datetime import date

import pytest
from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.db import Writer
from hydra.meta import collector
from hydra.watch.rt.engine import FakeClock, WatchRT, load_fixture
from hydra.watch.rt.sources import SourceError
from tests.meta_collection.conftest import T0, TOKEN, ad, configure, count, db_text, make_settings, rows

M = "/v1/watch/rt/meta"


@pytest.fixture
def engine(online, writer):
    e = WatchRT(online, writer, clock=FakeClock(T0), rng=random.Random(1), auto_assign=False)
    yield e
    e.close()


def test_meta_in_watch_rt_goes_through_audited_transport(online, writer, engine, graph, token):
    configure(writer, "zysk")
    graph.pages["zysk"] = [[ad(1), ad(2)]]
    engine.set_source("meta-adlibrary", enabled=True)
    run = engine.poll_source("meta-adlibrary", force=True, replay=False)
    assert run["fetched"] == 2 and run["error_pl"] is None
    assert count(online, "SELECT COUNT(*) FROM watch_items WHERE source='meta-adlibrary'") == 2
    assert count(online, "SELECT COUNT(*) FROM meta_ads WHERE mode='live'") == 2
    assert count(online, "SELECT COUNT(*) FROM meta_polls WHERE trigger='poll_now'") == 1
    audits = [json.loads(p[0]) for p in rows(online, "SELECT payload FROM events WHERE type='INTEGRATION_REQUEST'")]
    assert audits and all(a["path"].endswith("/ads_archive") and "?" not in a["path"] for a in audits)
    assert TOKEN not in db_text(online)


def test_watch_rt_offline_and_disabled_make_no_connection(tmp_path, graph, token):
    off = make_settings(tmp_path / "off", offline=True)
    w = Writer(off.db_path)
    e = WatchRT(off, w, clock=FakeClock(T0), auto_assign=False)
    try:
        configure(w, "zysk")
        with pytest.raises(SourceError) as exc:
            e.poll_source("meta-adlibrary", force=True, replay=False)
        assert exc.value.status == "offline"
    finally:
        e.close()
        w.close()
    on = make_settings(tmp_path / "on")
    w = Writer(on.db_path)
    e = WatchRT(on, w, clock=FakeClock(T0), auto_assign=False)
    try:
        configure(w, "zysk")
        with pytest.raises(SourceError):
            e.poll_source("meta-adlibrary", force=False, replay=False)  # source disabled
        r = collector.run_cycle(on, w, today=date(2026, 11, 22))  # connector flag off: http.request refuses
        assert r.status == "partial" and "wyłączony" in r.queries[0]["reason_pl"]
    finally:
        e.close()
        w.close()
    assert graph.calls == []


def test_demo_replay_feeds_synthetic_space(online, writer, engine):
    engine.prepare_replay(load_fixture("demo"), 3600)
    engine.state = "replay"
    for i in range(len(engine.replay.fixture.ticks)):
        engine.replay_tick(i)
    assert count(online, "SELECT COUNT(*) FROM meta_ads WHERE mode='synthetic'") == 2
    assert count(online, "SELECT COUNT(*) FROM meta_ads WHERE mode='live'") == 0
    assert count(online, "SELECT COUNT(*) FROM meta_outbox WHERE mode='synthetic'") == 2


def test_meta_demo_fixture_has_media_and_is_synthetic():
    fx = load_fixture("meta-demo")
    assert fx.synthetic is True
    body = json.loads((fx.path / fx.ticks[0]["meta-adlibrary"]).read_text())
    assert body["format"] == collector.PAGES_FORMAT and body["synthetic"] is True
    rows_ = [r for p in body["pages"] for r in p["data"]["data"]]
    assert any(r.get("hydra_synthetic_media") for r in rows_)


# ------------------------------------------------------------------------------------------------ API (auth off)
@pytest.fixture
def client(online, monkeypatch):
    with TestClient(create_app(online)) as c:
        yield c


def test_api_status_config_ads_report(client, online, graph, token):
    st = client.get(f"{M}/status").json()
    assert st["collector"]["state"] in ("disabled", "engine_stopped")
    assert st["token"] == {"configured": True, "source": "env", "last4": TOKEN[-4:]}
    assert st["budget"]["limit_per_h"] == 60 and "nie limit Meta" in st["budget"]["label_pl"]
    assert set(st["counters"]) >= {"records_fetched", "unique_ads", "new_revisions", "assets_acquired",
                                   "analyses_done", "analyses_partial", "assigned", "new_patterns", "abstained",
                                   "alerts_open", "analyst_confirmed"}
    assert TOKEN not in json.dumps(st)
    r = client.put(f"{M}/config", json={"queries": [{"kind": "keyword", "value": "szybki zysk"}], "budget_per_h": 30})
    assert r.status_code == 200 and r.json()["budget_per_h"] == 30 and r.json()["languages"] == []
    assert client.put(f"{M}/config", json={"queries": [{"kind": "page_ids", "value": "abc"}]}).status_code == 422
    graph.pages["szybki zysk"] = [[ad(1)]]
    j = client.post(f"{M}/collector", json={"action": "poll_now"}).json()
    assert j["run"]["fetched"] == 1
    ads = client.get(f"{M}/ads").json()["ads"]
    assert ads[0]["ad_id"] == "8000000000001" and ads[0]["mode_label_pl"] == "NA ŻYWO"
    assert client.get(f"{M}/ads", params={"view": "unprocessed"}).json()["ads"]
    d = client.get(f"{M}/ads/8000000000001").json()
    assert d["source_link"].endswith("?id=8000000000001") and d["revisions"][0]["artifacts"] == []
    j1 = client.post(f"{M}/ads/8000000000001/analyse").json()
    j2 = client.post(f"{M}/ads/8000000000001/analyse").json()
    assert j1["created"] is False and j2["created"] is False and j1["job"]["id"] == j2["job"]["id"]  # poll queued it
    assert client.get(f"{M}/queue").json()["counts"]["queued"] == 1
    assert client.get(f"{M}/ads/nope").status_code == 404
    per = client.get(f"{M}/report", params={"mode": "synthetic"}).json()
    assert per["mode"] == "synthetic" and per["synthetic"] is True
    md = client.get(f"{M}/report", params={"format": "md"})
    assert md.status_code == 200 and "Raport obserwacji" in md.text
    assert TOKEN not in json.dumps(client.get(f"{M}/status").json())


# ------------------------------------------------------------------------------------------------ API (auth on)
def test_roles_and_csrf(tmp_path, monkeypatch):
    from tests.auth.conftest import FakeClock as AuthClock
    from tests.auth.conftest import Harness, auth_settings

    monkeypatch.delenv("HYDRA_ADMIN_PASSWORD", raising=False)
    s = auth_settings(tmp_path)
    with TestClient(create_app(s)) as c:
        h = Harness(c, s, AuthClock())
        admin = h.admin()
        viewer, analyst = h.user("viewer"), h.user("analyst")
        assert viewer.get(f"{M}/status").status_code == 200
        assert viewer.post(f"{M}/ads/x/analyse").status_code == 403
        assert viewer.post(f"{M}/collector", json={"action": "poll_now"}).status_code == 403
        assert analyst.put(f"{M}/config", json={"budget_per_h": 10}).status_code == 403
        assert analyst.post(f"{M}/diagnose").status_code == 403
        assert analyst.post(f"{M}/collector", json={"action": "start"}).status_code == 403
        assert analyst.post(f"{M}/ads/x/analyse").status_code == 404  # allowed, unknown ad
        assert admin.put(f"{M}/config", json={"budget_per_h": 10}).status_code == 200
        no_csrf = h.client()
        h.login(no_csrf, "admin", "Root-Test-Passw0rd!")
        no_csrf.headers.pop("X-CSRF-Token")
        assert no_csrf.put(f"{M}/config", json={"budget_per_h": 11}).status_code == 403
        assert h.client().get(f"{M}/status").status_code == 401
