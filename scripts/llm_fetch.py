"""Download ONE pinned Bielik GGUF for the optional local AI analyst (ADR-008, docs/genes/LLM.md §7).

    HYDRA_OFFLINE=0 uv run python scripts/llm_fetch.py              # Bielik 4.5B Q4_K_M (~2.9 GB), default
    HYDRA_OFFLINE=0 uv run python scripts/llm_fetch.py --model 1.5b # Bielik 1.5B Q4_K_M (~0.97 GB), weak machines
    uv run python scripts/llm_fetch.py --verify                     # re-hash what is on disk (offline)

Files go to `$HYDRA_LLM_MODELS_DIR` (default ~/HydraNexus/models-llm), never into the repo or the Docker image.
Each file is pinned by repository revision and sha256 and is verified before it is moved into place. Then:
`scripts/llm_local_server.sh <printed path> 8090` and `HYDRA_LLM=1 HYDRA_LLM_BACKEND=local`.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

# Apache-2.0 (Bielik v3.0, SpeakLeash). The Q4_K_M files are a GGUF re-quantization of the official speakleash
# weights published by second-state (card: base_model speakleash/Bielik-*-v3.0-Instruct, licence apache-2.0);
# the official speakleash repos publish only Q8_0 and fp16 (`*-q8` keys below).
MODELS: dict[str, dict[str, str | int]] = {
    "4.5b": {"repo": "second-state/Bielik-4.5B-v3.0-Instruct-GGUF", "rev": "5ed9534f824c7c184c21320f4d339e75569d0987",
             "file": "Bielik-4.5B-v3.0-Instruct-Q4_K_M.gguf", "size": 2878886912,
             "sha256": "39fb78db7c5e1582d3ef5bded109cd98606aabd5acbb5c98601155174b6763e1",
             "alias": "bielik-4.5b-v3.0-instruct-q4_k_m"},
    "1.5b": {"repo": "second-state/Bielik-1.5B-v3.0-Instruct-GGUF", "rev": "6c316d2be07dee472901150c3f3e9d4f725a4706",
             "file": "Bielik-1.5B-v3.0-Instruct-Q4_K_M.gguf", "size": 972797408,
             "sha256": "ea9cc250a6c65718c290fd963d8e3237b924ccfc7632f20a6127bba5904a50d9",
             "alias": "bielik-1.5b-v3.0-instruct-q4_k_m"},
    "4.5b-q8": {"repo": "speakleash/Bielik-4.5B-v3.0-Instruct-GGUF", "rev": "f9b3ef0abcc8c37136800c6c66b46d8d9ea60830",
                "file": "Bielik-4.5B-v3.0-Instruct.Q8_0.gguf", "size": 5061215424,
                "sha256": "77eafc61197b394566939c7963c66861bb284eec6b183c56f5e531116b724aa6",
                "alias": "bielik-4.5b-v3.0-instruct-q8_0"},
    "1.5b-q8": {"repo": "speakleash/Bielik-1.5B-v3.0-Instruct-GGUF", "rev": "d3f43577b7b23c3e8a9671e1b35d26d74736277f",
                "file": "Bielik-1.5B-v3.0-Instruct.Q8_0.gguf", "size": 1699568288,
                "sha256": "014fc8a22eb85c557e395476071bacd14dd8c8a69dedb96e5dd17e8bb7cee9c3",
                "alias": "bielik-1.5b-v3.0-instruct-q8_0"},
}


def dest_dir() -> Path:
    return Path(os.environ.get("HYDRA_LLM_MODELS_DIR") or Path.home() / "HydraNexus" / "models-llm")


def target(m: dict) -> Path:
    return dest_dir() / str(m["repo"]).split("/")[1] / str(m["file"])


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(key: str) -> Path:
    m = MODELS[key]
    out = target(m)
    if out.is_file() and out.stat().st_size == m["size"] and sha256(out) == m["sha256"]:
        print(f"ok (already present): {out}")
        return out
    if os.environ.get("HYDRA_OFFLINE", "1") != "0":
        sys.exit("network download needs HYDRA_OFFLINE=0 (offline by default, ADR-003)")
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_suffix(out.suffix + ".part")
    url = f"https://huggingface.co/{m['repo']}/resolve/{m['rev']}/{m['file']}"
    print(f"downloading {url} ({int(m['size']) / 1e9:.2f} GB) -> {out}")
    req = urllib.request.Request(url, headers={"User-Agent": "hydra-nexus-llm-fetch/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r, part.open("wb") as f:  # noqa: S310 — fixed HF URL
        done = 0
        while chunk := r.read(1 << 22):
            f.write(chunk)
            done += len(chunk)
            print(f"\r  {done / 1e9:.2f} GB", end="", flush=True)
    print()
    got = sha256(part)
    if got != m["sha256"]:
        part.unlink(missing_ok=True)
        sys.exit(f"sha256 mismatch for {m['file']}: {got}")
    part.replace(out)
    print(f"ok: {out}\nserve: scripts/llm_local_server.sh {out} 8090 {m['alias']}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=sorted(MODELS), default="4.5b")
    ap.add_argument("--verify", action="store_true", help="only re-hash files already on disk")
    a = ap.parse_args()
    if a.verify:
        bad = 0
        for k, m in MODELS.items():
            p = target(m)
            if p.is_file():
                ok = sha256(p) == m["sha256"]
                bad += not ok
                print(f"{k}: {'ok' if ok else 'MISMATCH'} {p}")
        return 1 if bad else 0
    fetch(a.model)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
