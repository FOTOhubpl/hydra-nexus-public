"""hydra.parallel: budget, input-order results, crash isolation; pipeline.extract parallel == sequential."""

from __future__ import annotations

import pytest

from hydra import parallel

from . import jobs


def test_plan_budget_splits_cpus(monkeypatch):
    for k in ("HYDRA_EXTRACT_WORKERS", "HYDRA_TORCH_THREADS", "HYDRA_OCR_WORKERS", "HYDRA_CPU_BUDGET",
              "HYDRA_TORCH_SLOTS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(parallel.sys, "platform", "linux")
    p = parallel.plan(10, settings_workers=62, cpus=64)
    assert (p.workers, p.torch_threads, p.ocr_workers, p.torch_slots) == (10, 16, 5, 4)
    assert p.workers * p.ocr_workers <= 64  # no 64 x 64 nested OCR pools
    p = parallel.plan(200, settings_workers=62, cpus=64)
    assert (p.workers, p.torch_threads, p.ocr_workers, p.torch_slots) == (32, 16, 1, 4)
    # 128-core server: 48 workers at most, torch threads stay 16 (numerics), 8 x 16 threads inside inference
    p = parallel.plan(825, settings_workers=56, cpus=128)
    assert (p.workers, p.torch_threads, p.ocr_workers, p.torch_slots) == (parallel.MAX_WORKERS, 16, 1, 8)
    assert p.torch_slots * p.torch_threads <= 128
    assert not parallel.plan(1, settings_workers=62, cpus=64).parallel
    # a job budget (shared host): 24 cores -> 12 workers, one 16-thread slot
    monkeypatch.setenv("HYDRA_CPU_BUDGET", "24")
    p = parallel.plan(825, settings_workers=56, cpus=128)
    assert (p.workers, p.torch_threads, p.ocr_workers, p.torch_slots) == (12, 16, 1, 1)
    monkeypatch.delenv("HYDRA_CPU_BUDGET")
    # the CLAUDE.md per-job envelope: explicit threads, the gate never blocks
    for k, v in (("HYDRA_EXTRACT_WORKERS", "12"), ("HYDRA_TORCH_THREADS", "2"), ("HYDRA_OCR_WORKERS", "1")):
        monkeypatch.setenv(k, v)
    p = parallel.plan(825, settings_workers=56, cpus=128)
    assert (p.workers, p.torch_threads, p.ocr_workers, p.torch_slots) == (12, 2, 1, 0)
    for k in ("HYDRA_EXTRACT_WORKERS", "HYDRA_TORCH_THREADS", "HYDRA_OCR_WORKERS"):
        monkeypatch.delenv(k)
    monkeypatch.setattr(parallel.sys, "platform", "darwin")
    assert parallel.plan(10, settings_workers=8, cpus=10).workers == parallel.DARWIN_MAX_WORKERS
    assert parallel.plan(10, settings_workers=8, cpus=10).torch_slots == 0
    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    assert not parallel.plan(10, cpus=64).parallel


def test_cpu_budget_respects_container_quota(monkeypatch):
    """docker --cpus 6 on a 128-core host: os.cpu_count() says 128, the budget must say 6."""
    monkeypatch.delenv("HYDRA_CPU_BUDGET", raising=False)
    monkeypatch.setattr(parallel.os, "cpu_count", lambda: 128)
    monkeypatch.setattr(parallel.os, "sched_getaffinity", lambda _pid: set(range(128)), raising=False)
    monkeypatch.setattr(parallel, "_cgroup_cpu_quota", lambda: 6.5)
    assert parallel.available_cpus() == 6 and parallel.cpu_budget() == 6
    monkeypatch.setenv("HYDRA_CPU_BUDGET", "24")  # an explicit budget is still clamped to the quota
    assert parallel.cpu_budget() == 6
    monkeypatch.setattr(parallel, "_cgroup_cpu_quota", lambda: None)
    assert parallel.cpu_budget() == 24
    monkeypatch.setattr(parallel.os, "sched_getaffinity", lambda _pid: set(range(16)), raising=False)
    assert parallel.available_cpus() == 16


def test_torch_slots_gate_inference(tmp_path):
    """4 workers, 1 slot: never more than one job inside `compute_slot` at a time."""
    out = list(parallel.run_incidents([(i, str(tmp_path)) for i in range(8)], jobs.in_slot,
                                      parallel.Plan(4, 1, 0, torch_slots=1), _fail))
    assert all(ok for _, _, ok in out)
    assert max(r for _, r, _ in out) == 1
    with parallel.compute_slot():  # no gate in the parent process
        pass


def _fail(i, exc):
    return ("failed", i, type(exc).__name__)


def test_run_incidents_input_order_and_worker_state():
    p = parallel.Plan(3, 2, 0)
    delays = [0.3, 0.0, 0.2, 0.0, 0.1, 0.0]
    out = list(parallel.run_incidents([(i, d) for i, d in enumerate(delays)], jobs.square, p, _fail,
                                      order=[5, 4, 3, 2, 1, 0]))
    assert [i for i, _, _ in out] == list(range(len(delays)))
    assert all(ok for _, _, ok in out)
    assert [r[0] for _, r, _ in out] == [i * i for i in range(len(delays))]
    assert {r[2] for _, r, _ in out} == {"2"}  # per-worker torch thread budget reached the worker env
    assert len({r[1] for _, r, _ in out}) <= 3


@pytest.mark.parametrize("fn", [jobs.crash_on, jobs.raise_on])
def test_crashing_job_is_isolated(fn):
    p = parallel.Plan(2, 1, 0)
    out = list(parallel.run_incidents([(i, 2) for i in range(5)], fn, p, _fail))
    assert [i for i, _, _ in out] == list(range(5))
    good = {i: r for i, r, ok in out if ok}
    bad = [(i, r) for i, r, ok in out if not ok]
    assert len(bad) == 1 and bad[0][0] == 2 and bad[0][1][0] == "failed"
    expect = (lambda x: x * 10) if fn is jobs.crash_on else (lambda x: x + 1)
    assert good == {i: expect(i) for i in (0, 1, 3, 4)}


def _gene_rows(db):
    from contextlib import closing

    from hydra.db import connect

    with closing(connect(db, readonly=True)) as conn:
        genes = [tuple(r) for r in conn.execute(
            "SELECT incident_id, family, kind, value, vector_ref, quality, status, provenance, evidence, extractor,"
            " dependency_group FROM genes ORDER BY id")]
        vecs = [tuple(r) for r in conn.execute("SELECT key, model, dim, data FROM gene_vectors ORDER BY key")]
    return genes, vecs


def test_extract_parallel_matches_sequential(tmp_path, make_png, monkeypatch):
    """Real (hydra.genome) extractors that need no model weights: parallel genes == sequential genes."""
    from core.test_pipeline import corpus

    from hydra.config import Settings
    from hydra.pipeline import analyse

    ds = corpus(tmp_path, make_png)
    # spans all three extractor groups; artifact_sha256 (last group) is registered after meta/nar
    only = ["inf_url", "inf_dom", "nar", "meta", "artifact_sha256"]
    used: list[int] = []
    real = parallel.Pool.__init__

    def spy(self, p, fn, on_failure):
        used.append(p.workers)
        real(self, p, fn, on_failure)

    monkeypatch.setattr(parallel.Pool, "__init__", spy)
    out = {}
    for mode, workers in (("seq", "0"), ("par", "3")):
        monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", workers)
        root = tmp_path / mode
        s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                     state_dir=root / "state")
        s.ensure_dirs()
        res = analyse([ds], s, only=only)
        out[mode] = (_gene_rows(s.db_path), sorted(tuple(c.members) for c in res.clustering.campaigns),
                     [g.model_dump() for g in res.clustering.groups])
        assert not any(v.get("worker_failed") for v in res.metrics["extract"].values())
    assert used == [3], "the parallel run must go through the process pool"
    assert out["seq"][0][0], "expected genes"
    assert out["par"] == out["seq"]


