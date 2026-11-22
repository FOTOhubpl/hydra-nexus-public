"""Offline reference-data bundle for a clean jury machine (JURY-READY): MISP warninglists, Public Suffix List and
the CERT Polska warning list, shipped NEXT TO the image (never in git).

    uv run python scripts/reference_data.py build --out ~/HydraNexus/deliverables   # -> <out>/*.tar.zst
    uv run python scripts/reference_data.py install hydra-reference-data-<date>.tar.zst  # -> $HYDRA_DATA_DIR/external
    uv run python scripts/reference_data.py status                                       # which sets are present
    # without Python (Linux/macOS): mkdir -p ~/HydraNexus/data/external && \
    #   tar --zstd -xf hydra-reference-data-<date>.tar.zst -C ~/HydraNexus/data/external

Contents (operator decision 2026-11-22): `misp-warninglists/<commit>/` (CC0-1.0; the X4BNet lists inside are MIT),
`psl/` (MPL-2.0, unmodified file + licence text), `cert/` (CERT Polska warning list snapshot: free for any use, no
endorsement implied). NOT included: `knf/` (may name natural persons, re-use terms unverified) and `mf_hazard/`
(re-use terms unverified); they stay available only through the optional one-time online step
(`docker compose --profile models run --rm models` / `scripts/fetch_public_warnings.py`).
Each directory carries `SOURCE.md` and `SHA256SUMS`; the bundle root carries `MANIFEST.json`. `install` verifies
every checksum after unpacking and refuses unsafe tar members (absolute paths, `..`, links, devices).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SETS = ("misp-warninglists", "psl", "cert")
EXCLUDED = {"knf": "may name natural persons; re-use terms unverified",
            "mf_hazard": "re-use terms unverified"}
MPL_CANDIDATES = ("ui/node_modules/.pnpm/lightningcss@*/node_modules/lightningcss/LICENSE",)


def data_dir() -> Path:
    return Path(os.environ.get("HYDRA_DATA_DIR") or Path.home() / "HydraNexus" / "data").expanduser()


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_sums(d: Path) -> int:
    files = sorted(p for p in d.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (d / "SHA256SUMS").write_text("".join(f"{_sha(p)}  {p.relative_to(d).as_posix()}\n" for p in files))
    return len(files)


def _mpl_text() -> str | None:
    for pat in MPL_CANDIDATES:
        for p in sorted(REPO.glob(pat)):
            t = p.read_text(encoding="utf-8", errors="replace")
            if t.lstrip().startswith("Mozilla Public License Version 2.0"):
                return t
    return None


def build(src: Path, out: Path) -> Path:
    ext = src / "external"
    stage = Path(tempfile.mkdtemp(prefix="hydra-refdata-"))
    manifest: dict = {"bundle": "hydra-reference-data", "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
                      "sets": {}, "excluded": EXCLUDED}
    try:
        # MISP warninglists: the pinned snapshot folder (no `current` symlink; the loader picks the newest snapshot)
        base = ext / "misp-warninglists"
        cur = base / "current"
        snap = cur.resolve() if cur.exists() else max((p for p in base.iterdir() if (p / "lists").is_dir()),
                                                       key=lambda p: p.stat().st_mtime)
        dst = stage / "misp-warninglists" / snap.name
        shutil.copytree(snap, dst, symlinks=False)
        (stage / "misp-warninglists" / "SOURCE.md").write_text(
            f"# MISP warninglists\n\nSource: https://github.com/MISP/misp-warninglists @ {snap.name}\n"
            "Licence: CC0-1.0 (repository README); the X4BNet datacenter/VPN lists are MIT (X4BNet/lists_vpn).\n"
            "Copied unmodified (lists/ + README.md + SNAPSHOT.json). Used offline by hydra/enrich/warninglists.py.\n",
            encoding="utf-8")
        # PSL: unmodified file + MPL-2.0 text
        (stage / "psl").mkdir()
        shutil.copy2(ext / "psl" / "public_suffix_list.dat", stage / "psl" / "public_suffix_list.dat")
        mpl = _mpl_text()
        (stage / "psl" / "LICENSE-MPL-2.0.txt").write_text(
            mpl or "Mozilla Public License, v. 2.0: https://mozilla.org/MPL/2.0/\n", encoding="utf-8")
        (stage / "psl" / "SOURCE.md").write_text(
            "# Public Suffix List\n\nSource: https://github.com/publicsuffix/list @ "
            "d7d7f1fd55166aaf73ad2f526e72634490acd9c8 (public_suffix_list.dat)\nLicence: MPL-2.0; the file is "
            "unmodified and the licence text is LICENSE-MPL-2.0.txt.\n", encoding="utf-8")
        # CERT Polska: the snapshot read by hydra/enrich/cert_pl.py (domains.json) + its SNAPSHOT.json
        (stage / "cert").mkdir()
        shutil.copy2(ext / "cert" / "domains.json", stage / "cert" / "domains.json")
        meta = json.loads((ext / "cert" / "SNAPSHOT.json").read_text())
        (stage / "cert" / "SNAPSHOT.json").write_text(json.dumps(meta, indent=1))
        (stage / "cert" / "SOURCE.md").write_text(
            f"# CERT Polska warning list (Lista ostrzeżeń)\n\nSource: {meta.get('url')}\n"
            f"Retrieved: {meta.get('retrieved_at')} (snapshot {meta.get('snapshot')}, sha256 {meta.get('sha256')})\n"
            "Terms: free for any use including commercial, attribution not required; must not imply endorsement "
            "by CERT Polska.\n", encoding="utf-8")
        for name in SETS:
            n = _write_sums(stage / name)
            manifest["sets"][name] = {"files": n, "sha256sums": hashlib.sha256(
                (stage / name / "SHA256SUMS").read_bytes()).hexdigest()}
        (stage / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
        out.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        tar_path = out / f"hydra-reference-data-{stamp}.tar"
        with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tf:
            for p in sorted(stage.iterdir()):
                tf.add(p, arcname=p.name)
        if shutil.which("zstd"):
            subprocess.run(["zstd", "-19", "-T0", "-q", "-f", "--rm", str(tar_path)], check=True)  # noqa: S603, S607
            final = tar_path.with_suffix(".tar.zst")
        else:
            import gzip

            final = tar_path.with_suffix(".tar.gz")
            with tar_path.open("rb") as fi, gzip.open(final, "wb", 9) as fo:
                shutil.copyfileobj(fi, fo)
            tar_path.unlink()
        (final.parent / (final.name + ".sha256")).write_text(f"{_sha(final)}  {final.name}\n")
        return final
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _open_tar(path: Path) -> tarfile.TarFile:
    if path.name.endswith(".zst"):
        if not shutil.which("zstd"):
            raise SystemExit("zstd is needed to unpack .tar.zst (apt install zstd / brew install zstd)")
        data = subprocess.run(["zstd", "-dc", str(path)], check=True, capture_output=True).stdout  # noqa: S603, S607
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    return tarfile.open(path, mode="r:*")


def install(bundle: Path, dest: Path) -> dict:
    side = bundle.parent / (bundle.name + ".sha256")
    if side.is_file():
        want = side.read_text().split()[0]
        if _sha(bundle) != want:
            raise SystemExit(f"bundle checksum mismatch: {bundle}")
    dest.mkdir(parents=True, exist_ok=True)
    with _open_tar(bundle) as tf:
        for m in tf.getmembers():
            parts = Path(m.name).parts
            if m.name.startswith("/") or ".." in parts or not (m.isfile() or m.isdir()):
                raise SystemExit(f"unsafe member refused: {m.name}")
            if parts and parts[0] not in (*SETS, "MANIFEST.json"):
                raise SystemExit(f"unexpected member refused: {m.name}")
        tf.extractall(dest, filter="data")  # noqa: S202 — members checked above, data filter on top
    return verify(dest)


def verify(dest: Path) -> dict:
    out: dict = {}
    for name in SETS:
        d = dest / name
        sums = d / "SHA256SUMS"
        if not sums.is_file():
            out[name] = "missing" if not d.exists() else "present (no SHA256SUMS)"
            continue
        bad = [ln.split("  ", 1)[1] for ln in sums.read_text().splitlines()
               if ln and _sha(d / ln.split("  ", 1)[1]) != ln.split("  ", 1)[0]]
        out[name] = f"ok ({len(sums.read_text().splitlines())} files)" if not bad else f"CHECKSUM MISMATCH {bad[:3]}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, default=Path.home() / "HydraNexus" / "deliverables")
    b.add_argument("--src", type=Path, default=None, help="data dir holding external/ (default HYDRA_DATA_DIR)")
    i = sub.add_parser("install")
    i.add_argument("bundle", type=Path)
    i.add_argument("--dest", type=Path, default=None, help="default $HYDRA_DATA_DIR/external")
    sub.add_parser("status")
    a = ap.parse_args()
    if a.cmd == "build":
        p = build((a.src or data_dir()).expanduser(), a.out.expanduser())
        print(f"{p} ({p.stat().st_size / 1e6:.1f} MB)")
    elif a.cmd == "install":
        res = install(a.bundle.expanduser().resolve(), (a.dest or data_dir() / "external").expanduser())
        print(json.dumps(res, indent=1))
        return 0 if all(v.startswith("ok") for v in res.values()) else 1
    else:
        sys.path.insert(0, str(REPO))
        from hydra.config import get_settings
        from hydra.jury_report import reference_status

        print(json.dumps(reference_status(get_settings()), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
