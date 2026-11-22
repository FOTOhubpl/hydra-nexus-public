"""IMPORT-PROGRESS measurement: import wall time of a synthetic multi-file dataset ZIP with and without the progress
sink (hydra/import_progress.py), and (--stages) the per-stage seconds of one single-item analyse.

    uv run python scripts/bench_import_progress.py --files 2000 --incidents 20 --reps 3
    uv run python scripts/bench_import_progress.py --stages

Fresh temporary state per repetition (never ~/HydraNexus/state); synthetic files only (PNG + text).
"""

from __future__ import annotations

import argparse
import io
import json
import statistics
import tempfile
import threading
import time
import zipfile
from pathlib import Path

from PIL import Image


def png(i: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 24), (i % 255, (i * 7) % 255, 40)).save(buf, format="PNG")
    return buf.getvalue()


def make_zip(path: Path, files: int, incidents: int) -> None:
    per = files // incidents
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1, incidents + 1):
            zf.writestr(f"zbior/{i}/reklama.png", png(i))
            for k in range(per - 1):
                if k % 10 == 0:
                    zf.writestr(f"zbior/{i}/zasob_{k}.png", png(i * 1000 + k))
                else:
                    zf.writestr(f"zbior/{i}/notatka_{k}.txt", f"incydent {i} notatka {k} https://x{i}.invalid/{k}\n")


def settings_for(root: Path):
    from hydra.config import Settings

    s = Settings(root=root, data_dir=root / "data", models_dir=root / "models", cache_dir=root / "cache",
                 state_dir=root / "state")
    s.ensure_dirs()
    return s


def one_import(zip_path: Path, progress: bool) -> tuple[float, dict | None]:
    from hydra import import_progress
    from hydra.adapters import nask_folder
    from hydra.db import Writer
    from hydra.jury_report import list_import
    from hydra.pipeline import import_paths_as_incidents
    from hydra.storage import ContentStore

    with tempfile.TemporaryDirectory(prefix="bench-import-") as tmp:
        s = settings_for(Path(tmp))
        writer = Writer(s.db_path)
        try:
            # unpack first (the API's dataset check does it during the upload request): measure the job's import
            nask_folder.unpack_zip(zip_path, s)
            t = time.perf_counter()
            if progress:
                with import_progress.tracking(s, "bench") as sink:
                    list_import([zip_path], s)
                    with nask_folder.dataset_rules():
                        import_paths_as_incidents([zip_path], writer, ContentStore(s.cas_dir), s)
                wall = time.perf_counter() - t
                return wall, {**import_progress.read(s, "bench"), "writes": sink.writes}
            with nask_folder.dataset_rules():
                import_paths_as_incidents([zip_path], writer, ContentStore(s.cas_dir), s)
            return time.perf_counter() - t, None
        finally:
            writer.close()


def stages() -> dict:
    """Per-stage seconds of one single-item analyse (pipeline.analyse_input, kind=file) from its progress file."""
    from hydra.db import Writer
    from hydra.pipeline import analyse_input
    from hydra.progress import progress_dir

    with tempfile.TemporaryDirectory(prefix="bench-stages-") as tmp:
        s = settings_for(Path(tmp))
        inc = Path(tmp) / "inc"
        inc.mkdir()
        (inc / "reklama.png").write_bytes(png(3))
        (inc / "strona.html").write_text("<html><title>Inwestuj</title><body>Zarabiaj 5000 zł dziennie"
                                         " https://pko-bonus.invalid/</body></html>")
        writer = Writer(s.db_path)
        seen: list[tuple[float, str]] = []
        stop = threading.Event()

        def sample() -> None:
            d = progress_dir(s)
            while not stop.is_set():
                for f in d.glob("*.json"):
                    if f.name.endswith(".import.json"):
                        continue
                    try:
                        st = json.loads(f.read_text())["stage"]
                    except (OSError, ValueError, KeyError):
                        continue
                    if not seen or seen[-1][1] != st:
                        seen.append((time.perf_counter(), st))
                time.sleep(0.005)

        th = threading.Thread(target=sample, daemon=True)
        t0 = time.perf_counter()
        seen.append((t0, "import"))
        th.start()
        try:
            analyse_input("file", inc, s, writer=writer)
        finally:
            t1 = time.perf_counter()
            stop.set()
            th.join()
            writer.close()
        out: dict[str, float] = {}
        for (t, st), nxt in zip(seen, [*seen[1:], (t1, "end")], strict=True):
            out[st] = round(out.get(st, 0.0) + nxt[0] - t, 3)
        return {"wall_s": round(t1 - t0, 3), "stages_s": out}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=2000)
    ap.add_argument("--incidents", type=int, default=20)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--stages", action="store_true")
    a = ap.parse_args()
    if a.stages:
        print(json.dumps(stages(), indent=1))
        return
    with tempfile.TemporaryDirectory(prefix="bench-zip-") as tmp:
        z = Path(tmp) / "zbior.zip"
        make_zip(z, a.files, a.incidents)
        res: dict[str, list[float]] = {"without": [], "with": []}
        last = None
        for _ in range(a.reps):  # interleaved, so drift hits both arms
            res["without"].append(one_import(z, False)[0])
            w, last = one_import(z, True)
            res["with"].append(w)
        print(json.dumps({"files": a.files, "incidents": a.incidents, "zip_bytes": z.stat().st_size,
                          "without_s": [round(x, 3) for x in res["without"]],
                          "with_s": [round(x, 3) for x in res["with"]],
                          "median_without_s": round(statistics.median(res["without"]), 3),
                          "median_with_s": round(statistics.median(res["with"]), 3),
                          "final": {k: last[k] for k in ("phase", "files_total", "files_done", "percent", "rejected",
                                                         "incidents_done", "writes")} if last else None}, indent=1))


if __name__ == "__main__":
    main()
