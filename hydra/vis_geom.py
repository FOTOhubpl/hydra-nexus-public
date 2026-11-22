"""Pair geometry for VIS units (ADV-VIS): ORB + MAGSAC++ same-asset verification on visual near-miss pairs.

`vis.same_asset` existed but was never called (SPEC_ERRATA E-SH-6: `score_pair` sees genes, not images). This
module gives the scorer and the pair view one bounded entry point:

* `verify(a, b)`: ORB (2000 keypoints, ratio test 0.75) + `cv2.USAC_MAGSAC` homography with a mirrored retry; when
  ORB finds too few inliers, one RootSIFT retry (Arandjelovic & Zisserman 2012: L1-normalised SIFT, square root).
  Returns inliers, coverage on both sides (convex hull of the inliers / image area), grid occupancy (share of 8 x 8
  cells of each image holding an inlier), the homography in unit-image pixels (a -> b), its anisotropy and
  perspective, and up to `MAX_POINTS` inlier correspondences for the UI overlay.
* `near_miss(ra, rb, cos, near)`: the scorer hook. Only unit pairs that the hash rules did NOT match and whose
  DINOv2 cosine is >= `GEOM_COS_MIN`, at most `TOPK` per incident pair (highest cosine first) and `BUDGET` image
  verifications per process; results are memoised per (artifact, bbox) pair and cached on disk
  (`work/vis_geom/*.json`), so leave-one-out and robustness re-scoring cost nothing.

Flags (default off; with both off the scorer never calls this module and outputs are byte-identical):
* `HYDRA_ADV_GEOM=1`: a verified same-asset pair becomes an ASSET-tier reason (`geom_same_asset`, 0 campaign bits).
* `HYDRA_ADV_CROP=1`: a verified pair that is a crop/resize of the same picture (near-similarity transform:
  anisotropy <= `CROP_ANISO`, little perspective, hull coverage and grid occupancy above floors on both sides) counts
  like the crop sub-window rule (`visual_near_duplicate`, same class and stock gates in
  `synapse._visual_near_duplicate`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

VERSION = "vis_geom@1"
FLAG_GEOM = "HYDRA_ADV_GEOM"
FLAG_CROP = "HYDRA_ADV_CROP"

GEOM_COS_MIN = 0.80      # DINOv2 cosine floor for a near-miss pair worth verifying
TOPK = 4                 # unit pairs verified per incident pair
# Image verifications per process (bounded cost). Raised from 4000 on 2026-11-22 (Astra review of d5d5c3b: with the
# old cap an exhausted budget silently turned a verified crop into "independent", so results depended on how long
# the process had been running). Override with HYDRA_GEOM_BUDGET; every skipped verification is counted and logged.
BUDGET = int(os.environ.get("HYDRA_GEOM_BUDGET", "100000"))
ORB_N = 2000
RATIO = 0.75
RANSAC_PX = 5.0
MIN_INLIERS = 120        # PLAN_MAX 1.7 gate (vis.SAME_ASSET_INLIERS)
MIN_COVERAGE = 0.15      # SPEC_ERRATA: inliers must cover >= 15 % of the image (vis.SAME_ASSET_COVERAGE)
CROP_COVERAGE = 0.25     # crop rule: hull coverage on BOTH sides
CROP_GRID = 0.30         # crop rule: share of 8x8 cells with an inlier on BOTH sides (UI chrome alone fails)
CROP_ANISO = 1.25        # crop rule: singular-value ratio of the affine part (uniform resize ~ 1.0)
CROP_PERSP = 0.15        # crop rule: |h20 * w| + |h21 * h| (projective distortion across the image)
GRID = 8
MAX_POINTS = 150         # correspondences kept for the UI overlay
GEOM_ASSET_BITS = 4.0    # asset-tier bits of a verified same-asset pair (never campaign bits)


def geom_on() -> bool:
    return os.environ.get(FLAG_GEOM) == "1"


def crop_on() -> bool:
    # Default ON since 2026-11-22 (operator decision; hidden_sim full 112 -> 117 of 119, E-ADV-VIS). HYDRA_ADV_CROP=0
    # restores the previous behaviour.
    return os.environ.get(FLAG_CROP, "1") == "1"


def score_key_part() -> str:
    """Scoring-relevant ADV-VIS flag state, for cache keys of scored results (e.g. the opt-in incremental
    `StageCache` score key in `pipeline.analyse`). "" when every flag is off."""
    on = [n for n, v in (("geom", geom_on()), ("crop", crop_on()), ("hsv", os.environ.get("HYDRA_ADV_HSV") == "1"))
          if v]
    return f"{VERSION}:{'+'.join(on)}" if on else ""


def active() -> bool:
    """Any ADV-VIS scoring flag on (geometry, crop, HSV colour)."""
    return geom_on() or crop_on() or os.environ.get("HYDRA_ADV_HSV") == "1"


# ------------------------------------------------------------------------------------------ verification


def _gray(im: Image.Image, max_side: int = 1024) -> tuple[np.ndarray, float]:
    """Grey image for features and the factor unit px -> grey px (same resize rule as `vis._gray`)."""
    g = im.convert("L")
    f = 1.0
    if max(g.size) > max_side:
        f = max_side / max(g.size)
    elif max(g.size) < 256:
        f = 256 / max(g.size)
    if f != 1.0:
        rs = Image.Resampling.BILINEAR if f < 1 else Image.Resampling.BICUBIC
        g = g.resize((max(1, round(g.width * f)), max(1, round(g.height * f))), rs)
    return np.ascontiguousarray(np.asarray(g)), f


def _orb(gray: np.ndarray):
    import cv2

    return cv2.ORB_create(ORB_N).detectAndCompute(gray, None), cv2.NORM_HAMMING


def _rootsift(gray: np.ndarray):
    import cv2

    kp, d = cv2.SIFT_create(ORB_N).detectAndCompute(gray, None)
    if d is not None:
        d = d / np.maximum(d.sum(axis=1, keepdims=True), 1e-7)
        d = np.sqrt(d).astype(np.float32)
    return (kp, d), cv2.NORM_L2


def _match(fa, fb, norm) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    """(src, dst, H) of the MAGSAC++ inliers, or None."""
    import cv2

    (ka, da), (kb, db) = fa, fb
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return None
    pairs = cv2.BFMatcher(norm).knnMatch(da, db, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < RATIO * p[1].distance]
    if len(good) < 8:
        return None
    src = np.float32([ka[m.queryIdx].pt for m in good])
    dst = np.float32([kb[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, RANSAC_PX)
    if H is None or mask is None:
        return None
    keep = mask.ravel().astype(bool)
    return src[keep], dst[keep], H


def _hull_cov(pts: np.ndarray, shape: tuple[int, ...]) -> float:
    import cv2

    if len(pts) < 3:
        return 0.0
    return float(cv2.contourArea(cv2.convexHull(pts.astype(np.float32)))) / float(shape[0] * shape[1])


def _grid_occ(pts: np.ndarray, shape: tuple[int, ...]) -> float:
    if not len(pts):
        return 0.0
    h, w = shape[:2]
    cx = np.clip((pts[:, 0] / w * GRID).astype(int), 0, GRID - 1)
    cy = np.clip((pts[:, 1] / h * GRID).astype(int), 0, GRID - 1)
    return len(set(zip(cx.tolist(), cy.tolist(), strict=True))) / float(GRID * GRID)


def _shape_stats(H: np.ndarray, w: int, h: int) -> tuple[float, float, float]:
    """(anisotropy = s1/s2 of the affine part, perspective = |h20 w| + |h21 h|, scale = sqrt(s1 s2))."""
    Hn = H / H[2, 2] if abs(H[2, 2]) > 1e-12 else H
    s = np.linalg.svd(Hn[:2, :2], compute_uv=False)
    aniso = float(s[0] / max(s[1], 1e-9))
    persp = float(abs(Hn[2, 0]) * w + abs(Hn[2, 1]) * h)
    return aniso, persp, float(np.sqrt(abs(s[0] * s[1])))


def verify(a: Image.Image, b: Image.Image, *, rootsift: bool = True) -> dict:
    """Same-asset verification of two images. Coordinates in the result are pixels of `a` / `b` as given."""
    ga, fa_ = _gray(a)
    gb, fb_ = _gray(b)
    best: tuple | None = None
    method = "orb"
    feats_b, norm = _orb(gb)
    for mirrored in (False, True):
        qa = np.ascontiguousarray(ga[:, ::-1]) if mirrored else ga
        r = _match(_orb(qa)[0], feats_b, norm)
        if r is not None and (best is None or len(r[0]) > len(best[0])):
            best = (*r, mirrored)
    if rootsift and (best is None or len(best[0]) < MIN_INLIERS):
        fbs, normr = _rootsift(gb)
        r = _match(_rootsift(ga)[0], fbs, normr)
        if r is not None and (best is None or len(r[0]) > len(best[0])):
            best = (*r, False)
            method = "rootsift"
    out = {"version": VERSION, "method": method, "inliers": 0, "coverage_a": 0.0, "coverage_b": 0.0,
           "grid_a": 0.0, "grid_b": 0.0, "mirrored": False, "homography": None, "anisotropy": None,
           "perspective": None, "scale": None, "size_a": list(a.size), "size_b": list(b.size), "points": [],
           "same_asset": False, "crop_match": False}
    if best is None:
        return out
    src, dst, H, mirrored = best
    if mirrored:  # back to the original orientation of `a`
        src = src.copy()
        src[:, 0] = ga.shape[1] - 1 - src[:, 0]
        H = H @ np.array([[-1.0, 0, ga.shape[1] - 1], [0, 1, 0], [0, 0, 1]])
    # unit-image pixels: x_b = S_b^-1 H S_a x_a
    Hu = np.diag([1 / fb_, 1 / fb_, 1.0]) @ H @ np.diag([fa_, fa_, 1.0])
    aniso, persp, scale = _shape_stats(Hu, a.width, a.height)
    cov_a, cov_b = _hull_cov(src, ga.shape), _hull_cov(dst, gb.shape)
    occ_a, occ_b = _grid_occ(src, ga.shape), _grid_occ(dst, gb.shape)
    n = len(src)
    step = max(1, n // MAX_POINTS)
    pts = [[round(float(x) / fa_, 1), round(float(y) / fa_, 1), round(float(u) / fb_, 1), round(float(v) / fb_, 1)]
           for (x, y), (u, v) in zip(src[::step][:MAX_POINTS], dst[::step][:MAX_POINTS], strict=True)]
    same = n >= MIN_INLIERS and min(cov_a, cov_b) >= MIN_COVERAGE
    crop = (same and min(cov_a, cov_b) >= CROP_COVERAGE and min(occ_a, occ_b) >= CROP_GRID
            and not mirrored and aniso <= CROP_ANISO and persp <= CROP_PERSP)
    out.update(inliers=n, coverage_a=round(cov_a, 3), coverage_b=round(cov_b, 3), grid_a=round(occ_a, 3),
               grid_b=round(occ_b, 3), mirrored=bool(mirrored),
               homography=[[round(float(x), 6) for x in row] for row in Hu], anisotropy=round(aniso, 3),
               perspective=round(persp, 4), scale=round(scale, 4), points=pts, same_asset=bool(same),
               crop_match=bool(crop))
    return out


# ------------------------------------------------------------------------------------------ unit images + cache

_LOCK = threading.Lock()
_MEMO: dict[str, dict | None] = {}
_SPENT = [0]
SKIPPED = [0]  # verifications refused by the budget (analysis incomplete for those pairs)
_WARNED = [False]


def stats() -> dict:
    """Process-wide geometry counters for run reports: verifications done, refused by the budget, budget."""
    return {"verified": _SPENT[0], "skipped_budget": SKIPPED[0], "budget": BUDGET,
            "complete": SKIPPED[0] == 0}


def unit_ref(gene) -> tuple[str, tuple | None] | None:
    """(artifact_id, bbox) of the visual unit a gene points at, or None for video frames / missing evidence."""
    ev = getattr(gene, "evidence", None)
    if ev is None or not ev.artifact_id or "kind=frame" in (ev.note or ""):
        return None
    return ev.artifact_id, tuple(ev.bbox) if ev.bbox else None


def _key(ra: tuple, rb: tuple) -> str:
    return hashlib.sha1(json.dumps([VERSION, ra[0], ra[1], rb[0], rb[1]], default=list).encode()).hexdigest()


def _settings():
    from hydra.config import get_settings

    return get_settings()


def _state_roots(settings=None) -> list:
    """State dirs whose CAS may hold a unit's artefact, in order: the caller's settings, every state whose DB this
    process opened (`hydra.db.CONNECTED_STATE_DIRS`: the eval harness and the API pass explicit `Settings`, not
    the env), then the env default. The CAS is content-addressed, so any store holding the sha256 has the same bytes."""
    from pathlib import Path

    from hydra import db

    roots: list[Path] = []
    if settings is not None and settings.state_dir is not None:
        roots.append(Path(settings.state_dir))
    roots += sorted(db.CONNECTED_STATE_DIRS)
    s = _settings()
    if s.state_dir is not None:
        roots.append(Path(s.state_dir))
    out: list[Path] = []
    for r in roots:
        if r not in out:
            out.append(r)
    return out


def _locate(artifact_id: str, settings=None):
    """The state dir whose CAS holds `artifact_id`, or None."""
    from hydra.storage import ContentStore

    for root in _state_roots(settings):
        try:
            ContentStore(root / "cas").safe_path(artifact_id)
            return root
        except Exception:  # noqa: BLE001 -- not in this store
            continue
    return None


def load_unit(ref: tuple[str, tuple | None], settings=None, root=None) -> Image.Image | None:
    from hydra.genome.vis import load_evidence_image
    from hydra.storage import ContentStore

    root = root or _locate(ref[0], settings)
    if root is None:
        return None
    try:
        return load_evidence_image(ContentStore(root / "cas").safe_path(ref[0]), ref[1])
    except Exception as exc:  # noqa: BLE001 -- missing or unreadable artifact: unknown, no evidence
        log.debug("vis_geom: cannot load %s: %s", ref[0][:12], exc)
        return None


# Verification outcomes of `verify_status` (CR-ASTRA-SW-01/02). Only OK is a measurement; MISSING ("an image is not
# available") is no observation and is never memoised, so the pair is re-measured once the artefact is imported;
# SKIPPED ("the budget refused a required verification") is not evidence either way and makes the pair incomplete.
OK, MISSING, SKIPPED_BUDGET, FAILED = "ok", "missing", "skipped_budget", "failed"


def verify_status(ra: tuple, rb: tuple, settings=None, *, budget: bool = True) -> tuple[str, dict | None]:
    """(`OK`, result) | (`MISSING`, None) | (`SKIPPED_BUDGET`, None) | (`FAILED`, None) for two unit refs (ordered as
    given). Results are memoised and disk-cached in the work dir of the state that holds unit `a`; transient states
    (missing artefact, budget refusal, verification error) are not cached."""
    k = _key(ra, rb)
    with _LOCK:
        if k in _MEMO:
            return OK, _MEMO[k]
    root = _locate(ra[0], settings)
    if root is None:
        return MISSING, None  # SW-02: not memoised, the artefact may be imported later
    f = root / "work" / "vis_geom" / f"{k}.json"
    try:
        res = json.loads(f.read_text())
    except (OSError, ValueError):
        res = None
    if res is None:
        with _LOCK:
            if budget and _SPENT[0] >= BUDGET:
                SKIPPED[0] += 1
                if not _WARNED[0]:
                    _WARNED[0] = True
                    log.warning("vis_geom: limit weryfikacji geometrycznych (%d) wyczerpany: kolejne pary obrazów"
                                " nie są weryfikowane, analiza przycięć jest NIEPEŁNA (HYDRA_GEOM_BUDGET)", BUDGET)
                return SKIPPED_BUDGET, None
        ia, ib = load_unit(ra, settings, root=root), load_unit(rb, settings)
        if ia is None or ib is None:
            return MISSING, None  # SW-02: not memoised and no budget spent (nothing was measured)
        with _LOCK:
            if budget:
                _SPENT[0] += 1
        t0 = time.perf_counter()
        try:
            res = verify(ia, ib)
        except Exception as exc:  # noqa: BLE001
            log.warning("vis_geom verify failed: %s", exc)
            return FAILED, None
        res["seconds"] = round(time.perf_counter() - t0, 4)  # verification cost (load excluded), for runtime reports
        res["ref_a"], res["ref_b"] = [ra[0], ra[1]], [rb[0], rb[1]]
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp = f.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(res))
            tmp.replace(f)
        except OSError:
            pass
    with _LOCK:
        _MEMO[k] = res
    return OK, res


def cached_verify(ra: tuple, rb: tuple, settings=None, *, budget: bool = True) -> dict | None:
    """`verify_status` result, or None when an image is missing, the per-process budget is spent or the
    verification failed (use `verify_status` to tell these apart)."""
    return verify_status(ra, rb, settings, budget=budget)[1]


# ------------------------------------------------------------------------------------------ scorer hook


def near_miss(ra: list[tuple[str, dict]], rb: list[tuple[str, dict]], cos: np.ndarray, near: np.ndarray,
              skipped: list[tuple[int, int]] | None = None) -> tuple[np.ndarray, list[tuple[int, int, dict]]]:
    """Verify the top-`TOPK` unit pairs with cos >= GEOM_COS_MIN that no hash rule matched.

    Returns (crop matrix: verified crop/resize of the same picture, set only when HYDRA_ADV_CROP=1;
    verified same-asset results [(i, j, result)]). Unit pairs whose verification the budget refused are appended to
    `skipped` (CR-ASTRA-SW-01: a required check that did not run, not negative evidence)."""
    crop = np.zeros(near.shape, dtype=bool)
    found: list[tuple[int, int, dict]] = []
    ii, jj = np.nonzero((cos >= GEOM_COS_MIN) & ~near)
    if not len(ii):
        return crop, found
    order = sorted(zip(ii.tolist(), jj.tolist(), strict=True), key=lambda t: (-float(cos[t]), t))[:TOPK]
    for i, j in order:
        ua, ub = unit_ref(ra[i][1].get("gene")), unit_ref(rb[j][1].get("gene"))
        if ua is None or ub is None:
            continue
        status, res = verify_status(ua, ub)
        if status == SKIPPED_BUDGET and skipped is not None:
            skipped.append((i, j))
        if res is None or not res.get("same_asset"):
            continue
        found.append((i, j, res))
        if crop_on() and res.get("crop_match"):
            crop[i, j] = True
    return crop, found


def explanation(res: dict, cos: float) -> str:
    return (f"Geometria ({str(res.get('method', 'orb')).upper()} + MAGSAC++): {res['inliers']} zgodnych punktów,"
            f" pokrycie {res['coverage_a']:.2f}/{res['coverage_b']:.2f}"
            f"{', odbicie lustrzane' if res.get('mirrored') else ''}, DINOv2 cos={cos:.3f}")


GEOM_GROUP = "VIS:asset:geom_same_asset"
# CR-ASTRA-SW-01: key under which `scorer_hook` reports budget-skipped verifications of the pair to the scorer.
# `synapse.score_pair` pops it before the evidence groups are summed (it carries no bits) and turns it into a 0-bit
# note, `meta.geometry_incomplete` and, when the pair would otherwise be `independent`, `abstain` (analyst queue).
SKIPPED_GROUP = "VIS:geom_skipped"
SKIPPED_KIND = "geom_skipped"


def skipped_note(n: int):
    """0-bit pair note for `n` geometric verifications refused by the budget."""
    from hydra.contracts import Family, GeneMatch, Provenance

    return GeneMatch(family=Family.VIS, kind=SKIPPED_KIND, bits=0.0, provenance=Provenance.INF,
                     explanation_pl=(f"Weryfikacja geometryczna pominięta (limit zasobów, HYDRA_GEOM_BUDGET={BUDGET}):"
                                     f" {n} par obrazów niesprawdzonych, wynik niepełny; brak sprawdzenia nie jest"
                                     " dowodem niezależności (+0.0 bit)"))


def scorer_hook(ra: list[tuple[str, dict]], rb: list[tuple[str, dict]], cos: np.ndarray, near: np.ndarray,
                groups: dict) -> np.ndarray:
    """`synapse._visual_near_duplicate` hook (only called when a flag is on). Adds the ASSET-tier
    `geom_same_asset` reason (HYDRA_ADV_GEOM=1) and returns the crop matrix (HYDRA_ADV_CROP=1, else all False)."""
    from hydra import vis_colour
    from hydra.synapse import _match

    if vis_colour.on():
        vis_colour.scorer_hook(ra, rb, cos, groups)
    if not (geom_on() or crop_on()):
        return np.zeros(near.shape, dtype=bool)
    skipped: list[tuple[int, int]] = []
    crop, found = near_miss(ra, rb, cos, near, skipped)
    if skipped:  # one 0-bit entry per refused unit pair (count = len), all sharing one note
        note = skipped_note(len(skipped))
        groups[SKIPPED_GROUP].extend((0.0, note, "none", "VIS") for _ in skipped)
    if geom_on() and found:
        i, j, res = max(found, key=lambda t: (t[2]["inliers"], -t[0], -t[1]))
        (ua, da), (ub, db) = ra[i], rb[j]
        why = explanation(res, float(cos[i, j]))
        m = _match("VIS", "geom_same_asset", ua, ub, GEOM_ASSET_BITS, sim=float(cos[i, j]), ga=da.get("gene"),
                   gb=db.get("gene"), expl=(f"Ten sam zasób graficzny potwierdzony geometrycznie: {why}"
                                            f" ({len(found)} par obrazów; poziom zasobu, 0 bit kampanii)"
                                            f" (+{GEOM_ASSET_BITS:.1f} bit)"))
        groups[GEOM_GROUP].append((GEOM_ASSET_BITS, m, "asset", "VIS"))
    return crop
