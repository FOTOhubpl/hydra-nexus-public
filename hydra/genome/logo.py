"""LOGO genes: media mastheads, bank and product marks.

Sources (all emit `kind="logo"`, value = canonical brand, evidence bbox when localised):
1. ORB (2000 kp) + ratio test + MAGSAC homography against the small template set
   $HYDRA_DATA_DIR/nask/reference/logos/ (*.png + logos.json: brand, kind). Gate: >= 15 inliers,
   inlier ratio >= 0.25, a convex projected quad with a plausible scale and aspect. Precise but sparse:
   flat marks have few keypoints (VISUAL_GENES.md §5), so it is supporting evidence.
2. Brand lexicon (hydra/genome/assets/brands.json) on landing-asset file names and their alt/title text
   (OBS), including SVG logos that are not rasterised (e.g. `logo-gazeta.svg`).
3. `logo_strip` (template family): a page with >= 3 distinct broadcaster logos ("as seen on TV" strip).

A brand alone is not a campaign link (TVN24 appears in gambling and investment scams alike), so every
logo gene is in `dependency_group="vis_brand"`; platform marks (Google, Facebook, App Store, BLIK, Visa)
go to `"vis_boilerplate"`. Brand names in OCR text are GENES-TXT's job.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from hydra.contracts import EvidencePointer, Family, Gene, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.ident import _asset_key, alt_text_map, reference_dir
from hydra.genome.vis_media import VisualUnit, collect_units, unit_note

log = logging.getLogger(__name__)
EXTRACTOR = "logo@1"

MIN_INLIERS = 15
MIN_INLIER_RATIO = 0.25
TEMPLATE_SIDE = 400
TARGET_MAX_SIDE = 1024
ORB_TARGET_KP = 3000
BOILERPLATE_KINDS = {"platform"}
MAX_FRAMES = 40  # logo search on video frames: scene keyframes first, then evenly spaced


@dataclass
class Template:
    brand: str
    kind: str
    file: str
    kp: list
    desc: np.ndarray | None
    shape: tuple[int, int]


def _orb(n: int):
    import cv2

    return cv2.ORB_create(n, fastThreshold=10)


def load_templates(ctx: ExtractContext) -> list[Template]:
    if "logo_templates" in ctx.models:
        return ctx.models["logo_templates"]
    import cv2

    out: list[Template] = []
    rd = reference_dir(ctx)
    d = rd / "logos" if rd else None
    if d is not None and d.is_dir():
        meta = {}
        if (d / "logos.json").exists():
            try:
                meta = json.loads((d / "logos.json").read_text(encoding="utf-8")).get("templates", {})
            except Exception:  # noqa: BLE001
                meta = {}
        orb = _orb(1500)
        for p in sorted(d.glob("*.png")):
            g = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if g is None or min(g.shape) < 16:
                continue
            s = TEMPLATE_SIDE / max(g.shape)
            g = cv2.resize(g, None, fx=s, fy=s, interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
            kp, desc = orb.detectAndCompute(g, None)
            if desc is None or len(kp) < 10:
                continue
            m = meta.get(p.name, {})
            out.append(Template(m.get("brand", p.stem), m.get("kind", "media"), p.name, kp, desc, g.shape[:2]))
    ctx.models["logo_templates"] = out
    return out


def match_template(t: Template, kp, desc, target_shape: tuple[int, int]) -> tuple[int, np.ndarray | None]:
    """(inliers, projected quad in target px) or (0, None)."""
    import cv2

    if desc is None or t.desc is None or len(kp) < 10:
        return 0, None
    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    pairs = bf.knnMatch(t.desc, desc, k=2)
    good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < 0.75 * p[1].distance]
    if len(good) < MIN_INLIERS:
        return 0, None
    src = np.float32([t.kp[m.queryIdx].pt for m in good])
    dst = np.float32([kp[m.trainIdx].pt for m in good])
    H, mask = cv2.findHomography(src, dst, cv2.USAC_MAGSAC, 5.0)
    if H is None or mask is None:
        return 0, None
    n = int(mask.sum())
    if n < MIN_INLIERS or n / len(good) < MIN_INLIER_RATIO:
        return 0, None
    h, w = t.shape
    quad = cv2.perspectiveTransform(np.float32([[0, 0], [w, 0], [w, h], [0, h]]).reshape(-1, 1, 2), H).reshape(-1, 2)
    if not cv2.isContourConvex(quad.astype(np.float32)):
        return 0, None
    area = float(cv2.contourArea(quad))
    th, tw = target_shape
    if area < 300 or area > 1.5 * th * tw:  # a logo file may be the whole target
        return 0, None
    x0, y0 = quad.min(axis=0)
    x1, y1 = quad.max(axis=0)
    if x1 < 0 or y1 < 0 or x0 > tw or y0 > th:
        return 0, None
    qa = (x1 - x0) / max(1e-6, y1 - y0)
    ta = w / h
    if not (0.5 <= qa / ta <= 2.0):
        return 0, None
    return n, quad


def _units_for_logo_search(units: list[VisualUnit]) -> list[VisualUnit]:
    imgs = [u for u in units if u.kind in ("image", "region") and u.image_class not in ("pixel", "icon")
            and u.timecode_s is None]
    frames = [u for u in units if u.kind == "frame"]
    if len(frames) > MAX_FRAMES:
        scenes = [u for u in frames if u.meta.get("scene")]
        rest = [u for u in frames if not u.meta.get("scene")]
        step = max(1, len(rest) // max(1, MAX_FRAMES - len(scenes)))
        frames = (scenes + rest[::step])[:MAX_FRAMES]
    return imgs + frames


@lru_cache(maxsize=1)
def brand_lexicon() -> list[tuple[str, str, re.Pattern]]:
    d = json.loads((Path(__file__).parent / "assets" / "brands.json").read_text(encoding="utf-8"))
    return [(b, v["kind"], re.compile(r"(?<![a-z0-9])(?:" + "|".join(v["patterns"]) + r")", re.I))
            for b, v in d["brands"].items()]


def brands_in_text(text: str) -> list[tuple[str, str]]:
    low = (text or "").lower().replace("_", " ").replace("-", " ")
    return [(b, k) for b, k, rx in brand_lexicon() if rx.search(low)]


def _group(kind: str) -> str:
    return "vis_boilerplate" if kind in BOILERPLATE_KINDS else "vis_brand"


@register("logo", family=Family.VIS, version=1)
def logo(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    import cv2

    inc_id = inc.incident.id
    res = ExtractResult()
    seen: set[tuple[str, str, str]] = set()  # (brand, artifact, source) dedup

    def add(brand: str, kind: str, art_id: str, source: str, *, bbox=None, tc=None, quality=0.7, note=""):
        k = (brand, art_id, source)
        if k in seen:
            return
        seen.add(k)
        res.genes.append(Gene(incident_id=inc_id, family=Family.VIS, kind="logo", value=brand, quality=quality,
                              provenance=Provenance.OBS if source != "orb" else Provenance.INF,
                              extractor=EXTRACTOR, dependency_group=_group(kind),
                              evidence=EvidencePointer(artifact_id=art_id, bbox=bbox, timecode_s=tc,
                                                       note=f"source={source}; brand_kind={kind}; {note}"[:300])))

    # 1. ORB template matching
    try:
        templates = load_templates(ctx)
    except Exception as exc:  # noqa: BLE001
        log.warning("logo templates unavailable: %s", exc)
        templates = []
    if templates:
        orb = _orb(ORB_TARGET_KP)
        for u in _units_for_logo_search(collect_units(inc, ctx).units):
            try:
                g = cv2.cvtColor(np.asarray(u.image), cv2.COLOR_RGB2GRAY)
                f = 1.0
                if max(g.shape) < 400:
                    f = 400 / max(g.shape)
                    g = cv2.resize(g, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)
                kp, desc = orb.detectAndCompute(g, None)
                best: dict[str, tuple[int, np.ndarray, Template]] = {}
                for t in templates:
                    n, quad = match_template(t, kp, desc, g.shape[:2])
                    if n and (t.brand not in best or n > best[t.brand][0]):
                        best[t.brand] = (n, quad, t)
                for brand, (n, quad, t) in best.items():
                    x0, y0 = quad.min(axis=0) / f
                    x1, y1 = quad.max(axis=0) / f
                    x0, y0 = max(0.0, float(x0)), max(0.0, float(y0))
                    bbox = u.to_source_bbox(x0, y0, max(1.0, float(x1) - x0), max(1.0, float(y1) - y0))
                    add(brand, t.kind, u.artifact_id, "orb", bbox=bbox, tc=u.timecode_s,
                        quality=min(1.0, n / 120.0), note=unit_note(u, f"inliers={n}; template={t.file}"))
            except Exception as exc:  # noqa: BLE001
                log.debug("logo orb failed on %s: %s", u.uid, exc)

    # 2. brand lexicon on landing-asset file names and alt/title text
    alts = alt_text_map(inc)
    for a in inc.by_role("landing_asset"):
        name = Path(a.meta.path_in_incident).name
        mime = (a.meta.mime or "").lower()
        if not (mime.startswith("image/") or name.lower().endswith((".svg", ".ico", ".png", ".webp", ".jpg"))):
            continue
        is_logo = bool(re.search(r"(?i)logo|favicon|masthead", name)) or mime == "image/svg+xml"
        for b, k in brands_in_text(Path(name).stem):
            add(b, k, a.meta.artifact_id, "filename", quality=0.8 if is_logo else 0.5, note=f"file={name[:120]}")
        for text in alts.get(_asset_key(a.meta.path_in_incident), []):
            if is_logo or re.search(r"(?i)\blogo\b", text):
                for b, k in brands_in_text(text):
                    add(b, k, a.meta.artifact_id, "alt", quality=0.8, note=f"alt={text[:100]}; file={name[:80]}")

    # 3. broadcaster strip ("as seen on TV"): >= 3 distinct media brands among the landing assets
    media = {g.value for g in res.genes if "brand_kind=media" in (g.evidence.note or "")
             and g.evidence.artifact_id in {a.meta.artifact_id for a in inc.by_role("landing_asset")}}
    if len(media) >= 3:
        res.genes.append(Gene(incident_id=inc_id, family=Family.VIS, kind="logo_strip", value="broadcaster_strip",
                              quality=min(1.0, len(media) / 5), provenance=Provenance.INF, extractor=EXTRACTOR,
                              dependency_group="vis_template",
                              evidence=EvidencePointer(note=f"brands={','.join(sorted(media))}"[:300])))
    return res
