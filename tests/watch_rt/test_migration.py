"""Schema v6 (WATCH-RT): v5 -> latest keeps existing rows; a DB that passed v6 elsewhere gets the tables at init."""

from __future__ import annotations

from contextlib import closing

from hydra import db
from hydra.db import MIGRATIONS, Writer, connect, migrate, schema_version
from hydra.watch.rt.engine import WatchRT
from hydra.watch.rt.sources import ADAPTERS

WATCH_TABLES = {"watch_sources", "watch_items", "watch_watchlist", "watch_matches", "watch_runs"}


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _at_v5(path, monkeypatch) -> None:
    assert [v for v, _ in MIGRATIONS[:5]] == [1, 2, 3, 4, 5]
    with closing(connect(path)) as conn:
        monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS[:5])
        assert migrate(conn) == 5
        monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS)
        conn.execute("INSERT INTO artifacts (id, mime, size, source_ref, created_at) VALUES ('a' , 'text/plain', 3, "
                     "'x.txt', '2026-11-22T00:00:00+00:00')")
        conn.execute("INSERT INTO incidents (id, source, source_ref, created_at) VALUES ('I1', 't', 'r', "
                     "'2026-11-22T00:00:00+00:00')")
        assert not (_tables(conn) & WATCH_TABLES)


def test_v5_to_latest(tmp_path, monkeypatch):
    path = tmp_path / "hydra.db"
    _at_v5(path, monkeypatch)
    with closing(connect(path)) as conn:
        assert migrate(conn) == MIGRATIONS[-1][0]
        assert schema_version(conn) == MIGRATIONS[-1][0]
        assert WATCH_TABLES <= _tables(conn)
        assert conn.execute("SELECT id, size FROM artifacts").fetchall()[0][:] == ("a", 3)
        assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        assert migrate(conn) == MIGRATIONS[-1][0]  # idempotent


def test_db_past_v6_without_watch_tables_gets_them_at_engine_init(settings, monkeypatch):
    """Another branch's migration (the latest version) reached the DB first, so migrate() skips v6: WatchRT init
    creates the tables through the Writer (store.ensure_schema) and never touches user_version."""
    top = MIGRATIONS[-1][0]
    _at_v5(settings.db_path, monkeypatch)
    with closing(connect(settings.db_path)) as conn:
        conn.execute(f"PRAGMA user_version={top}")
    w = Writer(settings.db_path)
    try:
        with closing(connect(settings.db_path, readonly=True)) as conn:
            assert not (_tables(conn) & WATCH_TABLES) and schema_version(conn) == top
        eng = WatchRT(settings, w)
        with closing(connect(settings.db_path, readonly=True)) as conn:
            assert WATCH_TABLES <= _tables(conn)
            assert schema_version(conn) == top
            assert {r[0] for r in conn.execute("SELECT id FROM watch_sources")} == set(ADAPTERS)
            assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
        assert eng.status()["totals"]["items"] == 0
        WatchRT(settings, w).close()  # second init: no-op
        eng.close()
    finally:
        w.close()
