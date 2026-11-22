"""Extractor interface shared by all gene streams (architect-owned, like hydra/contracts.py).

An extractor turns one Incident (with its artifacts resolved to local files) into Genes and, optionally,
embedding vectors. It must never raise on bad input: return genes with status failed/unavailable instead.

    @register("har_hosts", family=Family.INF, version=1)
    def har_hosts(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult: ...

The pipeline (CORE) calls `run_all(inc, ctx)` in registration order, or a subset via `only=`.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import traceback
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from hydra.config import Settings
from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Incident, IncidentArtifact

log = logging.getLogger(__name__)


@dataclass
class ResolvedArtifact:
    meta: IncidentArtifact
    path: Path  # read-only original in the CAS (or a work copy for derived media)


@dataclass
class ResolvedIncident:
    incident: Incident
    artifacts: list[ResolvedArtifact]

    def by_role(self, *roles: str) -> list[ResolvedArtifact]:
        return [a for a in self.artifacts if a.meta.role in roles]


@dataclass
class ExtractContext:
    settings: Settings
    work_dir: Path                      # per-incident scratch dir for derived files (frames, crops)
    enrich: Any | None = None           # hydra.enrich client (replay-only unless HYDRA_ENRICH=1)
    models: dict[str, Any] = field(default_factory=dict)  # lazily loaded models shared across incidents
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Vector:
    key: str            # unique: f"{incident_id}|{artifact_id}|{kind}|{idx}"
    model: str          # e.g. "dinov2-small@ed25f3a"
    data: np.ndarray    # float32, L2-normalised, 1-D


@dataclass
class ExtractResult:
    genes: list[Gene] = field(default_factory=list)
    vectors: list[Vector] = field(default_factory=list)


ExtractorFn = Callable[[ResolvedIncident, ExtractContext], ExtractResult]


@dataclass
class Extractor:
    name: str
    family: Family
    version: int
    fn: ExtractorFn

    @property
    def tag(self) -> str:
        return f"{self.name}@{self.version}"


REGISTRY: dict[str, Extractor] = {}


def register(name: str, *, family: Family, version: int = 1) -> Callable[[ExtractorFn], ExtractorFn]:
    def deco(fn: ExtractorFn) -> ExtractorFn:
        if name in REGISTRY:
            raise ValueError(f"extractor {name!r} registered twice")
        REGISTRY[name] = Extractor(name, family, version, fn)
        return fn

    return deco


def failed_gene(inc: Incident, ex: Extractor, exc: BaseException, artifact_id: str | None = None) -> Gene:
    return Gene(
        incident_id=inc.id, family=ex.family, kind=ex.name, status=GeneStatus.FAILED, extractor=ex.tag,
        evidence=EvidencePointer(artifact_id=artifact_id, note=f"{type(exc).__name__}: {exc}"[:300]),
    )


# Per-extractor time limit (GAP-AUDIT B6): one odd file must not hang a hidden-set run. The limit is a SIGALRM
# timer around each extractor call (main thread only: the CLI loop and every pool worker); a timed-out extractor
# gives one `unavailable` gene with an `ExtractorTimeout` note, which the gene cache retries on a later run like any
# runtime failure. `ExtractorTimeout` is a BaseException so the extractors' own `except Exception` degradation
# handlers cannot swallow it. A hang inside C code (no bytecode runs, so the signal waits) is caught in pool
# workers by a watchdog that ends the process at 2 x limit + 60 s; the pool retries the job alone once, then
# records failed genes. HYDRA_EXTRACTOR_TIMEOUT_S overrides the limit, 0 turns it off.
EXTRACTOR_TIMEOUT_S = 600.0  # 75 x the longest extractor call measured on 825 incidents (8 s, SCALE)
IN_POOL_WORKER = False  # set by hydra.parallel in incident workers (watchdog may end the process)


class ExtractorTimeout(BaseException):  # noqa: N818 — reported as the gene note "ExtractorTimeout: ..."
    pass


def extractor_timeout_s() -> float:
    v = os.environ.get("HYDRA_EXTRACTOR_TIMEOUT_S", "").strip()
    try:
        return max(0.0, float(v)) if v else EXTRACTOR_TIMEOUT_S
    except ValueError:
        return EXTRACTOR_TIMEOUT_S


@contextmanager
def _time_limit(name: str, seconds: float) -> Iterator[None]:
    usable = (seconds > 0 and hasattr(signal, "setitimer") and threading.current_thread() is threading.main_thread()
              and signal.getitimer(signal.ITIMER_REAL)[0] == 0)
    if not usable:
        yield
        return

    def _expired(_signum, _frame):
        raise ExtractorTimeout(f"{name} exceeded the {seconds:g} s extractor time limit")

    watchdog = None
    if IN_POOL_WORKER:
        def _hard_exit() -> None:
            log.error("extractor %s still running after %.0f s inside native code: ending the worker", name,
                      2 * seconds + 60)
            os._exit(70)

        watchdog = threading.Timer(2 * seconds + 60, _hard_exit)
        watchdog.daemon = True
        watchdog.start()
    old = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)
        if watchdog is not None:
            watchdog.cancel()


def timeout_gene(inc: Incident, ex: Extractor, exc: ExtractorTimeout) -> Gene:
    return Gene(
        incident_id=inc.id, family=ex.family, kind=ex.name, status=GeneStatus.UNAVAILABLE, extractor=ex.tag,
        evidence=EvidencePointer(note=f"ExtractorTimeout: {exc}"[:300]),
    )


def run_all(inc: ResolvedIncident, ctx: ExtractContext, only: Iterable[str] | None = None) -> ExtractResult:
    """Run every registered extractor; one failing (or timed-out) extractor never stops the others."""
    out = ExtractResult()
    names = list(only) if only is not None else list(REGISTRY)
    limit = extractor_timeout_s()
    for name in names:
        ex = REGISTRY[name]
        try:
            with _time_limit(name, limit):
                res = ex.fn(inc, ctx)
        except ExtractorTimeout as exc:
            log.warning("extractor %s timed out on %s: %s", ex.tag, inc.incident.id, exc)
            out.genes.append(timeout_gene(inc.incident, ex, exc))
            continue
        except Exception as exc:  # noqa: BLE001 — degradation without failure (spec)
            log.warning("extractor %s failed on %s: %s", ex.tag, inc.incident.id, traceback.format_exc(limit=3))
            out.genes.append(failed_gene(inc.incident, ex, exc))
            continue
        out.genes.extend(res.genes)
        out.vectors.extend(res.vectors)
    return out


def load_family_modules() -> None:
    """Import all family modules so their @register decorators run. Missing modules are skipped."""
    import importlib

    for mod in ("har", "adshop", "dom", "inf", "domain_pl", "vis", "ident", "logo", "txt", "chat", "nar", "meta", "aud",
                "vid_avsync", "prov", "dfk"):
        try:
            importlib.import_module(f"hydra.genome.{mod}")
        except ModuleNotFoundError as exc:
            if exc.name != f"hydra.genome.{mod}":
                raise
