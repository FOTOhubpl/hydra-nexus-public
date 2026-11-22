"""VIS genes: perceptual hashes, image embeddings and image class for every visual unit of an incident.

Per unit (image, screenshot region, sampled video frame; see hydra/genome/vis_media.py):

| kind          | value                                   | use in CORE                                         |
|---------------|-----------------------------------------|-----------------------------------------------------|
| image_class   | creative / phone_screenshot / ...       | context, boilerplate filter (per image artifact)    |
| pdq           | 64 hex (256 bits), original orientation | Hamming <= 31 near-duplicate (resize/JPEG)          |
| pdq_canon     | min over the 8 dihedral PDQ variants    | survives flips/90-degree rotations, Hamming as above |
| phash         | 16 hex (64 bits)                        | Hamming <= 10                                       |
| phash_tiles   | 16 comma-separated 16-hex tile hashes   | count of tiles with Hamming <= 10 (crop/overlay)    |
| pdq_dihedral  | 8 comma-separated PDQ hex, original 1st | min Hamming over the 8 variants (flips/rotations)   |
| phash_multicrop | 22 comma-separated pHash of sub-windows | crop variants: window of one vs whole of the other  |
| dinov2        | vector_ref -> 384-d DINOv2-small CLS    | candidates + bits vs background (see below)         |

All hash genes of one unit share `dependency_group="vis_unit:<uid>"` (one bit budget per image).
Boilerplate units (icons, tracking pixels, payment/app-store/social marks, UI furniture) get
`dependency_group="vis_boilerplate"`, logo-like page images `"vis_brand"`, so rarity downweights them.

Never raises: per-artifact decode errors become `failed` genes; a missing model becomes `unavailable`.
"""

from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from hydra.contracts import EvidencePointer, Family, Gene, GeneStatus, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, Vector, register
from hydra.genome.vis_media import UnitSet, VisualUnit, collect_units, unit_note

log = logging.getLogger(__name__)

EXTRACTOR = "vis@2"  # v2 (2026-11-21): pdq_dihedral + phash_multicrop genes
DINO_KEY = "dinov2-small"
TILE_GRID = 4


# ------------------------------------------------------------------------------------------ hashing


def bits_to_hex(bits: np.ndarray) -> str:
    return np.packbits(np.asarray(bits, dtype=bool).astype(np.uint8)).tobytes().hex()


def hex_hamming(a: str, b: str) -> int:
    """Hamming distance between two equal-length hex strings (helper for CORE and tests)."""
    return int.bit_count(int(a, 16) ^ int(b, 16))


def tiles_match(a: str, b: str, max_dist: int = 10) -> int:
    """Number of tile positions whose tile pHashes are within max_dist bits."""
    ta, tb = a.split(","), b.split(",")
    return sum(1 for x, y in zip(ta, tb, strict=False) if hex_hamming(x, y) <= max_dist)


def pdq_dihedral(im: Image.Image) -> tuple[list[str], int]:
    """The 8 dihedral PDQ hashes (original orientation first, pdqhash order) and the quality 0-100."""
    import pdqhash

    a = np.ascontiguousarray(np.asarray(im.convert("RGB")))
    hashes, quality = pdqhash.compute_dihedral(a)
    q = int(quality if np.isscalar(quality) else np.asarray(quality).ravel()[0])
    return [bits_to_hex(h) for h in np.asarray(hashes)], q


def pdq_hashes(im: Image.Image) -> tuple[str, str, int]:
    """(pdq hex original orientation, canonical = lexicographic min of the 8 dihedral hashes, quality 0-100)."""
    hexes, q = pdq_dihedral(im)
    return hexes[0], min(hexes), q


# Sub-windows for crop variants (CR-ASTRA-6): uniform crops at 90 % and 80 % of each side on a 3x3 grid of positions,
# plus full-width / full-height strips at 85 % (banner or side bar removed). Each window covers >= 64 % of the
# image, far above the 15 % coverage floor for local evidence (SPEC_ERRATA).
MULTICROP_SCALES = (0.9, 0.8)
MULTICROP_STRIP = 0.85


