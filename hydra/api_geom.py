"""GET /v1/pairs/{a}/{b}/geometry: ORB/RootSIFT + MAGSAC++ verification of the most similar image pairs of two
incidents, for the pair / variant-diff view (ADV-VIS). Display only: it never changes a score, and it is on by
default because nothing is stored in the scoring tables (results are cached in `work/vis_geom/`, the same cache the
flag-gated scorer hook `hydra.vis_geom` reads).

Per verified image pair: inlier count, hull coverage and grid occupancy on both sides, homography (unit pixels
a -> b), anisotropy / perspective, `same_asset` (>= 120 inliers, coverage >= 15 %), `crop_match` (crop/resize of the
same picture), and up to 150 inlier correspondences with coordinates normalised to the whole artefact (0..1), so the
UI can draw them over `/v1/artifacts/{id}/thumb`.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["visual"])

MAX_PAIRS = 3       # image pairs verified per request
DISPLAY_COS = 0.5   # DINOv2 cosine floor for a pair worth showing


def _norm_points(res: dict, ref_a: tuple, ref_b: tuple, src_a: tuple[int, int], src_b: tuple[int, int]
                 ) -> list[list[float]]:
    """Unit-image px -> artefact-normalised [xa, ya, xb, yb] (bbox offset, unit scale)."""
    def tf(x: float, y: float, ref: tuple, unit: list[int], src: tuple[int, int]) -> tuple[float, float]:
        bx, by, bw, bh = ref[1] if ref[1] else (0.0, 0.0, float(src[0]), float(src[1]))
        return ((bx + x * bw / max(1, unit[0])) / max(1, src[0]), (by + y * bh / max(1, unit[1])) / max(1, src[1]))

    out = []
    for xa, ya, xb, yb in res.get("points") or []:
        pa = tf(xa, ya, ref_a, res["size_a"], src_a)
        pb = tf(xb, yb, ref_b, res["size_b"], src_b)
        out.append([round(pa[0], 4), round(pa[1], 4), round(pb[0], 4), round(pb[1], 4)])
    return out


def _source_size(st: Any, artifact_id: str) -> tuple[int, int] | None:
    from hydra.genome.vis_media import load_unit_image

    try:
        _im, orig, _scale = load_unit_image(st.store.safe_path(artifact_id), max_side=64)
        return int(orig[0]), int(orig[1])
    except Exception:  # noqa: BLE001
        return None


def geometry(st: Any, a: str, b: str) -> dict[str, Any]:
    from hydra import synapse, vis_geom
    from hydra.pipeline import load_genomes

    lo, hi = sorted((a, b))
    with st.reader() as conn:
        for i in (lo, hi):
            if not conn.execute("SELECT 1 FROM incidents WHERE id=?", (i,)).fetchone():
                raise HTTPException(status_code=404, detail=f"incident {i} not found")
        gs = load_genomes(conn, [lo, hi])

    def rows(g) -> list[tuple[str, dict]]:
        return sorted(((u, d) for u, d in g.units.items() if "dinov2" in d and d.get("class") not in
                       synapse.VIS_EXCLUDED_CLASSES and vis_geom.unit_ref(d.get("gene")) is not None),
                      key=lambda t: t[0])

    ra, rb = (rows(gs[lo]), rows(gs[hi])) if lo in gs and hi in gs else ([], [])
    pairs: list[dict[str, Any]] = []
    if ra and rb:
        cos = np.stack([d["dinov2"] for _, d in ra]) @ np.stack([d["dinov2"] for _, d in rb]).T
        order = sorted(((float(cos[i, j]), i, j) for i in range(len(ra)) for j in range(len(rb))
                        if cos[i, j] >= DISPLAY_COS), key=lambda t: (-t[0], t[1], t[2]))
        seen_a: set[int] = set()
        for c, i, j in order:
            if len(pairs) >= MAX_PAIRS:
                break
            if i in seen_a:
                continue
            ua, ub = vis_geom.unit_ref(ra[i][1]["gene"]), vis_geom.unit_ref(rb[j][1]["gene"])
            res = vis_geom.cached_verify(ua, ub, st.settings, budget=False)
            if res is None:
                continue
            seen_a.add(i)
            src_a, src_b = _source_size(st, ua[0]), _source_size(st, ub[0])
            pts = _norm_points(res, ua, ub, src_a, src_b) if src_a and src_b else []
            pairs.append({
                "unit_a": ra[i][0], "unit_b": rb[j][0], "artifact_a": ua[0], "artifact_b": ub[0],
                "bbox_a": list(ua[1]) if ua[1] else None, "bbox_b": list(ub[1]) if ub[1] else None,
                "class_a": ra[i][1].get("class"), "class_b": rb[j][1].get("class"), "dinov2_cos": round(c, 4),
                "source_size_a": list(src_a) if src_a else None, "source_size_b": list(src_b) if src_b else None,
                **{k: res.get(k) for k in ("method", "inliers", "coverage_a", "coverage_b", "grid_a", "grid_b",
                                           "mirrored", "homography", "anisotropy", "perspective", "scale",
                                           "size_a", "size_b", "same_asset", "crop_match")},
                "points": pts,
                "explanation_pl": vis_geom.explanation(res, c) if res.get("inliers") else
                "Geometria: brak wspólnych punktów (ORB/RootSIFT + MAGSAC++)",
            })
    return {"a": lo, "b": hi, "version": vis_geom.VERSION, "pairs": pairs,
            "thresholds": {"min_inliers": vis_geom.MIN_INLIERS, "min_coverage": vis_geom.MIN_COVERAGE,
                           "crop_coverage": vis_geom.CROP_COVERAGE, "crop_grid": vis_geom.CROP_GRID},
            "scoring_flags": {"HYDRA_ADV_GEOM": vis_geom.geom_on(), "HYDRA_ADV_CROP": vis_geom.crop_on()},
            "note_pl": ("Weryfikacja geometryczna najbardziej podobnych obrazów pary (punkty kluczowe ORB, w razie"
                        " potrzeby RootSIFT, homografia MAGSAC++). Widok pomocniczy: wynik pary liczy ją tylko przy"
                        " włączonych flagach HYDRA_ADV_GEOM / HYDRA_ADV_CROP.")}


@router.get("/pairs/{a}/{b}/geometry")
def pair_geometry(request: Request, a: str, b: str) -> dict[str, Any]:
    from hydra.api_task import _st

    return geometry(_st(request), a, b)


@router.get("/geometry/stats", summary="Liczniki weryfikacji geometrycznej / Geometry verification counters")
def geometry_stats() -> dict:
    """Verifications done and refused by the per-process budget (Astra review 2026-11-22): `complete` is false when
    any crop/same-asset check was skipped, so the analysis of those pairs is incomplete."""
    from hydra import vis_geom

    return vis_geom.stats()
