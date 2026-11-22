"""ADOPT #1 measurement: PDQ dihedral matching, canonical (lexicographic-min) hash vs min over the 8 variants.

SYNTHETIC creatives only (generated rectangles, ellipses and text bars; no downloaded or organiser images).
For every creative and transform (hflip, vflip, rot90, rot180, transpose, each also re-encoded as JPEG q70) the pair
(original, transformed) should match at PDQ Hamming <= 31. Reports the miss rate of
  old: Hamming(pdq_canon A, pdq_canon B)                       (synapse before 2026-11-21 21:00)
  new: min over k of Hamming(variant_k A, original B), both directions (`synapse.pdq_distance_matrix`)
and the false-match rate on all unrelated pairs at the same threshold.

    uv run python scripts/measure_pdq_dihedral.py [--n 50] [--json out.json]
"""

from __future__ import annotations

import argparse
import io
import json
from itertools import combinations

import numpy as np
from PIL import Image, ImageDraw, ImageOps

from hydra.genome.vis import pdq_dihedral

THRESHOLD = 31
TRANSFORMS = {
    "hflip": ImageOps.mirror,
    "vflip": ImageOps.flip,
    "rot90": lambda im: im.transpose(Image.Transpose.ROTATE_90),
    "rot180": lambda im: im.transpose(Image.Transpose.ROTATE_180),
    "transpose": lambda im: im.transpose(Image.Transpose.TRANSPOSE),
}


def creative(seed: int) -> Image.Image:
    rng = np.random.default_rng(seed)
    w, h = int(rng.integers(360, 720)), int(rng.integers(240, 720))
    im = Image.new("RGB", (w, h), tuple(int(c) for c in rng.integers(150, 255, 3)))
    d = ImageDraw.Draw(im)
    for _ in range(int(rng.integers(20, 90))):
        x, y = int(rng.integers(0, w)), int(rng.integers(0, h))
        dx, dy = int(rng.integers(10, w // 3)), int(rng.integers(10, h // 3))
        fill = tuple(int(c) for c in rng.integers(0, 255, 3))
        (d.ellipse if rng.random() < 0.3 else d.rectangle)((x, y, x + dx, y + dy), fill=fill)
    d.rectangle((10, 10, int(w * 0.6), int(h * 0.15)), fill=tuple(int(c) for c in rng.integers(0, 120, 3)))
    return im


def jpeg(im: Image.Image, q: int = 70) -> Image.Image:
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=q)
    return Image.open(io.BytesIO(buf.getvalue())).convert("RGB")


def ham(a: str, b: str) -> int:
    return int.bit_count(int(a, 16) ^ int(b, 16))


def old_d(ha: list[str], hb: list[str]) -> int:
    return ham(min(ha), min(hb))


def new_d(ha: list[str], hb: list[str]) -> int:
    return min(min(ham(x, hb[0]) for x in ha), min(ham(ha[0], y) for y in hb))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()
    originals = [creative(1000 + k) for k in range(a.n)]
    hashes = [pdq_dihedral(im)[0] for im in originals]
    per: dict[str, dict[str, int]] = {}
    for name, fn in TRANSFORMS.items():
        for enc in ("", "+jpeg70"):
            key = name + enc
            row = per.setdefault(key, {"pairs": 0, "old_miss": 0, "new_miss": 0})
            for im, h in zip(originals, hashes, strict=True):
                t = fn(im)
                ht = pdq_dihedral(jpeg(t) if enc else t)[0]
                row["pairs"] += 1
                row["old_miss"] += old_d(h, ht) > THRESHOLD
                row["new_miss"] += new_d(h, ht) > THRESHOLD
    # plain re-encode (no transform) as a control
    row = per.setdefault("jpeg70_only", {"pairs": 0, "old_miss": 0, "new_miss": 0})
    for im, h in zip(originals, hashes, strict=True):
        ht = pdq_dihedral(jpeg(im))[0]
        row["pairs"] += 1
        row["old_miss"] += old_d(h, ht) > THRESHOLD
        row["new_miss"] += new_d(h, ht) > THRESHOLD
    unrelated = list(combinations(range(a.n), 2))
    fm_old = sum(old_d(hashes[i], hashes[j]) <= THRESHOLD for i, j in unrelated)
    fm_new = sum(new_d(hashes[i], hashes[j]) <= THRESHOLD for i, j in unrelated)
    tot = {k: sum(r[k] for r in per.values()) for k in ("pairs", "old_miss", "new_miss")}
    out = {"n_creatives": a.n, "threshold": THRESHOLD, "per_transform": per, "total": tot,
           "old_miss_rate": round(tot["old_miss"] / tot["pairs"], 4),
           "new_miss_rate": round(tot["new_miss"] / tot["pairs"], 4),
           "unrelated_pairs": len(unrelated), "false_match_old": fm_old, "false_match_new": fm_new}
    print(json.dumps(out, indent=1))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