def multicrop_windows(size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    w0, h0 = size
    boxes = []
    for s in MULTICROP_SCALES:
        w, h = w0 * s, h0 * s
        for yi in range(3):
            for xi in range(3):
                x, y = (w0 - w) * xi / 2, (h0 - h) * yi / 2
                boxes.append((round(x), round(y), round(x + w), round(y + h)))
    hs, ws = h0 * MULTICROP_STRIP, w0 * MULTICROP_STRIP
    boxes += [(0, 0, w0, round(hs)), (0, round(h0 - hs), w0, h0), (0, 0, round(ws), h0), (round(w0 - ws), 0, w0, h0)]
    return boxes


def phash_multicrop_hex(im: Image.Image) -> str:
    """pHash of every `multicrop_windows` window, comma-separated (22 x 16 hex)."""
    import imagehash

    g = im.convert("L")
    return ",".join(str(imagehash.phash(g.crop(b))) for b in multicrop_windows(g.size))


def unit_hash_values(im: Image.Image) -> dict[str, tuple[str, float, str]]:
    """kind -> (value, quality, note extra) of every hash gene of one visual unit (used by `hash_genes`; the
    adversarial tests use it to build units exactly as the extractor does)."""
    out: dict[str, tuple[str, float, str]] = {}
    hexes, q = pdq_dihedral(im)
    if q >= PDQ_QUALITY_MIN:
        out["pdq"] = (hexes[0], q / 100.0, f"pdq_quality={q}")
        out["pdq_canon"] = (min(hexes), q / 100.0, "dihedral-min")
        out["pdq_dihedral"] = (",".join(hexes), q / 100.0, "8 dihedral variants, original first")
    if float(np.asarray(im.convert("L").resize((64, 64))).std()) > FLAT_STD_MAX:
        out["phash"] = (phash_hex(im), 1.0, "")
        out["phash_tiles"] = (phash_tiles_hex(im), 1.0, f"grid={TILE_GRID}x{TILE_GRID}")
        if min(im.size) >= 64:
            out["phash_multicrop"] = (phash_multicrop_hex(im), 1.0, "windows=22")
    return out


def phash_hex(im: Image.Image) -> str:
    import imagehash

    return str(imagehash.phash(im))


def phash_tiles_hex(im: Image.Image, grid: int = TILE_GRID) -> str:
    import imagehash

    g = im.convert("L").resize((64 * grid, 64 * grid), Image.Resampling.BILINEAR)
    out = []
    for y in range(grid):
        for x in range(grid):
            out.append(str(imagehash.phash(g.crop((x * 64, y * 64, x * 64 + 64, y * 64 + 64)), hash_size=8)))
    return ",".join(out)


# ------------------------------------------------------------------------------------------ embeddings


@lru_cache(maxsize=4)
def model_tag(key: str) -> str:
    """'dinov2-small@ed25f3a' from models.lock.json (revision), or the bare key."""
    lock = Path(__file__).resolve().parents[2] / "models.lock.json"
    try:
        rev = json.loads(lock.read_text())["entries"][key]["revision"]
        return f"{key}@{rev[:7]}"
    except Exception:  # noqa: BLE001
        return key


def embed_view(im: Image.Image, short_side: int = 256, max_aspect: float = 1.6) -> Image.Image:
    """Pre-downscaled view for the DINOv2 processor (SPEC_ERRATA #14: cheap preprocessing in the caller).

    The processor centre-crops a square; for tall screenshots / wide banners (aspect > max_aspect) that
    would drop most of the content, so they are letterboxed onto a square of their mean colour instead.
    """
    im = im.convert("RGB")
    w, h = im.size
    if max(w, h) / max(1, min(w, h)) > max_aspect:
        side = max(w, h)
        mean = tuple(int(c) for c in np.asarray(im.resize((16, 16))).reshape(-1, 3).mean(axis=0))
        sq = Image.new("RGB", (side, side), mean)
        sq.paste(im, ((side - w) // 2, (side - h) // 2))
        im = sq
        w, h = im.size
    f = short_side / min(w, h)
    if f < 1:
        im = im.resize((max(1, round(w * f)), max(1, round(h * f))), Image.Resampling.BILINEAR)
    return im


def get_embedder(ctx: ExtractContext, key: str = DINO_KEY):
    """Shared, lazily loaded DINOv2 embedder (ctx.models). Raises ModelUnavailable."""
    if key not in ctx.models:
        from hydra.ml.models import load_image_embedder, model_dir

        model_dir(key, ctx.settings)  # ModelUnavailable when this context has no weights
        ctx.models[key] = load_image_embedder(key)
    return ctx.models[key]


def background_stats(vectors: np.ndarray, k: int = 10) -> np.ndarray:
    """Per-vector background level: mean cosine of its k nearest *other* vectors in the corpus.

    Background normalisation (PLAN_MAX §1.8, lab: DINOv2 µAP 0.356 -> 0.477): CORE scores a pair as
    `cos(a, b) - (bg[a] + bg[b]) / 2`, which removes the "generic screenshot / white page" similarity floor.
    """
    from hydra.ml.models import cosine_topk

    if len(vectors) < 2:
        return np.zeros(len(vectors), np.float32)
    _, vals = cosine_topk(vectors, vectors, k=min(k, len(vectors) - 1), exclude_self=True)
    return vals.mean(axis=1).astype(np.float32)


def normalised_similarity(sim: float, bg_a: float, bg_b: float) -> float:
    return float(sim - (bg_a + bg_b) / 2.0)


# ------------------------------------------------------------------------------------------ genes


def dependency_group(u: VisualUnit) -> str:
    if u.boilerplate:
        return "vis_boilerplate"
    if u.image_class == "logo":
        return "vis_brand"
    if u.meta.get("app_ui"):
        return "vis_app_ui"
    return f"vis_unit:{u.uid}"


def _ev(u: VisualUnit, extra: str = "") -> EvidencePointer:
    return EvidencePointer(artifact_id=u.artifact_id, bbox=u.bbox, timecode_s=u.timecode_s,
                           note=unit_note(u, extra))


def _gene(inc_id: str, kind: str, u: VisualUnit, value: str | None = None, *, quality: float = 1.0,
          vector_ref: str | None = None, extra: str = "", provenance: Provenance = Provenance.OBS) -> Gene:
    return Gene(incident_id=inc_id, family=Family.VIS, kind=kind, value=value, vector_ref=vector_ref,
                quality=max(0.0, min(1.0, quality)), provenance=provenance, evidence=_ev(u, extra),
                extractor=EXTRACTOR, dependency_group=dependency_group(u))


PDQ_QUALITY_MIN = 50   # flat/blank images below this collide on every hash (VISUAL_GENES.md §1)
FLAT_STD_MAX = 6.0     # grey-level std below this = blank bar / spacer: no pHash genes


def hash_genes(inc_id: str, u: VisualUnit) -> list[Gene]:
    return [_gene(inc_id, kind, u, value, quality=q, extra=extra)
            for kind, (value, q, extra) in unit_hash_values(u.image).items()]


def hashable(u: VisualUnit) -> bool:
    return u.image_class != "pixel" and min(u.image.size) >= 32


def extract_units(inc_id: str, us: UnitSet, ctx: ExtractContext) -> ExtractResult:
    res = ExtractResult()
    t0 = time.perf_counter()
    for a, err in us.errors:
        res.genes.append(Gene(incident_id=inc_id, family=Family.VIS, kind="pdq", status=GeneStatus.FAILED,
                              extractor=EXTRACTOR, evidence=EvidencePointer(artifact_id=a.meta.artifact_id,
                                                                             note=err)))
    to_embed: list[VisualUnit] = []
    for u in us.units:
        if u.idx == 0 and u.kind == "image":
            res.genes.append(_gene(inc_id, "image_class", u, u.image_class,
                                   extra=f"size={u.source_size[0]}x{u.source_size[1]}"))
        if not hashable(u):
            continue
        try:
            res.genes.extend(hash_genes(inc_id, u))
        except Exception as exc:  # noqa: BLE001
            res.genes.append(Gene(incident_id=inc_id, family=Family.VIS, kind="pdq", status=GeneStatus.FAILED,
                                  extractor=EXTRACTOR, evidence=_ev(u, f"{type(exc).__name__}: {exc}"[:120])))
            continue
        if not u.boilerplate:
            to_embed.append(u)
    t_hash = time.perf_counter() - t0

    t1 = time.perf_counter()
    if to_embed:
        try:
            emb = get_embedder(ctx)
            vecs = emb.embed([embed_view(u.image) for u in to_embed], batch_size=32)
            tag = model_tag(DINO_KEY)
            for u, v in zip(to_embed, vecs, strict=True):
                key = f"{inc_id}|{u.artifact_id}|dinov2|{u.idx}"
                res.vectors.append(Vector(key=key, model=tag, data=np.asarray(v, dtype=np.float32)))
                res.genes.append(_gene(inc_id, "dinov2", u, None, vector_ref=key, provenance=Provenance.INF))
        except Exception as exc:  # noqa: BLE001 — ModelUnavailable or runtime error: unknown, not a penalty
            log.warning("dinov2 unavailable: %s", exc)
            res.genes.append(Gene(incident_id=inc_id, family=Family.VIS, kind="dinov2",
                                  status=GeneStatus.UNAVAILABLE, extractor=EXTRACTOR,
                                  evidence=EvidencePointer(note=f"{type(exc).__name__}: {exc}"[:300])))
    ctx.options.setdefault("_vis_timing", {})[inc_id] = {
        "units": len(us.units), "hash_s": round(t_hash, 3), "embed_s": round(time.perf_counter() - t1, 3),
        "embedded": len(to_embed)}
    return res


@register("vis", family=Family.VIS, version=2)
def vis(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    inc_id = inc.incident.id
    us = collect_units(inc, ctx)
    if not us.units and not us.errors:
        return ExtractResult(genes=[Gene(incident_id=inc_id, family=Family.VIS, kind="pdq",
                                         status=GeneStatus.NOT_APPLICABLE, extractor=EXTRACTOR,
                                         evidence=EvidencePointer(note="no image or video artifacts"))])
    res = extract_units(inc_id, us, ctx)
    from hydra.genome import vis_siglip

    res2 = vis_siglip.maybe_siglip(inc_id, us, ctx)
    res.genes.extend(res2.genes)
    res.vectors.extend(res2.vectors)
    return res


# ------------------------------------------------------------------------------------------ pair verification


SAME_ASSET_INLIERS = 120     # PLAN_MAX §1.7 gate
SAME_ASSET_COVERAGE = 0.15   # SPEC_ERRATA: inliers must cover >= 15% of the smaller image


def _orb_features(gray: np.ndarray, n: int = 2000):
    import cv2

    return cv2.ORB_create(n).detectAndCompute(gray, None)


def _inliers(fa, fb) -> tuple[int, np.ndarray | None]:
    import cv2

    (ka, da), (kb, db) = fa, fb
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return 0, None
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < 8:
        return 0, None
    src = np.float32([ka[m.queryIdx].pt for m in good])
    dst = np.float32([kb[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, 5.0)
    if H is None or mask is None:
        return 0, None
    keep = mask.ravel().astype(bool)
    return int(keep.sum()), src[keep]


def _gray(im: Image.Image, max_side: int = 1024) -> np.ndarray:
    g = im.convert("L")
    if max(g.size) > max_side:
        f = max_side / max(g.size)
        g = g.resize((max(1, round(g.width * f)), max(1, round(g.height * f))), Image.Resampling.BILINEAR)
    elif max(g.size) < 256:
        f = 256 / max(g.size)
        g = g.resize((max(1, round(g.width * f)), max(1, round(g.height * f))), Image.Resampling.BICUBIC)
    return np.asarray(g)


def same_asset(a: Image.Image, b: Image.Image) -> dict:
    """ORB (2000 kp) + MAGSAC homography with a mirrored retry: "same visual asset" evidence for a pair.

    Returns {inliers, coverage, mirrored, same_asset}. Coverage = area of the convex hull of the inliers in
    `a` / area of `a`. same_asset = inliers >= 120 and coverage >= 0.15. For CORE / the pair-evidence view:
    run it on candidate pairs only (~14 ms per pair on the M4, analyst measurement 837 pairs in 11.8 s).
    Apply it to regions, not to whole phone screens (app UI matches app UI).
    """
    import cv2

    ga, gb = _gray(a), _gray(b)
    fa = _orb_features(ga)
    fb = _orb_features(gb)
    n, pts = _inliers(fa, fb)
    nm, ptsm = _inliers(_orb_features(np.ascontiguousarray(ga[:, ::-1])), fb)
    mirrored = nm > n
    if mirrored:
        n, pts = nm, ptsm
    cov = 0.0
    if pts is not None and len(pts) >= 3:
        cov = float(cv2.contourArea(cv2.convexHull(pts.astype(np.float32)))) / float(ga.shape[0] * ga.shape[1])
    return {"inliers": int(n), "coverage": round(cov, 3), "mirrored": bool(mirrored),
            "same_asset": bool(n >= SAME_ASSET_INLIERS and cov >= SAME_ASSET_COVERAGE)}


def load_evidence_image(path: Path, bbox: tuple[float, float, float, float] | None = None) -> Image.Image:
    """Re-open the image a VIS gene points at (artifact path + evidence bbox in source px). For frames,
    open the cached frame JPEG instead (work_dir/frames/<artifact>/)."""
    from hydra.genome.vis_media import load_unit_image

    im, orig, scale = load_unit_image(path, max_side=4096)
    if bbox:
        x, y, w, h = bbox
        im = im.crop((round(x * scale), round(y * scale), round((x + w) * scale), round((y + h) * scale)))
    return im
