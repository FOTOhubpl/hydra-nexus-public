"""Scheduler: due times, seeded jitter, interval clamps, backoff, circuit breaker (FakeClock, no sleeps)."""

from __future__ import annotations

import asyncio
import random

from hydra.watch.rt import store
from hydra.watch.rt.engine import CB_OPEN_S, CB_THRESHOLD, JITTER, MAX_BACKOFF_S
from hydra.watch.rt.sources import ADAPTERS, SourceError, effective_min_interval

from .conftest import SEED, T0, source_row


def _due_in(eng, sid: str) -> float:
    return store.parse_iso(source_row(eng, sid)["next_due"]) - eng.clock.time()


def _step(eng) -> list[dict]:
    return asyncio.run(eng.step())


def test_enabled_source_due_now_then_jittered_interval(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    assert eng.due(T0) == []  # every source starts disabled
    eng.set_source("cert-pl", enabled=True)
    assert source_row(eng, "cert-pl")["next_due"] == store.iso(T0)
    assert eng.due(T0) == ["cert-pl"]
    eng.state = "live"
    runs = _step(eng)
    assert [r["source"] for r in runs] == ["cert-pl"] and runs[0]["error_pl"] is None
    # next_due = now + interval * (1 + U(0, 0.10)) drawn from the engine's seeded rng (first draw)
    expected = T0 + 60 * (1.0 + random.Random(SEED).uniform(0.0, JITTER))
    row = source_row(eng, "cert-pl")
    assert row["next_due"] == store.iso(expected) and row["state"] == "no_new"  # success without new items
    assert 60 <= _due_in(eng, "cert-pl") <= 60 * (1 + JITTER)
    calls = fetch.count()
    # not due one second before next_due: nothing is fetched
    eng.clock.t = store.parse_iso(row["next_due"]) - 1
    assert eng.due(eng.clock.time()) == [] and _step(eng) == [] and fetch.count() == calls
    eng.clock.t += 1
    assert [r["source"] for r in _step(eng)] == ["cert-pl"] and fetch.count() > calls


def test_interval_clamped_to_effective_min(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    assert effective_min_interval(ADAPTERS["openphish"]) >= 3600
    v = eng.set_source("openphish", enabled=True, interval_s=60)
    assert v["interval_s"] >= 3600 and source_row(eng, "openphish")["interval_s"] >= 3600
    # even a row edited below the policy (older build, manual SQL) is polled at the policy interval
    eng.writer.run(lambda c: c.execute("UPDATE watch_sources SET interval_s=60 WHERE id='openphish'"))
    assert eng.status()["sources"][2]["interval_s"] >= 3600
    run = eng.poll_source("openphish")
    assert run["error_pl"] is None
    assert 3600 <= _due_in(eng, "openphish") <= 3600 * (1 + JITTER)


def test_disabled_source_never_polled(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    eng.set_source("cert-pl", enabled=True)
    eng.state = "live"
    for _ in range(30):
        _step(eng)
        eng.clock.advance(600)
    assert fetch.count("cert-pl") > 0
    assert {c[0] for c in fetch.calls} == {"cert-pl"}  # urlhaus / openphish / crtsh / meta stay disabled
    eng.set_source("cert-pl", enabled=False)
    n = fetch.count()
    for _ in range(5):
        eng.clock.advance(3600)
        assert _step(eng) == []
    assert fetch.count() == n and source_row(eng, "cert-pl")["state"] == "disabled"


def test_interval_change_via_set_source(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    eng.set_source("cert-pl", enabled=True, interval_s=300)
    eng.poll_source("cert-pl")
    assert 300 <= _due_in(eng, "cert-pl") <= 300 * (1 + JITTER)
    eng.set_source("cert-pl", interval_s=120)
    eng.clock.advance(400)
    eng.poll_source("cert-pl")
    assert 120 <= _due_in(eng, "cert-pl") <= 120 * (1 + JITTER)


def test_backoff_doubles_then_circuit_opens_then_half_open(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    eng.set_source("cert-pl", enabled=True)
    eng.state = "live"
    fetch.fail = SourceError("http", "CERT Polska: HTTP 503.", 503)
    for n in range(1, CB_THRESHOLD):
        runs = _step(eng)
        assert runs and runs[0]["error_status"] == "http"
        row = source_row(eng, "cert-pl")
        assert row["consecutive_errors"] == n and row["state"] == "backoff"
        b = min(MAX_BACKOFF_S, 60 * 2 ** n)
        assert b <= _due_in(eng, "cert-pl") <= b * (1 + JITTER)
        eng.clock.t = store.parse_iso(row["next_due"])
    _step(eng)  # fifth consecutive error
    row = source_row(eng, "cert-pl")
    now = eng.clock.time()
    assert row["consecutive_errors"] == CB_THRESHOLD and row["state"] == "circuit_open"
    assert row["circuit_open_until"] == store.iso(now + CB_OPEN_S) == row["next_due"]
    calls = fetch.count()
    eng.clock.advance(CB_OPEN_S - 1)
    assert _step(eng) == [] and fetch.count() == calls  # open: nothing attempted
    eng.clock.advance(1)
    _step(eng)  # half-open trial: exactly one poll, fails -> open again
    assert fetch.count() > calls
    row = source_row(eng, "cert-pl")
    assert row["state"] == "circuit_open" and row["consecutive_errors"] == CB_THRESHOLD + 1
    assert row["circuit_open_until"] == store.iso(eng.clock.time() + CB_OPEN_S)
    fetch.fail = None
    eng.clock.advance(CB_OPEN_S)
    runs = _step(eng)
    row = source_row(eng, "cert-pl")
    assert runs[0]["error_pl"] is None
    assert row["state"] == "no_new" and row["consecutive_errors"] == 0 and row["circuit_open_until"] is None
    assert row["last_error"] is None and 60 <= _due_in(eng, "cert-pl") <= 66


def test_backoff_cap(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    assert eng.backoff_s(60, 10) == MAX_BACKOFF_S == 3600
    assert eng.backoff_s(1200, 2) == 3600 and eng.backoff_s(60, 2) == 240
    eng.set_source("cert-pl", enabled=True, interval_s=1200)
    fetch.fail = SourceError("http", "x", 500)
    eng.poll_source("cert-pl")
    eng.clock.advance(3000)
    eng.poll_source("cert-pl")  # 1200 * 2^2 = 4800 -> capped at 3600 (+ jitter)
    assert MAX_BACKOFF_S <= _due_in(eng, "cert-pl") <= MAX_BACKOFF_S * (1 + JITTER)


def test_needs_key_and_rate_limited_do_not_trip_breaker(online, fetch, make_engine):
    eng = make_engine(online, fetch)
    eng.set_source("urlhaus", enabled=True)  # no URLHAUS_AUTH_KEY anywhere (isolated HOME / secrets dir)
    for _ in range(CB_THRESHOLD + 2):
        run = eng.poll_source("urlhaus")
        assert run["error_status"] == "needs_key"
        eng.clock.advance(120)
    row = source_row(eng, "urlhaus")
    assert row["state"] == "needs_key" and row["consecutive_errors"] == 0 and row["circuit_open_until"] is None
    assert fetch.count("urlhaus") == 0  # nothing is sent without the key
    eng.set_source("cert-pl", enabled=True)
    fetch.fail = SourceError("rate_limited", "Limit zapytań.")
    for _ in range(CB_THRESHOLD + 2):
        assert eng.poll_source("cert-pl")["error_status"] == "rate_limited"
        row = source_row(eng, "cert-pl")
        assert row["state"] == "backoff" and row["next_due"] == store.iso(eng.clock.time() + 60)
        eng.clock.advance(60)
    assert source_row(eng, "cert-pl")["consecutive_errors"] == 0
