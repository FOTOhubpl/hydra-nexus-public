"""Fail-closed network rules: offline polls nothing; live polls go through hydra.integrations.http (audited)."""

from __future__ import annotations

import asyncio
import json

import pytest

from hydra.integrations import http
from hydra.integrations import state as istate
from hydra.watch.rt.engine import MSG_OFFLINE_PL
from hydra.watch.rt.sources import SourceError

from .conftest import count, source_row


def test_offline_poll_raises_and_fetches_nothing(offline, fetch, make_engine):
    eng = make_engine(offline, fetch)
    eng.set_source("cert-pl", enabled=True)
    for force in (False, True):
        with pytest.raises(SourceError) as ei:
            eng.poll_source("cert-pl", force=force)
        assert ei.value.status == "offline" and ei.value.message_pl == MSG_OFFLINE_PL
    eng.state = "live"
    assert asyncio.run(eng.step()) == []
    assert eng.state == "offline"
    assert asyncio.run(eng.step()) == [] and eng.state == "offline"  # stays offline while HYDRA_OFFLINE=1
    assert fetch.count() == 0
    assert count(eng, "SELECT COUNT(*) FROM watch_runs") == 0
    assert count(eng, "SELECT COUNT(*) FROM watch_items") == 0
    st = eng.status()
    assert st["engine"]["offline"] is True and st["engine"]["message_pl"] == MSG_OFFLINE_PL
    assert next(s for s in st["sources"] if s["id"] == "cert-pl")["state"] == "offline"


def test_offline_with_default_live_fetcher_sends_nothing(offline, transport, make_engine):
    eng = make_engine(offline)  # the real live fetcher (hydra.integrations.http)
    eng.set_source("cert-pl", enabled=True)
    with pytest.raises(SourceError):
        eng.poll_source("cert-pl", force=True)
    assert transport.calls == []


def _lines(*hosts: str) -> bytes:
    return ("\n".join(json.dumps({"RegisterPositionId": f"t-{i}", "DomainAddress": h, "ActionType": "block",
                                  "ActionTime": "2026-11-22T03:00:00"}) for i, h in enumerate(hosts)) + "\n").encode()


def test_live_poll_goes_through_audited_http(online, transport, public_dns, make_engine):
    transport.get_body = _lines("a-one.example", "b-two.example")
    eng = make_engine(online)
    eng.set_source("cert-pl", enabled=True)  # also the connector's enabled flag (operator consent)
    assert istate.is_enabled(online, "cert-pl")
    run = eng.poll_source("cert-pl")
    assert run["error_pl"] is None and run["new"] == 2
    assert [m for m, _ in transport.calls] == ["HEAD", "GET"]
    assert all(u.startswith("https://hole.cert.pl/domains/v2/actions_") for _, u in transport.calls)
    with eng.reader() as conn:
        rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM events WHERE type=?", (http.EVENT,))]
    assert len(rows) == 2 and {r["connector"] for r in rows} == {"cert-pl"}
    assert {r["method"] for r in rows} == {"HEAD", "GET"} and all(r["host"] == "hole.cert.pl" for r in rows)

    # connector switched off in Integracje (watch source still enabled): refused before any request
    istate.set_enabled(online, "cert-pl", False)
    n = len(transport.calls)
    eng.clock.advance(120)
    run = eng.poll_source("cert-pl")
    assert run["error_status"] == "disabled" and len(transport.calls) == n
    row = source_row(eng, "cert-pl")
    assert row["state"] == "error" and row["consecutive_errors"] == 0


def test_replay_poll_without_replay_never_uses_live_fetcher(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    with pytest.raises(SourceError) as ei:
        eng.poll_source("cert-pl", force=True, replay=True)
    assert ei.value.status == "replay" and fetch.count() == 0
