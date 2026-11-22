"""DATA-MASS: bulk materialisation of external datasets into per-dataset corpus folders (server only).

    uv run python scripts/materialise_mass.py --out ~/HydraNexus/data/external-big/mass-phreshphish \
        --take "phreshphish:phish=20000,benign=20000,pl=all" --workers 6
    uv run python -m hydra.adapters.datasets import <out>          # with HYDRA_STATE_DIR=<own state dir>

Differences to `hydra.adapters.datasets.materialise` (which builds small label-stratified evaluation corpora):
- quota per class (`Row.group`) in hash order instead of label-group selection; `pl=N|all` takes `lang=pl` rows
  first; `all` takes every row; other `k=v` pairs are adapter filters (`lang`, `split`, `scam_only`, ...);
- every incident of the evaluation corpora (`corpus-demo`, `corpus-putra` under the root) is skipped, so a model
  fitted on a mass corpus never sees an evaluation item (pass `--keep-eval` to disable);
- parquet adapters whose `load` accepts `shards` are read in parallel, one process per parquet shard.

Writes the same `hydra_corpus.json` / `CORPUS.md` as `materialise`, so `import_corpus` reads it unchanged.
Dataset bytes are copied, never parsed, rendered or executed.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

EVAL_CORPORA = ("corpus-demo", "corpus-putra")
QUOTA_KEYS = {"all", "pl"}


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def parse_take(s: str) -> tuple[str, dict[str, str]]:
    """`name:cls=N,pl=all,lang=pl` → (name, raw options). Quotas are class names of the dataset (`Row.group`),
    `pl` and `all`; anything else is an adapter filter (`split_opts`)."""
    name, _, rest = s.partition(":")
    opts = dict(kv.split("=", 1) if "=" in kv else (kv, "all") for kv in rest.split(",") if kv)
    return name.strip(), {k.strip(): v.strip() for k, v in opts.items()}


def split_opts(name: str, opts: dict[str, str], classes: set[str]) -> tuple[dict[str, str], dict[str, Any]]:
    from hydra.adapters.datasets.__main__ import parse_opts

    quotas = {k: v for k, v in opts.items() if k in QUOTA_KEYS or k in classes}
    filt = parse_opts(name + ":" + ",".join(f"{k}={v}" for k, v in opts.items() if k not in quotas))[1]
    return quotas, filt


def pick(rows: list, quotas: dict[str, str]) -> list:
    """Hash-ordered sample per class; deterministic. Empty quotas = everything."""
    rows = sorted(rows, key=lambda r: _h(r.key))
    if not quotas or "all" in quotas:
        return rows
    picked: dict[str, Any] = {}
    if "pl" in quotas:
        pl = [r for r in rows if (r.extra or {}).get("lang") == "pl"]
        picked |= {r.key: r for r in (pl if quotas["pl"] == "all" else pl[: int(quotas["pl"])])}
    for cls, n in quotas.items():
        if cls in QUOTA_KEYS:
            continue
        cand = [r for r in rows if r.group == cls and r.key not in picked]
        picked |= {r.key: r for r in (cand if n == "all" else cand[: int(n)])}
    return list(picked.values())


def eval_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    for c in EVAL_CORPORA:
        f = root / c / "hydra_corpus.json"
        if f.is_file():
            ids |= {e["id"] for e in json.loads(f.read_text())["incidents"].values()}
    return ids


def _entry(x: Any, d: Path) -> tuple[str, dict[str, Any]]:
    from hydra.adapters.datasets import meta_of

    m = meta_of(x)
    keep = ("id", "dataset", "source", "label", "group_label", "roles", "label_kind", "verdict", "lang")
    return d.name, {k: m.get(k) for k in keep} | {"files": sorted(x.files)}


def _write_batch(name: str, root: str, rows: list, out: str, shard: str | None, skip: set[str]) -> dict:
    from hydra.adapters.datasets import _module, dataset_root, write_incident

    m = _module(name)
    r = dataset_root(name, Path(root))
    it = m.load(r, rows, shards=[Path(shard)]) if shard else m.load(r, rows)
    entries: dict[str, dict[str, Any]] = {}
    for x in it:
        if not x.files or x.incident_id in skip:
            continue
        d = write_incident(x, Path(out))
        k, e = _entry(x, d)
        entries[k] = e
    return entries


def materialise_one(name: str, opts: dict[str, str], root: Path, out: Path, workers: int, keep_eval: bool) -> dict:
    from hydra.adapters.datasets import _module, dataset_root

    m = _module(name)
    r = dataset_root(name, root)
    all_rows = m.index(r)
    classes = {x.group for x in all_rows if x.group}
    quotas, filt = split_opts(name, opts, classes)
    if filt:
        ok = m.where(**filt)
        all_rows = [x for x in all_rows if ok is None or ok(x)]
    rows = pick(all_rows, quotas)
    skip = set() if keep_eval else eval_ids(root)
    t0 = time.time()
    entries: dict[str, dict[str, Any]] = {}
    shardable = hasattr(m, "_files") and "shards" in inspect.signature(m.load).parameters
    if shardable and workers > 1:
        shards = [str(p) for p in m._files(r)]
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_write_batch, name, str(root), rows, str(out), s, skip) for s in shards]
            for i, f in enumerate(as_completed(futs), 1):
                entries |= f.result()
                print(f"{name}: shard {i}/{len(futs)} -> {len(entries)} incidents ({time.time() - t0:.0f} s)",
                      flush=True)
    else:
        entries = _write_batch(name, str(root), rows, str(out), None, skip)
    print(f"{name}: index {len(all_rows)}, picked {len(rows)}, written {len(entries)} "
          f"({time.time() - t0:.0f} s)", flush=True)
    return entries


def main(argv: list[str] | None = None) -> int:
    from hydra.adapters.datasets import CORPUS_FILE, corpus_md, corpus_stats
    from hydra.adapters.datasets.__main__ import default_root

    ap = argparse.ArgumentParser(prog="scripts/materialise_mass.py")
    ap.add_argument("--root", type=Path, default=default_root())
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--take", action="append", required=True, help="name[:cls=N|all,pl=N|all,all,<filter>=v]")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--keep-eval", action="store_true", help="do not skip incidents of the evaluation corpora")
    a = ap.parse_args(argv)
    out = a.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict[str, Any]] = {}
    mix = []
    for t in a.take:
        name, opts = parse_take(t)
        mix.append([name, opts])
        entries |= materialise_one(name, opts, a.root.expanduser(), out, max(1, min(a.workers, 48)), a.keep_eval)
    stats = corpus_stats(entries)
    stats["lang_pl"] = sum(1 for e in entries.values() if e.get("lang") == "pl")
    doc = {"version": 1, "note": "External mass corpus (not organiser data, evaluation-corpus items removed unless "
           "--keep-eval). Import with `python -m hydra.adapters.datasets import <this folder>`; never "
           "`hydra analyse <folder>`.", "mix": mix, "keep_eval": a.keep_eval, "stats": stats,
           "incidents": dict(sorted(entries.items()))}
    (out / CORPUS_FILE).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    (out / "CORPUS.md").write_text(corpus_md(doc) + f"\nPolish (lang=pl) incidents: {stats['lang_pl']}\n")
    print(json.dumps({"incidents": stats["incidents"], "lang_pl": stats["lang_pl"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
