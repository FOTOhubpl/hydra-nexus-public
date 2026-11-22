"""Generic end-to-end library/throughput smoke test of the skeleton. NOT a product feature.

Proves on this machine, offline, that the generic pieces work together: API upload -> import job ->
per-item final statuses -> audit hash chain -> DINOv2 / e5 embeddings of the imported artifacts ->
exact cosine top-k. Inputs are generated synthetic files (random shapes and words); there is no
task logic, no task data and no thresholds here. Prints a timing table (import s, embed img/s,
peak RSS) for docs/PROJECT_STATE.md.

    just smoke                              # TestClient (in-process ASGI)
    uv run python scripts/smoke_e2e.py --http --images 200

State lives in a temporary HYDRA_STATE_DIR that is deleted afterwards (unless --keep).
Exit code 0 only when every check passes.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import resource
import shutil
import socket
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FINAL_STATUSES = {"stored", "duplicate", "quarantined", "failed"}
WORDS = ("alfa", "beta", "gamma", "delta", "zażółć", "gęślą", "jaźń", "kot", "dom", "rzeka", "most", "las",
         "okno", "chmura", "kamień", "pociąg", "lampa", "zegar", "papier", "ogień")


# ------------------------------------------------------------------------------------------------
# synthetic input
# ------------------------------------------------------------------------------------------------
def _random_image(rng: np.random.Generator, i: int) -> bytes:
    w, h = int(rng.integers(320, 1280)), int(rng.integers(240, 1080))
    im = Image.new("RGB", (w, h), tuple(int(c) for c in rng.integers(0, 256, 3)))
    d = ImageDraw.Draw(im)
    for _ in range(int(rng.integers(5, 30))):
        x0, y0 = int(rng.integers(0, w)), int(rng.integers(0, h))
        x1, y1 = x0 + int(rng.integers(10, w // 2)), y0 + int(rng.integers(10, h // 2))
        color = tuple(int(c) for c in rng.integers(0, 256, 3))
        [d.rectangle, d.ellipse][int(rng.integers(0, 2))]((x0, y0, x1, y1), fill=color)
    font = ImageFont.load_default(size=int(rng.integers(18, 64)))
    text = " ".join(rng.choice(WORDS, size=int(rng.integers(1, 5))))
    d.text((int(rng.integers(0, w // 2)), int(rng.integers(0, h - 40))), text, fill=(255, 255, 255), font=font)
    buf = io.BytesIO()
    if i % 2:
        im.save(buf, format="JPEG", quality=int(rng.integers(60, 95)))
    else:
        im.save(buf, format="PNG")
    return buf.getvalue()


def build_zip(n_images: int, seed: int = 0) -> tuple[bytes, int]:
    """Returns (zip bytes, number of entries)."""
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    n = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(n_images):
            ext = "jpg" if i % 2 else "png"
            zf.writestr(f"images/img_{i:04d}.{ext}", _random_image(rng, i))
            n += 1
        for i in range(4):
            zf.writestr(f"text/note_{i}.txt", " ".join(rng.choice(WORDS, size=int(rng.integers(5, 25)))))
            n += 1
        for i in range(2):
            doc = {"id": i, "words": rng.choice(WORDS, size=6).tolist(), "value": float(rng.random())}
            zf.writestr(f"meta/record_{i}.json", json.dumps(doc, ensure_ascii=False))
            n += 1
        zf.writestr("bad/blob.bin", bytes(range(256)) * 16)  # not an allowed MIME -> quarantined
        n += 1
    return buf.getvalue(), n


# ------------------------------------------------------------------------------------------------
# HTTP client: in-process TestClient or a real uvicorn on a free loopback port
# ------------------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Uvicorn:
    def __init__(self, app) -> None:  # type: ignore[no-untyped-def]
        import uvicorn

        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):  # type: ignore[no-untyped-def]
        import httpx

        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", timeout=120)
        return self.client

    def __exit__(self, *exc) -> None:  # type: ignore[no-untyped-def]
        self.client.close()
        self.server.should_exit = True
        self.thread.join(timeout=10)


def _peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024  # bytes on macOS, KiB on Linux


# ------------------------------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--images", type=int, default=50)
    ap.add_argument("--http", action="store_true", help="real uvicorn on a free loopback port instead of TestClient")
    ap.add_argument("--keep", action="store_true", help="keep the temporary state dir")
    ap.add_argument("--allow-missing-models", action="store_true", help="skip embeddings when weights are absent")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="hydra-smoke-"))
    os.environ["HYDRA_STATE_DIR"] = str(tmp / "state")  # also seen by get_settings() in hydra.ml.models
    smoke_pw = "smoke-Admin-Passw0rd-2026"  # AUTH-LOGIN: product default HYDRA_AUTH=1, a known first-admin password
    os.environ["HYDRA_ADMIN_PASSWORD"] = smoke_pw

    from hydra.api import create_app
    from hydra.config import Settings, get_settings
    from hydra.db import connect
    from hydra.events import verify_chain
    from hydra.media import load_work_image
    from hydra.ml.models import ModelUnavailable, cosine_topk, load_image_embedder, load_text_embedder
    from hydra.storage import ContentStore

    get_settings.cache_clear()
    settings = Settings()
    rows: list[tuple[str, str]] = []
    failures: list[str] = []

    def check(ok: bool, what: str) -> None:
        print(f"  [{'ok' if ok else 'FAIL'}] {what}")
        if not ok:
            failures.append(what)

    try:
        t0 = time.perf_counter()
        payload, n_entries = build_zip(args.images)
        t_gen = time.perf_counter() - t0
        rows.append(("generate ZIP", f"{t_gen:.2f} s ({n_entries} entries, {len(payload) / 1e6:.1f} MB)"))

        app = create_app(settings)
        if args.http:
            ctx = _Uvicorn(app)
        else:
            from fastapi.testclient import TestClient

            ctx = TestClient(app)
        print(f"smoke: {platform.node()} {platform.system()}-{platform.machine()}, state={tmp}, "
              f"client={'uvicorn' if args.http else 'TestClient'}")

        with ctx as client:
            assert client.get("/health").json()["status"] == "ok"
            if settings.auth:
                check(client.get("/v1/jobs").status_code == 401, "API closed before login (HYDRA_AUTH=1)")
                r = client.post("/v1/auth/login", json={"username": "admin", "password": smoke_pw})
                check(r.status_code == 200, f"admin login (HTTP {r.status_code})")
                client.headers["X-CSRF-Token"] = r.json().get("csrf_token", "")
            t0 = time.perf_counter()
            r = client.post("/v1/import", files=[("files", ("smoke.zip", payload, "application/zip"))])
            check(r.status_code == 202, f"upload accepted (HTTP {r.status_code})")
            job_id = r.json()["job_id"]
            deadline = time.monotonic() + 600
            while True:
                job = client.get(f"/v1/jobs/{job_id}").json()
                if job["status"] in ("done", "failed") or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
            t_import = time.perf_counter() - t0
            rows.append(("import (upload+job)", f"{t_import:.2f} s ({n_entries / t_import:.0f} files/s)"))
            check(job["status"] == "done", f"job finished with status {job['status']!r}")
            check(job["total"] == n_entries, f"job total {job['total']} == ZIP entries {n_entries}")
            print(f"  items_by_status: {job['items_by_status']}")
            check(job["items_by_status"].get("quarantined", 0) == 1, "exactly the one bad file is quarantined")
            audit = client.get("/v1/audit/verify").json()
            check(bool(audit["ok"]), f"audit chain verifies ({audit['events']} events)")

        with connect(settings.db_path, readonly=True) as conn:
            items = conn.execute("SELECT status FROM job_items WHERE job_id=?", (job_id,)).fetchall()
            check(len(items) == n_entries and all(i["status"] in FINAL_STATUSES for i in items),
                  f"every item has a final status ({len(items)} rows)")
            arts = [dict(a) for a in conn.execute("SELECT id, mime FROM artifacts ORDER BY id")]
            ok, n_events, _ = verify_chain(conn)
            check(ok, f"audit chain re-verified from SQLite ({n_events} events)")

        store = ContentStore(settings.cas_dir)
        image_ids = [a["id"] for a in arts if a["mime"].startswith("image/")]
        text_ids = [a["id"] for a in arts if a["mime"].startswith("text/") or a["mime"] == "application/json"]
        check(len(image_ids) == args.images, f"{len(image_ids)} image artifacts")

        # --- image embeddings -------------------------------------------------------------------
        try:
            t0 = time.perf_counter()
            img_emb = load_image_embedder("dinov2-small")
            rows.append(("load DINOv2-small", f"{time.perf_counter() - t0:.2f} s ({img_emb.device.device}, "
                                              f"{img_emb.device.dtype})"))
            t0 = time.perf_counter()
            images = [load_work_image(store.path_for(i)).image for i in image_ids]
            t_decode = time.perf_counter() - t0
            img_emb.embed(images[:2])  # warm-up (kernels, autocast), excluded from the rate
            t0 = time.perf_counter()
            v_img = img_emb.embed(images)
            t_img = time.perf_counter() - t0
            rows.append(("decode work images", f"{t_decode:.2f} s"))
            rows.append(("embed images", f"{len(images) / t_img:.1f} img/s ({len(images)} in {t_img:.2f} s)"))
            check(v_img.shape == (len(images), img_emb.dim), f"image embeddings {v_img.shape}")
            t0 = time.perf_counter()
            idx, val = cosine_topk(v_img, v_img, k=5, exclude_self=True)
            rows.append(("cosine_topk images k=5", f"{(time.perf_counter() - t0) * 1000:.1f} ms"))
            check(idx.shape == (len(images), 5) and bool(np.all(idx != np.arange(len(images))[:, None])),
                  "image top-k excludes self")
        except ModelUnavailable as exc:
            rows.append(("embed images", f"unavailable: {exc}"))
            if not args.allow_missing_models:
                failures.append("dinov2-small missing")

        # --- text embeddings --------------------------------------------------------------------
        try:
            t0 = time.perf_counter()
            txt_emb = load_text_embedder("e5-small")
            rows.append(("load e5-small", f"{time.perf_counter() - t0:.2f} s"))
            texts = [store.path_for(i).read_text(encoding="utf-8", errors="replace") for i in text_ids]
            txt_emb.embed(texts[:1])  # warm-up, excluded from the rate
            t0 = time.perf_counter()
            v_txt = txt_emb.embed(texts)
            t_txt = time.perf_counter() - t0
            rows.append(("embed texts", f"{len(texts) / t_txt:.1f} texts/s ({len(texts)} in {t_txt:.2f} s)"))
            idx, _ = cosine_topk(v_txt, v_txt, k=2, exclude_self=True)
            check(v_txt.shape == (len(texts), txt_emb.dim) and idx.shape == (len(texts), 2),
                  f"text embeddings {v_txt.shape} + top-k")
        except ModelUnavailable as exc:
            rows.append(("embed texts", f"unavailable: {exc}"))
            if not args.allow_missing_models:
                failures.append("e5-small missing")

        rows.append(("peak RSS (process)", f"{_peak_rss_mb():.0f} MB"))
    finally:
        if args.keep:
            print(f"state kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    width = max(len(k) for k, _ in rows)
    print("\n" + "-" * (width + 50))
    for k, v in rows:
        print(f"{k:<{width}}  {v}")
    print("-" * (width + 50))
    print("SMOKE OK" if not failures else f"SMOKE FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
