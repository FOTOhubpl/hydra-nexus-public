"""Seed the enrichment replay cache from an exploratory cache (same JSON layout), idempotently.

Usage: uv run python scripts/enrich_seed.py SRC_CACHE_DIR [--dest DIR]
SRC layout: <src>/<source>/<name>.json with {source, key, url, fetched_at, status, data}. Error records
(`*.error.json`) are skipped. Existing destination files are kept (a newer live fetch wins). Adds `query`.
Destination defaults to Settings.enrich_cache (HYDRA_ENRICH_CACHE).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from hydra.enrich.client import default_cache_dir

SOURCES = {"rdap", "rdap_ip", "dns", "crtsh", "internetdb", "urlscan", "wayback"}


def main() -> None:
    src = Path(sys.argv[1])
    dest = Path(sys.argv[sys.argv.index("--dest") + 1]) if "--dest" in sys.argv else default_cache_dir()
    n: Counter = Counter()
    for f in sorted(src.glob("*/*.json")):
        source = f.parent.name
        if source not in SOURCES or f.name.endswith(".error.json"):
            n["skipped"] += 1
            continue
        try:
            j = json.loads(f.read_text())
        except ValueError:
            n["bad"] += 1
            continue
        out = dest / source / f.name
        if out.exists():
            n["exists"] += 1
            continue
        j.setdefault("query", j.get("key") or f.stem)
        j.setdefault("seeded_from", "analysis cache")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(j, ensure_ascii=False, indent=1))
        n[f"copied_{source}"] += 1
    print(dest, dict(n))


if __name__ == "__main__":
    main()
