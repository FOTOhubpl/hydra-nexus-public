"""Offline OCR returning text lines with boxes. Engines (choose on a real sample — SPEC_ERRATA):

- "rapidocr"  : PP-OCRv6 small det+rec from $HYDRA_MODELS_DIR/rapidocr-v6 (best Polish diacritics: 0.96
                on the test line vs 0.84 for the v5 latin set; see docs/MODEL_REGISTRY.md).
- "tesseract" : Tesseract pol+eng, --psm 11 (sparse text; default psm reads 0 chars on busy backgrounds).
- "hybrid"    : RapidOCR detection + Tesseract `pol` recognition per detected line (87–99% with
                diacritics in the feasibility run).
Every engine returns the same `OcrLine` list, so callers record `extractor=ocr.<engine>@<version>`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image

from hydra.ml.models import model_dir


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: tuple[int, int, int, int]  # x0, y0, x1, y1 in the input image's pixel coordinates
    score: float  # engine confidence in [0, 1]
    engine: str


def _bbox(poly) -> tuple[int, int, int, int]:
    pts = np.asarray(poly, dtype=float).reshape(-1, 2)
    return int(pts[:, 0].min()), int(pts[:, 1].min()), int(pts[:, 0].max()), int(pts[:, 1].max())


def _rapid(threads: int = 0):
    """Engine for the CURRENT models dir: resolving the dir outside the cache means a settings change (another
    HYDRA_MODELS_DIR, removed weights) raises ModelUnavailable instead of reusing a stale engine (CR-101)."""
    return _rapid_at(str(model_dir("rapidocr-v6")), threads)


@lru_cache(maxsize=2)
def _rapid_at(root: str, threads: int = 0):
    from rapidocr import EngineType, ModelType, OCRVersion, RapidOCR

    d = Path(root)
    ort = EngineType.ONNXRUNTIME
    params = {
        "Global.model_root_dir": str(d),
        "Det.engine_type": ort,
        "Det.ocr_version": OCRVersion.PPOCRV6,
        "Det.model_type": ModelType.SMALL,
        "Det.model_path": str(d / "PP-OCRv6_det_small.onnx"),
        "Cls.engine_type": ort,
        "Cls.ocr_version": OCRVersion.PPOCRV4,
        "Cls.model_type": ModelType.MOBILE,
        "Cls.model_path": str(d / "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
        "Rec.engine_type": ort,
        "Rec.ocr_version": OCRVersion.PPOCRV6,
        "Rec.model_type": ModelType.SMALL,
        "Rec.model_path": str(d / "PP-OCRv6_rec_small.onnx"),
    }
    if threads:
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = threads
    return RapidOCR(params=params)


def _to_bgr(image: Image.Image) -> np.ndarray:
    return np.asarray(image.convert("RGB"))[:, :, ::-1].copy()


def ocr_rapid(image: Image.Image) -> list[OcrLine]:
    threads = int(os.environ.get("HYDRA_OCR_THREADS", "0"))
    res = _rapid(threads)(_to_bgr(image))
    if res is None or res.boxes is None or res.txts is None:
        return []
    return [
        OcrLine(t, _bbox(b), float(s), "rapidocr") for b, t, s in zip(res.boxes, res.txts, res.scores, strict=False)
    ]


def ocr_tesseract(image: Image.Image, lang: str = "pol+eng", psm: int = 11) -> list[OcrLine]:
    import pytesseract

    data = pytesseract.image_to_data(
        image.convert("RGB"), lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
    )
    # group words into lines by (block, paragraph, line)
    lines: dict[tuple[int, int, int], list[int]] = {}
    for i, word in enumerate(data["text"]):
        if word.strip() and float(data["conf"][i]) >= 0:
            lines.setdefault((data["block_num"][i], data["par_num"][i], data["line_num"][i]), []).append(i)
    out: list[OcrLine] = []
    for idx in lines.values():
        x0 = min(data["left"][i] for i in idx)
        y0 = min(data["top"][i] for i in idx)
        x1 = max(data["left"][i] + data["width"][i] for i in idx)
        y1 = max(data["top"][i] + data["height"][i] for i in idx)
        text = " ".join(data["text"][i] for i in idx)
        conf = float(np.mean([float(data["conf"][i]) for i in idx])) / 100.0
        out.append(OcrLine(text, (x0, y0, x1, y1), conf, "tesseract"))
    return out


def ocr_hybrid(image: Image.Image, pad: int = 4) -> list[OcrLine]:
    import pytesseract

    out: list[OcrLine] = []
    for line in ocr_rapid(image):
        x0, y0, x1, y1 = line.box
        crop = image.crop((max(0, x0 - pad), max(0, y0 - pad), min(image.width, x1 + pad), min(image.height, y1 + pad)))
        if crop.height < 32:  # tesseract likes ~30+ px glyphs
            crop = crop.resize((round(crop.width * 32 / max(1, crop.height)), 32), Image.Resampling.LANCZOS)
        text = pytesseract.image_to_string(crop.convert("L"), lang="pol", config="--psm 7").strip()
        out.append(OcrLine(text or line.text, line.box, line.score, "hybrid" if text else "rapidocr"))
    return out


def ocr(image: Image.Image | Path, engine: str = "rapidocr") -> list[OcrLine]:
    if isinstance(image, Path):
        with Image.open(image) as im:
            image = im.convert("RGB")
    if engine == "rapidocr":
        return ocr_rapid(image)
    if engine == "tesseract":
        return ocr_tesseract(image)
    if engine == "hybrid":
        return ocr_hybrid(image)
    raise ValueError(f"unknown OCR engine {engine!r}")
