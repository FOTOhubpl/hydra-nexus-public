"""DATAFLOW (ADR-0009): incremental per-run aggregates == full recomputation, on synthetic data.

Property: after any random sequence of events (new material, re-decisions of new and base incidents, pair
reviews and undos, unsure marks, exports) the materialised `agg_*` tables equal `dataflow.recompute()`, which
derives every aggregate from scratch. Each event goes through the production write path and the event tap.
"""

from __future__ import annotations

import random
import tempfile
from contextlib import closing
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from hydra import dataflow
from hydra.config import Settings
from hydra.db import Writer, connect

from .dataflow_world import RUN, World


def _settings(root: Path) -> Settings:
    s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                 state_dir=root / "state")
    s.ensure_dirs()
    return s


def _check(db: Path, where: str) -> None:
    with closing(connect(db, readonly=True)) as conn:
        assert dataflow.is_ready(conn, RUN), where
        a = dataflow.normalise(dataflow.snapshot(conn, RUN))
        b = dataflow.normalise(dataflow.recompute(conn, RUN))
    for name in b:
        if a[name] != b[name]:
            bad = sorted((k for k in set(a[name]) | set(b[name]) if a[name].get(k) != b[name].get(k)), key=str)[:4]
            detail = [(k, a[name].get(k), b[name].get(k)) for k in bad]
            raise AssertionError(f"{where}: {name} differs (incremental, full): {detail}")


def _run_sequence(seed: int, steps: int, root: Path, *, build_first: bool = True) -> World:
    s = _settings(root)
    w = Writer(s.db_path)
    try:
        world = World(w, random.Random(seed))
        world.build_base(n_incidents=random.Random(seed).randint(8, 26), n_campaigns=random.Random(seed).randint(2, 7))
        if build_first:
            dataflow.build(s.db_path, w, RUN)
            _check(s.db_path, "after base build")
        for i in range(steps):
            world.step()
            if build_first:
                _check(s.db_path, f"seed {seed} step {i} ({world.log[-1] if world.log else '-'})")
        if not build_first:  # build after the fact: base + catch-up of every delta must give the same state
            dataflow.build(s.db_path, w, RUN)
            _check(s.db_path, f"seed {seed} late build")
        # a rebuild of a run with deltas is idempotent
        dataflow.build(s.db_path, w, RUN)
        _check(s.db_path, f"seed {seed} rebuild")
        return world
    finally:
        w.close()


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_incremental_equals_full_recompute(tmp_path: Path, seed: int) -> None:
    world = _run_sequence(seed, 40, tmp_path)
    assert world.new, "the sequence added new material"


@hsettings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(seed=st.integers(min_value=0, max_value=2**31 - 1), steps=st.integers(min_value=1, max_value=30))
def test_property_random_event_sequences(seed: int, steps: int) -> None:
    with tempfile.TemporaryDirectory() as d:
        _run_sequence(seed, steps, Path(d))


@pytest.mark.parametrize("seed", [11, 12])
def test_late_build_catches_up(tmp_path: Path, seed: int) -> None:
    _run_sequence(seed, 30, tmp_path, build_first=False)


