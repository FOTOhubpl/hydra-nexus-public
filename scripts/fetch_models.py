#!/usr/bin/env python3
"""Fetch, pin and verify the offline model weights + external reference data for HYDRA NEXUS.

Generic environment preparation (no task logic). Everything lands OUTSIDE the repo:
  weights          -> $HYDRA_MODELS_DIR/<key>/            (default ~/HydraNexus/models)
  reference data   -> $HYDRA_DATA_DIR/external/<key>/     (default ~/HydraNexus/data/external)
  derived caches   -> $HYDRA_CACHE_DIR/tldextract/        (per platform: cache/<os>-<arch>)
and every byte is recorded in models.lock.json (repo root, mirrored to $HYDRA_MODELS_DIR).

Download (needs network; idempotent - pinned files already on disk are re-hashed, not re-fetched):
    HYDRA_OFFLINE=0 . scripts/env.sh && uv run python scripts/fetch_models.py [--with-base]
Verify (offline: re-hash everything in the lock, then one isolated smoke test per key):
    . scripts/env.sh && uv run python scripts/fetch_models.py --verify

Rules enforced here: pinned revisions only, safetensors/ONNX/config/tokenizer files only
(never .bin/.pt/pickle), no trust_remote_code, smoke tests run with HF offline flags AND a
socket guard, so a silent download attempt fails the test instead of succeeding.

Download path needs only the stdlib + huggingface_hub. The PSL step uses tldextract's own cache
API (a project dependency) so the cache layout always matches the installed tldextract.
--verify imports the runtime stack (torch, transformers, sentence-transformers, cv2, rapidocr).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------------
# Locations (same defaults as scripts/env.sh)
# --------------------------------------------------------------------------------------------
HYDRA_ROOT = Path(os.environ.get("HYDRA_ROOT", Path.home() / "HydraNexus")).expanduser()
PLATFORM_TAG = f"{platform.system().lower()}-{platform.machine()}"
MODELS_DIR = Path(os.environ.get("HYDRA_MODELS_DIR", HYDRA_ROOT / "models")).expanduser()
DATA_DIR = Path(os.environ.get("HYDRA_DATA_DIR", HYDRA_ROOT / "data")).expanduser()
EXTERNAL_DIR = DATA_DIR / "external"
CACHE_DIR = Path(os.environ.get("HYDRA_CACHE_DIR", HYDRA_ROOT / "cache" / PLATFORM_TAG)).expanduser()
ROOTS = {"models": MODELS_DIR, "external": EXTERNAL_DIR, "cache": CACHE_DIR}
ROOT_LABELS = {
    "models": "$HYDRA_MODELS_DIR",
    "external": "$HYDRA_DATA_DIR/external",
    "cache": "$HYDRA_CACHE_DIR",
}

_SCRIPT_REPO = Path(__file__).resolve().parents[1]
REPO_ROOT = _SCRIPT_REPO if (_SCRIPT_REPO / "pyproject.toml").exists() else Path.cwd()
DEFAULT_LOCK = REPO_ROOT / "models.lock.json"
LOCK_MIRROR = MODELS_DIR / "models.lock.json"

USER_AGENT = "hydra-nexus-fetch/1.0"
FORBIDDEN_SUFFIXES = (".bin", ".pt", ".pth", ".pkl", ".pickle", ".ckpt", ".h5", ".msgpack", ".joblib")
HF_IGNORE = [
    "*.bin",
    "*.pt",
    "*.pth",
    "*.pkl",
    "*.pickle",
    "*.ckpt",
    "*.h5",
    "*.msgpack",
    "onnx/*",
    "openvino/*",
    ".eval_results/*",
]

# --------------------------------------------------------------------------------------------
# Registry. Every source is pinned to an immutable revision and/or an expected sha256.
# --------------------------------------------------------------------------------------------
MS_ONNX = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0/onnx"
HF_TEXT_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "sentencepiece.bpe.model",
    "modules.json",
    "sentence_bert_config.json",
    "1_Pooling/config.json",
]

REGISTRY: dict[str, dict[str, Any]] = {
    "dinov2-small": {
        "kind": "hf",
        "repo": "facebook/dinov2-small",
        "revision": "ed25f3a31f01632728cabb09d1542f84ab7b0056",
        "license": "Apache-2.0",
        "backend": "transformers AutoModel (PyTorch, safetensors)",
        "allow": ["model.safetensors", "config.json", "preprocessor_config.json"],
        "default": True,
    },
    "e5-small": {
        "kind": "hf",
        "repo": "intfloat/multilingual-e5-small",
        "revision": "614241f622f53c4eeff9890bdc4f31cfecc418b3",
        "license": "MIT",
        "backend": "sentence-transformers (PyTorch, safetensors)",
        "allow": ["model.safetensors", *HF_TEXT_FILES],
        "default": True,
    },
    "e5-base": {
        "kind": "hf",
        "repo": "intfloat/multilingual-e5-base",
        "revision": "d128750597153bb5987e10b1c3493a34e5a4502a",
        "license": "MIT",
        "backend": "sentence-transformers (PyTorch, safetensors)",
        "allow": ["model.safetensors", *HF_TEXT_FILES],
        "default": False,  # --with-base
    },
    # OpenCV Zoo models via the official OpenCV org mirror on the HF Hub. The sha256 must also
    # equal the git-lfs oid of the same file in github.com/opencv/opencv_zoo at a pinned commit.
    "yunet": {
        "kind": "hf",
        "repo": "opencv/face_detection_yunet",
        "revision": "3cc26e7f1014a5ee5d74a42acee58bafc9d0a310",
        "license": "MIT",
        "backend": "OpenCV cv2.FaceDetectorYN (ONNX)",
        "allow": ["face_detection_yunet_2023mar.onnx", "LICENSE"],
        "expect_sha256": {
            "face_detection_yunet_2023mar.onnx": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        },
        "cross_check": {
            "repo": "https://github.com/opencv/opencv_zoo",
            "commit": "48b10fa505aeb51fbd127fc60a5c660040e823b7",
            "path": "models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
            "lfs_oid_sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        },
        "default": True,
    },
    "sface": {
        "kind": "hf",
        "repo": "opencv/face_recognition_sface",
        "revision": "3d7082438a6e4551e840c9b2bb60b71e8da4b524",
        "license": "Apache-2.0",
        "backend": "OpenCV cv2.FaceRecognizerSF (ONNX)",
        "allow": ["face_recognition_sface_2021dec.onnx", "LICENSE"],
        "expect_sha256": {
            "face_recognition_sface_2021dec.onnx": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        },
        "cross_check": {
            "repo": "https://github.com/opencv/opencv_zoo",
            "commit": "48b10fa505aeb51fbd127fc60a5c660040e823b7",
            "path": "models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
            "lfs_oid_sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        },
        "default": True,
    },
    # faster-whisper ASR (hydra/genome/aud.py). model.bin is a CTranslate2 flat tensor file (no pickle, no code),
    # the only weight format faster-whisper loads; it is allowed by name only for this key.
    "faster-whisper-small": {
        "kind": "hf",
        "repo": "Systran/faster-whisper-small",
        "revision": "536b0662742c02347bc0e980a01041f333bce120",
        "license": "MIT",
        "backend": "faster-whisper / CTranslate2 (CPU int8)",
        "allow": ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"],
        "allow_bin": ["model.bin"],
        "default": True,
    },
    # MediaPipe Face Landmarker bundle (TFLite inside a ZIP .task file), used by vid_avsync and prov.
    # Google publishes no git revision for it: the versioned URL path (float16/1) + sha256 pin it.
    "mediapipe-face-landmarker": {
        "kind": "url",
        "source": "https://storage.googleapis.com/mediapipe-models/face_landmarker",
        "revision": "float16/1",
        "pinned_by": "versioned URL path float16/1 + sha256 (same value as hydra/genome/vid_avsync.LANDMARKER_SHA256)",
        "license": "Apache-2.0 (MediaPipe model card: Face Landmarker)",
        "backend": "mediapipe.tasks FaceLandmarker (TFLite, CPU)",
        "files": [
            {
                "name": "face_landmarker.task",
                "url": "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/"
                "face_landmarker.task",
                "sha256": "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
            },
        ],
        "default": True,
    },
    # AudioSeal watermark detector (hydra/genome/dfk.py, vendored code in hydra/vendor/audioseal). Upstream ships
    # only a pickle checkpoint (.pth holding an omegaconf config). It is downloaded at the pinned HF commit,
    # sha256-checked, read with the allow-list unpickler of hydra.vendor.audioseal (no callable outside torch tensor
    # rebuilding is ever resolved), weight-norm folded in float64 and written as safetensors; the .pth is deleted.
    # The runtime loads only the safetensors file, whose sha256 is pinned too (conversion is deterministic).
    "audioseal-detector": {
        "kind": "url_convert",
        "source": "https://huggingface.co/facebook/audioseal",
        "revision": "3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba",
        "pinned_by": "HF commit 3c19eba5 + sha256 of detector_base.pth and of the converted safetensors "
        "(same values as hydra/genome/dfk.AS_SRC_SHA256 / AS_SHA256)",
        "license": "MIT (code and weights, facebook/audioseal model card; training data VoxPopuli, CC0)",
        "backend": "hydra.vendor.audioseal detector (PyTorch, safetensors, CPU)",
        "src": {
            "name": "detector_base.pth",
            "url": "https://huggingface.co/facebook/audioseal/resolve/3c19eba53390776cf2cc9ed5f6c9ac67ce72ecba/"
            "detector_base.pth",
            "sha256": "8a78e8a83584113523e161fc599fcab10fd0e94c04d2eb9d2fa1e9ec91ab69d9",
            "size": 34667641,
        },
        "files": [
            {
                "name": "detector_base.safetensors",
                "sha256": "449efba12e01c194ac526c0c7dfd062bfe9fe5a6799273d13d78e174926cf1f9",
            },
        ],
        "default": True,
    },
    # RapidOCR normally fetches these from ModelScope on first use. URLs + sha256 are copied from
    # rapidocr==3.10.0 default_models.yaml (onnxruntime.PP-OCRv5.*), tag v3.10.0 of RapidAI/RapidOCR.
    "rapidocr": {
        "kind": "url",
        "source": "https://www.modelscope.cn/models/RapidAI/RapidOCR",
        "revision": "v3.10.0",
        "pinned_by": "rapidocr==3.10.0 default_models.yaml: onnxruntime.PP-OCRv5 "
        "(det mobile, cls mobile, rec latin mobile)",
        "license": "Apache-2.0 (PaddleOCR PP-OCRv5 weights, ONNX conversion by RapidAI)",
        "backend": "RapidOCR 3.10.0 + onnxruntime (CPU)",
        "files": [
            {
                "name": "ch_PP-OCRv5_det_mobile.onnx",
                "url": f"{MS_ONNX}/PP-OCRv5/det/ch_PP-OCRv5_det_mobile.onnx",
                "sha256": "4d97c44a20d30a81aad087d6a396b08f786c4635742afc391f6621f5c6ae78ae",
            },
            {
                "name": "ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx",
                "url": f"{MS_ONNX}/PP-OCRv5/cls/ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx",
                "sha256": "54379ae5174d026780215fc748a7f31910dee36818e63d49e17dc598ecc82df7",
            },
            {
                "name": "latin_PP-OCRv5_rec_mobile.onnx",
                "url": f"{MS_ONNX}/PP-OCRv5/rec/latin_PP-OCRv5_rec_mobile.onnx",
                "sha256": "b20bd37c168a570f583afbc8cd7925603890efbcdc000a59e22c269d160b5f5a",
            },
        ],
        "default": True,
    },
    # rapidocr 3.10.0's own default set (multilingual incl. pl). The wheel bundles the same bytes;
    # a pinned copy here keeps the registry independent of the site-packages layout.
    "rapidocr-v6": {
        "kind": "url",
        "source": "https://www.modelscope.cn/models/RapidAI/RapidOCR",
        "revision": "v3.10.0",
        "pinned_by": "rapidocr==3.10.0 default_models.yaml: onnxruntime.PP-OCRv6 det/rec small "
        "+ PP-OCRv4 cls mobile (identical to the files bundled in the wheel)",
        "license": "Apache-2.0 (PaddleOCR PP-OCRv6/v2.0 weights, ONNX conversion by RapidAI)",
        "backend": "RapidOCR 3.10.0 + onnxruntime (CPU)",
        "files": [
            {
                "name": "PP-OCRv6_det_small.onnx",
                "url": f"{MS_ONNX}/PP-OCRv6/det/PP-OCRv6_det_small.onnx",
                "sha256": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
            },
            {
                "name": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                "url": f"{MS_ONNX}/PP-OCRv4/cls/ch_ppocr_mobile_v2.0_cls_mobile.onnx",
                "sha256": "e47acedf663230f8863ff1ab0e64dd2d82b838fceb5957146dab185a89d6215c",
            },
            {
                "name": "PP-OCRv6_rec_small.onnx",
                "url": f"{MS_ONNX}/PP-OCRv6/rec/PP-OCRv6_rec_small.onnx",
                "sha256": "6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884",
            },
        ],
        "default": True,
    },
}

EXTERNAL: dict[str, dict[str, Any]] = {
    # Living list, cannot be pinned: one timestamped snapshot is kept and reused (idempotent);
    # pass --refresh-external to take a new one.
    "cert-domains": {
        "url": "https://hole.cert.pl/domains/v2/domains.json",
        "license": "CERT Polska warning list: free for any use, attribution not required; "
        "must not imply endorsement by CERT Polska",
        "dir": "cert",
    },
    "psl": {
        "url": "https://raw.githubusercontent.com/publicsuffix/list/"
        "d7d7f1fd55166aaf73ad2f526e72634490acd9c8/public_suffix_list.dat",
        "revision": "d7d7f1fd55166aaf73ad2f526e72634490acd9c8",
        "sha256": "3084c6ca4ac1dfb6e32461504b5dc494cc58ace521957e9e978ed0f870b2de94",
        "source": "https://github.com/publicsuffix/list",
        "license": "MPL-2.0",
        "dir": "psl",
    },
}

# tldextract offline cache: TLDExtract(cache_dir=$HYDRA_CACHE_DIR/tldextract, suffix_list_urls=())
# looks up namespace "publicsuffix.org-tlds" keyed by {urls: (), fallback_to_snapshot: <bool>}.
TLDEXTRACT_SUBDIR = "tldextract"
TLDEXTRACT_NAMESPACE = "publicsuffix.org-tlds"


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------
def today() -> str:
    return dt.date.today().isoformat()


def sha256_file(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def git_blob_sha1(path: Path) -> str:
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def file_record(root: Path, path: Path) -> dict[str, Any]:
    return {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size, "sha256": sha256_file(path)}


def list_files(directory: Path) -> list[Path]:
    """All regular files under directory, excluding huggingface_hub's local_dir metadata."""
    out = []
    for p in sorted(directory.rglob("*")):
        rel = p.relative_to(directory).as_posix()
        if p.is_file() and not rel.startswith(".cache/") and not p.name.endswith(".lock"):
            out.append(p)
    return out