def test_worker_with_nested_pool_exits(tmp_path):
    """Regression: an incident worker holding an open OCR pool must not hang the pool shutdown."""
    import subprocess
    import sys
    from pathlib import Path

    script = tmp_path / "nested.py"
    script.write_text(
        "from hydra import parallel\n"
        "from perf import jobs\n"
        "if __name__ == '__main__':\n"
        "    out = list(parallel.run_incidents([(i,) for i in range(4)], jobs.nested_pool, parallel.Plan(2, 1, 2),"
        " lambda i, e: None))\n"
        "    print([r for _, r, _ in out])\n")
    tests_dir = str(Path(__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=90,
                       env={**__import__("os").environ, "PYTHONPATH": tests_dir})
    assert r.returncode == 0, r.stderr[-2000:]
    assert r.stdout.strip() == "[1, 2, 3, 4]"


class _LossyExecutor:
    """Wraps a real executor and silently loses one submission (its future never completes), the symptom seen on
    the laptop: parent waiting on futures, every worker idle."""

    def __init__(self, ex, drop: int):
        self.ex, self.drop, self.n = ex, drop, 0

    def submit(self, fn, *args):
        from concurrent.futures import Future

        self.n += 1
        return Future() if self.n == self.drop else self.ex.submit(fn, *args)

    def shutdown(self, **kw):
        self.ex.shutdown(**kw)

    @property
    def _processes(self):
        return self.ex._processes


def test_lost_job_is_finished_in_process_by_the_stall_watchdog(monkeypatch, caplog):
    import os

    monkeypatch.setenv("HYDRA_POOL_STALL_S", "2")
    monkeypatch.setattr(parallel, "POLL_S", 0.5)
    real = parallel._executor
    made: list = []

    def lossy(workers, *a):
        w = _LossyExecutor(real(workers, *a), drop=3 if not made else 0)
        made.append(w)
        return w

    monkeypatch.setattr(parallel, "_executor", lossy)
    out = list(parallel.run_incidents([(i, 0.0) for i in range(6)], jobs.square, parallel.Plan(2, 1, 0), _fail))
    assert [i for i, _, _ in out] == list(range(6))
    assert all(ok for _, _, ok in out)
    assert [r[0] for _, r, _ in out] == [i * i for i in range(6)]
    assert out[2][1][1] == os.getpid()  # the lost job ran in this process
    assert all(r[1] != os.getpid() for i, r, _ in out if i != 2)
    assert "process pool stalled" in caplog.text


def test_long_job_does_not_trip_the_stall_watchdog(monkeypatch, caplog):
    monkeypatch.setenv("HYDRA_POOL_STALL_S", "1")
    monkeypatch.setattr(parallel, "POLL_S", 0.3)
    out = list(parallel.run_incidents([(0, 3.0), (1, 0.0)], jobs.square, parallel.Plan(2, 1, 0), _fail))
    assert [r[0] for _, r, _ in out] == [0, 1]
    assert "process pool stalled" not in caplog.text


def test_group_split_is_reproducible_across_processes():
    """nexus.split_component: the seeded Leiden/Louvain split of one component is the same in every process
    (string hashing used to reorder the subgraph's edges)."""
    import os
    import subprocess
    import sys

    code = ("import random, networkx as nx\n"
            "from hydra.nexus import split_component\n"
            "rng = random.Random(3); g = nx.Graph(); ns = [f'C-{i:04d}' for i in range(300)]\n"
            "g.add_nodes_from(ns)\n"
            "for _ in range(1200):\n"
            "    a, b = rng.sample(ns, 2); g.add_edge(a, b, weight=rng.uniform(5, 30))\n"
            "comp = max(nx.connected_components(g), key=len)\n"
            "print(sorted(sorted(p) for p in split_component(g, set(comp), 30)[0]))\n")
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                           env={**os.environ, "PYTHONHASHSEED": str(seed)}).stdout for seed in (1, 2, 3)}
    assert len(outs) == 1