def test_version_and_deltas(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    w = Writer(s.db_path)
    try:
        world = World(w, random.Random(7))
        world.build_base()
        out = dataflow.build(s.db_path, w, RUN)
        assert out["version"] == 1
        with closing(connect(s.db_path, readonly=True)) as conn:
            v0 = dataflow.state_row(conn, RUN)["version"]
        world.assign(n_pairs=3, decision="new_campaign")
        world.export()
        with closing(connect(s.db_path, readonly=True)) as conn:
            st_ = dataflow.state_row(conn, RUN)
            rows = conn.execute("SELECT kind, seq, version, payload FROM agg_deltas WHERE run_id=? ORDER BY id",
                                (RUN,)).fetchall()
            info = dataflow.live_info(conn, RUN)
        assert st_["version"] == v0 + 2
        kinds = [r[0] for r in rows]
        assert kinds[0] == "reset" and kinds[1:] == ["assign", "export"]
        seqs = [r[1] for r in rows[1:]]
        assert seqs == sorted(seqs) and st_["seq"] == seqs[-1]
        msg = dataflow._loads(rows[1][3])
        assert msg["kpis"]["new_incidents"] == 1 and msg["nodes"][0]["new"] is True
        assert msg["nodes"][0]["map_campaign"] == f"solo:{world.new[0]}"
        assert info["new_incidents"] == 1 and info["by_decision"] == {"new_campaign": 1}
    finally:
        w.close()


def test_tap_failure_never_fails_the_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    w = Writer(s.db_path)
    try:
        world = World(w, random.Random(3))
        world.build_base()
        dataflow.build(s.db_path, w, RUN)

        def boom(*_a, **_k):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(dataflow, "apply_assignment", boom)
        run_id = world.assign(n_pairs=2)
        with closing(connect(s.db_path, readonly=True)) as conn:
            assert conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone()  # the business write landed
            assert dataflow.state_row(conn, RUN)["status"] == "stale"
        monkeypatch.undo()
        dataflow.build(s.db_path, w, RUN)  # rebuild = base + catch-up
        _check(s.db_path, "after rebuild of a stale run")
    finally:
        w.close()


def test_disabled_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(tmp_path)
    w = Writer(s.db_path)
    try:
        world = World(w, random.Random(5))
        world.build_base()
        dataflow.build(s.db_path, w, RUN)
        monkeypatch.setenv("HYDRA_DATAFLOW", "0")
        world.assign(n_pairs=1)
        with closing(connect(s.db_path, readonly=True)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM agg_deltas WHERE kind='assign'").fetchone()[0] == 0
    finally:
        w.close()


def test_batch_run_streams_one_batch_delta(tmp_path: Path) -> None:
    """merge-batch4: an `assign_batch` run (hydra.batch) against a materialised base emits one `batch` delta with
    the new pattern ids and duplicates, bumps the version, and leaves incremental == full recomputation."""
    import json

    from hydra.pipeline import _write_run

    s = _settings(tmp_path)
    w = Writer(s.db_path)
    try:
        world = World(w, random.Random(11))
        world.build_base()
        dataflow.build(s.db_path, w, RUN)
        world.assign(n_pairs=1)
        with closing(connect(s.db_path, readonly=True)) as conn:
            v0 = dataflow.state_row(conn, RUN)["version"]
        metrics = {"summary": {"items": 2, "new_patterns": 1}, "decisions": {"new_campaign": 2},
                   "new_patterns": [{"id": "N-0000abcd", "size": 2, "members": ["new-a", "new-b"]}],
                   "items": [{"incident_id": "new-a", "decision": "new_campaign", "new_pattern_id": "N-0000abcd"},
                             {"incident_id": "new-b", "decision": "new_campaign", "new_pattern_id": "N-0000abcd",
                              "duplicate_of": "new-a"}]}
        w.run(lambda conn: _write_run(conn, "assign_batch-t1", "assign_batch", {"base_run": RUN}, metrics, {}, None))
        with closing(connect(s.db_path, readonly=True)) as conn:
            row = conn.execute("SELECT kind, version, payload FROM agg_deltas WHERE run_id=? ORDER BY id DESC LIMIT 1",
                               (RUN,)).fetchone()
            assert dataflow.state_row(conn, RUN)["version"] == v0 + 1
        assert row[0] == "batch" and row[1] == v0 + 1
        batch = json.loads(row[2])["batch"]
        assert batch["run_id"] == "assign_batch-t1" and batch["base_run"] == RUN
        assert [p["id"] for p in batch["new_patterns"]] == ["N-0000abcd"]
        assert batch["items"][1]["duplicate_of"] == "new-a"
        _check(s.db_path, "after a batch run")
    finally:
        w.close()
