"""`python -m hydra.adapters.datasets list | stats | materialise | import` (docs/datasets/EXTERNAL.md).

    python -m hydra.adapters.datasets list
    python -m hydra.adapters.datasets stats phreshphish-2k --root ~/HydraNexus/data/external-big
    python -m hydra.adapters.datasets materialise --root ~/HydraNexus/data/external-big \\
        --out ~/HydraNexus/data/external-big/corpus-demo \\
        --mix "phreshphish-2k:limit=400,min_group=3,per_group_cap=30,unlabelled=40" \\
        --mix "purino-scam-ads:limit=300,min_group=3,per_group_cap=15,unlabelled=30"
    python -m hydra.adapters.datasets import ~/HydraNexus/data/external-big/corpus-demo
    hydra analyse --corpus ext:phreshphish-2k,ext:purino-scam-ads
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from hydra.adapters.datasets import import_corpus, list_datasets, materialise, rows


def default_root() -> Path:
    base = os.environ.get("HYDRA_DATA_DIR")
    return (Path(base) if base else Path.home() / "HydraNexus" / "data") / "external-big"


def parse_opts(s: str) -> tuple[str, dict]:
    name, _, rest = s.partition(":")
    opts: dict = {}
    for kv in filter(None, rest.split(",")):
        k, _, v = kv.partition("=")
        v = v.strip()
        opts[k.strip()] = (int(v) if v.lstrip("-").isdigit() else
                           True if v.lower() == "true" else False if v.lower() == "false" else v)
    return name.strip(), opts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m hydra.adapters.datasets")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    s = sub.add_parser("stats", help="label-group statistics of one dataset (aggregates only)")
    s.add_argument("spec", help="name[:opt=v,...]")
    s.add_argument("--root", type=Path, default=default_root())
    m = sub.add_parser("materialise")
    m.add_argument("--root", type=Path, default=default_root())
    m.add_argument("--out", type=Path, required=True)
    m.add_argument("--mix", action="append", required=True, help="name:limit=N,min_group=K,per_group_cap=C,...")
    m.add_argument("--clean", action="store_true")
    i = sub.add_parser("import")
    i.add_argument("corpus", type=Path)
    i.add_argument("--only", default="", help="comma list of datasets")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for d in list_datasets():
            print(json.dumps(d.__dict__, ensure_ascii=False))
    elif a.cmd == "stats":
        name, opts = parse_opts(a.spec)
        rs = rows(name, a.root, **opts)
        labels = Counter(r.label for r in rs if r.label)
        sizes = sorted(labels.values(), reverse=True)
        print(json.dumps({"dataset": name, "rows": len(rs), "labelled": sum(sizes), "label_groups": len(sizes),
                          "groups_ge2": sum(1 for v in sizes if v >= 2), "groups_ge5": sum(1 for v in sizes if v >= 5),
                          "largest": sizes[:10], "classes": dict(Counter(r.group for r in rs))}, indent=1))
    elif a.cmd == "materialise":
        doc = materialise([parse_opts(x) for x in a.mix], a.root, a.out, clean=a.clean)
        print(json.dumps(doc["stats"], indent=1))
    elif a.cmd == "import":
        reps = import_corpus(a.corpus, only={x for x in a.only.split(",") if x} or None)
        print(json.dumps({"imported": len(reps), "artifacts": sum(r.files_seen for r in reps),
                          "errors": sum(len(r.errors) for r in reps),
                          "roles": dict(sum((Counter(r.counts) for r in reps), Counter()))}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
