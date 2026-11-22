"""Shared pieces of the external-dataset adapters (DATA-HUNT, docs/datasets/EXTERNAL.md).

An adapter turns one external dataset into `ExtIncident`s: a few named files (bytes or a source path) plus the
labels. The registry (`hydra.adapters.datasets`) writes them as incident folders that `hydra.adapters.nask_folder`
imports unchanged, and imports them with `Incident.source = "ext:<dataset>"` (the corpus flag of `hydra.corpus`).

Dataset bytes are untrusted: adapters copy them, never parse HTML, never render or execute anything. Parquet
columns are read with pyarrow (project dependency).
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# folder file names; nask_folder.classify maps them to roles (url.txt → url, landing.html → landing_html,
# ad_creative.* → ad_creative, ad_text.txt → text); screenshot.png gets an explicit role override
URL_FILE = "url.txt"
HTML_FILE = "landing.html"
SCREENSHOT_STEM = "screenshot"
AD_CREATIVE_STEM = "ad_creative"
AD_TEXT_FILE = "ad_text.txt"


@dataclass(frozen=True)
class DatasetInfo:
    name: str                 # registry name, also the corpus suffix: source = "ext:<name>"
    title: str
    homepage: str
    licence: str
    verdict: str              # SHIP-SAFE | EVAL-ONLY | EXCLUDED (docs/datasets/EXTERNAL.md)
    modality: str
    label_kind: str           # what `label` means: brand_target, advertiser_page, source_site, ...
    dir: str                  # folder under external-big/
    citation: str = ""


@dataclass
class Row:
    """Lightweight index row: enough to select a stratified sample before any content is loaded."""

    key: str
    label: str | None         # raw dataset label (prefixed later): grouping unit
    group: str | None         # raw umbrella label (phish / benign / scam / ...)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtIncident:
    dataset: str
    key: str
    files: dict[str, bytes | Path]
    label: str | None = None          # "ext:<dataset>:<label>"
    group_label: str | None = None    # "ext:<dataset>:<group>"
    roles: dict[str, str] = field(default_factory=dict)  # file name → ArtifactRole value override
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def source(self) -> str:
        return f"ext:{self.dataset}"

    @property
    def incident_id(self) -> str:
        return f"ext-{self.dataset}-{slug(self.key)}"


SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def slug(s: str, n: int = 48) -> str:
    out = SLUG_RE.sub("-", s).strip("-.")[:n]
    return out or hashlib.sha256(s.encode()).hexdigest()[:12]


def unique_key(key: str) -> str:
    """Incident key that stays unique after `slug` truncation (48 chars): a 12-hex hash prefix, then the
    readable key. For adapters whose natural keys share long prefixes (URL folders, review/case paths)."""
    return f"{hashlib.sha256(key.encode()).hexdigest()[:12]}-{key}"


def norm_label(s: Any) -> str | None:
    """Raw label → stable lower-case token; empty / None / 'none' / 'nan' → None."""
    if s is None:
        return None
    t = re.sub(r"\s+", " ", str(s)).strip().lower()
    return None if t in ("", "none", "nan", "null", "unknown") else t


def prefixed(dataset: str, label: str | None) -> str | None:
    return f"ext:{dataset}:{label}" if label else None


def image_ext(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".bin"


def select(rows: Iterable[Row], *, limit: int | None = None, min_group: int = 1, per_group_cap: int | None = None,
           unlabelled: int = 0, where: Any = None) -> list[Row]:
    """Deterministic stratified sample: keep labels with >= `min_group` rows (the unit we evaluate linking on),
    at most `per_group_cap` rows per label, plus up to `unlabelled` rows without a label (open-set negatives).
    Order: by label size (largest first), then key; round-robin across labels so a `limit` keeps many groups."""
    by: dict[str | None, list[Row]] = defaultdict(list)
    for r in rows:
        if where is None or where(r):
            by[r.label].append(r)
    negatives = sorted(by.pop(None, []), key=lambda r: _h(r.key))[:unlabelled]
    groups = [sorted(v, key=lambda r: _h(r.key)) for k, v in by.items() if len(v) >= min_group]
    groups.sort(key=lambda g: (-len(g), g[0].label or ""))
    if per_group_cap:
        groups = [g[:per_group_cap] for g in groups]
    budget = None if limit is None else max(0, limit - len(negatives))
    picked: list[Row] = []
    if budget is None:
        picked = [r for g in groups for r in g]
    else:
        # whole groups first (a group cut to one member is useless for linking), largest first
        for g in groups:
            if len(picked) + len(g) <= budget:
                picked.extend(g)
            elif budget - len(picked) >= min_group:
                picked.extend(g[: budget - len(picked)])
            if len(picked) >= budget:
                break
    if limit is not None and len(picked) + len(negatives) > limit:
        negatives = negatives[: max(0, limit - len(picked))]
    return picked + negatives


def _h(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def parquet_files(root: Path, pattern: str) -> list[Path]:
    return sorted(root.glob(pattern))


def cached_index(root: Path, name: str, build: Any) -> list[Row]:
    """Index rows cached next to the data (`<root>/.hydra-index-<name>.json.gz`), rebuilt when the parquet
    files change (names + sizes + mtimes)."""
    import gzip
    import json

    files = sorted(p for p in root.rglob("*.parquet"))
    sig = hashlib.sha256("|".join(f"{p.relative_to(root)}:{p.stat().st_size}:{int(p.stat().st_mtime)}"
                                  for p in files).encode()).hexdigest()
    cache = root / f".hydra-index-{name}.json.gz"
    if cache.exists():
        try:
            d = json.loads(gzip.decompress(cache.read_bytes()))
            if d.get("sig") == sig:
                return [Row(r["key"], r["label"], r["group"], r.get("extra") or {}) for r in d["rows"]]
        except (OSError, ValueError, KeyError):
            pass
    rows = list(build())
    try:
        payload = {"sig": sig, "rows": [r.__dict__ for r in rows]}
        cache.write_bytes(gzip.compress(json.dumps(payload).encode()))
    except OSError:
        pass  # read-only data folder: no cache
    return rows


def iter_parquet(files: list[Path], columns: list[str], batch_size: int = 256) -> Iterator[dict[str, Any]]:
    import pyarrow.parquet as pq

    for f in files:
        pf = pq.ParquetFile(f)
        cols = [c for c in columns if c in pf.schema_arrow.names]
        for batch in pf.iter_batches(batch_size=batch_size, columns=cols):
            yield from batch.to_pylist()


def iter_parquet_keys(files: list[Path], key_col: str, keys: set[str], columns: list[str],
                      batch_size: int = 64) -> Iterator[dict[str, Any]]:
    """Rows whose `key_col` is in `keys`; row groups without a wanted key are skipped after reading the key
    column only, so big content columns (HTML, images) are read just for row groups that hold a wanted row."""
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    want = set(keys)
    for f in files:
        if not want:
            return
        pf = pq.ParquetFile(f)
        names = pf.schema_arrow.names
        cols = [c for c in dict.fromkeys([key_col, *columns]) if c in names]
        for rg in range(pf.num_row_groups):
            ks = pf.read_row_group(rg, columns=[key_col]).column(0)
            mask = pc.is_in(ks, value_set=pa.array(sorted(want), type=ks.type))
            if not pc.any(mask).as_py():
                continue
            tab = pf.read_row_group(rg, columns=cols).filter(mask)
            for r in tab.to_pylist():
                want.discard(r[key_col])
                yield r
