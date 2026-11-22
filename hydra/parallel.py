"""Process-parallel gene extraction over incidents (PERF stream).

`hydra.pipeline.extract` hands the incidents that need extraction to a `Pool` (spawn-context process pool that
also takes follow-up jobs while running); `run_incidents` is the fixed-job-list form that yields results back
**in input order**. Workers compute and write their own gene cache files: they return `ExtractResult` +
enrichment records, and the caller persists them through the single `Writer` (ADR-002), one transaction per
incident, in input order, exactly as the sequential loop does.

Budget (`plan`): the job's core budget (`HYDRA_CPU_BUDGET`, default all cores) is split between the incident
processes, so nested pools never multiply (`HYDRA_OCR_WORKERS` and `HYDRA_TORCH_THREADS` are set per worker; OCR
stays at 1 onnxruntime thread per process, SPEC_ERRATA #9/#13). Defaults:
- Linux / no accelerator: `min(MAX_WORKERS, Settings.workers, budget // CORES_PER_WORKER)` incident processes on
  CPU (DINOv2 bf16 autocast in each).
- macOS: up to `DARWIN_MAX_WORKERS` processes; MPS is a per-process device, so each worker opens its own.

Torch threads are a *numerics* setting, not a budget share: bf16 DINOv2 vectors depend on the intra-op thread
count (SPEC_ERRATA #14, re-measured 2026-11-21 on 40 synthetic images: 1/2/4 vs 16 threads differ by up to 3.1e-3,
8 vs 16 agree on a batch of 40 but not on a batch of 1). Every worker therefore uses the sequential default
(`hydra.ml.device`, min(16, cpus)) and the parallel path stays byte-identical to the sequential one. What keeps
`workers x threads` from oversubscribing the host is a cross-process semaphore: at most `torch_slots`
(`budget // threads`) workers run model inference at the same time (`compute_slot`, wrapped around the DINOv2,
SigLIP2 and e5 forward passes); the other workers keep decoding, hashing and OCR-ing meanwhile. OpenMP threads of
an idle worker sleep instead of spinning (`OMP_WAIT_POLICY=PASSIVE`).

Overrides (env, until they become Settings fields, see docs/CONTRACT_REQUESTS.md):
`HYDRA_EXTRACT_WORKERS` (0 or 1 = sequential in the caller), `HYDRA_CPU_BUDGET`, `HYDRA_TORCH_THREADS`,
`HYDRA_TORCH_SLOTS` (0 = no gate), `HYDRA_OCR_WORKERS`.

Failure isolation: a job that raises or whose process dies (segfault, OOM kill → `BrokenProcessPool`) is
retried once alone in a fresh process; if that fails too, the incident gets one `failed` gene per extractor
and is not marked as cached, so the next run retries it. A crashed worker never stops the run.
"""

from __future__ import annotations

import contextlib
import faulthandler
import logging
import multiprocessing as mp
import os
import pickle
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DARWIN_MAX_WORKERS = 4   # 16 GB laptop: each worker holds its own torch + DINOv2/SigLIP2/e5/face models
MAX_WORKERS = 48         # per job (~1.4 GB RSS each); the 128-core server is shared by parallel agents
CORES_PER_WORKER = 2     # the worker's own Python thread + its share of OCR / decoding
MAX_OCR_PER_WORKER = 8
MAX_TORCH_THREADS = 16   # SPEC_ERRATA #14: bf16 DINOv2 measured with 16 threads
SLOT_TIMEOUT_S = 900.0   # a worker killed inside a slot leaks it: waiters give up and run ungated after this


@dataclass(frozen=True)
class Plan:
    workers: int        # incident processes; <= 1 means "sequential in the caller"
    torch_threads: int  # per worker
    ocr_workers: int    # per worker OCR pool size (<= 1: OCR in-process, 1 onnxruntime thread)
    torch_slots: int = 0  # workers allowed inside model inference at once (0 = no gate)

    @property
    def parallel(self) -> bool:
        return self.workers > 1


def _env_int(name: str) -> int | None:
    v = os.environ.get(name)
    if v is None or v.strip() == "":
        return None
    try:
        return int(v)
    except ValueError:
        log.warning("%s=%r is not an integer; ignored", name, v)
        return None


def _read(path: str) -> str:
    with open(path, encoding="ascii") as fh:
        return fh.read()


