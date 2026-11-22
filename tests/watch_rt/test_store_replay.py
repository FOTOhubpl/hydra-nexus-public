"""Dedup, replay (engine + CLI), live/replay separation, recorder round trip, retention."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from hydra.db import Writer, connect
from hydra.watch.rt import engine as rt_engine
from hydra.watch.rt import indicators as ind
from hydra.watch.rt import sources as src
from hydra.watch.rt import store
from hydra.watch.rt.engine import FakeClock, WatchRT, load_fixture
from hydra.watch.rt.sources import RawItem

from .conftest import T0, count


def _item(ext: str, host: str, payload: dict | None = None) -> RawItem:
    return RawItem(ext, "domain", host, ind.from_host(host), "2026-11-22T03:00:00+00:00", payload or {"h": host})


def _commit(eng, items, *, now: float, matches=None, replay=False):
    return eng.writer.run(lambda c: store.commit_poll(
        c, "cert-pl", started=now, duration_ms=1, items=items, matches=matches or {}, http_status=200,
        not_modified=False, error=None, replay=replay, source_fields={}, now=now))


def test_dedup_same_external_id(online, make_engine):
    eng = make_engine(online)
    a, b = _item("cert:1", "a-one.example"), _item("cert:2", "b-two.example")
    res = _commit(eng, [a, a, b], now=T0)  # duplicate inside one poll
    assert len(res["item_ids"]) == 2
    res2 = _commit(eng, [_item("cert:1", "a-one.example", {"changed": True})], now=T0 + 60)
    assert res2["item_ids"] == []
    with eng.reader() as conn:
        rows = conn.execute("SELECT external_id, seen_count, last_seen FROM watch_items ORDER BY id").fetchall()
        runs = conn.execute("SELECT new, fetched FROM watch_runs ORDER BY id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("cert:1", 2), ("cert:2", 1)]
    assert rows[0][2] == store.iso(T0 + 60)
    assert [tuple(r) for r in runs] == [(2, 3), (0, 1)]


def test_dedup_across_polls_through_engine(online, fetch, make_engine):
    fetch.bodies["openphish"] = b"https://x-one.example/login\nhttps://x-two.example/a\nhttps://x-one.example/login\n"
    eng = make_engine(online, fetch)
    eng.set_source("openphish", enabled=True)
    r1 = eng.poll_source("openphish")
    eng.clock.advance(4000)
    r2 = eng.poll_source("openphish")
    assert (r1["new"], r2["new"]) == (2, 0)
    assert count(eng, "SELECT COUNT(*) FROM watch_items") == 2
    assert count(eng, "SELECT MIN(seen_count) FROM watch_items") == 2


def _replay(eng: WatchRT, fx) -> None:
    eng.prepare_replay(fx, 3600)
    eng.state = "replay"
    asyncio.run(eng.run_replay())


def _counts(eng) -> tuple[int, int, int, int]:
    return (count(eng, "SELECT COUNT(*) FROM watch_items"), count(eng, "SELECT COUNT(*) FROM watch_matches"),
            count(eng, "SELECT COUNT(*) FROM watch_runs"), count(eng, "SELECT SUM(seen_count) FROM watch_items"))


def expected_demo_matches(fx) -> int:
    """Independent count: parse every tick with the adapters' parsers, dedup by (source, external_id), exact-match
    the manifest watchlist (unique item x entry)."""
    keys = {(e["kind"], ind.normalise(e["kind"], e["value"])) for e in fx.manifest["watchlist"]}
    seen: dict[tuple[str, str], RawItem] = {}
    crt_max = 0
    for tick in fx.ticks:
        for sid, name in tick.items():
            raw = (fx.path / name).read_text(encoding="utf-8")
            if sid == "cert-pl":
                items = src.parse_cert_lines(raw)
            elif sid == "urlhaus":
                items = src.parse_urlhaus(json.loads(raw))
            elif sid == "openphish":
                items = src.parse_openphish(raw)
            elif sid == "crtsh":
                items, crt_max = src.parse_crtsh(json.loads(raw), "kw", crt_max)
            else:
                items = src.parse_ads(json.loads(raw))
            for it in items:
                seen.setdefault((sid, it.external_id), it)
    return sum(len({p for p in it.indicators if p in keys}) for it in seen.values())


def test_demo_replay_counts_and_idempotence(offline, make_engine):
    fx = load_fixture("demo")
    assert fx.synthetic and len(fx.ticks) == 12
    eng = make_engine(offline)  # replay works offline: no network path is involved
    _replay(eng, fx)
    first = _counts(eng)
    expected = expected_demo_matches(fx)
    assert expected == 8 and first[1] == expected and first[0] > 0
    assert count(eng, "SELECT COUNT(*) FROM watch_items WHERE replay=0") == 0
    assert count(eng, "SELECT COUNT(*) FROM watch_matches WHERE replay=0") == 0
    assert count(eng, "SELECT COUNT(*) FROM watch_runs WHERE replay=0") == 0
    # duplicate cert line (syn-hit-1 at ticks 2 and 9): one row, seen twice
    assert count(eng, "SELECT seen_count FROM watch_items WHERE external_id LIKE '%cert:syn-hit-1'") == 2
    eng.clock.advance(3600)
    _replay(eng, fx)  # prepare_replay clears replay rows: identical counts
    assert _counts(eng) == first
    by_kind = dict(eng.reader().__enter__().execute(
        "SELECT indicator_kind, strength FROM watch_matches WHERE indicator_kind IN ('domain', 'host')"
    ).fetchall())
    assert by_kind == {"domain": "weak", "host": "strong"}


def test_cli_replay_and_live_views_exclude_replay(tmp_path: Path, monkeypatch):
    from hydra import config
    from hydra.cli import app

    state = tmp_path / "cli-state"
    monkeypatch.setenv("HYDRA_STATE_DIR", str(state))
    monkeypatch.setenv("HYDRA_OFFLINE", "1")
    config.get_settings.cache_clear()

    async def no_sleep(self, s, wake=None):  # the CLI uses the real clock: no real waiting in tests
        await asyncio.sleep(0)

    monkeypatch.setattr(rt_engine.Clock, "sleep", no_sleep)
    r = CliRunner().invoke(app, ["watch", "replay", "demo", "--speed", "3600"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output[r.output.index("\n{") + 1:])
    assert out["fixture"] == "demo" and out["synthetic"] is True
    assert out["totals"]["items"] > 0
    assert out["totals"]["matches"] == expected_demo_matches(load_fixture("demo")) == 8
    db = config.get_settings().db_path
    assert db.parent.parent == state or str(db).startswith(str(state))
    with connect(db, readonly=True) as conn:
        for t in ("watch_items", "watch_matches", "watch_runs"):
            assert conn.execute(f"SELECT COUNT(*) FROM {t} WHERE replay=0").fetchone()[0] == 0
            assert conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] > 0
    # a fresh (stopped, i.e. live view) engine on the same state never mixes replay runs into its charts
    s = config.get_settings()
    w = Writer(s.db_path)
    try:
        eng = WatchRT(s, w)
        st = eng.status()
        assert len(st["sparkline"]) == 60 and len(st["timeline"]) == 48
        assert sum(m["fetched"] for m in st["sparkline"]) == 0 and sum(h["new"] for h in st["timeline"]) == 0
        eng.state = "replay"
        assert sum(m["fetched"] for m in eng.status()["sparkline"]) > 0
        eng.state = "stopped"
    finally:
        w.close()


def _cert_lines(prefix: str, n: int) -> bytes:
    return ("\n".join(json.dumps({"RegisterPositionId": f"{prefix}-{i}", "DomainAddress": f"{prefix}{i}.example",
                                  "ActionType": "block", "ActionTime": "2026-11-22T03:00:00"}) for i in range(n))
            + "\n").encode()


def test_recorder_round_trip(online, fetch, make_engine, monkeypatch, tmp_path):
    monkeypatch.setenv("HYDRA_WATCH_RECORD", "1")
    eng = make_engine(online, fetch)
    eng.set_source("cert-pl", enabled=True)
    fetch.bodies["cert-pl"] = _cert_lines("rec-a", 3)
    eng.poll_source("cert-pl")
    eng.clock.advance(120)
    fetch.bodies["cert-pl"] = _cert_lines("rec-b", 2)
    eng.poll_source("cert-pl")
    rec_dirs = list((Path(online.state_dir) / "watch" / "rt" / "recordings").iterdir())
    assert len(rec_dirs) == 1
    fx = load_fixture(str(rec_dirs[0]))
    assert fx.synthetic is False and len(fx.ticks) == 2
    with eng.reader() as conn:
        live = sorted(r[0] for r in conn.execute("SELECT value FROM watch_items WHERE replay=0"))
    assert len(live) == 5
    # replay of the recording on a fresh state gives the same items
    monkeypatch.delenv("HYDRA_WATCH_RECORD")
    other = make_engine(rt_settings(tmp_path / "other"), clock=FakeClock(T0))
    _replay(other, fx)
    with other.reader() as conn:
        rep = sorted(r[0] for r in conn.execute("SELECT value FROM watch_items WHERE replay=1"))
    assert rep == live


def test_replay_of_recording_keeps_live_rows_untouched(online, fetch, make_engine, monkeypatch):
    """Replay rows and live rows of the same feed item stay separate: a replay never bumps or hides a live row."""
    monkeypatch.setenv("HYDRA_WATCH_RECORD", "1")
    eng = make_engine(online, fetch)
    eng.set_source("cert-pl", enabled=True)
    fetch.bodies["cert-pl"] = _cert_lines("same", 3)
    eng.poll_source("cert-pl")
    monkeypatch.delenv("HYDRA_WATCH_RECORD")
    before = eng.reader().__enter__().execute(
        "SELECT id, seen_count, last_seen FROM watch_items WHERE replay=0 ORDER BY id").fetchall()
    rec = next((Path(online.state_dir) / "watch" / "rt" / "recordings").iterdir())
    eng.clock.advance(600)
    _replay(eng, load_fixture(str(rec)))
    with eng.reader() as conn:
        after = conn.execute("SELECT id, seen_count, last_seen FROM watch_items WHERE replay=0 ORDER BY id").fetchall()
        n_rep = conn.execute("SELECT COUNT(*) FROM watch_items WHERE replay=1").fetchone()[0]
    assert [tuple(r) for r in after] == [tuple(r) for r in before]
    assert n_rep == 3
    # and the next live poll of the same items still dedups against the live rows only
    eng.state = "stopped"
    eng.replay = None
    eng.clock.advance(120)
    assert eng.poll_source("cert-pl")["new"] == 0
    assert count(eng, "SELECT COUNT(*) FROM watch_items WHERE replay=1") == 3


def rt_settings(root: Path):
    from .conftest import make_settings

    root.mkdir(parents=True, exist_ok=True)
    return make_settings(root, offline=True)


def test_retention_keeps_matched_items(online, make_engine, monkeypatch):
    eng = make_engine(online)
    eng.add_entry("host", "keep-me.example", "manual test")
    old = T0 - 40 * 86400
    a, b = _item("cert:old-a", "drop-me.example"), _item("cert:old-b", "keep-me.example")
    hits = eng.index.match(b.indicators)
    assert hits
    _commit(eng, [a, b], now=old, matches={b.external_id: hits})
    _commit(eng, [_item("cert:new", "fresh.example")], now=T0)
    assert count(eng, "SELECT COUNT(*) FROM watch_runs") == 2
    res = eng.housekeep()  # defaults: items 30 days, runs 14 days
    assert res == {"items": 1, "runs": 1}
    with eng.reader() as conn:
        left = sorted(r[0] for r in conn.execute("SELECT external_id FROM watch_items"))
        n_matches = conn.execute("SELECT COUNT(*) FROM watch_matches").fetchone()[0]
    assert left == ["cert:new", "cert:old-b"] and n_matches == 1
    monkeypatch.setenv("HYDRA_WATCH_RUNS_RETENTION_DAYS", "0")
    eng.clock.advance(1)
    assert eng.housekeep()["runs"] == 1
