"""Optional SigLIP2-base image embedding (second visual score, PLAN_MAX §1.7).

Off unless the weights are in the verified registry ($HYDRA_MODELS_DIR/siglip2-base) and
HYDRA_VIS_SIGLIP2 is not "0". SigLIP2 similarity is correlated with the text on the image, so its genes
share `dependency_group="vis_txt_semantic"` with nothing else in VIS; CORE may merge that group with OCR
text bits. DINOv2 stays the default candidate generator.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import numpy as np

from hydra.contracts import EvidencePointer, Family, Gene, Provenance
from hydra.genome.base import ExtractContext, ExtractResult, Vector
from hydra.genome.vis_media import UnitSet, unit_note

log = logging.getLogger(__name__)
KEY = "siglip2-base"


@dataclass
class SiglipEmbedder:
    model: object
    processor: object
    device: str

    def embed(self, images, batch_size: int = 16) -> np.ndarray:
        import torch

        from hydra.parallel import compute_slot

        out = []
        for i in range(0, len(images), batch_size):
            x = self.processor(images=[im.convert("RGB") for im in images[i:i + batch_size]],
                               return_tensors="pt").to(self.device)  # type: ignore[operator]
            with compute_slot(), torch.inference_mode():
                f = self.model.get_image_features(**x)  # type: ignore[attr-defined]
                f = getattr(f, "pooler_output", f)
            out.append(torch.nn.functional.normalize(f.float(), dim=-1).cpu().numpy())
        return np.concatenate(out).astype(np.float32) if out else np.zeros((0, 768), np.float32)


def enabled(ctx: ExtractContext) -> bool:
    if os.environ.get("HYDRA_VIS_SIGLIP2", "1") == "0" or ctx.options.get("siglip2") is False:
        return False
    md = ctx.settings.models_dir
    return md is not None and (md / KEY / "model.safetensors").exists()


def load(ctx: ExtractContext) -> SiglipEmbedder:
    if KEY not in ctx.models:
        from transformers import AutoModel, AutoProcessor

        from hydra.ml.device import pick_device

        path = ctx.settings.models_dir / KEY  # type: ignore[operator]
        dev = pick_device().device
        model = AutoModel.from_pretrained(path, local_files_only=True, use_safetensors=True).to(dev).eval()
        proc = AutoProcessor.from_pretrained(path, local_files_only=True)
        ctx.models[KEY] = SiglipEmbedder(model, proc, dev)
    return ctx.models[KEY]


def maybe_siglip(inc_id: str, us: UnitSet, ctx: ExtractContext) -> ExtractResult:
    res = ExtractResult()
    if not enabled(ctx):
        return res
    units = [u for u in us.units if not u.boilerplate and u.image_class != "pixel" and min(u.image.size) >= 16]
    if not units:
        return res
    try:
        emb = load(ctx)
        from hydra.genome.vis import embed_view, model_tag

        vecs = emb.embed([embed_view(u.image, short_side=224) for u in units])
        tag = model_tag(KEY)
        for u, v in zip(units, vecs, strict=True):
            key = f"{inc_id}|{u.artifact_id}|siglip2|{u.idx}"
            res.vectors.append(Vector(key=key, model=tag, data=v))
            res.genes.append(Gene(incident_id=inc_id, family=Family.VIS, kind="siglip2", vector_ref=key,
                                  provenance=Provenance.INF, extractor="vis@1",
                                  dependency_group="vis_txt_semantic",
                                  evidence=EvidencePointer(artifact_id=u.artifact_id, bbox=u.bbox,
                                                           timecode_s=u.timecode_s, note=unit_note(u))))
    except Exception as exc:  # noqa: BLE001
        log.warning("siglip2 failed: %s", exc)
    return res
