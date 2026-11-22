"""Deployment-local lexicon overlay, kept outside git.

The repository ships generic lexicons only (prior knowledge of Polish scam ads, well-known public brands).
Terms learned from the organiser sample (invented platform and product names, verbatim headline phrases,
impersonated persons named in the material) may not be distributed publicly (task PDF §8), so they live in
`$HYDRA_DATA_DIR/nask/lexicons/*.json` and are merged at import time when that directory exists.
`HYDRA_LEXICON_DIR` overrides the location; `HYDRA_LEXICON_DIR=none` disables the overlay (the test suite
does this, so unit tests never depend on local files).

Files (all optional, UTF-8 JSON, patterns in the folded form of `hydra.genome.txt_norm.fold`):
- `terms.json`: `{"<GROUP>": [{"id", "patterns", "weight"?, "origin"?}], "SCHEMES": {"<scheme>": [...]}}`;
  GROUP is one of MOTIFS, PERSONAS, PROMISES, CTAS, URGENCY, MEDIA_BRANDS, INSTITUTION_BRANDS,
  KNOWN_PRODUCTS. A term whose id already exists gets the extra patterns appended; others are added.
- `persons.json`: `[{"canonical", "surnames", "given"?, "role"?, "origin"?}]`, checked before the generic
  gazetteer.
- `names.json`: `{"first_names": [...], "not_surname": [...]}`: extra given names (original spelling) and
  capitalised words that are never a surname.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


def lexicon_dir() -> Path | None:
    env = os.environ.get("HYDRA_LEXICON_DIR")
    if env is not None:
        return None if env.strip().lower() in ("", "none", "off", "0") else Path(env).expanduser()
    data = os.environ.get("HYDRA_DATA_DIR")
    base = Path(data).expanduser() if data else Path.home() / "HydraNexus" / "data"
    return base / "nask" / "lexicons"


@lru_cache(maxsize=8)
def load(name: str) -> Any:
    """Parsed `<lexicon_dir>/<name>.json`, or None when the overlay is disabled or the file is absent."""
    d = lexicon_dir()
    p = d / f"{name}.json" if d else None
    if p is None or not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def active() -> bool:
    return any(load(n) is not None for n in ("terms", "persons", "names"))
