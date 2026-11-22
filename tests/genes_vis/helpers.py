"""Synthetic visual material for the VIS/ID/LOGO tests (no organiser data)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from hydra.contracts import Incident, IncidentArtifact
from hydra.genome.base import ResolvedArtifact, ResolvedIncident


def photo(seed: int, size: tuple[int, int] = (480, 320)) -> Image.Image:
    """A 'photo': smooth random colour field plus shapes (stable PDQ quality, distinct per seed)."""
    rng = np.random.default_rng(seed)
    small = (rng.random((6, 9, 3)) * 255).astype(np.uint8)
    im = Image.fromarray(small).resize(size, Image.Resampling.BICUBIC)
    d = ImageDraw.Draw(im)
    for _ in range(6):
        x, y = int(rng.integers(0, size[0] - 60)), int(rng.integers(0, size[1] - 60))
        d.ellipse((x, y, x + int(rng.integers(20, 80)), y + int(rng.integers(20, 80))),
                  fill=tuple(int(c) for c in rng.integers(0, 255, 3)))
    return im


def feed_screenshot(ad: Image.Image, size: tuple[int, int] = (585, 1266)) -> Image.Image:
    """Phone-sized white UI with text lines above/below a full-width ad photo."""
    w, h = size
    im = Image.new("RGB", size, (255, 255, 255))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, w, int(0.05 * h)), fill=(250, 250, 250))
    d.text((20, 10), "21:24", fill=(0, 0, 0))
    for y in range(int(0.08 * h), int(0.25 * h), 22):
        d.text((20, y), "Sponsorowane  Lorem ipsum dolor sit amet", fill=(30, 30, 30))
    top = int(0.28 * h)
    ad_h = int(w * ad.height / ad.width)
    im.paste(ad.resize((w, ad_h)), (0, top))
    for y in range(top + ad_h + 20, int(0.9 * h), 22):
        d.text((20, y), "Lubie to  Komentarz  Udostepnij 123", fill=(30, 30, 30))
    return im


def save(im: Image.Image, path: Path, **kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, **kw)
    return path


def artifact(path: Path, role: str, mime: str | None = None, rel: str | None = None) -> ResolvedArtifact:
    data = path.read_bytes()
    ext = path.suffix.lower().lstrip(".")
    known = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
             "mp4": "video/mp4", "html": "text/html", "svg": "image/svg+xml"}
    mime = mime or known.get(ext, "application/octet-stream")
    return ResolvedArtifact(IncidentArtifact(artifact_id=hashlib.sha256(data).hexdigest(), role=role,
                                             path_in_incident=rel or path.name, mime=mime, size=len(data)), path)


def incident(inc_id: str, arts: list[ResolvedArtifact]) -> ResolvedIncident:
    return ResolvedIncident(Incident(id=inc_id, source="test", source_ref=inc_id, artifacts=[a.meta for a in arts]),
                            arts)