def test_downstream_fork_maps_match_in_process(tmp_path, make_png, monkeypatch):
    """Pair scores, robustness, conformal calibration and self-assignment computed in forked workers
    (hydra.parallel.fork_map, Linux) are identical to the in-process loops."""
    from core.test_pipeline import corpus

    from hydra import pipeline
    from hydra.config import Settings

    ds = corpus(tmp_path, make_png)
    only = ["inf_url", "inf_dom", "nar", "meta", "artifact_sha256"]
    monkeypatch.setenv("HYDRA_EXTRACT_WORKERS", "0")
    monkeypatch.setattr(pipeline, "FORK_MIN_ITEMS", 1)
    out = {}
    for mode, workers in (("seq", "0"), ("fork", "3")):
        monkeypatch.setenv("HYDRA_SCORE_WORKERS", workers)
        root = tmp_path / mode
        s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                     state_dir=root / "state")
        s.ensure_dirs()
        res = pipeline.analyse([ds], s, only=only)
        m = res.metrics
        out[mode] = (res.pairs, sorted(tuple(c.members) for c in res.clustering.campaigns), m["conformal"],
                     {k: v for k, v in m["assignment"].items() if k != "seconds"})
        assert m["score_workers"] == (int(workers) if parallel.sys.platform == "linux" else 0)
    assert out["seq"][0], "expected candidate pairs"
    assert out["fork"] == out["seq"]
