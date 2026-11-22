"""Offline loaders for the pre-downloaded models in $HYDRA_MODELS_DIR (see models.lock.json).

Every loader uses local files only (`local_files_only=True`), never `trust_remote_code`, and
returns L2-normalised float32 numpy embeddings. Missing weights raise ModelUnavailable so callers
can record the modality as `unavailable` instead of crashing (spec: degradation without failure).
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from hydra.config import Settings, get_settings
from hydra.ml.device import DeviceInfo, pick_device


class ModelUnavailable(RuntimeError):
    pass


def model_dir(key: str, settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    assert settings.models_dir is not None
    path = settings.models_dir / key
    if not path.is_dir() or not any(path.iterdir()):
        raise ModelUnavailable(f"model '{key}' not found in {settings.models_dir} (run scripts/fetch_models.py)")
    return path


def _l2(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x, axis=1, keepdims=True)
    return (x / np.maximum(n, 1e-12)).astype(np.float32)


def _autocast(dev: DeviceInfo):
    import torch

    if dev.device == "cpu" and dev.dtype == "bfloat16":
        return torch.autocast("cpu", dtype=torch.bfloat16)
    return contextlib.nullcontext()


@dataclass
class ImageEmbedder:
    """DINOv2 (ViT-S/14 → 384-d CLS embedding)."""

    key: str
    dim: int
    device: DeviceInfo
    _model: object
    _processor: object

    def embed(self, images: Sequence[Image.Image], batch_size: int = 32) -> np.ndarray:
        import torch

        from hydra.parallel import compute_slot

        out: list[np.ndarray] = []
        for i in range(0, len(images), batch_size):
            batch = [im.convert("RGB") for im in images[i : i + batch_size]]
            inputs = self._processor(images=batch, return_tensors="pt").to(self.device.device)  # type: ignore[operator]
            with compute_slot(), torch.inference_mode(), _autocast(self.device):
                hidden = self._model(**inputs).last_hidden_state  # type: ignore[operator]
            out.append(hidden[:, 0].float().cpu().numpy())
        return _l2(np.concatenate(out)) if out else np.zeros((0, self.dim), np.float32)


@dataclass
class TextEmbedder:
    """multilingual-e5 (small: 384-d, base: 768-d). e5 needs "query: " / "passage: " prefixes."""

    key: str
    dim: int
    device: DeviceInfo
    _model: object

    def embed(self, texts: Sequence[str], kind: str = "passage", batch_size: int = 64) -> np.ndarray:
        if kind not in ("query", "passage"):
            raise ValueError("kind must be 'query' or 'passage'")
        from hydra.parallel import compute_slot

        prefixed = [f"{kind}: {t}" for t in texts]
        with compute_slot():
            vecs = self._model.encode(  # type: ignore[attr-defined]
                prefixed, batch_size=batch_size, normalize_embeddings=True, convert_to_numpy=True,
                show_progress_bar=False,
            )
        return np.asarray(vecs, dtype=np.float32)


def load_image_embedder(key: str = "dinov2-small", device: DeviceInfo | None = None) -> ImageEmbedder:
    from transformers import AutoImageProcessor, AutoModel

    path = model_dir(key)
    dev = device or pick_device()
    processor = AutoImageProcessor.from_pretrained(path, local_files_only=True)
    model = AutoModel.from_pretrained(path, local_files_only=True, use_safetensors=True).to(dev.device).eval()
    dim = int(getattr(model.config, "hidden_size", 384))
    return ImageEmbedder(key, dim, dev, model, processor)


def load_text_embedder(key: str = "e5-small", device: DeviceInfo | None = None) -> TextEmbedder:
    from sentence_transformers import SentenceTransformer

    path = model_dir(key)
    dev = device or pick_device()
    model = SentenceTransformer(str(path), device=dev.device, local_files_only=True)
    getter = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    dim = int(getter() or 384)
    return TextEmbedder(key, dim, dev, model)


def cosine_topk(queries: np.ndarray, corpus: np.ndarray, k: int = 30, exclude_self: bool = False,
                chunk: int = 4096) -> tuple[np.ndarray, np.ndarray]:
    """Exact top-k by cosine on L2-normalised vectors (torch matmul; replaces FAISS on macOS).

    Returns (indices [n, k], scores [n, k]). 20k x 20k top-30 measured at ~0.3 s on M4 CPU.
    """
    import torch

    q = torch.from_numpy(np.ascontiguousarray(queries, dtype=np.float32))
    c = torch.from_numpy(np.ascontiguousarray(corpus, dtype=np.float32))
    k = min(k + (1 if exclude_self else 0), c.shape[0])
    idx_parts, val_parts = [], []
    for s in range(0, q.shape[0], chunk):
        sims = q[s : s + chunk] @ c.T
        if exclude_self:
            rows = torch.arange(sims.shape[0])
            cols = rows + s
            valid = cols < sims.shape[1]
            sims[rows[valid], cols[valid]] = -2.0
        vals, idx = torch.topk(sims, k=k, dim=1)
        if exclude_self:
            vals, idx = vals[:, :-1], idx[:, :-1]
        idx_parts.append(idx.numpy())
        val_parts.append(vals.numpy())
    return np.concatenate(idx_parts), np.concatenate(val_parts)
