"""Aggregate-only overlap of a reference dataset's indicators with other corpora (DATA-MASS, server only).

    uv run python scripts/ioc_overlap.py --dataset certpl-tollfraud-2026 \
        --target nask=~/HydraNexus/data/nask --target mass=~/HydraNexus/data/external-big/mass-phreshphish

The dataset adapter must expose `iocs(root) -> {kind: set[str]}` (e.g. `certpl_tollfraud_2026`). Every file under
each target (and every member of a ZIP file, one level) is hashed (sha256 of the bytes, compared with the `sha256`
kind) and searched as lower-case text for the other kinds with token boundaries. Output is a JSON document of
COUNTS ONLY per target and kind (distinct indicator values matched, files matched): no indicator values, no file
names, no content are printed, so the script is safe to run on organiser data (CLAUDE.md guard: aggregates only).
Files are only read, never executed.

`--images DIR` adds an image check: every image under DIR (the reference set's own images, e.g. csirt-knf) is
hashed (sha256 + 64-bit pHash, `imagehash`, as `hydra.genome.vis`); every image blob in each target is compared:
exact sha256 hits and pHash near-duplicates (Hamming <= `--phash-max`, default 10 as in the VIS gene). Counts only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from collections.abc import Iterator
from pathlib import Path

MAX_TEXT = 32 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024


def _boundary(kind: str, value: str) -> str:
    v = re.escape(value.lower())
    if kind in ("ip", "meta_profile_id", "ad_library_id"):
        return rf"(?<![0-9.]){v}(?![0-9])"
    if kind == "sha256":
        return rf"(?<![0-9a-f]){v}(?![0-9a-f])"
    return rf"(?<![a-z0-9_-]){v}(?![a-z0-9_-])"


def compile_iocs(iocs: dict[str, set[str]]) -> dict[str, re.Pattern[bytes]]:
    out = {}
    for kind, values in iocs.items():
        vals = sorted(v for v in values if v)
        if vals:
            out[kind] = re.compile("|".join(_boundary(kind, v) for v in vals).encode())
    return out


def iter_blobs(target: Path) -> Iterator[bytes]:
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    for p in files:
        try:
            if p.stat().st_size > MAX_FILE:
                continue
            data = p.read_bytes()
        except OSError:
            continue
        yield data
        if data[:4] == b"PK\x03\x04":
            try:
                import io

                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    for info in zf.infolist():
                        if not info.is_dir() and info.file_size <= MAX_FILE:
                            yield zf.read(info)
            except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, ValueError):
                continue


def scan(target: Path, iocs: dict[str, set[str]], pats: dict[str, re.Pattern[bytes]]) -> dict:
    shas = {s.lower() for s in iocs.get("sha256", set())}
    res: dict = {"blobs": 0, "bytes": 0, "blob_sha256_hits": 0, "kinds": {}}
    matched: dict[str, set[bytes]] = {k: set() for k in pats}
    files_hit: dict[str, int] = dict.fromkeys(pats, 0)
    sha_values: set[str] = set()
    for data in iter_blobs(target):
        res["blobs"] += 1
        res["bytes"] += len(data)
        h = hashlib.sha256(data).hexdigest()
        if h in shas:
            res["blob_sha256_hits"] += 1
            sha_values.add(h)
        if b"\x00" in data[:4096]:
            continue  # binary blob (image, video, archive): sha256 check only, no text search
        low = data[:MAX_TEXT].lower()
        for kind, pat in pats.items():
            found = set(m.group(0) for m in pat.finditer(low))
            if found:
                matched[kind] |= found
                files_hit[kind] += 1
    for kind in pats:
        res["kinds"][kind] = {"indicators": len(iocs.get(kind, ())), "distinct_matched": len(matched[kind]),
                              "blobs_matched": files_hit[kind]}
    res["blob_sha256_distinct"] = len(sha_values)
    return res


IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8")


def _phash(data: bytes) -> int | None:
    import io

    import imagehash
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as im:
            return int(str(imagehash.phash(im.convert("RGB"))), 16)
    except Exception:  # noqa: BLE001 - undecodable image: skipped, counted as not hashed
        return None


def image_refs(folder: Path) -> tuple[set[str], list[int]]:
    shas, ph = set(), []
    for data in iter_blobs(folder):
        if data[:4].startswith(IMAGE_MAGIC) or data[:3] == b"\xff\xd8\xff":
            shas.add(hashlib.sha256(data).hexdigest())
            h = _phash(data)
            if h is not None:
                ph.append(h)
    return shas, ph


def scan_images(target: Path, shas: set[str], ph: list[int], max_dist: int) -> dict:
    res = {"images": 0, "hashed": 0, "sha256_hits": 0, "phash_near": 0, "reference_images_matched": 0}
    matched: set[int] = set()
    for data in iter_blobs(target):
        if not (data[:4].startswith(IMAGE_MAGIC) or data[:3] == b"\xff\xd8\xff"):
            continue
        res["images"] += 1
        if hashlib.sha256(data).hexdigest() in shas:
            res["sha256_hits"] += 1
        h = _phash(data)
        if h is None:
            continue
        res["hashed"] += 1
        near = [i for i, r in enumerate(ph) if bin(h ^ r).count("1") <= max_dist]
        if near:
            res["phash_near"] += 1
            matched.update(near)
    res["reference_images_matched"] = len(matched)
    return res


def main(argv: list[str] | None = None) -> int:
    from hydra.adapters.datasets import _module, dataset_root
    from hydra.adapters.datasets.__main__ import default_root

    ap = argparse.ArgumentParser(prog="scripts/ioc_overlap.py")
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--root", type=Path, default=default_root())
    ap.add_argument("--target", action="append", required=True, help="name=path (file or folder)")
    ap.add_argument("--images", type=Path, default=None, help="folder with the reference set's images")
    ap.add_argument("--phash-max", type=int, default=10)
    a = ap.parse_args(argv)
    m = _module(a.dataset)
    iocs = m.iocs(dataset_root(a.dataset, a.root.expanduser())) if hasattr(m, "iocs") else {}
    pats = compile_iocs(iocs)
    out = {"dataset": a.dataset, "indicator_counts": {k: len(v) for k, v in iocs.items()}, "targets": {},
           "note": "counts only; no indicator values or file names are printed"}
    shas, ph = image_refs(a.images.expanduser()) if a.images else (set(), [])
    if a.images:
        out["reference_images"] = {"sha256": len(shas), "phashed": len(ph), "phash_max": a.phash_max}
    for t in a.target:
        name, _, path = t.partition("=")
        p = Path(path).expanduser()
        if not p.exists():
            out["targets"][name] = {"missing": True}
            continue
        out["targets"][name] = scan(p, iocs, pats) if pats else {}
        if a.images:
            out["targets"][name]["images"] = scan_images(p, shas, ph, a.phash_max)
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
