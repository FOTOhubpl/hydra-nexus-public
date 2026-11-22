# MIT License
#
# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# `AudioSealDetector` follows src/audioseal/models.py (class AudioSealDetector) and the hyper-parameters of
# src/audioseal/cards/audioseal_detector_16bits.yaml of https://github.com/facebookresearch/audioseal @
# 1496f7662d6aa625602bb0dcd1b4997b63b47609. Modifications by HYDRA NEXUS (2026-11-21):
# - detector only (no generator, no streaming, no loudness normaliser, no TorchScript exports);
# - built with norm="none": the weight-norm parametrisation of the checkpoint is folded into plain weights by
#   `fold_weight_norm` (exact at inference), so the shipped weights are a plain safetensors state dict;
# - `load_checkpoint_restricted` and `convert_checkpoint` are our own code (no upstream equivalent): they read the
#   upstream .pth with an allow-list unpickler that never resolves a callable outside torch tensor rebuilding.
"""AudioSeal watermark detector (Meta FAIR, MIT code and weights), detector-only subset."""

from __future__ import annotations

import pickle
import types
from pathlib import Path
from typing import Any

SEANET = {
    "channels": 1, "dimension": 128, "n_filters": 32, "n_residual_layers": 1, "ratios": [8, 5, 4, 2],
    "activation": "ELU", "activation_params": {"alpha": 1.0}, "norm_params": {}, "kernel_size": 7,
    "last_kernel_size": 7, "residual_kernel_size": 3, "dilation_base": 2, "causal": False, "pad_mode": "constant",
    "true_skip": True, "compress": 2, "lstm": 2, "disable_norm_outer_blocks": 0,
}
NBITS = 16
OUTPUT_DIM = 32
SAMPLE_RATE = 16000


def build_detector(norm: str = "none"):
    """AudioSealDetector(16 bits): SEANet encoder that keeps the time resolution + a 1x1 conv to 2 + nbits."""
    import torch

    from hydra.vendor.audioseal.seanet import SEANetEncoderKeepDimension

    class AudioSealDetector(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            encoder = SEANetEncoderKeepDimension(output_dim=OUTPUT_DIM, norm=norm, **SEANET)
            last_layer = torch.nn.Conv1d(encoder.output_dim, 2 + NBITS, 1)
            self.detector = torch.nn.Sequential(encoder, last_layer)
            self.nbits = NBITS

        def forward(self, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
            """x: batch x 1 x samples at 16 kHz. Returns (per-sample [p_not_marked, p_marked], message probs)."""
            result = self.detector(x)
            probs = torch.softmax(result[:, :2, :], dim=1)
            message = torch.sigmoid(result[:, 2:, :].mean(dim=-1))
            return probs, message

    return AudioSealDetector()


# ------------------------------------------------------------------------------------------ checkpoint I/O
# Our own code. The upstream checkpoint is a torch zip-pickle holding {"xp.cfg": <omegaconf config>, "model":
# <state dict>}. torch.load(weights_only=True) refuses it (omegaconf globals), so a custom Unpickler is used:
# tensor-rebuild functions and plain containers resolve to the real objects; every other global becomes an inert
# placeholder class that only stores its state. No other callable is ever resolved, so loading runs no code.


class _Inert:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args

    def __setstate__(self, state: Any) -> None:
        self.state = state

    def __call__(self, *args: Any, **kwargs: Any) -> _Inert:
        return _Inert()


_TORCH_OK = {("torch._utils", "_rebuild_tensor_v2"), ("torch._utils", "_rebuild_parameter"),
             ("collections", "OrderedDict"), ("collections", "defaultdict")}
_BUILTINS_OK = {"dict", "list", "set", "tuple", "int", "float", "str", "bool", "frozenset"}


class _AllowListUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        if (module, name) in _TORCH_OK:
            return super().find_class(module, name)
        if module in ("builtins", "__builtin__"):
            if name in _BUILTINS_OK:
                return getattr(__import__("builtins"), name)
            if name == "long":
                return int
        return _Inert


_PICKLE_MODULE = types.SimpleNamespace(Unpickler=_AllowListUnpickler, load=pickle.load, __name__="hydra_allowlist")


def load_checkpoint_restricted(path: Path) -> dict[str, Any]:
    """State dict ("model" entry) of an upstream AudioSeal .pth, read with the allow-list unpickler."""
    import torch

    ck = torch.load(str(path), map_location="cpu", weights_only=False, pickle_module=_PICKLE_MODULE)  # noqa: S614
    sd = ck.get("model", ck) if isinstance(ck, dict) else None
    if not isinstance(sd, dict) or not all(isinstance(v, torch.Tensor) for v in sd.values()):
        raise ValueError("unexpected AudioSeal checkpoint layout")
    return dict(sd)


def fold_weight_norm(sd: dict[str, Any]) -> dict[str, Any]:
    """weight = g * v / ||v|| (norm over all dims but 0, torch weight_norm default) for every weight_g/weight_v."""
    import torch

    out: dict[str, Any] = {}
    for k, v in sd.items():
        if k.endswith(".weight_g"):
            continue
        if k.endswith(".weight_v"):
            base = k[: -len(".weight_v")]
            g = sd[base + ".weight_g"].double()
            vd = v.double()  # float64 math, then float32: the converted bytes do not depend on the CPU's SIMD order
            norm = torch.linalg.vector_norm(vd.reshape(vd.shape[0], -1), dim=1).reshape(g.shape)
            out[base + ".weight"] = (g * vd / norm).to(v.dtype).contiguous()
        else:
            out[k] = v.contiguous()
    return out


def convert_checkpoint(src: Path, dest: Path) -> Path:
    """Upstream detector .pth -> plain safetensors state dict loadable by build_detector(norm="none")."""
    from safetensors.torch import save_file

    sd = fold_weight_norm(load_checkpoint_restricted(src))
    model = build_detector()
    missing, unexpected = model.load_state_dict(sd, strict=False)
    if missing or unexpected:
        raise ValueError(f"AudioSeal key mismatch: missing={missing[:5]} unexpected={unexpected[:5]}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    meta = {"source": "facebook/audioseal detector_base.pth", "converted_by": "hydra.vendor.audioseal"}
    save_file(sd, str(tmp), metadata=meta)  # constant metadata: the output bytes depend only on the input
    _canonical_metadata_order(tmp, meta)
    tmp.replace(dest)
    return dest


def _canonical_metadata_order(path: Path, meta: dict[str, str]) -> None:
    """safetensors writes `__metadata__` from a Rust HashMap, so the key order (and the file's sha256) changed
    from one process to the next (measured 2026-11-21: 2 orders, about half the conversions each). Rewrite the
    block in `meta`'s order; same keys and values, so the header length and every tensor offset stay the same."""
    import json
    import struct

    with open(path, "r+b") as fh:
        (n,) = struct.unpack("<Q", fh.read(8))
        header = fh.read(n)
        start = header.index(b'"__metadata__":') + len(b'"__metadata__":')
        end = header.index(b"}", start) + 1
        block = json.dumps(meta, separators=(",", ":"), ensure_ascii=False).encode()
        if json.loads(header[start:end]) != meta or len(block) != end - start:
            raise ValueError("unexpected safetensors metadata block")
        fh.seek(8 + start)
        fh.write(block)


def load_detector(path: Path):
    """Detector from our converted safetensors file (no pickle at run time)."""
    from safetensors.torch import load_file

    model = build_detector()
    model.load_state_dict(load_file(str(path)), strict=True)
    return model.eval()
