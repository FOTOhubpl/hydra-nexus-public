"""Public-source Polish scam lexicons shipped with the repository (PL-SCAM stream, 2026-11-21).

Files next to this module, every entry carrying a source id or URL (docs/research/PL_SCAM_ECOSYSTEM.md):
- `pl_scam_schemes.json`: scheme taxonomy (`schemes`), lure-phrase terms (`terms`, each with `group`, optional
  `scheme`, folded `patterns`, `weight`, `source`, short `quote`) and the source registry (`sources`);
- `pl_brands.json`: impersonated media / institutions / banks / couriers / marketplaces / retailers with folded
  text patterns, domain tokens and official registrable domains;
- `pl_public_figures.json`: persons documented in a primary public source as abused in scam ads;
- `pl_domain_patterns.json`: TLD statistics (CERT Polska list vs Tranco) and Polish domain service words.

Unlike the deployment-local overlay (`local.py`, organiser-derived, never in git) these are derived from public
sources only. `HYDRA_PL_LEXICON=0` disables them (ablation: measure what they add).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FILES = ("pl_scam_schemes", "pl_brands", "pl_public_figures", "pl_domain_patterns")


def enabled() -> bool:
    return os.environ.get("HYDRA_PL_LEXICON", "1").strip().lower() not in ("0", "off", "none", "false", "")


@lru_cache(maxsize=8)
def load(name: str) -> dict[str, Any]:
    """Parsed `<name>.json` from this package, or {} when disabled or missing."""
    if name not in FILES or not enabled():
        return {}
    p = HERE / f"{name}.json"
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def sources() -> dict[str, dict[str, Any]]:
    return dict(load("pl_scam_schemes").get("sources") or {})


def source_url(source_id: str | None) -> str | None:
    if not source_id:
        return None
    s = sources().get(source_id)
    return s.get("url") if s else None


def schemes() -> dict[str, dict[str, Any]]:
    return {s["id"]: s for s in load("pl_scam_schemes").get("schemes") or []}


def brands() -> dict[str, dict[str, Any]]:
    return {b["id"]: b for b in load("pl_brands").get("brands") or []}


def figures() -> list[dict[str, Any]]:
    return list(load("pl_public_figures").get("persons") or [])
