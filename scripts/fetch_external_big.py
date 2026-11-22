#!/usr/bin/env python3
"""Fetch, pin and verify the larger EXTERNAL datasets (mentor-approved, 2026-11-21 ~16:00; docs/datasets/EXTERNAL.md).

External data stays outside the repo and apart from organiser data:
    $HYDRA_DATA_DIR/external-big/<dir>/            (default ~/HydraNexus/data/external-big)
Every file is pinned in hydra/adapters/datasets/datasets.lock.json (url, bytes, sha256, licence, verdict); the
lock is mirrored to <root>/datasets.lock.json and each dataset folder gets a SOURCE.md.

    python3 scripts/fetch_external_big.py                      # fetch missing files, verify all, write SOURCE.md
    python3 scripts/fetch_external_big.py --only phreshphish-2k,purino-scam-ads
    python3 scripts/fetch_external_big.py --verify-only        # presence + sha256 only, no network
    python3 scripts/fetch_external_big.py --pin <name>        # (maintainer) re-list an hf/zenodo entry

Idempotent: files present with the pinned sha256 are skipped. Stdlib only, no logins, no API keys. Entries whose
sha256 is null (HF non-LFS files, auto-converted parquet) are hashed on first download and the lock is updated
("pinned on first fetch"). Downloaded bytes are untrusted data: this script never imports or executes them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOCK = REPO / "hydra" / "adapters" / "datasets" / "datasets.lock.json"
UA = "hydra-external-fetch/0.1 (dataset evaluation)"


def data_root() -> Path:
    base = os.environ.get("HYDRA_DATA_DIR")
    return (Path(base) if base else Path.home() / "HydraNexus" / "data") / "external-big"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _get(url: str, timeout: int = 120):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)


def download(url: str, dst: Path, tries: int = 8) -> None:
    """Anonymous HF downloads get HTTP 429 under load: back off (Retry-After, else 15 s x attempt) and retry."""
    tmp = dst.with_name(dst.name + ".part")
    dst.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, tries + 1):
        try:
            with _get(url, timeout=300) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 22)
            tmp.replace(dst)
            return
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == tries:
                raise
            wait = e.headers.get("Retry-After") if e.headers else None
            time.sleep(min(300, float(wait) if wait and wait.isdigit() else 15 * attempt))
        except (TimeoutError, ConnectionError, urllib.error.URLError):
            if attempt == tries:
                raise
            time.sleep(10 * attempt)


# ------------------------------------------------------------------------------------------ HF pinning


def _hf_tree(repo: str, rev: str) -> list[dict]:
    url = f"https://huggingface.co/api/datasets/{repo}/tree/{rev}?recursive=true&expand=false"
    out: list[dict] = []
    while url:
        with _get(url) as r:
            out += json.load(r)
            link = r.headers.get("Link", "")
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return [x for x in out if x.get("type") == "file"]


def _hf_rev(repo: str) -> str:
    with _get(f"https://huggingface.co/api/datasets/{repo}") as r:
        return json.load(r)["sha"]


def pin_hf(ds: dict) -> None:
    src = ds["source"]
    repo = src["repo"]
    rev = src.get("revision") or _hf_rev(repo)
    inc = [re.compile(p) for p in src.get("include", [".*"])]
    files = []
    for x in _hf_tree(repo, rev):
        if not any(p.fullmatch(x["path"]) for p in inc):
            continue
        q = urllib.parse.quote(x["path"])
        files.append({"path": x["path"], "url": f"https://huggingface.co/datasets/{repo}/resolve/{rev}/{q}",
                      "bytes": x.get("size"), "sha256": (x.get("lfs") or {}).get("oid")})
    src["revision"] = rev
    ds["files"] = sorted(files, key=lambda f: f["path"])
    ds["bytes_total"] = sum(f["bytes"] or 0 for f in files)


def pin_zenodo(ds: dict) -> None:
    """Zenodo publishes md5 only: keep it, sha256 is pinned on first fetch."""
    src = ds["source"]
    with _get(f"https://zenodo.org/api/records/{src['record']}") as r:
        rec = json.load(r)
    inc = [re.compile(p) for p in src.get("include", [".*"])]
    files = [{"path": f["key"], "url": f["links"]["self"], "bytes": f["size"],
              "md5": f["checksum"].removeprefix("md5:"), "sha256": None}
             for f in rec["files"] if any(p.fullmatch(f["key"]) for p in inc)]
    src["revision"] = f"zenodo:{rec['id']} v{rec.get('revision', '')} {rec['metadata'].get('publication_date', '')}"
    ds["files"] = sorted(files, key=lambda f: f["path"])
    ds["bytes_total"] = sum(f["bytes"] for f in files)


PINNERS = {"hf": pin_hf, "zenodo": pin_zenodo}


# ------------------------------------------------------------------------------------------ fetch / verify


def source_md(ds: dict) -> str:
    lines = [f"# {ds['name']} (external, not organiser data)", "",
             f"- homepage: {ds.get('homepage', '')}", f"- licence: {ds['licence']}",
             f"- verdict: **{ds['verdict']}** ({ds.get('verdict_note', '')})",
             f"- corpus flag: `ext:{ds['name']}` (Incident.source); never mixed with `nask-sample`",
             f"- retrieved: {ds.get('retrieved', '')}", f"- citation: {ds.get('citation', '')}",
             f"- use: {ds.get('use', '')}",
             "- DO NOT commit or redistribute. Aggregates only in public material.", "",
             "| file | url | bytes | sha256 |", "|---|---|---|---|"]
    lines += [f"| {f['path']} | {f['url']} | {f['bytes']} | {f['sha256']} |" for f in ds["files"]]
    return "\n".join(lines) + "\n"


def fetch_one(d: Path, f: dict, verify_only: bool) -> tuple[str, dict]:
    p = d / f["path"]
    if not p.exists():
        if verify_only:
            return "MISS", f
        try:
            download(f["url"], p)
        except Exception as e:  # noqa: BLE001 - reported per file, the run continues
            return f"FAIL {type(e).__name__}: {e}", f
    h = sha256(p)
    if f.get("md5") and f.get("sha256") is None:
        md5 = hashlib.md5(usedforsecurity=False)
        with open(p, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                md5.update(b)
        if md5.hexdigest() != f["md5"]:
            return f"BAD md5 {md5.hexdigest()}", f
    if f.get("sha256") is None:
        f["sha256"], f["bytes"] = h, p.stat().st_size
        return "PIN", f
    return ("ok" if h == f["sha256"] else f"BAD sha256 {h}"), f


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--lock", default=str(LOCK))
    ap.add_argument("--root", default=None, help="default $HYDRA_DATA_DIR/external-big")
    ap.add_argument("--only", default="")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument("--pin", default="", help="comma list of hf/zenodo entries to re-list (needs network)")
    ap.add_argument("--jobs", type=int, default=8)
    a = ap.parse_args()
    lock_path = Path(a.lock)
    lock = json.loads(lock_path.read_text())
    root = Path(a.root) if a.root else data_root()
    only = {x for x in a.only.split(",") if x}
    changed = False
    for name in [x for x in a.pin.split(",") if x]:
        ds = next(x for x in lock["datasets"] if x["name"] == name)
        PINNERS[ds["source"]["kind"]](ds)
        changed = True
        print(f"[pin ] {name}: {len(ds['files'])} files, {ds['bytes_total'] / 1e9:.2f} GB @ {ds['source']['revision']}")
    bad = 0
    for ds in lock["datasets"]:
        if (only and ds["name"] not in only) or ds.get("fetch") is False:
            continue
        if not ds.get("files") and ds["source"].get("kind") in PINNERS and not a.verify_only:
            PINNERS[ds["source"]["kind"]](ds)
            changed = True
        d = root / ds["dir"]
        d.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=a.jobs) as ex:
            results = list(ex.map(lambda f, d=d: fetch_one(d, f, a.verify_only), ds["files"]))
        counts: dict[str, int] = {}
        for status, f in results:
            key = status.split(" ")[0]
            counts[key] = counts.get(key, 0) + 1
            if key == "PIN":
                changed = True
            if key in ("MISS", "FAIL", "BAD"):
                bad += 1
                print(f"[{key:4}] {ds['name']}/{f['path']}: {status}")
        print(f"[{'ok' if not any(k in counts for k in ('MISS', 'FAIL', 'BAD')) else 'BAD':4}] {ds['name']}: {counts}")
        if not a.verify_only:
            ds.setdefault("retrieved", dt.date.today().isoformat())
            (d / "SOURCE.md").write_text(source_md(ds))
    if changed:
        lock_path.write_text(json.dumps(lock, indent=1, ensure_ascii=False) + "\n")
    if not a.verify_only:
        root.mkdir(parents=True, exist_ok=True)
        (root / "datasets.lock.json").write_text(json.dumps(lock, indent=1, ensure_ascii=False) + "\n")
    print("FAILED" if bad else "ALL OK", f"({bad} problems)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
