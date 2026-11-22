"""Visual units shared by the VIS, ID and LOGO extractors.

A *visual unit* is one image the visual genes are computed on:
- `image`: an image artifact (ad creative, phone/desktop screenshot, page asset, page screenshot);
- `region`: a photo-like band cut out of a screenshot (the ad inside a feed, without UI text and chrome);
- `frame`: a sampled video frame (1 fps + scene-change keyframes, near-identical frames dropped).

Units are built once per incident and cached in `ctx.options` so the three extractors share decoding and
frame extraction. Videos are never loaded whole: one ffmpeg pass streams the file and writes downscaled
JPEGs to `ctx.work_dir/frames/<artifact>/`.

Everything here is a heuristic and is documented in docs/genes/VIS.md.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from hydra.genome.base import ExtractContext, ResolvedArtifact, ResolvedIncident

log = logging.getLogger(__name__)

Image.MAX_IMAGE_PIXELS = 120_000_000

IMAGE_ROLES = ("ad_creative", "feed_screenshot", "landing_asset", "landing_screenshot", "other")
VIDEO_ROLES = ("screen_recording", "ad_video", "landing_asset", "other")

UNIT_MAX_SIDE = 1024          # pixels of the cached unit image (hashes, faces, logos)
MAX_IMAGE_UNITS = 400         # per incident, images + regions
MAX_FRAMES_PER_VIDEO = 600    # 1 fps sampling bound (10 min)
MAX_KEPT_FRAMES = 120         # frames kept per video after near-duplicate removal
FRAME_DEDUP_PDQ = 40          # a 1 fps frame is kept only if PDQ distance to the last kept frame > this
SCENE_THRESHOLD = 0.30        # ffmpeg scene score for keyframes
MAX_FRAME_GAP_S = 10.0        # keep at least one frame every 10 s even when nothing changes (talking heads)

_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".ts", ".avi", ".3gp"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif"}

# Page-asset names that are UI furniture, payment marks or generic icons: tagged boilerplate so CORE's
# rarity weighting does not let them link campaigns.
_BOILERPLATE_NAME = re.compile(
    r"(?i)(^|[^a-z0-9])(icon|ico|arrow|close|mute|unmute|play|pause|like|likes|share|search|star|stars|rating|"
    r"flag|flags|sprite|spinner|loader|loading|check|tick|cross|menu|burger|hamburger|nav|button|btn|badge|"
    r"visa|mastercard|maestro|blik|paypal|przelewy24|p24|payu|applepay|apple-pay|gpay|google-pay|"
    r"app-?store|google-?play|appstore|googleplay|googlelogo|facebook|instagram|twitter|youtube|tiktok|"
    r"whatsapp|telegram|messenger|fb|ig|pixel|spacer|blank|transparent|placeholder|lock|ssl|secure|"
    r"trustpilot|delivered)([^a-z0-9]|$)"
)
_LOGO_NAME = re.compile(r"(?i)(logo|favicon|masthead|brand)")


@dataclass
class VisualUnit:
    uid: str                         # f"{artifact_id[:12]}:{idx}" — unique inside the incident
    artifact: ResolvedArtifact
    idx: int                         # 0 = the artifact itself; regions / frames count up
    kind: str                        # image | region | frame
    image: Image.Image               # RGB, longest side <= UNIT_MAX_SIDE
    scale: float                     # unit px / source px
    offset: tuple[float, float]      # top-left of the unit in source px (regions, chrome crops)
    source_size: tuple[int, int]     # source image / video frame size (px)
    image_class: str
    name: str
    boilerplate: bool = False
    timecode_s: float | None = None
    parent_uid: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def artifact_id(self) -> str:
        return self.artifact.meta.artifact_id

    def to_source_bbox(self, x: float, y: float, w: float, h: float) -> tuple[float, float, float, float]:
        """Unit-pixel box -> source-pixel box (x, y, w, h)."""
        s = self.scale or 1.0
        return (round(self.offset[0] + x / s, 1), round(self.offset[1] + y / s, 1), round(w / s, 1),
                round(h / s, 1))

    @property
    def bbox(self) -> tuple[float, float, float, float] | None:
        """Box of the unit in its source (only for crops)."""
        if self.kind == "region" or self.meta.get("chrome_cropped"):
            return self.to_source_bbox(0, 0, self.image.width, self.image.height)
        return None


# ------------------------------------------------------------------------------------------ helpers


def is_video(a: ResolvedArtifact) -> bool:
    mime = (a.meta.mime or "").lower()
    if mime.startswith("video/") or mime in ("application/vnd.apple.mpegurl",):
        return not mime.endswith("mpegurl")
    return Path(a.meta.path_in_incident).suffix.lower() in _VIDEO_EXT and not mime.startswith(("text/", "image/"))


def is_image(a: ResolvedArtifact) -> bool:
    mime = (a.meta.mime or "").lower()
    if mime == "image/svg+xml" or mime.startswith("image/svg"):
        return False
    if mime.startswith("image/"):
        return True
    if mime in ("application/octet-stream", "") and Path(a.meta.path_in_incident).suffix.lower() in _IMAGE_EXT:
        return True
    return False


def _rgb(im: Image.Image) -> Image.Image:
    if im.mode == "RGB":
        return im
    if im.mode in ("I", "I;16", "I;16B", "I;16L", "F"):  # 16-bit / float: rescale, PIL would clip to white
        a = np.asarray(im, dtype=np.float64)
        hi = 65535.0 if a.max() > 255 else 255.0
        return Image.fromarray(np.clip(a / hi * 255.0, 0, 255).astype(np.uint8)).convert("RGB")
    if im.mode in ("RGBA", "LA", "P", "PA"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def load_unit_image(path: Path, max_side: int = UNIT_MAX_SIDE) -> tuple[Image.Image, tuple[int, int], float]:
    """RGB, EXIF-transposed, longest side <= max_side. Returns (image, original size, scale)."""
    with Image.open(path) as im:
        full = im.size  # header size, before draft()
        orientation = im.getexif().get(0x0112, 1) if hasattr(im, "getexif") else 1
        if im.format == "JPEG":
            im.draft("RGB", (max_side, max_side))  # fast DCT downscale for large JPEGs
        im.load()
        im = ImageOps.exif_transpose(im) or im
        im = _rgb(im)
        orig = (full[1], full[0]) if orientation in (5, 6, 7, 8) else full
        if max(im.size) > max_side:
            f = max_side / max(im.size)
            im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.Resampling.LANCZOS)
        scale = im.width / max(1, orig[0])
        return im.copy(), orig, scale


def classify_image(w: int, h: int, role: str, name: str) -> str:
    """creative | phone_screenshot | desktop_screenshot | page_screenshot | page_image | avatar | logo | icon |
    pixel (heuristics on size, aspect, artifact role and file name)."""
    lo, hi = min(w, h), max(w, h)
    aspect = h / max(1, w)
    if w * h <= 64 or lo < 8:
        return "pixel"
    if hi <= 64:
        return "icon"
    if _LOGO_NAME.search(name) and hi <= 1200:
        return "logo"
    if role in ("feed_screenshot", "ad_creative") and aspect >= 1.85 and w >= 600:
        return "phone_screenshot"
    if role == "feed_screenshot":
        return "phone_screenshot" if aspect >= 1.5 else "desktop_screenshot"
    if role == "ad_creative":
        return "creative"
    if role == "landing_screenshot":
        return "page_screenshot"
    if role == "landing_asset":
        if hi <= 200 and 0.8 <= aspect <= 1.25:
            return "avatar"
        if hi <= 400 and (aspect <= 0.4 or aspect >= 2.5) and lo <= 160:
            return "logo"
        return "page_image"
    if aspect >= 1.85 and w >= 600:
        return "phone_screenshot"
    return "creative"


def is_boilerplate(image_class: str, name: str) -> bool:
    if image_class in ("pixel", "icon"):
        return True
    return bool(_BOILERPLATE_NAME.search(Path(name).stem.replace("_", "-")))


# Fractions of the screen height taken by the iOS/Android status bar and the home indicator / nav bar.
PHONE_TOP_CHROME = 0.06
PHONE_BOTTOM_CHROME = 0.07


def crop_phone_chrome(im: Image.Image) -> tuple[Image.Image, int]:
    """Remove the status bar and home indicator of a phone screenshot. Returns (crop, top offset in px)."""
    top = int(round(im.height * PHONE_TOP_CHROME))
    bottom = im.height - int(round(im.height * PHONE_BOTTOM_CHROME))
    if bottom - top < 32:
        return im, 0
    return im.crop((0, top, im.width, bottom)), top


def photo_regions(im: Image.Image, max_regions: int = 3, min_h_frac: float = 0.12,
                  min_w_frac: float = 0.5) -> list[tuple[int, int, int, int]]:
    """Photo-like rectangles inside a UI screenshot (x0, y0, x1, y1 in `im` pixels).

    UI background is the dominant colour; feed text rows keep most pixels at that colour, photos almost
    none. Rows with >= 70% non-background pixels (median-smoothed) form bands; bands shorter than
    `min_h_frac` of the height are dropped; each band is trimmed to its non-background columns.
    """
    w0 = 256
    if im.width < 32 or im.height < 32:
        return []
    f = w0 / im.width
    small = im.convert("RGB").resize((w0, max(8, round(im.height * f))), Image.Resampling.BILINEAR)
    a = np.asarray(small, dtype=np.int16)
    q = (a // 8).reshape(-1, 3)
    codes = q[:, 0] * 1024 + q[:, 1] * 32 + q[:, 2]
    bg_code = np.bincount(codes).argmax()
    bg = np.array([bg_code // 1024, (bg_code // 32) % 32, bg_code % 32]) * 8 + 4
    nonbg = np.abs(a - bg).max(axis=2) > 18
    H, W = nonbg.shape
    row = nonbg.mean(axis=1)
    k = 5
    pad = np.pad(row, (k // 2, k // 2), mode="edge")
    row_s = np.median(np.lib.stride_tricks.sliding_window_view(pad, k), axis=1)
    on = row_s >= 0.70
    bands: list[tuple[int, int]] = []
    y = 0
    gap = max(1, int(0.02 * H))
    while y < H:
        if not on[y]:
            y += 1
            continue
        y0 = y
        while y < H and on[y]:
            y += 1
        if bands and y0 - bands[-1][1] <= gap:
            bands[-1] = (bands[-1][0], y)
        else:
            bands.append((y0, y))
    out = []
    for y0, y1 in bands:
        if (y1 - y0) < min_h_frac * H:
            continue
        col = nonbg[y0:y1].mean(axis=0)
        xs = np.where(col >= 0.5)[0]
        if xs.size == 0:
            continue
        x0, x1 = int(xs[0]), int(xs[-1]) + 1
        if (x1 - x0) < min_w_frac * W:
            continue
        if (y1 - y0) * (x1 - x0) >= 0.9 * H * W:
            continue  # the whole image is the photo; the image unit already covers it
        out.append((round(x0 / f), round(y0 / f), min(im.width, round(x1 / f)), min(im.height, round(y1 / f))))
    out.sort(key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    return out[:max_regions]


# ------------------------------------------------------------------------------------------ video


def video_frames(path: Path, out_dir: Path, every_s: float = 1.0, max_side: int = UNIT_MAX_SIDE,
                 scene_threshold: float = SCENE_THRESHOLD,
                 max_frames: int = MAX_FRAMES_PER_VIDEO) -> list[tuple[Path, float, bool]]:
    """One streaming ffmpeg pass: 1 fps frames + scene-change keyframes. Returns [(jpg, t_s, is_scene)].

    Results are cached on disk (`out_dir/done`), so the three visual extractors and reruns share them.
    """
    done = out_dir / "done"
    if not done.exists():
        exe = shutil.which("ffmpeg")
        if not exe:
            raise RuntimeError("ffmpeg not found on PATH")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        scale = (f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'")
        scenes_txt = out_dir / "scenes.txt"
        meta_path = str(scenes_txt).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        graph = (f"[0:v]{scale},split=2[a][b];[a]fps=1/{every_s}[o1];"
                 f"[b]select='gt(scene,{scene_threshold})',metadata=mode=print:file='{meta_path}'[o2]")
        # local files only: a hostile HLS/concat input must never make ffmpeg fetch http URLs
        cmd = [exe, "-v", "error", "-nostdin", "-y", "-protocol_whitelist", "file,pipe", "-i", str(path),
               "-filter_complex", graph,
               "-map", "[o1]", "-frames:v", str(max_frames), "-q:v", "3", str(out_dir / "f_%06d.jpg"),
               "-map", "[o2]", "-fps_mode", "vfr", "-frames:v", "300", "-q:v", "3", str(out_dir / "s_%04d.jpg")]
        proc = subprocess.run(cmd, check=False, capture_output=True, timeout=1800)
        # ffmpeg exits non-zero when the scene branch wrote nothing (no scene change): fine if frames exist
        if not any(out_dir.glob("f_*.jpg")):
            err = proc.stderr.decode("utf-8", "replace")[-300:]
            raise RuntimeError(f"ffmpeg produced no frames (rc={proc.returncode}): {err}")
        done.write_text("ok")
    frames: list[tuple[Path, float, bool]] = []
    for p in sorted(out_dir.glob("f_*.jpg")):
        n = int(p.stem[2:])
        frames.append((p, round((n - 1) * every_s, 3), False))
    times: list[float] = []
    st = out_dir / "scenes.txt"
    if st.exists():
        times = [float(m) for m in re.findall(r"pts_time:([0-9.]+)", st.read_text())]
    for i, p in enumerate(sorted(out_dir.glob("s_*.jpg"))):
        t = times[i] if i < len(times) else float("nan")
        frames.append((p, round(t, 3), True))
    frames.sort(key=lambda x: (x[1] if x[1] == x[1] else 1e9, not x[2]))
    return frames


def _pdq_bits(im: Image.Image) -> np.ndarray:
    import pdqhash

    h, _ = pdqhash.compute(np.asarray(im))
    return np.asarray(h, dtype=bool)


def select_frames(frames: list[tuple[Path, float, bool]], dedup: int = FRAME_DEDUP_PDQ,
                  max_kept: int = MAX_KEPT_FRAMES) -> list[tuple[Path, float, bool, Image.Image]]:
    """Drop near-identical frames (static screen recordings): keep a frame when its PDQ distance to every
    kept frame of the last 10 is > dedup. Scene keyframes use the same rule (they are often duplicates of
    a 1 fps frame)."""
    kept: list[tuple[Path, float, bool, Image.Image]] = []
    recent: list[np.ndarray] = []
    for p, t, scene in frames:
        try:
            with Image.open(p) as im:
                im = _rgb(im)
                im.load()
                im = im.copy()
        except Exception:  # noqa: BLE001
            continue
        bits = _pdq_bits(im)
        stale = kept and t == t and kept[-1][1] == kept[-1][1] and t - kept[-1][1] >= MAX_FRAME_GAP_S
        if not stale and any(int((bits != r).sum()) <= dedup for r in recent):
            continue
        kept.append((p, t, scene, im))
        recent = (recent + [bits])[-10:]
        if len(kept) >= max_kept:
            break
    return kept


# ------------------------------------------------------------------------------------------ units


def _units_for_image(a: ResolvedArtifact, next_idx: int) -> list[VisualUnit]:
    name = Path(a.meta.path_in_incident).name
    im, orig, scale = load_unit_image(a.path)
    cls = classify_image(orig[0], orig[1], a.meta.role, name)
    base_uid = a.meta.artifact_id[:12]
    unit = VisualUnit(uid=f"{base_uid}:0", artifact=a, idx=0, kind="image", image=im, scale=scale,
                      offset=(0.0, 0.0), source_size=orig, image_class=cls, name=name,
                      boilerplate=is_boilerplate(cls, name))
    units = [unit]
    if cls in ("pixel",):
        return units
    work, top = im, 0
    if cls == "phone_screenshot":
        work, top = crop_phone_chrome(im)
        unit.image = work
        unit.offset = (0.0, top / scale)
        unit.meta["chrome_cropped"] = True
        unit.meta["app_ui"] = True  # whole phone screen: app UI dominates (Ad Library panels, feed chrome)
    if cls in ("phone_screenshot", "desktop_screenshot", "creative", "page_screenshot"):
        max_regions = 6 if cls == "page_screenshot" else 3
        for k, (x0, y0, x1, y1) in enumerate(photo_regions(work, max_regions=max_regions), start=1):
            units.append(VisualUnit(
                uid=f"{base_uid}:{k}", artifact=a, idx=k, kind="region", image=work.crop((x0, y0, x1, y1)),
                scale=scale, offset=(x0 / scale, (y0 + top) / scale), source_size=orig,
                image_class="ad_region" if cls != "page_screenshot" else "page_region", name=name,
                parent_uid=unit.uid))
    return units


def _video_size(a: ResolvedArtifact) -> tuple[int | None, int | None]:
    w, h = a.meta.meta.get("width"), a.meta.meta.get("height")
    if w and h:
        return int(w), int(h)
    try:
        from hydra.media import probe

        for st in probe(a.path).get("streams", []):
            if st.get("codec_type") == "video":
                w, h = int(st["width"]), int(st["height"])
                rot = str(st.get("tags", {}).get("rotate", "0"))
                for sd in st.get("side_data_list", []) or []:
                    rot = str(sd.get("rotation", rot))
                if rot.strip("-") in ("90", "270"):
                    w, h = h, w
                return w, h
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _units_for_video(a: ResolvedArtifact, ctx: ExtractContext) -> list[VisualUnit]:
    name = Path(a.meta.path_in_incident).name
    out_dir = ctx.work_dir / "frames" / a.meta.artifact_id[:16]
    frames = video_frames(a.path, out_dir)
    kept = select_frames(frames)
    units: list[VisualUnit] = []
    base_uid = a.meta.artifact_id[:12]
    src_w, src_h = _video_size(a)
    for k, (_p, t, scene, im) in enumerate(kept, start=1):
        scale = im.width / src_w if src_w else 1.0
        phone = im.height / max(1, im.width) >= 1.85
        offset_y = 0.0
        if phone:
            im, top = crop_phone_chrome(im)
            offset_y = top / scale
        tc = None if t != t else t
        fu = VisualUnit(
            uid=f"{base_uid}:{k}", artifact=a, idx=k, kind="frame", image=im, scale=scale,
            offset=(0.0, offset_y), source_size=(src_w or im.width, src_h or im.height), image_class="frame",
            name=name, timecode_s=tc, meta={"scene": scene, "chrome_cropped": phone, "app_ui": phone})
        units.append(fu)
        if phone:
            for r, (x0, y0, x1, y1) in enumerate(photo_regions(im, max_regions=2), start=1):
                units.append(VisualUnit(
                    uid=f"{base_uid}:{k}r{r}", artifact=a, idx=k * 1000 + r, kind="region",
                    image=im.crop((x0, y0, x1, y1)), scale=scale, offset=(x0 / scale, offset_y + y0 / scale),
                    source_size=fu.source_size, image_class="ad_region", name=name, timecode_s=tc,
                    parent_uid=fu.uid))
    return units


@dataclass
class UnitSet:
    units: list[VisualUnit]
    errors: list[tuple[ResolvedArtifact, str]]   # artifacts that could not be decoded
    skipped: list[tuple[ResolvedArtifact, str]]  # not visual (svg, text), or over the unit cap


def collect_units(inc: ResolvedIncident, ctx: ExtractContext) -> UnitSet:
    """All visual units of an incident (cached per incident in ctx.options['_vis_units'])."""
    cache = ctx.options.setdefault("_vis_units", {})
    key = (inc.incident.id, str(ctx.work_dir))
    if key in cache:
        return cache[key]
    cache.clear()  # keep only the current incident in memory
    units: list[VisualUnit] = []
    errors: list[tuple[ResolvedArtifact, str]] = []
    skipped: list[tuple[ResolvedArtifact, str]] = []
    seen: set[str] = set()
    n_img = 0
    for a in inc.artifacts:
        if a.meta.artifact_id in seen:
            continue
        try:
            if is_video(a) and a.meta.role in VIDEO_ROLES:
                seen.add(a.meta.artifact_id)
                units.extend(_units_for_video(a, ctx))
            elif is_image(a) and a.meta.role in IMAGE_ROLES:
                if n_img >= MAX_IMAGE_UNITS:
                    skipped.append((a, "unit cap"))
                    continue
                seen.add(a.meta.artifact_id)
                us = _units_for_image(a, len(units))
                n_img += len(us)
                units.extend(us)
            elif (a.meta.mime or "").startswith("image/svg"):
                skipped.append((a, "svg"))
        except Exception as exc:  # noqa: BLE001 — one bad file never stops the incident
            log.warning("visual unit failed for %s: %s", a.meta.path_in_incident, exc)
            errors.append((a, f"{type(exc).__name__}: {exc}"[:200]))
    us = UnitSet(units, errors, skipped)
    cache[key] = us
    return us


def unit_note(u: VisualUnit, extra: str = "") -> str:
    parts = [f"unit={u.uid}", f"class={u.image_class}", f"kind={u.kind}"]
    if u.boilerplate:
        parts.append("boilerplate")
    if extra:
        parts.append(extra)
    parts.append(f"file={u.name}")
    return "; ".join(parts)[:300]