def assert_real_onnx(path: Path) -> None:
    head = path.read_bytes()[:64]
    if head.startswith(b"version https://git-lfs") or path.stat().st_size < 4096:
        raise RuntimeError(f"{path} is a git-lfs pointer / stub, not an ONNX model")
    if head[:1] != b"\x08":  # ModelProto field 1 (ir_version, varint) always comes first
        raise RuntimeError(f"{path} does not look like an ONNX ModelProto")


def http_download(url: str, dest: Path, expect_sha256: str | None = None) -> dict[str, str]:
    """Stream url -> dest atomically; verify sha256 when given. Returns selected headers."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        total = int(headers.get("content-length", 0) or 0)
        done = 0
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            h.update(chunk)
            done += len(chunk)
        if total and done != total:
            raise RuntimeError(f"short read for {url}: {done} != {total}")
    digest = h.hexdigest()
    if expect_sha256 and digest != expect_sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"sha256 mismatch for {url}: got {digest}, expected {expect_sha256}")
    tmp.replace(dest)
    print(f"    fetched {dest.name}: {human(done)} in {time.monotonic() - t0:.1f}s")
    return {k: headers[k] for k in ("last-modified", "etag", "content-length") if k in headers}


def load_lock(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text())
    return {}


def write_lock(lock: dict[str, Any], paths: list[Path]) -> None:
    text = json.dumps(lock, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    for p in paths:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(text)
            tmp.replace(p)
        except OSError as exc:  # e.g. the read-only /app copy inside the container: the mirror still gets it
            print(f"lock not written to {p}: {type(exc).__name__}")
            continue
        print(f"lock written: {p}  (sha256 {hashlib.sha256(text.encode()).hexdigest()[:16]})")


def keep_date(old: dict[str, Any] | None, files: list[dict[str, Any]], field: str = "downloaded") -> str:
    """Re-use the earlier date when the bytes did not change (keeps the lock reproducible)."""
    if old and old.get("files") == files and old.get(field):
        return old[field]
    return today()


# --------------------------------------------------------------------------------------------
# download steps
# --------------------------------------------------------------------------------------------
def fetch_hf(key: str, spec: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    from huggingface_hub import HfApi, snapshot_download

    target = MODELS_DIR / key
    print(f"[{key}] {spec['repo']}@{spec['revision'][:12]} -> {target}")
    allow_bin = set(spec.get("allow_bin", ()))
    snapshot_download(
        repo_id=spec["repo"],
        revision=spec["revision"],
        local_dir=target,
        allow_patterns=spec["allow"],
        ignore_patterns=[p for p in HF_IGNORE if p != "*.bin"] if allow_bin else HF_IGNORE,
    )
    files = list_files(target)
    bad = [f.name for f in files if f.suffix in FORBIDDEN_SUFFIXES and f.name not in allow_bin]
    if bad:
        raise RuntimeError(f"[{key}] pickle-style weights present, refusing: {bad}")
    cfg = target / "config.json"
    if cfg.exists() and "auto_map" in json.loads(cfg.read_text()):
        raise RuntimeError(f"[{key}] config.json has auto_map (remote code) - not allowed")

    # Cross-check every file against the Hub's tree metadata at the pinned revision:
    # LFS files by sha256, small git files by git blob sha1.
    info = HfApi().model_info(spec["repo"], revision=spec["revision"], files_metadata=True)
    remote = {s.rfilename: s for s in info.siblings}
    records = []
    for f in files:
        rec = file_record(target, f)
        sib = remote.get(rec["path"])
        if sib is None:
            raise RuntimeError(f"[{key}] {rec['path']} not in {spec['repo']}@{spec['revision']}")
        if sib.lfs is not None:
            if sib.lfs.sha256 != rec["sha256"]:
                raise RuntimeError(f"[{key}] {rec['path']} sha256 != Hub LFS oid")
        elif sib.blob_id and git_blob_sha1(f) != sib.blob_id:
            raise RuntimeError(f"[{key}] {rec['path']} git blob sha1 != Hub blob id")
        want = spec.get("expect_sha256", {}).get(rec["path"])
        if want and want != rec["sha256"]:
            raise RuntimeError(f"[{key}] {rec['path']} sha256 != pinned {want}")
        if f.suffix == ".onnx":
            assert_real_onnx(f)
        records.append(rec)
    missing = [p for p in spec["allow"] if not (target / p).exists()]
    if missing:
        raise RuntimeError(f"[{key}] expected files missing after download: {missing}")

    entry: dict[str, Any] = {
        "kind": "model",
        "source": "huggingface",
        "repo": spec["repo"],
        "url": f"https://huggingface.co/{spec['repo']}/tree/{spec['revision']}",
        "revision": spec["revision"],
        "license": spec["license"],
        "backend": spec["backend"],
        "root": "models",
        "dir": key,
    }
    if "cross_check" in spec:
        entry["cross_check"] = spec["cross_check"]
    entry["files"] = records
    entry["total_size"] = sum(r["size"] for r in records)
    entry["downloaded"] = keep_date(old, records)
    print(f"    ok: {len(records)} files, {human(entry['total_size'])}")
    return entry


def fetch_url_model(key: str, spec: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    target = MODELS_DIR / key
    print(f"[{key}] {spec['source']}@{spec['revision']} -> {target}")
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for f in spec["files"]:
        dest = target / f["name"]
        if dest.exists() and sha256_file(dest) == f["sha256"]:
            print(f"    present {f['name']}")
        else:
            http_download(f["url"], dest, f["sha256"])
        if dest.suffix == ".onnx":
            assert_real_onnx(dest)
        rec = file_record(target, dest)
        rec["url"] = f["url"]
        records.append(rec)
    entry = {
        "kind": "model",
        "source": spec["source"],
        "url": spec["source"],
        "revision": spec["revision"],
        "pinned_by": spec["pinned_by"],
        "license": spec["license"],
        "backend": spec["backend"],
        "root": "models",
        "dir": key,
        "files": records,
        "total_size": sum(r["size"] for r in records),
    }
    entry["downloaded"] = keep_date(old, records)
    print(f"    ok: {len(records)} files, {human(entry['total_size'])}")
    return entry


def fetch_url_convert(key: str, spec: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    """Pinned pickle checkpoint -> converted safetensors (see the audioseal-detector registry comment)."""
    target = MODELS_DIR / key
    print(f"[{key}] {spec['source']}@{spec['revision'][:12]} -> {target}")
    target.mkdir(parents=True, exist_ok=True)
    out = spec["files"][0]
    dest = target / out["name"]
    if dest.exists() and sha256_file(dest) == out["sha256"]:
        print(f"    present {out['name']}")
    else:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from hydra.vendor.audioseal import convert_checkpoint

        src = MODELS_DIR / f".{key}-{spec['src']['name']}.part"
        try:
            http_download(spec["src"]["url"], src, spec["src"]["sha256"])
            convert_checkpoint(src, dest)
        finally:
            src.unlink(missing_ok=True)
        got = sha256_file(dest)
        if got != out["sha256"]:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"[{key}] converted file sha256 {got} != pinned {out['sha256']}")
    rec = file_record(target, dest)
    entry = {
        "kind": "model",
        "source": spec["source"],
        "url": f"{spec['source']}/tree/{spec['revision']}",
        "revision": spec["revision"],
        "pinned_by": spec["pinned_by"],
        "license": spec["license"],
        "backend": spec["backend"],
        "root": "models",
        "dir": key,
        "derived_from": {k: spec["src"][k] for k in ("name", "url", "sha256", "size")},
        "files": [rec],
        "total_size": rec["size"],
    }
    entry["downloaded"] = keep_date(old, [rec])
    print(f"    ok: {human(rec['size'])} (converted from {spec['src']['name']})")
    return entry


def fetch_cert(spec: dict[str, Any], refresh: bool) -> dict[str, Any]:
    d = EXTERNAL_DIR / spec["dir"]
    d.mkdir(parents=True, exist_ok=True)
    latest, meta_path = d / "domains.json", d / "SNAPSHOT.json"
    print(f"[cert-domains] {spec['url']} -> {d}")
    if refresh or not (latest.exists() and meta_path.exists()):
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        snap = d / f"domains_{stamp}.json"
        headers = http_download(spec["url"], snap)
        data = json.loads(snap.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data or "DomainAddress" not in data[0]:
            raise RuntimeError("unexpected CERT list format")
        shutil.copyfile(snap, latest)
        digest = sha256_file(snap)
        with (d / "SHA256SUMS").open("a") as fh:
            fh.write(f"{digest}  {snap.name}\n")
        meta = {
            "url": spec["url"],
            "retrieved_at": dt.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.UTC).isoformat(),
            "snapshot": snap.name,
            "sha256": digest,
            "size": snap.stat().st_size,
            "entries": len(data),
            "active_entries": sum(1 for x in data if not x.get("DeleteDate")),
            "http_last_modified": headers.get("last-modified"),
            "http_etag": headers.get("etag"),
        }
        meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    else:
        print("    snapshot present (use --refresh-external for a new one)")
    meta = json.loads(meta_path.read_text())
    if sha256_file(latest) != meta["sha256"]:
        raise RuntimeError("domains.json does not match SNAPSHOT.json sha256")
    files = [file_record(d, p) for p in (latest, d / meta["snapshot"], meta_path)]
    print(
        f"    ok: {meta['entries']} entries ({meta['active_entries']} active), "
        f"{human(meta['size'])}, retrieved {meta['retrieved_at']}"
    )
    return {
        "kind": "data",
        "source": "CERT Polska",
        "url": spec["url"],
        "license": spec["license"],
        "revision": f"snapshot {meta['retrieved_at']}",
        "retrieved_at": meta["retrieved_at"],
        "http_last_modified": meta["http_last_modified"],
        "entries": meta["entries"],
        "active_entries": meta["active_entries"],
        "root": "external",
        "dir": spec["dir"],
        "files": files,
        "total_size": sum(f["size"] for f in files),
        "downloaded": meta["retrieved_at"][:10],
    }


def seed_tldextract_cache(psl_text: str) -> list[Path]:
    """Store the parsed pinned PSL where TLDExtract(suffix_list_urls=()) looks it up."""
    from tldextract.cache import DiskCache
    from tldextract.suffix_list import extract_tlds_from_suffix_list

    public, private = extract_tlds_from_suffix_list(psl_text)
    cache = DiskCache(str(CACHE_DIR / TLDEXTRACT_SUBDIR))
    paths = []
    for fallback in (True, False):  # cover both TLDExtract(fallback_to_snapshot=...) settings
        key = {"urls": (), "fallback_to_snapshot": fallback}
        cache.set(namespace=TLDEXTRACT_NAMESPACE, key=key, value=[public, private])
        paths.append(Path(cache._key_to_cachefile_path(TLDEXTRACT_NAMESPACE, key)))
    return paths


def fetch_psl(spec: dict[str, Any], old: dict[str, Any] | None) -> dict[str, Any]:
    import importlib.metadata as md

    d = EXTERNAL_DIR / spec["dir"]
    dest = d / "public_suffix_list.dat"
    print(f"[psl] {spec['source']}@{spec['revision'][:12]} -> {dest}")
    if dest.exists() and sha256_file(dest) == spec["sha256"]:
        print("    present")
    else:
        http_download(spec["url"], dest, spec["sha256"])
    text = dest.read_text(encoding="utf-8")
    if "// ===BEGIN PRIVATE DOMAINS===" not in text:
        raise RuntimeError("downloaded file is not the Public Suffix List")
    digest = sha256_file(dest)
    (d / "SHA256SUMS").write_text(f"{digest}  {dest.name}\n")
    cache_files = seed_tldextract_cache(text)
    files = [file_record(d, dest)]
    derived = [file_record(CACHE_DIR, p) for p in cache_files]
    print(f"    ok: {human(files[0]['size'])}; tldextract cache seeded in {CACHE_DIR / TLDEXTRACT_SUBDIR}")
    return {
        "kind": "data",
        "source": spec["source"],
        "url": spec["url"],
        "revision": spec["revision"],
        "license": spec["license"],
        "root": "external",
        "dir": spec["dir"],
        "files": files,
        "total_size": files[0]["size"],
        "derived": {
            "root": "cache",
            "tldextract_version": md.version("tldextract"),
            "usage": "TLDExtract(cache_dir=$HYDRA_CACHE_DIR/tldextract, suffix_list_urls=())",
            "files": derived,
        },
        "downloaded": keep_date(old, files),
    }


def _on_disk(entry: dict[str, Any]) -> bool:
    base = ROOTS[entry["root"]] / entry["dir"]
    return all((base / f["path"]).is_file() for f in entry["files"])


def run_download(args: argparse.Namespace) -> int:
    if os.environ.get("HF_HUB_OFFLINE") == "1":
        print("HF_HUB_OFFLINE=1 - downloads need:  HYDRA_OFFLINE=0 . scripts/env.sh", file=sys.stderr)
        return 2
    lock_path = Path(args.lock)
    lock = load_lock(lock_path) or load_lock(LOCK_MIRROR)
    entries: dict[str, Any] = dict(lock.get("entries", {}))
    keys = args.only or [k for k, s in REGISTRY.items() if s["default"] or (k == "e5-base" and args.with_base)] + (
        [] if args.skip_external else list(EXTERNAL)
    )
    failed = []
    for key in keys:
        try:
            if key in REGISTRY:
                spec = REGISTRY[key]
                fn = {"hf": fetch_hf, "url_convert": fetch_url_convert}.get(spec["kind"], fetch_url_model)
                entries[key] = fn(key, spec, entries.get(key))
            elif key == "cert-domains":
                entries[key] = fetch_cert(EXTERNAL[key], args.refresh_external)
            elif key == "psl":
                entries[key] = fetch_psl(EXTERNAL[key], entries.get(key))
            else:
                raise KeyError(f"unknown key {key!r}; known: {[*REGISTRY, *EXTERNAL]}")
        except Exception as exc:  # keep going, report at the end
            failed.append(key)
            print(f"    FAILED {key}: {type(exc).__name__}: {exc}", file=sys.stderr)
    order = [*REGISTRY, *EXTERNAL]
    lock = {
        "schema": 1,
        "generator": "scripts/fetch_models.py",
        "roots": ROOT_LABELS,
        "policy": "pinned revisions; safetensors/ONNX/TFLite/CTranslate2/config/tokenizer only; no pickle weights; "
        "no trust_remote_code",
        # keep earlier entries only while their files are on this disk (e.g. e5-base without --with-base is dropped)
        "entries": {k: entries[k] for k in order if k in entries and (k in keys or _on_disk(entries[k]))},
    }
    lock["total_size"] = sum(e["total_size"] for e in lock["entries"].values())
    write_lock(lock, [lock_path] + ([LOCK_MIRROR] if LOCK_MIRROR != lock_path else []))
    if failed:
        print(f"FAILED: {failed}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------------------------
# verify: hashes + isolated offline smoke tests
# --------------------------------------------------------------------------------------------
def verify_hashes(lock: dict[str, Any]) -> dict[str, list[str]]:
    problems: dict[str, list[str]] = {}
    for key, e in lock["entries"].items():
        errs: list[str] = []
        # entry files are relative to <root>/<dir>; derived files to <root> itself
        groups = [(ROOTS[e["root"]] / e["dir"], e["files"])]
        if "derived" in e:
            groups.append((ROOTS[e["derived"]["root"]], e["derived"]["files"]))
        for base, files in groups:
            for f in files:
                p = base / f["path"]
                if not p.exists():
                    errs.append(f"missing {p}")
                elif p.stat().st_size != f["size"] or sha256_file(p) != f["sha256"]:
                    errs.append(f"sha256/size mismatch {p}")
        if e["kind"] == "model":
            listed = {f["path"] for f in e["files"]}
            extra = [p.relative_to(MODELS_DIR / e["dir"]).as_posix() for p in list_files(MODELS_DIR / e["dir"])]
            extra = [x for x in extra if x not in listed]
            if extra:
                errs.append(f"unlisted files: {extra}")
        problems[key] = errs
    return problems


def _install_socket_guard() -> list[str]:
    """Make any non-local network use raise inside the smoke-test process."""
    import socket

    attempts: list[str] = []
    local = {"localhost", "127.0.0.1", "::1"}
    orig_connect, orig_gai = socket.socket.connect, socket.getaddrinfo

    def connect(self, address):  # type: ignore[no-untyped-def]
        if self.family == getattr(socket, "AF_UNIX", None) or (
            isinstance(address, tuple) and address and address[0] in local
        ):
            return orig_connect(self, address)
        attempts.append(repr(address))
        raise OSError(f"network blocked by offline smoke test: {address!r}")

    def getaddrinfo(host, *a, **kw):  # type: ignore[no-untyped-def]
        if host in local or host is None:
            return orig_gai(host, *a, **kw)
        attempts.append(str(host))
        raise socket.gaierror(f"network blocked by offline smoke test: {host!r}")

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect  # type: ignore[method-assign,assignment]
    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]
    return attempts


def rapidocr_params(model_dir: Path, variant: str = "rapidocr") -> dict[str, Any]:
    """Exact RapidOCR(params=...) that loads only local files (model_path set => no download).

    The enum-typed keys (engine_type, ocr_version, model_type) must be Enums, not strings.
    Both rec ONNX files embed their character list (metadata "character"): no rec_keys_path.
    """
    from rapidocr import EngineType, LangRec, ModelType, OCRVersion

    v4, v5, v6 = OCRVersion.PPOCRV4, OCRVersion.PPOCRV5, OCRVersion.PPOCRV6
    if variant == "rapidocr":  # PP-OCRv5: det mobile + PP-LCNet textline cls + latin rec
        parts = {
            "Det": (v5, ModelType.MOBILE, "ch_PP-OCRv5_det_mobile.onnx"),
            "Cls": (v5, ModelType.MOBILE, "ch_PP-LCNet_x0_25_textline_ori_cls_mobile.onnx"),
            "Rec": (v5, ModelType.MOBILE, "latin_PP-OCRv5_rec_mobile.onnx"),
        }
    elif variant == "rapidocr-v6":  # rapidocr 3.10.0 defaults: v6 det/rec small + v2.0 cls
        parts = {
            "Det": (v6, ModelType.SMALL, "PP-OCRv6_det_small.onnx"),
            "Cls": (v4, ModelType.MOBILE, "ch_ppocr_mobile_v2.0_cls_mobile.onnx"),
            "Rec": (v6, ModelType.SMALL, "PP-OCRv6_rec_small.onnx"),
        }
    else:
        raise KeyError(variant)
    # Global.model_root_dir: nothing is ever written into site-packages
    params: dict[str, Any] = {"Global.model_root_dir": str(model_dir)}
    for sec, (ver, mtype, fname) in parts.items():
        params[f"{sec}.engine_type"] = EngineType.ONNXRUNTIME
        params[f"{sec}.ocr_version"] = ver  # Cls: also selects the cls input shape
        params[f"{sec}.model_type"] = mtype
        params[f"{sec}.model_path"] = str(model_dir / fname)
    if variant == "rapidocr":
        params["Rec.lang_type"] = LangRec.LATIN
    return params


FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
]


def _smoke(key: str) -> dict[str, Any]:  # runs in a child process
    attempts = _install_socket_guard()
    import numpy as np

    res: dict[str, Any] = {}
    t0 = time.perf_counter()
    m = MODELS_DIR / key
    if key == "dinov2-small":
        import torch
        from PIL import Image
        from transformers import AutoImageProcessor, AutoModel

        torch.set_num_threads(min(8, os.cpu_count() or 1))
        proc = AutoImageProcessor.from_pretrained(m, local_files_only=True)
        model = AutoModel.from_pretrained(m, local_files_only=True, use_safetensors=True).eval()
        yy, xx = np.mgrid[0:256, 0:320]
        arr = np.stack([xx % 256, yy, (xx + yy) % 256], -1).astype(np.uint8)
        img = Image.fromarray(arr)
        with torch.inference_mode():
            model(**proc(images=img, return_tensors="pt"))
            t1 = time.perf_counter()
            emb = model(**proc(images=img, return_tensors="pt")).pooler_output
        t2 = time.perf_counter()
        assert tuple(emb.shape) == (1, 384), emb.shape
        res["detail"] = f"pooler_output {tuple(emb.shape)}"
    elif key in ("e5-small", "e5-base"):
        import torch
        from sentence_transformers import SentenceTransformer

        torch.set_num_threads(min(8, os.cpu_count() or 1))
        dim = 384 if key == "e5-small" else 768
        model = SentenceTransformer(str(m), device="cpu", local_files_only=True, trust_remote_code=False)
        texts = [
            "query: jak rozpoznać fałszywy sklep internetowy?",
            "passage: Fałszywe sklepy internetowe często podszywają się pod znane marki.",
        ]
        model.encode(texts, normalize_embeddings=True)
        t1 = time.perf_counter()
        emb = model.encode(texts, normalize_embeddings=True)
        t2 = time.perf_counter()
        assert emb.shape == (2, dim), emb.shape
        res["detail"] = f"emb {emb.shape}, cos(q,p)={float(emb[0] @ emb[1]):.3f}"
    elif key == "yunet":
        import cv2

        det = cv2.FaceDetectorYN.create(str(m / "face_detection_yunet_2023mar.onnx"), "", (320, 320), 0.6, 0.3, 50)
        img = np.full((320, 320, 3), 200, np.uint8)
        cv2.ellipse(img, (160, 170), (80, 105), 0, 0, 360, (150, 180, 220), -1)
        for x in (125, 195):
            cv2.circle(img, (x, 140), 10, (40, 40, 40), -1)
        cv2.ellipse(img, (160, 220), (30, 12), 0, 0, 180, (60, 60, 160), 4)
        det.detect(img)
        t1 = time.perf_counter()
        _, faces = det.detect(img)
        t2 = time.perf_counter()
        res["detail"] = f"detect ok, faces={0 if faces is None else len(faces)} (synthetic image)"
    elif key == "sface":
        import cv2

        rec = cv2.FaceRecognizerSF.create(str(m / "face_recognition_sface_2021dec.onnx"), "")
        crop = np.random.default_rng(0).integers(0, 255, (112, 112, 3), dtype=np.uint8)
        rec.feature(crop)
        t1 = time.perf_counter()
        feat = rec.feature(crop)
        t2 = time.perf_counter()
        assert feat.shape == (1, 128), feat.shape
        res["detail"] = f"feature {feat.shape}"
    elif key in ("rapidocr", "rapidocr-v6"):
        import difflib

        from PIL import Image, ImageDraw, ImageFont
        from rapidocr import RapidOCR

        # Pillow's bundled font has no Polish glyphs, so a system TTF is required here.
        text = "Zażółć gęślą jaźń 2026"
        font_path = next((f for f in FONT_CANDIDATES if Path(f).exists()), None)
        font = ImageFont.truetype(font_path, 48) if font_path else ImageFont.load_default(size=48)
        img = Image.new("RGB", (int(font.getlength(text)) + 80, 128), "white")
        ImageDraw.Draw(img).text((40, 40), text, font=font, fill="black")
        arr = np.asarray(img)[:, :, ::-1].copy()
        engine = RapidOCR(params=rapidocr_params(m, key) | {"Global.log_level": "warning"})
        engine(arr)
        t1 = time.perf_counter()
        out = engine(arr)
        t2 = time.perf_counter()
        got = " ".join(out.txts or ())
        ratio = difflib.SequenceMatcher(None, got, text).ratio()
        res["detail"] = f"read {got!r} (match {ratio:.2f}, font {Path(font_path or 'pil-default').name})"
        assert got and ratio >= 0.5, res["detail"]
    elif key == "faster-whisper-small":
        from faster_whisper import WhisperModel

        model = WhisperModel(str(m), device="cpu", compute_type="int8", cpu_threads=4)
        sr = 16000
        tt = np.arange(sr * 2) / sr
        audio = (0.1 * np.sin(2 * np.pi * 220 * tt)).astype(np.float32)
        t1 = time.perf_counter()
        segs, info = model.transcribe(audio, language="pl", beam_size=1)
        n = len(list(segs))
        t2 = time.perf_counter()
        res["detail"] = f"transcribe ok (synthetic tone, {n} segments, lang {info.language})"
    elif key == "mediapipe-face-landmarker":
        import mediapipe as mp
        from mediapipe.tasks.python import BaseOptions, vision

        opt = vision.FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path=str(m / "face_landmarker.task")),
                                           num_faces=1)
        lm = vision.FaceLandmarker.create_from_options(opt)
        img = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.full((256, 256, 3), 200, np.uint8))
        t1 = time.perf_counter()
        out = lm.detect(img)
        t2 = time.perf_counter()
        res["detail"] = f"detect ok, faces={len(out.face_landmarks)} (blank image)"
    elif key == "audioseal-detector":
        import torch

        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from hydra.vendor.audioseal import load_detector

        det = load_detector(m / "detector_base.safetensors")
        x = torch.zeros(1, 1, 16000)
        t1 = time.perf_counter()
        with torch.no_grad():
            probs, _msg = det(x)
        t2 = time.perf_counter()
        frac = float((probs[0, 1] > 0.5).float().mean())
        assert frac < 0.5, frac
        res["detail"] = f"detect ok, marked fraction on 1 s of silence {frac:.3f}"
    elif key == "cert-domains":
        p = EXTERNAL_DIR / "cert" / "domains.json"
        data = json.loads(p.read_text(encoding="utf-8"))
        t1 = time.perf_counter()
        domains = {x["DomainAddress"].lower() for x in data if not x.get("DeleteDate")}
        t2 = time.perf_counter()
        assert len(domains) > 1000, len(domains)
        res["detail"] = f"{len(data)} rows, {len(domains)} active domains"
    elif key == "psl":
        import tldextract

        ex = tldextract.TLDExtract(
            cache_dir=str(CACHE_DIR / TLDEXTRACT_SUBDIR), suffix_list_urls=(), fallback_to_snapshot=False
        )
        ex("example.com")
        t1 = time.perf_counter()
        r = ex("sub.przyklad.com.pl")
        t2 = time.perf_counter()
        got = (r.subdomain, r.domain, r.suffix)
        assert got == ("sub", "przyklad", "com.pl"), got
        res["detail"] = f"tldextract offline: {got}"
    else:
        raise KeyError(key)
    if attempts:
        raise RuntimeError(f"network access attempted: {attempts}")
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1 << 20) if sys.platform == "darwin" else rss / 1024
    res.update(load_ms=round((t1 - t0) * 1000), infer_ms=round((t2 - t1) * 1000, 1), peak_rss_mb=round(rss_mb))
    return res


def run_verify(args: argparse.Namespace) -> int:
    lock_path = Path(args.lock)
    lock = load_lock(lock_path)
    if not lock:
        print(f"no lock at {lock_path}", file=sys.stderr)
        return 2
    keys = args.only or list(lock["entries"])
    print(f"verify on {platform.node()} ({PLATFORM_TAG}); lock {lock_path}")
    t = time.perf_counter()
    problems = verify_hashes(lock)
    print(
        f"sha256 re-check of {sum(len(e['files']) for e in lock['entries'].values())} files "
        f"in {time.perf_counter() - t:.1f}s"
    )
    env = dict(
        os.environ,
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
        HYDRA_OFFLINE="1",
        TOKENIZERS_PARALLELISM="false",
    )
    rows = []
    for key in keys:
        e = lock["entries"][key]
        row: dict[str, Any] = {
            "key": key,
            "sha256": "ok" if not problems.get(key) else "FAIL",
            "hash_errors": problems.get(key, []),
        }
        if not args.no_smoke:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--_smoke", key],
                env=env,
                capture_output=True,
                text=True,
                timeout=900,
            )
            last = (proc.stdout.strip().splitlines() or [""])[-1]
            try:
                row.update(json.loads(last))
            except json.JSONDecodeError:
                row.update(status="fail", detail=(proc.stderr.strip().splitlines() or ["?"])[-1])
        else:
            row["status"] = "skipped"
        row["ok"] = row["sha256"] == "ok" and row.get("status") in ("ok", "skipped")
        row["size"] = e["total_size"]
        rows.append(row)

    hdr = f"{'key':<13} {'result':<6} {'sha256':<6} {'load_ms':>8} {'infer_ms':>9} {'peakRSS':>8}  detail"
    print("\n" + hdr + "\n" + "-" * len(hdr))
    for r in rows:
        print(
            f"{r['key']:<13} {'ok' if r['ok'] else 'FAIL':<6} {r['sha256']:<6} "
            f"{r.get('load_ms', '-')!s:>8} {r.get('infer_ms', '-')!s:>9} "
            f"{(str(r['peak_rss_mb']) + ' MB') if 'peak_rss_mb' in r else '-':>8}  {r.get('detail', '')}"
        )
        for err in r["hash_errors"]:
            print(f"{'':<13} ! {err}")
    report = {
        "host": platform.node(),
        "platform": PLATFORM_TAG,
        "date": today(),
        "lock_sha256": sha256_file(lock_path),
        "rows": rows,
    }
    out = MODELS_DIR / f"verify-{PLATFORM_TAG}.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"\nreport: {out}\nlock sha256: {report['lock_sha256']}")
    return 0 if all(r["ok"] for r in rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--verify", action="store_true", help="offline: re-hash + smoke tests")
    ap.add_argument("--with-base", action="store_true", help="also fetch e5-base (~1.1 GB)")
    ap.add_argument("--only", nargs="+", metavar="KEY", help=f"subset of {[*REGISTRY, *EXTERNAL]}")
    ap.add_argument("--skip-external", action="store_true", help="models only (no CERT/PSL)")
    ap.add_argument("--refresh-external", action="store_true", help="take a new CERT snapshot")
    ap.add_argument("--no-smoke", action="store_true", help="--verify: hashes only")
    ap.add_argument("--lock", default=str(DEFAULT_LOCK), help=f"lock path (default {DEFAULT_LOCK})")
    ap.add_argument("--_smoke", metavar="KEY", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args._smoke:
        try:
            res = _smoke(args._smoke)
            res["status"] = "ok"
        except Exception as exc:
            res = {"status": "fail", "detail": f"{type(exc).__name__}: {str(exc)[:300]}"}
        print(json.dumps(res, ensure_ascii=False))
        return 0
    return run_verify(args) if args.verify else run_download(args)


if __name__ == "__main__":
    sys.exit(main())
