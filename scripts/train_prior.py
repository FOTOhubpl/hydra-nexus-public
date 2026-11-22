"""TRAIN: background document-frequency prior from large external corpora (server only, never organiser data).

Three steps, all on the server (raw external data never leaves it; docs/datasets/EXTERNAL.md):

    uv run python scripts/train_prior.py materialise --out ~/HydraNexus/data/external-big/corpus-train \
        --take "phreshphish:benign=12000,phish=6000,pl=all" --take "purino-scam-ads:all" ...
    HYDRA_EXTRACT_WORKERS=12 HYDRA_TORCH_THREADS=2 HYDRA_OCR_WORKERS=1 OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 \
        uv run python scripts/train_prior.py extract --corpus <out> --state ~/HydraNexus/state-train \
        [--datasets phreshphish,web-legit-tranco --skip txt_embed,ident,aud_fp,vid_avsync,inf_har,inf_enrich]
    uv run python scripts/train_prior.py build --state <state> --out hydra/priors/background_df.json.gz

`materialise` writes incident folders like `hydra.adapters.datasets.materialise` (same `hydra_corpus.json`), with a
plain hash-ordered sample per (dataset, class) instead of the label-group selection, and skips every incident of the
evaluation corpora (`corpus-demo`, `corpus-putra`), so evaluation on them never sees its own items in the prior.
`extract` imports the folders with `source = ext:<dataset>` into a separate state dir and runs the registered
extractors (enrichment off, empty replay cache) in batches. `build` counts document frequencies of discrete gene
values over *units* (one registrable domain for pages, one advertiser page for ads, else one incident), hashes every
(kind, value) and writes only counts: `hydra.priors` documents the format. Only SHIP-SAFE datasets enter `build`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home() / "HydraNexus"
EXT = HOME / "data" / "external-big"
EVAL_CORPORA = ("corpus-demo", "corpus-putra")


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _eval_ids(root: Path) -> set[str]:
    ids: set[str] = set()
    for c in EVAL_CORPORA:
        f = root / c / "hydra_corpus.json"
        if f.is_file():
            ids |= {e["id"] for e in json.loads(f.read_text())["incidents"].values()}
    return ids


def _parse_take(s: str) -> tuple[str, dict[str, str]]:
    name, _, rest = s.partition(":")
    opts = dict(kv.split("=", 1) if "=" in kv else (kv, "all") for kv in rest.split(",") if kv)
    return name, opts


def _pick(rows: list, opts: dict[str, str]) -> list:
    """Hash-ordered sample per class (`Row.group`): `<class>=N|all`; `pl=N|all` takes lang=pl rows first;
    `all` takes everything."""
    rows = sorted(rows, key=lambda r: _h(r.key))
    if "all" in opts:
        return rows
    picked: dict[str, object] = {}
    if "pl" in opts:
        pl = [r for r in rows if (r.extra or {}).get("lang") == "pl"]
        n = len(pl) if opts["pl"] == "all" else int(opts["pl"])
        picked |= {r.key: r for r in pl[:n]}
    for cls, n in opts.items():
        if cls == "pl":
            continue
        cand = [r for r in rows if r.group == cls and r.key not in picked]
        take = len(cand) if n == "all" else max(0, int(n))
        picked |= {r.key: r for r in cand[:take]}
    return list(picked.values())


def materialise(args: argparse.Namespace) -> int:
    from hydra.adapters.datasets import CORPUS_FILE, _module, corpus_stats, dataset_root, meta_of, write_incident

    out = args.out.expanduser()
    out.mkdir(parents=True, exist_ok=True)
    skip = _eval_ids(args.root)
    doc_f = out / CORPUS_FILE
    entries: dict[str, dict] = json.loads(doc_f.read_text())["incidents"] if doc_f.is_file() else {}
    for spec in args.take:
        name, opts = _parse_take(spec)
        m = _module(name)
        root = dataset_root(name, args.root)
        rows = _pick(m.index(root), opts)
        t, n, skipped = time.time(), 0, 0
        for x in m.load(root, rows):
            if not x.files:
                continue
            if x.incident_id in skip:
                skipped += 1
                continue
            d = write_incident(x, out)
            meta = meta_of(x)
            entries[d.name] = {k: meta[k] for k in ("id", "dataset", "source", "label", "group_label", "roles",
                                                    "label_kind", "verdict")} | {"files": sorted(x.files)}
            n += 1
        print(json.dumps({"dataset": name, "opts": opts, "selected": len(rows), "written": n,
                          "skipped_eval_items": skipped, "seconds": round(time.time() - t, 1)}), flush=True)
        doc_f.write_text(json.dumps({"version": 1, "note": "TRAIN prior corpus (external only; eval corpora "
                                     "excluded)", "stats": corpus_stats(entries), "incidents": entries},
                                    indent=1, ensure_ascii=False) + "\n")
    return 0


def extract(args: argparse.Namespace) -> int:
    from contextlib import closing

    from hydra.adapters.datasets import CORPUS_FILE, _set_meta
    from hydra.adapters.nask_folder import import_incident, load_incident
    from hydra.config import Settings
    from hydra.contracts import ArtifactRole
    from hydra.db import Writer, connect
    from hydra.pipeline import extract as run_extract
    from hydra.storage import ContentStore

    corpus = args.corpus.expanduser().resolve()
    doc = json.loads((corpus / CORPUS_FILE).read_text())
    state = args.state.expanduser()
    if state.resolve() in {(HOME / s).resolve() for s in ("state", "state-int", "state-public")}:
        raise SystemExit("refusing to write the TRAIN corpus into a product state dir")
    empty_cache = state / "empty-enrich-cache"
    empty_cache.mkdir(parents=True, exist_ok=True)
    s = Settings(state_dir=state, enrich=False, enrich_cache=empty_cache)
    s.ensure_dirs()
    writer = Writer(s.db_path)
    store = ContentStore(s.cas_dir)
    items = sorted(doc["incidents"].items(), key=lambda kv: _h(kv[0]))
    if args.datasets:
        keep = set(args.datasets.split(","))
        items = [kv for kv in items if kv[1]["dataset"] in keep]
    if args.folders:  # e.g. every Polish page first, whatever the shard
        want = set(args.folders.expanduser().read_text().split())
        items = [kv for kv in items if kv[0] in want]
    if args.exclude_folders:
        drop = set(args.exclude_folders.expanduser().read_text().split())
        items = [kv for kv in items if kv[0] not in drop]
    only = None
    if args.skip:
        from hydra.genome import base

        base.load_family_modules()
        import hydra.genome.artifact  # noqa: F401 — registers the CORE extractor, as pipeline.extract does

        skip = set(args.skip.split(","))
        unknown = skip - set(base.REGISTRY)
        if unknown:
            raise SystemExit(f"unknown extractors in --skip: {sorted(unknown)}")
        only = [n for n in base.REGISTRY if n not in skip]
    if args.limit:
        items = items[: args.limit]
    if args.shard:
        k, n_sh = (int(x) for x in args.shard.split("/"))
        items = [kv for kv in items if int(_h(kv[0])[:8], 16) % n_sh == k]
    with closing(connect(s.db_path, readonly=True)) as conn:
        done = {r["id"] for r in conn.execute("SELECT DISTINCT incident_id id FROM genes")}
    for other in args.exclude_state:  # extracted by another shard state: never twice (build counts each once)
        with closing(connect(other.expanduser() / "hydra.sqlite", readonly=True)) as conn:
            done |= {r["id"] for r in conn.execute("SELECT DISTINCT incident_id id FROM genes")}
    items = [kv for kv in items if kv[1]["id"] not in done]
    print(json.dumps({"todo": len(items), "already": len(done), "skip": sorted(set(args.skip.split(",")) - {""}),
                      "datasets": args.datasets or "all"}), flush=True)
    t0 = time.time()
    try:
        for b in range(0, len(items), args.batch):
            batch = items[b: b + args.batch]
            incs = []
            for folder, e in batch:
                roles = {k: ArtifactRole(v) for k, v in (e.get("roles") or {}).items()}
                try:
                    import_incident(corpus / folder, writer, store, s, incident_id=e["id"], source=e["source"],
                                    label=e["label"], roles=roles or None, prefix="ext")
                except Exception as exc:  # noqa: BLE001 — one bad page never stops the corpus
                    print(f"IMPORT-FAIL {e['id']}: {type(exc).__name__}: {exc}"[:300], file=sys.stderr, flush=True)
                    continue
                ext = {"dataset": e["dataset"], "label_kind": e["label_kind"], "verdict": e["verdict"]}
                writer.run(lambda c, iid=e["id"], g=e["group_label"], x=ext: _set_meta(c, iid, g, x))
                with closing(connect(s.db_path, readonly=True)) as conn:
                    inc = load_incident(conn, e["id"])
                if inc is not None:
                    incs.append(inc)
            try:
                run_extract(incs, writer, store, s, only=only)
            except Exception as exc:  # noqa: BLE001
                print(f"EXTRACT-FAIL batch {b}: {type(exc).__name__}: {exc}"[:300], file=sys.stderr, flush=True)
            el = time.time() - t0
            n = b + len(batch)
            print(json.dumps({"done": n, "of": len(items), "seconds": round(el, 1),
                              "per_s": round(n / max(el, 1e-9), 2)}), flush=True)
    finally:
        writer.close()
    return 0


def build(args: argparse.Namespace) -> int:
    from contextlib import closing

    from hydra.corpus import corpus_of
    from hydra.db import connect
    from hydra.priors import SCHEMA, excluded_kind, value_key

    lock = json.loads((args.root / "datasets.lock.json").read_text())
    verdict = {d["name"]: d for d in lock["datasets"]}
    states = [p.expanduser() for p in args.state]
    unit_of: dict[str, str] = {}
    cls_of: dict[str, str] = {}
    src_of: dict[str, str] = {}
    df: dict[str, Counter] = defaultdict(Counter)        # key -> Counter(class -> units)
    kind_units: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    units_by_class: dict[str, set[str]] = defaultdict(set)
    per_source: Counter = Counter()
    refused: Counter = Counter()
    seen: set[tuple[str, str]] = set()  # (value key, unit) across all states: a unit counts once per value
    owner: dict[str, int] = {}  # incident -> first state holding its genes (shard states never double count)
    for si, st in enumerate(states):
        with closing(connect(st / "hydra.sqlite", readonly=True)) as conn:
            mine = {r[0] for r in conn.execute("SELECT DISTINCT incident_id FROM genes")} - set(owner)
            owner |= dict.fromkeys(mine, si)
            for r in conn.execute("SELECT id, source, label, meta FROM incidents"):
                if r["id"] not in mine:
                    continue  # imported but not extracted, or already counted from an earlier state
                src = corpus_of(r["source"])
                name = src.split(":", 1)[-1]
                if not src.startswith(("ext:", "public:")) or verdict.get(name, {}).get("verdict") != "SHIP-SAFE":
                    refused[src] += 1
                    continue
                meta = json.loads(r["meta"] or "{}")
                g = (meta.get("group_label") or "").rsplit(":", 1)[-1] or "unknown"
                cls_of[r["id"]] = "legit" if g in {"benign", "non_scam", "legitimate", "legit"} else "fraud"
                src_of[r["id"]] = src
            # unit: advertiser page for ads (purino label), registrable domain for pages, else the incident
            dom: dict[str, str] = {}
            for r in conn.execute("SELECT incident_id, value FROM genes WHERE kind='registrable_domain'"
                                  " AND status='observed' AND value IS NOT NULL ORDER BY id"):
                dom.setdefault(r["incident_id"], r["value"])
            for r in conn.execute("SELECT id, label FROM incidents"):
                if r["id"] not in cls_of or owner.get(r["id"]) != si:
                    continue
                lab = r["label"] or ""
                if "purino" in src_of[r["id"]] and lab:
                    unit_of[r["id"]] = f"{cls_of[r['id']]}|{lab}"
                elif r["id"] in dom:
                    unit_of[r["id"]] = f"{cls_of[r['id']]}|dom:{dom[r['id']]}"
                else:
                    unit_of[r["id"]] = f"{cls_of[r['id']]}|inc:{r['id']}"
            for r in conn.execute("SELECT incident_id, kind, value FROM genes WHERE status='observed'"
                                  " AND value IS NOT NULL AND vector_ref IS NULL"):
                iid = r["incident_id"]
                if owner.get(iid) != si or iid not in unit_of or excluded_kind(r["kind"]):
                    continue
                u, c = unit_of[iid], cls_of[iid]
                kind_units[r["kind"]][c].add(u)
                k = value_key(r["kind"], r["value"])
                if (k, u) in seen:
                    continue
                seen.add((k, u))
                df[k][c] += 1
    for iid, u in unit_of.items():
        units_by_class[cls_of[iid]].add(u)
        per_source[src_of[iid]] += 1
    min_df = args.min_df
    rows = [[k, c.get("legit", 0), c.get("fraud", 0)] for k, c in df.items()
            if c.get("legit", 0) + c.get("fraud", 0) >= min_df]
    rows.sort()
    sources = []
    for src, n in sorted(per_source.items()):
        d = verdict[src.split(":", 1)[-1]]
        sources.append({"source": src, "incidents": n, "licence": d["licence"], "verdict": d["verdict"],
                        "homepage": d["homepage"], "citation": d.get("citation")})
    out = {
        "schema": SCHEMA, "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "hash": "sha256(kind + '\\x1f' + value)[:16] hex; values are never stored",
        "unit": "registrable domain (pages) / advertiser page (ads) / incident; df counts distinct units",
        "min_df": min_df,
        "units": {c: len(v) for c, v in units_by_class.items()},
        "incidents": sum(per_source.values()),
        "n_by_kind": {k: {c: len(v) for c, v in by.items()} for k, by in sorted(kind_units.items())},
        "sources": sources, "refused_sources": dict(refused),
        "columns": ["key", "df_legit", "df_fraud"], "df": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()
    args.out.write_bytes(gzip.compress(data, mtime=0))
    print(json.dumps({"out": str(args.out), "bytes": args.out.stat().st_size, "keys": len(rows),
                      "all_keys": len(df), "units": out["units"], "incidents": out["incidents"],
                      "sources": {s["source"]: s["incidents"] for s in sources}, "refused": dict(refused)}),
          flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("materialise")
    m.add_argument("--root", type=Path, default=EXT)
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--take", action="append", required=True, help="dataset:class=N|all,pl=N|all or dataset:all")
    e = sub.add_parser("extract")
    e.add_argument("--corpus", type=Path, required=True)
    e.add_argument("--state", type=Path, required=True)
    e.add_argument("--batch", type=int, default=400)
    e.add_argument("--limit", type=int, default=0)
    e.add_argument("--shard", default="", help="K/N: only items whose folder hash mod N == K (parallel parents)")
    e.add_argument("--exclude-state", type=Path, action="append", default=[],
                   help="skip incidents already extracted in this state (other shards); repeatable")
    e.add_argument("--folders", type=Path, default=None, help="file of corpus folder names: only these")
    e.add_argument("--exclude-folders", type=Path, default=None, help="file of corpus folder names to leave out")
    e.add_argument("--datasets", default="", help="comma list: only these datasets of the corpus")
    e.add_argument("--skip", default="", help="comma list of extractors not to run. The prior reads discrete genes "
                   "only, so vector-only extractors can be skipped for prior-only sets; keep them for the fusion fit")
    b = sub.add_parser("build")
    b.add_argument("--root", type=Path, default=EXT)
    b.add_argument("--state", type=Path, action="append", required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--min-df", type=int, default=3)
    a = ap.parse_args(argv)
    a.root = getattr(a, "root", EXT)
    return {"materialise": materialise, "extract": extract, "build": build}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
