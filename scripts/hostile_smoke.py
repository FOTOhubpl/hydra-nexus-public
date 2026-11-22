"""E02 hostile-input smoke on the release candidate: real CLI (`hydra import`, `hydra analyse`), default limits.

    uv run python scripts/hostile_smoke.py [--root /tmp/hydra-hostile-XXXX] [--json out.json] [--no-analyse]

Builds a synthetic hostile corpus (no organiser data) under a fresh temp root and runs the product's own CLI on it
with a temp HYDRA_STATE_DIR / HYDRA_CACHE_DIR, offline:
  zip bomb (deflate ratio ~1000, ~300 MB declared), nested zip bomb, zip path traversal (../, absolute, ok/../../),
  zip entry symlink, folder symlink pointing outside, active SVG/HTML/PDF, PNG with a 50,000 x 50,000 IHDR,
  a real 10,000 x 10,000 PNG (100 Mpx), a file above max_file_mb (sparse), an HTML polyglot named .jpg,
  and a benign control incident.
Checks: exit codes, wall time, peak RSS of each CLI call, per-file job_items status, no file escaped the corpus,
no leftover import-* scratch, the control incident stored. Prints one JSON object; exit 1 if any check fails.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import resource
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
import zipfile
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def declared_bomb_png() -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 50_000, 50_000, 8, 2, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(b"\x00")) + _png_chunk(b"IEND", b""))


def real_large_png(side: int = 10_000) -> bytes:
    """A valid grey PNG of side x side pixels, streamed row by row (small on disk, 100 Mpx decoded)."""
    row = b"\x00" + b"\x80" * side
    comp = zlib.compressobj(9)
    body = b"".join(comp.compress(row) for _ in range(side)) + comp.flush()
    return (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
            + _png_chunk(b"IDAT", body) + _png_chunk(b"IEND", b""))


def small_png() -> bytes:
    from PIL import Image, ImageDraw

    im = Image.new("RGB", (480, 200), "white")
    ImageDraw.Draw(im).text((10, 80), "Syntetyczna reklama: inwestuj-teraz.example", fill="black")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def zip_bomb(declared_mb: int = 300) -> bytes:
    buf = io.BytesIO()
    chunk = b"\x00" * (1 << 20)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z, z.open("zeros.bin", "w") as fh:
        for _ in range(declared_mb):
            fh.write(chunk)
    return buf.getvalue()


def traversal_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in ("../escape-dotdot.txt", "/escape-absolute.txt", "ok/../../escape-nested.txt"):
            z.writestr(name, b"ESCAPED " + name.encode())
        z.writestr("good.txt", "Syntetyczny poprawny wpis: https://kontrolny.example/")
        info = zipfile.ZipInfo("link-to-etc")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    return buf.getvalue()


def build_corpus(root: Path) -> Path:
    corpus = root / "corpus"
    inc = {name: corpus / name for name in ("inc-control", "inc-zipbomb", "inc-nested", "inc-traversal",
                                            "inc-active", "inc-images", "inc-oversize", "inc-symlink")}
    for d in inc.values():
        d.mkdir(parents=True)
    (inc["inc-control"] / "url.txt").write_text("https://kontrolny.example/oferta\n")
    (inc["inc-control"] / "screenshot.png").write_bytes(small_png())
    (inc["inc-zipbomb"] / "bomb.zip").write_bytes(zip_bomb())
    (inc["inc-zipbomb"] / "url.txt").write_text("https://bomba.example/\n")
    inner = zip_bomb(250)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("inner.zip", inner)
        z.writestr("note.txt", "nested archive")
    (inc["inc-nested"] / "nested.zip").write_bytes(buf.getvalue())
    (inc["inc-traversal"] / "paths.zip").write_bytes(traversal_zip())
    (inc["inc-active"] / "active.svg").write_bytes(
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="HOSTILE_SENTINEL()"><script>HOSTILE_SENTINEL()</script>'
        b'<image href="http://198.51.100.7/beacon.png"/></svg>')
    (inc["inc-active"] / "page.html").write_bytes(
        b'<html><head><meta http-equiv="refresh" content="0;url=http://198.51.100.7/"></head><body>'
        b'<script>fetch("http://198.51.100.7/x")</script><img src="http://198.51.100.7/p.gif" onerror="x()">'
        b'<iframe src="file:///etc/passwd"></iframe>Oferta inwestycyjna</body></html>')
    (inc["inc-active"] / "doc.pdf").write_bytes(b"%PDF-1.7\n1 0 obj<</S/JavaScript/JS(app.alert(1))>>endobj\n%%EOF")
    (inc["inc-active"] / "polyglot.jpg").write_bytes(b"<html><script>HOSTILE_SENTINEL()</script></html>")
    (inc["inc-images"] / "declared-bomb.png").write_bytes(declared_bomb_png())
    (inc["inc-images"] / "real-100mpx.png").write_bytes(real_large_png())
    (inc["inc-images"] / "truncated.png").write_bytes(small_png()[:200])
    big = inc["inc-oversize"] / "big.bin"
    with big.open("wb") as fh:  # sparse, above the default max_file_mb=200
        fh.truncate(260 * 1024 * 1024)
    (inc["inc-oversize"] / "url.txt").write_text("https://duzy.example/\n")
    # second dataset: one ZIP = one incident (README input layout) next to a benign incident folder
    zipset = root / "zipset"
    (zipset / "inc-control").mkdir(parents=True)
    (zipset / "inc-control" / "url.txt").write_text("https://kontrolny-zip.example/\n")
    (zipset / "zip-bomb.zip").write_bytes(zip_bomb())
    (zipset / "zip-nested.zip").write_bytes(buf.getvalue())
    (zipset / "zip-traversal.zip").write_bytes(traversal_zip())
    outside = root / "outside-secret.txt"
    outside.write_text("must never be imported")
    (inc["inc-symlink"] / "link.txt").symlink_to(outside)
    (inc["inc-symlink"] / "url.txt").write_text("https://symlink.example/\n")
    return corpus


def run_cli(args: list[str], env: dict[str, str], log: Path, timeout: int) -> dict:
    t0 = time.monotonic()
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    try:
        proc = subprocess.run(["uv", "run", "--frozen", "hydra", *args], cwd=REPO, env=env, capture_output=True,
                              text=True, timeout=timeout)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = "timeout", exc.stdout or "", exc.stderr or ""
    secs = round(time.monotonic() - t0, 1)
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    scale = 1 if platform.system() == "Darwin" else 1024  # bytes on macOS, KiB on Linux
    log.write_text(f"$ hydra {' '.join(args)}\nexit {rc}\n--- stdout\n{out}\n--- stderr\n{err}")
    return {"args": args[:1], "exit": rc, "seconds": secs,
            "peak_rss_mb": round(max(peak, before) * scale / 2**20, 1), "log": str(log)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", type=Path, default=None)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--no-analyse", action="store_true")
    ap.add_argument("--timeout", type=int, default=900)
    a = ap.parse_args(argv)
    root = a.root or Path(tempfile.mkdtemp(prefix="hydra-hostile-"))
    root.mkdir(parents=True, exist_ok=True)
    corpus = build_corpus(root)
    state, cache = root / "state", root / "cache"
    env = {**os.environ, "HYDRA_STATE_DIR": str(state), "HYDRA_CACHE_DIR": str(cache), "HYDRA_OFFLINE": "1",
           "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HYDRA_ENRICH": "0", "HYDRA_LLM": "0",
           "HYDRA_MODELS_DIR": os.environ.get("HYDRA_MODELS_DIR",
                                              str(Path.home() / "HydraNexus" / "models")),
           "HF_HOME": str(cache / "huggingface"), "TORCH_HOME": str(cache / "torch"),
           "HYDRA_EXTRACT_WORKERS": os.environ.get("HYDRA_EXTRACT_WORKERS", "2"),
           "HYDRA_TORCH_THREADS": "2", "OMP_NUM_THREADS": "2", "MKL_NUM_THREADS": "2", "HYDRA_OCR_WORKERS": "1"}
    res: dict = {"root": str(root), "host": platform.node().split(".")[0], "platform": platform.platform(),
                 "commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True,
                                          text=True).stdout.strip(),
                 "corpus_bytes_on_disk": sum(os.stat(p, follow_symlinks=False).st_blocks * 512
                                             for p in corpus.rglob("*")),
                 "runs": []}
    res["runs"].append(run_cli(["import", str(corpus)], env, root / "import.log", a.timeout))
    zipset = root / "zipset"
    zips = sorted(str(z) for z in zipset.glob("zip-*.zip"))
    res["runs"].append(run_cli(["import", *zips], env, root / "import-zips.log", a.timeout))
    items = []
    db = state / "hydra.sqlite"
    if not db.exists():
        cands = list(state.rglob("*.sqlite"))
        db = cands[0] if cands else db
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        for n, job in enumerate(con.execute("SELECT id FROM jobs WHERE kind='import' ORDER BY rowid").fetchall()):
            items += [{"import": n + 1, **dict(r)} for r in con.execute(
                "SELECT source_ref, status, error_code, substr(coalesce(detail,''),1,120) AS detail FROM job_items "
                "WHERE job_id=? ORDER BY idx", (job["id"],))]
        con.close()
    res["import_items"] = items
    if not a.no_analyse:
        res["runs"].append(run_cli(["analyse", str(corpus)], env, root / "analyse.log", a.timeout))
        res["runs"].append(run_cli(["analyse", str(zipset)], env, root / "analyse-zipset.log", a.timeout))
    escaped = [str(p) for p in root.rglob("escape-*.txt")] + [str(p) for p in Path("/").glob("escape-*.txt")]
    scratch = [str(p) for p in state.rglob("import-*") if p.is_dir()]
    def status_of(fragment: str) -> set[str]:
        return {i["status"] for i in items if fragment in i["source_ref"]}

    checks = {
        "import_exit_0": all(r["exit"] == 0 for r in res["runs"] if r["args"] == ["import"]),
        "analyse_folders_exit_0": a.no_analyse or res["runs"][2]["exit"] == 0,
        "analyse_zip_incidents_exit_0": a.no_analyse or res["runs"][3]["exit"] == 0,
        "control_stored": status_of("inc-control") <= {"stored", "duplicate"} and bool(status_of("inc-control")),
        "zip_bomb_not_stored": "stored" not in status_of("zeros.bin") and bool(status_of("bomb.zip")
                                                                                | status_of("zeros.bin")),
        "nested_bomb_not_stored": "stored" not in status_of("inner.zip/") and "stored" not in status_of("zeros.bin"),
        "traversal_entries_not_stored": not any("escape" in i["source_ref"] and i["status"] == "stored"
                                                for i in items),
        "traversal_sibling_stored": "stored" in status_of("good.txt"),
        "zip_symlink_not_stored": "stored" not in status_of("link-to-etc"),
        "folder_symlink_not_followed": "stored" not in status_of("link.txt"),
        "oversize_not_stored": "stored" not in status_of("big.bin"),
        "nothing_escaped": not escaped,
        "no_scratch_left": not scratch,
        "every_input_has_status": all(i["status"] in {"stored", "duplicate", "quarantined", "failed", "skipped"}
                                      for i in items) and len(items) > 0,
    }
    res["escaped_files"], res["scratch_left"], res["checks"] = escaped, scratch, checks
    res["ok"] = all(checks.values())
    text = json.dumps(res, indent=1, default=str)
    if a.json:
        a.json.write_text(text)
    print(text)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
