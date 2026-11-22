"""Generic media helpers: working copies of images, video frames, scenes, audio.

Originals in the CAS are never modified; everything here writes derived files under
settings.work_dir/<artifact_id>/ and can be rebuilt at any time.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = 120_000_000  # decompression-bomb guard (~11k x 11k)


@dataclass
class WorkImage:
    image: Image.Image          # RGB, EXIF orientation applied, longest side <= max_side
    original_size: tuple[int, int]
    scale: float                # work / original (multiply original coords by this)


def load_work_image(path: Path, max_side: int = 2048) -> WorkImage:
    with Image.open(path) as im:
        im.load()
        im = ImageOps.exif_transpose(im)
        original = im.size
        if im.mode != "RGB":
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
        scale = 1.0
        if max(im.size) > max_side:
            scale = max_side / max(im.size)
            im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
        return WorkImage(im.copy(), original, scale)


# A hostile "video" can be an HLS/concat/DASH playlist that points ffmpeg at http(s) URLs: input is local files only.
LOCAL_ONLY = ("-protocol_whitelist", "file,pipe")


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg not found on PATH")
    return exe


def probe(path: Path) -> dict:
    exe = shutil.which("ffprobe")
    if not exe:
        raise RuntimeError("ffprobe not found on PATH")
    out = subprocess.run(
        [exe, "-v", "error", *LOCAL_ONLY, "-print_format", "json", "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    return json.loads(out)


def extract_frames(path: Path, out_dir: Path, every_s: float = 1.0, max_side: int = 1024,
                   max_frames: int = 600) -> list[Path]:
    """One JPEG every `every_s` seconds; frame_000001.jpg ≈ t=0s. Bounded by max_frames."""
    out_dir.mkdir(parents=True, exist_ok=True)
    vf = f"fps=1/{every_s},scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'"
    subprocess.run(
        [_ffmpeg(), "-v", "error", "-y", *LOCAL_ONLY, "-i", str(path), "-vf", vf, "-frames:v", str(max_frames),
         "-q:v", "3", str(out_dir / "frame_%06d.jpg")],
        check=True, capture_output=True, timeout=600,
    )
    return sorted(out_dir.glob("frame_*.jpg"))


def extract_audio_wav16k(path: Path, out_path: Path) -> Path | None:
    """Mono 16 kHz WAV, or None when the file has no audio stream."""
    streams = probe(path).get("streams", [])
    if not any(s.get("codec_type") == "audio" for s in streams):
        return None
    out_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_ffmpeg(), "-v", "error", "-y", *LOCAL_ONLY, "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(out_path)],
        check=True, capture_output=True, timeout=600,
    )
    return out_path


def detect_scenes(path: Path, threshold: float = 27.0) -> list[tuple[float, float]]:
    """Scene boundaries in seconds via PySceneDetect ContentDetector."""
    from scenedetect import ContentDetector, detect

    return [(s.get_seconds(), e.get_seconds()) for s, e in detect(str(path), ContentDetector(threshold=threshold))]
