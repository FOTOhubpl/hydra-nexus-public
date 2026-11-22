"""Multi-process OCR with a content-addressed cache (TXT family).

- Engine: `hydra.ml.ocr` (RapidOCR PP-OCRv6 small by default). Lab result (PLAN_MAX §1.7): N worker
  processes x 1 onnxruntime thread beats one process with N threads, so the pool sets
  HYDRA_OCR_THREADS=1 / OMP_THREAD_LIMIT=1 inside each worker only (SPEC_ERRATA 9).
- Cache: `$HYDRA_CACHE_DIR/ocr/<engine>-<CACHE_VERSION>/<sha256[:2]>/<sha256>[_<variant>].json`, keyed by the
  sha256 of the image bytes (the CAS artifact id for originals, the frame bytes for video frames).
- Phone screenshots: the OS status bar (clock, battery, carrier) and the home indicator are cropped before
  OCR; boxes are mapped back to the original pixel coordinates.
- `workers=0` runs in-process (tests, tiny inputs). Errors never propagate: a failed image returns
  `OcrResult(ok=False, error=...)`.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CACHE_VERSION = "1"
PHONE_MIN_ASPECT = 1.9   # h / w of a portrait phone screenshot (iPhone 1170x2532 = 2.16)
PHONE_TOP_CROP = 0.06    # status bar (47 pt of 844 pt = 5.6%) plus margin; visual analyst: ~6%
PHONE_BOTTOM_CROP = 0.05  # home indicator + tab-bar icons (a bottom Safari address bar sits above this)
MAX_SIDE = 2048           # work-image bound (hydra.media.load_work_image)


@dataclass
class OcrBox:
    text: str
    box: tuple[int, int, int, int]  # x0, y0, x1, y1 in ORIGINAL image pixels
    score: float


@dataclass
class OcrResult:
    sha256: str
    engine: str
    lines: list[OcrBox] = field(default_factory=list)
    size: tuple[int, int] = (0, 0)  # original (w, h)
    crop: tuple[int, int, int, int] | None = None  # region that was OCR'd (x0, y0, x1, y1), original px
    seconds: float = 0.0
    cached: bool = False
    ok: bool = True
    error: str | None = None
    # CR-ASTRA-PIPE-101: typed failure class, set from the exception type (never parsed from `error` text):
    # "unavailable" = the engine/weights are not installed (unknown observation), "failed" = this image failed.
    error_kind: str | None = None

    def to_json(self) -> dict:
        d = asdict(self)
        d.pop("cached", None)
        return d

    @classmethod
    def from_json(cls, d: dict) -> OcrResult:
        lines = [OcrBox(x["text"], tuple(x["box"]), float(x["score"])) for x in d.get("lines", [])]  # type: ignore[arg-type]
        crop = tuple(d["crop"]) if d.get("crop") else None
        return cls(d["sha256"], d["engine"], lines, tuple(d.get("size") or (0, 0)), crop,  # type: ignore[arg-type]
                   float(d.get("seconds", 0.0)), True, bool(d.get("ok", True)), d.get("error"),
                   d.get("error_kind"))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_phone_screenshot(w: int, h: int) -> bool:
    return w > 0 and h / w >= PHONE_MIN_ASPECT and 600 <= w <= 1600


def chrome_crop(w: int, h: int) -> tuple[int, int, int, int] | None:
    if not is_phone_screenshot(w, h):
        return None
    return 0, round(h * PHONE_TOP_CROP), w, h - round(h * PHONE_BOTTOM_CROP)


def cache_path(cache_dir: Path, sha: str, engine: str, variant: str = "") -> Path:
    name = f"{sha}_{variant}.json" if variant else f"{sha}.json"
    return cache_dir / "ocr" / f"{engine}-{CACHE_VERSION}" / sha[:2] / name


def _engine_fn(engine: str) -> Callable:
    from hydra.ml.ocr import ocr as _ocr

    return lambda im: _ocr(im, engine=engine)


def ocr_file(path: Path, sha: str, engine: str = "rapidocr", crop_chrome: bool = True,
             engine_fn: Callable | None = None) -> OcrResult:
    """OCR one image file (no cache). Never raises."""
    t0 = time.perf_counter()
    try:
        from hydra.media import load_work_image

        wi = load_work_image(path, max_side=MAX_SIDE)
        w, h = wi.original_size
        crop = chrome_crop(w, h) if crop_chrome else None
        im = wi.image
        off_x = off_y = 0.0
        if crop:
            s = wi.scale
            im = im.crop((round(crop[0] * s), round(crop[1] * s), round(crop[2] * s), round(crop[3] * s)))
            off_x, off_y = crop[0] * s, crop[1] * s
        fn = engine_fn or _engine_fn(engine)
        lines = []
        for ln in fn(im):
            text = (ln.text or "").strip()
            if not text:
                continue
            x0, y0, x1, y1 = ln.box
            box = (round((x0 + off_x) / wi.scale), round((y0 + off_y) / wi.scale),
                   round((x1 + off_x) / wi.scale), round((y1 + off_y) / wi.scale))
            lines.append(OcrBox(text, box, round(float(ln.score), 4)))
        return OcrResult(sha, engine, lines, (w, h), crop, round(time.perf_counter() - t0, 3))
    except Exception as exc:  # noqa: BLE001 — one bad image must not stop the batch
        from hydra.ml.models import ModelUnavailable

        kind = "unavailable" if isinstance(exc, ModelUnavailable) else "failed"
        return OcrResult(sha, engine, size=(0, 0), seconds=round(time.perf_counter() - t0, 3), ok=False,
                         error=f"{type(exc).__name__}: {exc}"[:300], error_kind=kind)


def _worker_init() -> None:
    os.environ["HYDRA_OCR_THREADS"] = "1"
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"


def _worker_job(args: tuple[str, str, str, bool]) -> dict:
    path, sha, engine, crop_chrome = args
    return ocr_file(Path(path), sha, engine, crop_chrome).to_json()


MAX_DEFAULT_WORKERS = 16  # single-process path (assign, one incident); hydra.parallel sets its own budget


def default_workers() -> int:
    """`HYDRA_OCR_WORKERS` (set per incident worker by `hydra.parallel` from its CPU share), else half the
    cores capped at 16 (was 8: on the 64-vCPU server the single-process path left most cores idle)."""
    env = os.environ.get("HYDRA_OCR_WORKERS", "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            log.warning("HYDRA_OCR_WORKERS=%r is not an integer; using the default", env)
    return max(1, min((os.cpu_count() or 2) // 2, MAX_DEFAULT_WORKERS))


class OcrPool:
    """Cached OCR over many images. `run([(path, sha), ...])` returns results in input order."""

    def __init__(self, cache_dir: Path | None, engine: str = "rapidocr", workers: int | None = None,
                 engine_fn: Callable | None = None):
        self.cache_dir = cache_dir
        self.engine = engine
        self.workers = default_workers() if workers is None else workers
        self.engine_fn = engine_fn  # in-process override (tests); forces workers=0
        if engine_fn is not None:
            self.workers = 0
        self._exec: ProcessPoolExecutor | None = None

    # -- cache
    def _load(self, sha: str, variant: str) -> OcrResult | None:
        if self.cache_dir is None:
            return None
        p = cache_path(self.cache_dir, sha, self.engine, variant)
        if not p.is_file():
            return None
        try:
            return OcrResult.from_json(json.loads(p.read_text("utf-8")))
        except Exception:  # noqa: BLE001 — corrupt cache entry = miss
            return None

    def _store(self, res: OcrResult, variant: str) -> None:
        if self.cache_dir is None or not res.ok:
            return
        p = cache_path(self.cache_dir, res.sha256, self.engine, variant)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(res.to_json(), ensure_ascii=False), "utf-8")
            tmp.replace(p)
        except OSError as exc:
            log.warning("OCR cache write failed: %s", exc)

    # -- execution
    def _executor(self) -> ProcessPoolExecutor:
        if self._exec is None:
            import multiprocessing as mp

            self._exec = ProcessPoolExecutor(max_workers=self.workers, mp_context=mp.get_context("spawn"),
                                             initializer=_worker_init)
        return self._exec

    def run(self, items: Iterable[tuple[Path, str | None]], crop_chrome: bool = True) -> list[OcrResult]:
        items = [(Path(p), sha or sha256_file(Path(p))) for p, sha in items]
        variant = "" if crop_chrome else "nocrop"
        out: list[OcrResult | None] = [self._load(sha, variant) for _, sha in items]
        todo = [i for i, r in enumerate(out) if r is None]
        if todo:
            if self.workers <= 1 or len(todo) == 1:
                for i in todo:
                    p, sha = items[i]
                    out[i] = ocr_file(p, sha, self.engine, crop_chrome, self.engine_fn)
            else:
                try:
                    jobs = [(str(items[i][0]), items[i][1], self.engine, crop_chrome) for i in todo]
                    for i, d in zip(todo, self._executor().map(_worker_job, jobs), strict=True):
                        out[i] = OcrResult.from_json(d)
                        out[i].cached = False  # type: ignore[union-attr]
                except Exception as exc:  # noqa: BLE001 — broken pool: fall back to in-process
                    log.warning("OCR pool failed (%s); running in-process", exc)
                    self.close()
                    for i in todo:
                        if out[i] is None:
                            p, sha = items[i]
                            out[i] = ocr_file(p, sha, self.engine, crop_chrome, self.engine_fn)
            for i in todo:
                self._store(out[i], variant)  # type: ignore[arg-type]
        return out  # type: ignore[return-value]

    def close(self) -> None:
        if self._exec is not None:
            self._exec.shutdown(wait=False, cancel_futures=True)
            self._exec = None


_POOLS: dict[tuple, OcrPool] = {}


def shared_pool(cache_dir: Path | None, engine: str = "rapidocr", workers: int | None = None) -> OcrPool:
    """Process-wide pool reused across incidents (worker start-up + model load cost ~1-2 s)."""
    key = (str(cache_dir), engine, workers)
    if key not in _POOLS:
        _POOLS[key] = OcrPool(cache_dir, engine, workers)
    return _POOLS[key]


@atexit.register
def _close_pools() -> None:
    for p in _POOLS.values():
        p.close()
