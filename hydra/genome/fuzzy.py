"""TLSH fuzzy hashes for kit builds (Oliver et al., CTC 2013; py-tlsh, Apache-2.0 or BSD).

Genes `tlsh_html_skeleton`, `tlsh_js`, `tlsh_css` (dependency group `inf:kit:fuzzy_build`) carry a TLSH digest
(`T1...`). They catch edited kit builds where sha256 and the normalised build hashes differ. They are compared
by distance, not by equality: `pair_bits(distance, background)` turns a distance into surprise bits against the
background distribution of cross-incident distances in the corpus (no fixed threshold). Kit tier only: evidence
for a campaign group, never for "same campaign" by itself.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from itertools import combinations

TLSH_KINDS = ("tlsh_html_skeleton", "tlsh_js", "tlsh_css")
MIN_BYTES = 256
MAX_BITS = 10.0
# Measured on the NASK sample (docs/genes/INF.md §7): same-kit JS/CSS pairs at distance 0-1, unrelated pairs >= 111.
# Distances above NEAR_BAND give 0 bits whatever the background says (small corpora make the background coarse).
NEAR_BAND = 30


def tlsh_value(data: bytes) -> str | None:
    """TLSH digest, or None when TLSH refuses the input (TNULL: under 50 bytes or too little variety)."""
    import tlsh

    if len(data) < 50:
        return None
    h = tlsh.hash(data)
    return h if h and h != "TNULL" else None


def distance(a: str, b: str) -> int:
    import tlsh

    return int(tlsh.diff(a, b))


def best_distance(a: Iterable[str], b: Iterable[str]) -> int | None:
    bl = list(b)
    ds = [distance(x, y) for x in a for y in bl]
    return min(ds) if ds else None


def background(values_by_incident: Mapping[str, Iterable[str]], max_pairs: int = 50_000) -> list[int]:
    """Sorted best distances over all incident pairs (one number per pair that both have the gene)."""
    items = [(k, list(v)) for k, v in values_by_incident.items() if v]
    out: list[int] = []
    for (_, a), (_, b) in combinations(items, 2):
        d = best_distance(a, b)
        if d is not None:
            out.append(d)
        if len(out) >= max_pairs:
            break
    return sorted(out)


def pair_bits(d: int, bg: list[int], band: int = NEAR_BAND) -> float:
    """-log2 P(background distance <= d), add-one smoothing, capped at MAX_BITS; 0 above the near band."""
    if not bg or d > band:
        return 0.0
    import bisect

    k = bisect.bisect_right(bg, d)
    p = (k + 1) / (len(bg) + 1)
    return min(MAX_BITS, -math.log2(p))
