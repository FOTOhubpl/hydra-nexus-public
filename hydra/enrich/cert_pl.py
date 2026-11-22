"""CERT Polska warning list (Lista ostrzeżeń przed niebezpiecznymi stronami), offline snapshot, host-level lookup.

Snapshot: `$HYDRA_DATA_DIR/external/cert/domains.json` (+ SNAPSHOT.json with retrieval time and sha256), source
https://hole.cert.pl/domains/v2/domains.json, published by CERT Polska for public use. Lookup is by exact host
and by each parent label up to the registrable domain, reported separately: a registrable-domain hit is weaker
(PLAN_MAX §1: domain-level matching gave 17% false hits on free hosts).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from hydra.enrich.domains import registrable_domain


def snapshot_dir() -> Path:
    root = Path(os.environ.get("HYDRA_DATA_DIR", Path.home() / "HydraNexus" / "data"))
    return Path(os.environ.get("HYDRA_CERT_PL_DIR", root / "external" / "cert"))


@lru_cache(maxsize=1)
def _load() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    d = snapshot_dir()
    f = d / "domains.json"
    if not f.is_file():
        return {}, {}
    idx: dict[str, dict[str, Any]] = {}
    for e in json.loads(f.read_text()):
        host = str(e.get("DomainAddress", "")).lower().strip().rstrip(".")
        if host and (host not in idx or idx[host].get("DeleteDate")):
            idx[host] = e
    meta = {}
    if (d / "SNAPSHOT.json").is_file():
        meta = json.loads((d / "SNAPSHOT.json").read_text())
    return idx, meta


def available() -> bool:
    return bool(_load()[0])


def snapshot_meta() -> dict[str, Any]:
    return _load()[1]


def lookup(host: str) -> dict[str, Any]:
    idx, meta = _load()
    host = host.lower().rstrip(".")
    out: dict[str, Any] = {
        "snapshot": meta.get("snapshot"),
        "retrieved_at": meta.get("retrieved_at"),
        "host_listed": False,
        "parent_listed": None,
    }
    e = idx.get(host)
    if e:
        out.update(host_listed=True, insert_date=e.get("InsertDate"), delete_date=e.get("DeleteDate"))
    rd = registrable_domain(host)
    labels = host.split(".")
    for i in range(1, len(labels) - 1):
        parent = ".".join(labels[i:])
        if parent in idx:
            out["parent_listed"] = parent
            out["parent_insert_date"] = idx[parent].get("InsertDate")
            break
        if parent == rd:
            break
    return out