def _cgroup_cpu_quota() -> float | None:
    """CPU quota of this cgroup in cores (`docker run --cpus`, compose `cpus:`), None when unlimited/unknown."""
    try:  # cgroup v2: "<quota> <period>" or "max <period>"
        quota, period = _read("/sys/fs/cgroup/cpu.max").split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        q = int(_read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
        p = int(_read("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
        return q / p if q > 0 and p > 0 else None
    except (OSError, ValueError):
        return None


def available_cpus() -> int:
    """Cores this process can really use: os.cpu_count() capped by the affinity mask and the cgroup CPU quota.
    Inside a container os.cpu_count() reports the host's cores, not the `--cpus` limit."""
    n = os.cpu_count() or 1
    with contextlib.suppress(AttributeError, OSError):
        n = min(n, len(os.sched_getaffinity(0)))
    q = _cgroup_cpu_quota()
    if q:
        n = min(n, max(1, int(q)))
    return n


def cpu_budget(cpus: int | None = None) -> int:
    """Cores this job may use: `HYDRA_CPU_BUDGET` (clamped to the available cores), default every available core
    (`available_cpus`: affinity mask and cgroup quota respected)."""
    cpus = cpus or available_cpus()
    b = _env_int("HYDRA_CPU_BUDGET")
    return max(1, min(cpus, b)) if b else cpus


def plan(n_jobs: int, settings_workers: int | None = None, cpus: int | None = None) -> Plan:
    """How many incident processes, and the per-process torch / OCR budget, for `n_jobs` incidents."""
    budget = cpu_budget(cpus)
    cpus = cpus or os.cpu_count() or 1  # torch-thread default stays on os.cpu_count (numerics, see docstring)
    cap = _env_int("HYDRA_EXTRACT_WORKERS")
    if cap is None:
        cap = (min(DARWIN_MAX_WORKERS, max(1, budget // 2)) if sys.platform == "darwin"
               else min(MAX_WORKERS, settings_workers or cpus, max(1, budget // CORES_PER_WORKER)))
    n = max(0, min(n_jobs, cap))
    if n <= 1:
        return Plan(n, 0, 0)
    share = max(1, budget // n)
    # torch threads = the sequential default (min(16, cpus), hydra.ml.device): bf16 results depend on the
    # thread count (module docstring), so the budget is enforced by torch slots, never by fewer threads.
    threads = _env_int("HYDRA_TORCH_THREADS") or max(1, min(MAX_TORCH_THREADS, cpus))
    ocr = _env_int("HYDRA_OCR_WORKERS")
    if ocr is None:  # the worker itself takes one core of its share; 1 = OCR in-process (same numerics)
        ocr = max(1, min(MAX_OCR_PER_WORKER, share - 1))
    slots = _env_int("HYDRA_TORCH_SLOTS")
    if slots is None:
        slots = 0 if sys.platform == "darwin" else max(1, budget // threads)
    if slots >= n:
        slots = 0  # the gate could never block
    return Plan(n, threads, ocr, slots)


def worker_env(p: Plan) -> dict[str, str]:
    """Environment of an incident worker, applied before torch / onnxruntime / OpenCV are imported."""
    t = str(p.torch_threads)
    return {
        "HYDRA_TORCH_THREADS": t, "OMP_NUM_THREADS": t, "MKL_NUM_THREADS": t, "OPENBLAS_NUM_THREADS": t,
        "HYDRA_OCR_WORKERS": str(p.ocr_workers),
        "HYDRA_OCR_THREADS": "1",           # in-process OCR (pool size <= 1) also uses one onnxruntime thread
        "HYDRA_EXTRACT_WORKERS": "0",       # never nest an incident pool inside a worker
        "TOKENIZERS_PARALLELISM": "false",
        # idle OpenMP threads sleep instead of spinning (48 workers x 16 spinning threads = load ~600)
        "OMP_WAIT_POLICY": "PASSIVE", "KMP_BLOCKTIME": "0",
    }


def picklable(obj: Any) -> bool:
    try:
        pickle.dumps(obj)
        return True
    except Exception:  # noqa: BLE001 — lambdas, open handles, test doubles: run sequentially
        return False


# ------------------------------------------------------------------------------------------ worker side

_STATE: dict[str, Any] = {}
_SLOT: Any = None  # cross-process semaphore (torch slots), set by the worker initializer
_BUSY: Any = None  # shared array: pid of each worker currently inside a job (0 = free entry)
_ACTIVITY: Any = None  # shared double: wall time of the last job start or end in any worker


@contextlib.contextmanager
def compute_slot() -> Iterator[None]:
    """Hold one torch slot around a model forward pass (no-op outside a gated worker pool)."""
    sem = _SLOT
    if sem is None:
        yield
        return
    got = sem.acquire(timeout=SLOT_TIMEOUT_S)
    if not got:
        log.warning("no torch slot within %.0f s (a worker died inside one?); running ungated", SLOT_TIMEOUT_S)
    try:
        yield
    finally:
        if got:
            sem.release()


def _close_nested_pools() -> None:
    """Shut down the worker's own OCR pool before it exits.

    A pool worker leaves through `multiprocessing.util._exit_function`, which joins every non-daemon child
    *before* `concurrent.futures` would stop its executors (Python atexit hooks never run there). Without this
    finaliser an incident worker that opened an OCR pool waits for its idle OCR processes forever (measured
    on the Linux server: all jobs done, every process idle in waitpid).
    """
    try:
        from hydra.genome import ocr_pool
    except Exception:  # noqa: BLE001
        return
    for pool in list(ocr_pool._POOLS.values()):
        ex, pool._exec = pool._exec, None
        if ex is not None:
            ex.shutdown(wait=True, cancel_futures=True)


def _init(env: dict[str, str], slot: Any = None, busy: Any = None, activity: Any = None) -> None:
    global _SLOT, _BUSY, _ACTIVITY
    os.environ.update(env)
    _SLOT, _BUSY, _ACTIVITY = slot, busy, activity
    from hydra.genome import base

    base.IN_POOL_WORKER = True  # the extractor watchdog may end this process (never the caller's)
    from multiprocessing import util

    util.Finalize(None, _close_nested_pools, exitpriority=100)  # runs before children are joined
    if os.environ.get("HYDRA_DEVICE", "auto") in ("", "auto", "cpu"):
        try:  # cv2 keeps its own thread pool (all cores by default) in every process
            import cv2

            cv2.setNumThreads(1)  # resize / decode per worker; the workers are the parallelism
        except Exception:  # noqa: BLE001
            pass


def _mark(busy: bool, idx: int = -1) -> int:
    """Record this worker as busy (returns its entry) or free again (stall watchdog in `Pool`)."""
    if _BUSY is None:
        return -1
    with _BUSY.get_lock():
        if busy:
            for i in range(len(_BUSY)):
                if _BUSY[i] == 0:
                    _BUSY[i] = os.getpid()
                    idx = i
                    break
        elif idx >= 0:
            _BUSY[idx] = 0
    if _ACTIVITY is not None:
        _ACTIVITY.value = time.time()
    return idx


def _job(fn: Callable, args: tuple) -> Any:
    """Run `fn(*args, state)` with a per-process state dict (models loaded once per worker)."""
    idx = _mark(True)
    try:
        return fn(*args, _STATE)
    finally:
        _mark(False, idx)


# ------------------------------------------------------------------------------------------ parent side


def _executor(workers: int, env: dict[str, str], slot: Any = None, busy: Any = None,
              activity: Any = None) -> ProcessPoolExecutor:
    return ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"), initializer=_init,
                               initargs=(env, slot, busy, activity))


STALL_S = 180.0  # HYDRA_POOL_STALL_S: no job running, none started or finished for this long = stalled pool
POLL_S = 5.0


def _stall_s() -> float:
    v = os.environ.get("HYDRA_POOL_STALL_S", "").strip()
    try:
        return float(v) if v else STALL_S
    except ValueError:
        return STALL_S


def _alive_pids(executors: list[ProcessPoolExecutor]) -> set[int]:
    out: set[int] = set()
    for ex in executors:
        for proc in list((getattr(ex, "_processes", None) or {}).values()):
            if proc.is_alive():  # reaps a dead child, so a zombie does not count as alive
                out.add(proc.pid)
    return out


def _kill(ex: ProcessPoolExecutor) -> None:
    for proc in list((getattr(ex, "_processes", None) or {}).values()):
        with contextlib.suppress(Exception):
            proc.kill()
    with contextlib.suppress(Exception):
        ex.shutdown(wait=False, cancel_futures=True)


class Pool:
    """A spawn process pool that accepts jobs while results are being consumed (follow-up jobs, e.g. the dependent
    extractors of an incident whose other jobs are done) and returns results in completion order.

    Failure isolation: a job that raises, or whose process dies (segfault, OOM kill: every unfinished job of the
    shared pool then fails with `BrokenProcessPool`), is retried once alone in a fresh process, at most
    `p.workers` such processes at a time; if that fails too, `on_failure(key, exc)` is its result (`ok=False`).
    Jobs submitted after a crash go to a fresh shared pool.

    Stall watchdog: jobs pending while no live worker is inside a job and none started or finished for
    `HYDRA_POOL_STALL_S` (180 s) means work was lost between the parent and the workers (seen on the laptop: parent
    waiting on futures, every worker idle in sem_wait). The pool then dumps the parent's thread stacks to stderr
    (root-cause evidence), kills its processes and finishes every pending and later job in this process, the
    sequential path's results (same functions, same numerics), with a logged warning. A long job keeps its worker
    marked busy, so it never trips the watchdog."""

    def __init__(self, p: Plan, fn: Callable, on_failure: Callable[[Any, BaseException], Any]) -> None:
        self.p, self.fn, self.on_failure = p, fn, on_failure
        self.env = worker_env(p)
        ctx = mp.get_context("spawn")
        self.slot = ctx.BoundedSemaphore(p.torch_slots) if p.torch_slots > 0 else None
        self.busy = ctx.Array("q", 2 * max(1, p.workers) + 16)
        self.activity = ctx.Value("d", time.time())
        self._ex: ProcessPoolExecutor | None = None
        # future -> (key, args, first failure or None, solo executor or None)
        self._futs: dict[Future, tuple[Any, tuple, BaseException | None, ProcessPoolExecutor | None]] = {}
        self._retry: list[tuple[Any, tuple, BaseException]] = []
        self._solo = 0
        self._inline: list[tuple[Any, tuple]] | None = None  # set once the pool stalled: run here
        self._state: dict[str, Any] = {}
        self._last_done = time.time()
        self.stalled = False

    def _new(self, workers: int) -> ProcessPoolExecutor:
        return _executor(workers, self.env, self.slot, self.busy, self.activity)

    def submit(self, key: Any, args: tuple) -> None:
        if self._inline is not None:
            self._inline.append((key, args))
            return
        if self._ex is None:
            self._ex = self._new(self.p.workers)
        try:
            f = self._ex.submit(_job, self.fn, args)
        except BrokenProcessPool:
            self._ex.shutdown(wait=False, cancel_futures=True)
            self._ex = self._new(self.p.workers)
            f = self._ex.submit(_job, self.fn, args)
        self._futs[f] = (key, args, None, None)

    @property
    def pending(self) -> int:
        return len(self._futs) + len(self._retry) + len(self._inline or [])

    def _start_retries(self) -> None:
        while self._retry and self._solo < max(1, self.p.workers):
            key, args, first = self._retry.pop(0)
            ex = self._new(1)
            self._futs[ex.submit(_job, self.fn, args)] = (key, args, first, ex)
            self._solo += 1

    def _executors(self) -> list[ProcessPoolExecutor]:
        return [x for x in [self._ex, *(v[3] for v in self._futs.values())] if x is not None]

    def _is_stalled(self, stall_s: float) -> bool:
        if not self._futs:
            return False
        idle = time.time() - max(self._last_done, float(self.activity.value))
        if idle < stall_s:
            return False
        with self.busy.get_lock():
            busy = {pid for pid in self.busy[:] if pid}
        return not (busy & _alive_pids(self._executors()))

    def _fall_back(self, stall_s: float) -> None:
        pending = [(k, a) for k, a, _f, _s in self._futs.values()] + [(k, a) for k, a, _e in self._retry]
        log.warning("process pool stalled: %d jobs pending, no worker inside a job and no progress for %.0f s;"
                    " finishing them in this process (sequential). Parent thread stacks follow on stderr.",
                    len(pending), stall_s)
        with contextlib.suppress(Exception):
            faulthandler.dump_traceback(all_threads=True)
        for ex in self._executors():
            _kill(ex)
        self._ex, self._futs, self._retry, self._solo = None, {}, [], 0
        self._inline = pending
        self.stalled = True

    def _run_inline(self) -> tuple[Any, Any, bool]:
        key, args = self._inline.pop(0)  # type: ignore[union-attr]
        try:
            return key, self.fn(*args, self._state), True
        except Exception as exc:  # noqa: BLE001 — same contract as a job that failed twice
            log.error("in-process job %s failed (%s: %s)", key, type(exc).__name__, exc)
            return key, self.on_failure(key, exc), False

    def next_result(self) -> tuple[Any, Any, bool]:
        """Block until a job finishes; `(key, result, ok)`. Raises LookupError when nothing is pending."""
        stall_s = _stall_s()
        while True:
            if self._inline is not None:
                if not self._inline:
                    raise LookupError("no pending jobs")
                return self._run_inline()
            self._start_retries()
            if not self._futs:
                raise LookupError("no pending jobs")
            finished, _ = wait(set(self._futs), timeout=min(POLL_S, stall_s), return_when=FIRST_COMPLETED)
            if not finished:
                if self._is_stalled(stall_s):
                    self._fall_back(stall_s)
                continue
            self._last_done = time.time()
            for f in finished:  # unhandled finished futures stay in _futs for the next call
                key, args, first, solo = self._futs.pop(f)
                exc = f.exception()
                if solo is not None:
                    solo.shutdown(wait=True)
                    self._solo -= 1
                if exc is None:
                    return key, f.result(), True
                if first is None:
                    log.warning("extraction worker failed on job %s (%s: %s); retrying alone", key,
                                type(exc).__name__, exc)
                    self._retry.append((key, args, exc))
                    continue
                log.error("extraction job %s failed twice (%s: %s; first: %s)", key, type(exc).__name__, exc, first)
                return key, self.on_failure(key, exc), False

    def close(self) -> None:
        if self._ex is not None:
            self._ex.shutdown(wait=True, cancel_futures=True)
        for _f, (_k, _a, _first, solo) in list(self._futs.items()):
            if solo is not None:
                solo.shutdown(wait=True, cancel_futures=True)
        self._futs.clear()
        self._retry.clear()
        self._inline = None


def run_incidents(jobs: list[tuple], fn: Callable, p: Plan, on_failure: Callable[[int, BaseException], Any],
                  order: list[int] | None = None) -> Iterator[tuple[int, Any, bool]]:
    """Run `fn(*jobs[i], state)` for every job in a process pool; yield `(i, result, ok)` in input order.

    `order` is the submission order (e.g. most expensive first); results are still yielded in input order.
    `fn` must be a module-level function (spawn pickles it by reference). A job that fails twice yields
    `(i, on_failure(i, exc), False)` (see `Pool`).
    """
    pool = Pool(p, fn, on_failure)
    done: dict[int, tuple[Any, bool]] = {}
    nxt = 0
    try:
        for i in (order or range(len(jobs))):
            pool.submit(i, jobs[i])
        while nxt < len(jobs):
            i, r, ok = pool.next_result()
            done[i] = (r, ok)
            while nxt in done:
                r, ok = done.pop(nxt)
                yield nxt, r, ok
                nxt += 1
    finally:
        pool.close()


# ------------------------------------------------------------------------------------------ downstream (fork)

_SHARED: Any = None


def downstream_workers(n_items: int) -> int:
    """Processes for the scoring / calibration / self-assignment maps: `HYDRA_SCORE_WORKERS`, else the extraction
    worker count when the operator set one (the per-job envelope in CLAUDE.md), else the job's core budget
    (at most MAX_WORKERS). 0 or 1 = in-process. Linux only: the maps fork (macOS fork is unsafe)."""
    if sys.platform != "linux":
        return 0
    n = _env_int("HYDRA_SCORE_WORKERS")
    if n is None:
        n = _env_int("HYDRA_EXTRACT_WORKERS")
    if n is None:
        n = min(MAX_WORKERS, cpu_budget())
    return max(0, min(n, n_items))


def _fork_call(task: tuple[Callable, list]) -> list:
    fn, chunk = task
    return [fn(_SHARED, x) for x in chunk]


def fork_map(fn: Callable[[Any, Any], Any], items: list, shared: Any, workers: int | None = None,
             chunks_per_worker: int = 4, on_chunk: Callable[[int], None] | None = None) -> list:
    """`[fn(shared, x) for x in items]`, computed in forked processes that inherit `shared` (no pickling of the
    corpus); results in input order, identical to the in-process loop (same code, same process image). `fn` must
    be a module-level function. `on_chunk(done_items)` reports progress in the caller. Falls back to the loop when
    `workers` <= 1. Children must not use the DB writer, torch or the progress file (the caller does)."""
    global _SHARED
    workers = downstream_workers(len(items)) if workers is None else workers
    if workers <= 1 or len(items) < 2:
        out = []
        for i, x in enumerate(items, 1):
            out.append(fn(shared, x))
            if on_chunk and i % 200 == 0:
                on_chunk(i)
        return out
    size = max(1, -(-len(items) // (workers * chunks_per_worker)))
    chunks = [items[i: i + size] for i in range(0, len(items), size)]
    _SHARED = shared
    try:
        with mp.get_context("fork").Pool(workers) as pool:
            out: list = []
            for part in pool.imap(_fork_call, [(fn, c) for c in chunks], chunksize=1):
                out.extend(part)
                if on_chunk:
                    on_chunk(len(out))
    finally:
        _SHARED = None
    return out
