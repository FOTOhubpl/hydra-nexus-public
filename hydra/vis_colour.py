"""HSV colour histogram: a weak VIS unit feature (ADV-VIS, `HYDRA_ADV_HSV=1`, default off).

The histogram is a joint 8 (hue) x 4 (saturation) x 4 (value) histogram of the unit downscaled to 128 px. It is
computed when a pair is scored, from the unit image re-opened through its evidence pointer (the same path as
`hydra.vis_geom`), and memoised per unit. It is not a stored extraction gene, because a new gene would change the
extraction code digest and with it every key of the host-wide gene CAS, also with the flag off.

Colour layout is cheap and survives crops, resizes, overlays and re-encoding. It does not survive a recolour, and two
unrelated creatives in the same brand palette collide, so it is never campaign evidence. The scorer hook adds an
ASSET-tier reason (`hsv_colour`, `HSV_BITS`) only when the histogram intersection of two eligible units is
>= `HSV_MIN` AND their DINOv2 cosine is >= `HSV_COS_MIN`, for at most `TOPK` unit pairs per incident pair.
"""

from __future__ import annotations

import os
import threading

import numpy as np
from PIL import Image

FLAG = "HYDRA_ADV_HSV"
BINS = (8, 4, 4)
HSV_MIN = 0.90
HSV_COS_MIN = 0.85
HSV_BITS = 1.0
TOPK = 4
GROUP = "VIS:asset:hsv_colour"

_LOCK = threading.Lock()
_MEMO: dict[tuple, np.ndarray | None] = {}


def on() -> bool:
    return os.environ.get(FLAG) == "1"


def hsv_hist(im: Image.Image) -> np.ndarray:
    """Normalised joint HSV histogram [8*4*4] (sums to 1)."""
    g = im.convert("RGB")
    f = 128 / max(g.size)
    if f < 1:
        g = g.resize((max(1, round(g.width * f)), max(1, round(g.height * f))), Image.Resampling.BILINEAR)
    hsv = np.asarray(g.convert("HSV"), dtype=np.int32).reshape(-1, 3)
    idx = [np.minimum(hsv[:, k] * n // 256, n - 1) for k, n in enumerate(BINS)]
    flat = (idx[0] * BINS[1] + idx[1]) * BINS[2] + idx[2]
    h = np.bincount(flat, minlength=int(np.prod(BINS))).astype(np.float64)
    return h / max(1.0, h.sum())


def hsv_hist_hex(im: Image.Image) -> str:
    """Compact display form (1 byte per bin, 256 hex chars)."""
    return np.clip(np.rint(hsv_hist(im) * 255), 0, 255).astype(np.uint8).tobytes().hex()


def intersection(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.minimum(a, b).sum())


def unit_hist(ref: tuple | None) -> np.ndarray | None:
    from hydra import vis_geom

    if ref is None:
        return None
    key = (ref[0], tuple(ref[1]) if ref[1] else None)
    with _LOCK:
        if key in _MEMO:
            return _MEMO[key]
    im = vis_geom.load_unit(ref)
    h = hsv_hist(im) if im is not None else None
    with _LOCK:
        _MEMO[key] = h
    return h


def scorer_hook(ra: list[tuple[str, dict]], rb: list[tuple[str, dict]], cos: np.ndarray, groups: dict) -> None:
    """Best (intersection, cos) unit pair above both floors -> one ASSET-tier reason."""
    from hydra import vis_geom
    from hydra.synapse import _match

    ii, jj = np.nonzero(cos >= HSV_COS_MIN)
    order = sorted(zip(ii.tolist(), jj.tolist(), strict=True), key=lambda t: (-float(cos[t]), t))[:TOPK]
    best = None
    for i, j in order:
        ha = unit_hist(vis_geom.unit_ref(ra[i][1].get("gene")))
        hb = unit_hist(vis_geom.unit_ref(rb[j][1].get("gene")))
        if ha is None or hb is None:
            continue
        x = intersection(ha, hb)
        if x >= HSV_MIN and (best is None or (x, float(cos[i, j])) > best[:2]):
            best = (x, float(cos[i, j]), i, j)
    if best is None:
        return
    x, c, i, j = best
    (ua, da), (ub, db) = ra[i], rb[j]
    m = _match("VIS", "hsv_colour", ua, ub, HSV_BITS, sim=round(x, 3), ga=da.get("gene"), gb=db.get("gene"),
               expl=(f"Zbliżony rozkład kolorów (histogram HSV 8x4x4, część wspólna {x:.2f}, DINOv2 cos={c:.3f});"
                     f" słaba cecha, poziom zasobu, 0 bit kampanii (+{HSV_BITS:.1f} bit)"))
    groups[GROUP].append((HSV_BITS, m, "asset", "VIS"))
