"""Module-level job functions for the spawn pool tests (spawn pickles functions by reference)."""

from __future__ import annotations

import os
import time
from typing import Any


def square(x: int, delay: float, state: dict[str, Any]) -> tuple[int, int, str | None]:
    state["calls"] = state.get("calls", 0) + 1
    time.sleep(delay)
    return x * x, os.getpid(), os.environ.get("HYDRA_TORCH_THREADS")


def crash_on(x: int, bad: int, state: dict[str, Any]) -> int:
    if x == bad:
        os._exit(13)  # hard death: BrokenProcessPool in the parent
    return x * 10


def raise_on(x: int, bad: int, state: dict[str, Any]) -> int:
    if x == bad:
        raise ValueError("boom")
    return x + 1


def nested_pool(x: int, state: dict[str, Any]) -> int:
    """Opens a nested OCR process pool (as txt_lines does inside an incident worker) and leaves it open."""
    from hydra.genome import ocr_pool

    pool = ocr_pool.shared_pool(None, "rapidocr", 2)
    return pool._executor().submit(int, str(x)).result() + 1


def in_slot(x: int, marker_dir: str, state: dict[str, Any]) -> int:
    """Holds a torch slot for a moment; records how many jobs were inside a slot at the same time."""
    from pathlib import Path

    from hydra.parallel import compute_slot

    d = Path(marker_dir)
    with compute_slot():
        mine = d / f"{x}.in"
        mine.touch()
        inside = len(list(d.glob("*.in")))
        time.sleep(0.2)
        mine.unlink()
    return inside
