"""Shipped background priors: document frequencies of gene values over large public corpora (TRAIN stream).

`background_df.json.gz` (built on the server by `scripts/train_prior.py build`, only from SHIP-SAFE datasets, see
`docs/datasets/EXTERNAL.md`) holds, per hashed (kind, value), the number of distinct *units* of the background
corpus carrying it, split into legitimate pages / ads and fraud pages / ads, plus the number of units observing each
kind. No value, page, image or text of a third party is stored: the key is `sha256(kind + "\\x1f" + value)[:16]`.
Values held by fewer than `min_df` units are not stored (they behave as unseen).

Use (`hydra.entropy.Rarity.discrete_bits`): the background can only make a value *more common*, never rarer than
the local corpus alone says. Values unseen in the background keep their local rarity.

Switch: `HYDRA_BACKGROUND_PRIOR` = `1` / `mixed` (legit + fraud units), `legit` (legit units only), `0` (off).
Loaded offline from the package directory; no network. Organiser (NASK) data never enters this file.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

SCHEMA = "hydra.background_df/1"
PRIOR_FILE = Path(__file__).resolve().parent / "background_df.json.gz"
DEFAULT_MODE = "mixed"  # architect decision 2026-11-22 (METHODOLOGY §5, SPEC_ERRATA E-TRAIN-1); 0 = old behaviour
MIN_KIND_UNITS = 200     # a kind observed by fewer background units is ignored (too thin to say "common")

# Never counted into a shipped prior: personal data (even hashed), identities, capture metadata of the local run.
_EXCLUDED_KIND_RE = re.compile(
    r"(^|_)(email|phone|iban|account|person|persona|commenter|byline|author|face|capi_token|operator_id)"
    r"|^capture_|^collected_|fake_comment|comment_names", re.I)


def excluded_kind(kind: str) -> bool:
    return bool(_EXCLUDED_KIND_RE.search(kind))


def value_key(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}\x1f{value}".encode()).hexdigest()[:16]


@dataclass
class ShippedPrior:
    """Same `lookup(kind, value) -> (df, n)` interface as `hydra.entropy.BackgroundPrior`."""

    corpus: str
    n: int
    n_by_kind: dict[str, int]
    df: dict[str, int]
    meta: dict[str, Any] = field(default_factory=dict)

    def lookup(self, kind: str, value: str) -> tuple[int, int]:
        n = self.n_by_kind.get(kind, 0)
        if n < MIN_KIND_UNITS or excluded_kind(kind):
            return 0, 0
        return self.df.get(value_key(kind, value), 0), n

    def summary(self) -> dict[str, Any]:
        return {"corpus": self.corpus, "units": self.n, "incidents": self.meta.get("incidents"),
                "kinds": sum(1 for v in self.n_by_kind.values() if v >= MIN_KIND_UNITS),
                "sources": [{k: s.get(k) for k in ("source", "incidents", "licence")}
                            for s in self.meta.get("sources", [])],
                "schema": self.meta.get("schema"), "created": self.meta.get("created")}


def mode() -> str:
    m = os.environ.get("HYDRA_BACKGROUND_PRIOR", DEFAULT_MODE).strip().lower()
    return {"1": "mixed", "true": "mixed", "on": "mixed", "yes": "mixed", "": "off", "false": "off",
            "no": "off", "0": "off"}.get(m, m)


@lru_cache(maxsize=4)
def _load(path: str, which: str) -> ShippedPrior | None:
    p = Path(path)
    if not p.is_file():
        return None
    d = json.loads(gzip.decompress(p.read_bytes()))
    if d.get("schema") != SCHEMA:
        return None
    cols = d["columns"]
    il, ifr = cols.index("df_legit"), cols.index("df_fraud")
    use_fraud = which == "mixed"
    df = {r[0]: r[il] + (r[ifr] if use_fraud else 0) for r in d["df"]}
    df = {k: v for k, v in df.items() if v > 0}
    n_by_kind = {k: int(c.get("legit", 0)) + (int(c.get("fraud", 0)) if use_fraud else 0)
                 for k, c in d["n_by_kind"].items()}
    units = int(d["units"].get("legit", 0)) + (int(d["units"].get("fraud", 0)) if use_fraud else 0)
    meta = {k: d.get(k) for k in ("schema", "created", "incidents", "units", "sources", "min_df", "unit", "hash")}
    return ShippedPrior(f"shipped:{which}", units, n_by_kind, df, meta)


def prior_file() -> Path:
    """The shipped file, or `HYDRA_BACKGROUND_PRIOR_FILE` (TRAIN/EVAL: compare candidate priors before shipping)."""
    return Path(os.environ.get("HYDRA_BACKGROUND_PRIOR_FILE") or PRIOR_FILE).expanduser()


def default_prior(path: Path | None = None) -> ShippedPrior | None:
    """The shipped prior in the mode chosen by `HYDRA_BACKGROUND_PRIOR` (None when off or the file is absent)."""
    m = mode()
    if m not in ("mixed", "legit"):
        return None
    return _load(str(path or prior_file()), m)


def fingerprint() -> str | None:
    """Identity of the prior in effect (mode + file sha256), for caches keyed on scoring inputs; None when off."""
    m = mode()
    f = prior_file()
    if m not in ("mixed", "legit") or not f.is_file():
        return None
    return f"{m}:{hashlib.sha256(f.read_bytes()).hexdigest()[:16]}"


def describe() -> dict[str, Any]:
    """For the UI / methodology view and run metrics."""
    p = default_prior()
    f = prior_file()
    where = {} if f == PRIOR_FILE else {"file": str(f)}
    if p is None:
        return {"enabled": False, "mode": mode(), "file_present": f.is_file(), **where}
    return {"enabled": True, "mode": mode(), **where, **p.summary()}
