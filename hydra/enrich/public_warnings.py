"""Polish public warning lists as offline snapshots: KNF Lista ostrzeżeń publicznych, MF register of illegal gambling
domains (PL-SCAM stream, 2026-11-21). Host-level lookup, replayable, no network at runtime.

Snapshots (made by `scripts/fetch_public_warnings.py`, never in git):
- `$HYDRA_DATA_DIR/external/knf/knf_warnings.json` + `SNAPSHOT.json` (override `HYDRA_KNF_DIR`); source
  https://www.knf.gov.pl/dla_konsumenta/ostrzezenia_publiczne (669 warnings on 2026-11-21). Entries are entities;
  only the websites named in an entry become domains. Entity names can be natural persons, so genes carry only the
  domain, the category and the list position.
- `$HYDRA_DATA_DIR/external/mf_hazard/register.json` + `SNAPSHOT.json` (override `HYDRA_MF_HAZARD_DIR`); source
  https://hazard.mf.gov.pl/api/Register (XML, 60 231 domains on 2026-11-21).

A missing snapshot means "not checked" (`available()` is False), never "not listed". A KNF hit on the registrable
domain only (the entry names `example.com`, the incident uses `x.example.com`) is reported with level
`registrable`. Membership is a label (tier `label`, 0 bits): an authority warned about the domain or its operator.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from hydra.enrich.domains import is_boilerplate, is_platform, registrable_domain


def _root() -> Path:
    return Path(os.environ.get("HYDRA_DATA_DIR", Path.home() / "HydraNexus" / "data")).expanduser()


def knf_dir() -> Path:
    return Path(os.environ.get("HYDRA_KNF_DIR", _root() / "external" / "knf")).expanduser()


def mf_dir() -> Path:
    return Path(os.environ.get("HYDRA_MF_HAZARD_DIR", _root() / "external" / "mf_hazard")).expanduser()


def norm_host(h: str) -> str:
    h = (h or "").strip().lower().rstrip(".")
    for p in ("https://", "http://"):
        if h.startswith(p):
            h = h[len(p):]
    h = h.split("/", 1)[0].split(":", 1)[0]
    return h[4:] if h.startswith("www.") else h


def _meta(d: Path) -> dict[str, Any]:
    p = d / "SNAPSHOT.json"
    return json.loads(p.read_text()) if p.is_file() else {}


@lru_cache(maxsize=1)
def _knf() -> tuple[dict[str, list[dict]], dict[str, list[dict]], dict[str, Any]]:
    f = knf_dir() / "knf_warnings.json"
    if not f.is_file():
        return {}, {}, {}
    by_host: dict[str, list[dict]] = {}
    by_rd: dict[str, list[dict]] = {}
    for e in json.loads(f.read_text(encoding="utf-8")):
        for d in e.get("domains") or []:
            h = norm_host(d)
            if not h or is_boilerplate(h):
                continue  # an entity "operating via facebook.com" must not label every Facebook link
            row = {"domain": h, "category": e.get("category", ""), "lp": e.get("lp"), "table": e.get("table")}
            by_host.setdefault(h, []).append(row)
            rd = registrable_domain(h)
            if rd and not is_platform(h) and not is_boilerplate(rd):
                by_rd.setdefault(rd, []).append(row)
    return by_host, by_rd, _meta(knf_dir())


@lru_cache(maxsize=1)
def _mf() -> tuple[dict[str, dict], dict[str, Any]]:
    f = mf_dir() / "register.json"
    if not f.is_file():
        return {}, {}
    idx: dict[str, dict] = {}
    for e in json.loads(f.read_text(encoding="utf-8")):
        h = norm_host(e.get("domain", ""))
        if h and not e.get("date_removed"):
            idx[h] = e
    return idx, _meta(mf_dir())


def available() -> dict[str, bool]:
    return {"knf": bool(_knf()[0]), "mf_hazard": bool(_mf()[0])}


def snapshot_meta() -> dict[str, dict[str, Any]]:
    return {"knf": _knf()[2], "mf_hazard": _mf()[1]}


def lookup(host: str) -> dict[str, Any]:
    """{"knf": [hits], "mf_hazard": hit | None, "*_snapshot": ...}; each hit has `domain` and `level`
    ("host" = exact host without www., "registrable" = same registrable domain)."""
    h = norm_host(host)
    by_host, by_rd, kmeta = _knf()
    mf, mmeta = _mf()
    out: dict[str, Any] = {"knf": [], "mf_hazard": None, "knf_snapshot": kmeta.get("snapshot"),
                           "mf_snapshot": mmeta.get("snapshot")}
    if not h:
        return out
    if h in by_host:
        out["knf"] = [{**r, "level": "host"} for r in by_host[h]]
    else:
        rd = registrable_domain(h)
        if rd and rd in by_rd:
            out["knf"] = [{**r, "level": "registrable"} for r in by_rd[rd]]
    if h in mf:
        out["mf_hazard"] = {**mf[h], "domain": h, "level": "host"}
    else:
        rd = registrable_domain(h)
        if rd and rd != h and rd in mf:
            out["mf_hazard"] = {**mf[rd], "domain": rd, "level": "registrable"}
    return out
