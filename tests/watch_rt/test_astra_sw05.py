"""CR-ASTRA-SW-05: a known WATCH-RT item is refreshed and re-matched (Astra audit reproducers, synthetic only).

Moved from the independent audit bundle astra-software-evidence-a370d4d/test_confirmed.py; assertions unchanged,
plus a no-duplicate check for repeated identical polls.
"""
from __future__ import annotations

import json
import random
from contextlib import contextmanager

from hydra.db import Writer


@contextmanager
def _watch(settings, fetch=None):
    from hydra.watch.rt.engine import FakeClock, WatchRT

    writer = Writer(settings.db_path)
    engine = WatchRT(settings, writer, clock=FakeClock(1_800_000_000.0),
                     fetcher=fetch, rng=random.Random(1234), auto_assign=False)
    try:
        yield engine
    finally:
        engine.close()
        writer.close()


def _commit(engine, item):
    from hydra.watch.rt import store

    return engine.writer.run(lambda conn: store.commit_poll(
        conn, "meta-adlibrary", started=engine.clock.time(), duration_ms=1, items=[item],
        matches={item.external_id: engine.index.match(item.indicators)}, http_status=200,
        not_modified=False, error=None, replay=False, source_fields={}, now=engine.clock.time(),
    ))


def test_sw05_changed_external_item_updates_indicators_and_matches(settings):
    from hydra.watch.rt.sources import RawItem

    first = RawItem("ad:synthetic-1", "ad", "old.example", [("host", "old.example")],
                    "2026-11-22T03:00:00Z", {"landing_host": "old.example"})
    second = RawItem("ad:synthetic-1", "ad", "new.example", [("host", "new.example")],
                     "2026-11-22T03:01:00Z", {"landing_host": "new.example"})
    with _watch(settings) as engine:
        engine.add_entry("host", "new.example", "Synthetic updated landing")
        _commit(engine, first)
        engine.clock.advance(60)
        out = _commit(engine, second)
        with engine.reader() as conn:
            row = dict(conn.execute("SELECT * FROM watch_items").fetchone())
        assert row["payload_sha256"] == second.payload_sha256
        assert json.loads(row["indicators"]) == [["host", "new.example"]]
        assert len(out["match_ids"]) == 1
        # Repeating the identical observation must not duplicate the match or the item.
        engine.clock.advance(60)
        again = _commit(engine, second)
        assert again["match_ids"] == [] and again["item_ids"] == []
        with engine.reader() as conn:
            assert conn.execute("SELECT COUNT(*) FROM watch_items").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM watch_matches").fetchone()[0] == 1
            assert conn.execute("SELECT match_count FROM watch_items").fetchone()[0] == 1


def test_sw05_later_watchlist_entry_matches_item_when_feed_repeats_it(settings):
    from tests.watch_rt.conftest import FakeFetch

    settings = settings.model_copy(update={"offline": False})
    fetch = FakeFetch()
    fetch.bodies["openphish"] = b"https://new-watch.example/login\n"
    with _watch(settings, fetch) as engine:
        engine.set_source("openphish", enabled=True)
        before = engine.poll_source("openphish")
        assert before["new"] == 1 and before["matched"] == 0
        engine.add_entry("host", "new-watch.example", "Synthetic new watch")
        assert engine.index.match([("host", "new-watch.example")])
        engine.clock.advance(4000)
        after = engine.poll_source("openphish")
        assert after["error_pl"] is None
        assert after["matched"] == 1
        # A third identical poll is no new information: no new item, no duplicate match row.
        engine.clock.advance(4000)
        third = engine.poll_source("openphish")
        assert third["error_pl"] is None
        assert third["new"] == 0 and third["matched"] == 0
        with engine.reader() as conn:
            assert conn.execute("SELECT COUNT(*) FROM watch_matches").fetchone()[0] == 1
