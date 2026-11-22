"""Device and thread selection.

Measured 20.11.2026 (scratchpad feasibility run):
- M4 laptop: MPS ~113 img/s DINOv2-small; CPU 62-68 img/s.
- the Linux server (64 vCPU, no GPU): fp32 ~115 img/s, bf16 with 16 threads ~243 img/s
  (cosine vs fp32 >= 0.9997), so bf16 autocast is the CPU default on Linux x86.
- faiss-cpu and torch abort together on macOS (OMP Error #15) — use torch top-k instead.

Threads: `HYDRA_TORCH_THREADS` (set per worker by `hydra.parallel`, which splits the CPU count between the
incident processes) wins; otherwise min(16, cpu count). An explicit value is applied on every device (MPS
workers still run preprocessing and small ops on CPU threads); the default only on CPU.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass


@dataclass(frozen=True)
class DeviceInfo:
    device: str            # "mps" | "cpu" | "cuda"
    dtype: str             # "float32" | "bfloat16"
    threads: int


def pick_device(prefer: str | None = None) -> DeviceInfo:
    import torch

    prefer = prefer or os.environ.get("HYDRA_DEVICE")
    if prefer in (None, "auto"):
        if torch.cuda.is_available():
            prefer = "cuda"
        elif torch.backends.mps.is_available():
            prefer = "mps"
        else:
            prefer = "cpu"
    explicit = int(os.environ.get("HYDRA_TORCH_THREADS", "0") or 0)
    threads = explicit or min(16, os.cpu_count() or 1)
    if prefer == "cpu" or explicit:
        torch.set_num_threads(threads)
    is_x86_linux = platform.system() == "Linux" and platform.machine() in ("x86_64", "AMD64")
    dtype = "bfloat16" if (prefer == "cpu" and is_x86_linux) else "float32"
    return DeviceInfo(prefer, os.environ.get("HYDRA_TORCH_DTYPE", dtype), threads)
