"""Corpus flag: which dataset an incident belongs to, and which corpora a run may use.

Mentor rule (docs/NASK_TASK.md, 2026-11-21 ~16:00): larger external datasets may train/calibrate the system and
enlarge the corpus, but must be clearly separated from organiser data, and the system must be able to run on the
NASK sample only. The corpus of an incident is derived from `incidents.source`:

| source | corpus |
|---|---|
| `nask-sample`, `nask*` | `nask` |
| `upload`, `url`, `screenshot`, `api` | `upload` (analyst material; belongs to the NASK mode) |
| `ext:<name>` | `ext:<name>` (DATA-HUNT adapters, hydra/adapters/datasets) |
| `public:<name>` | `public:<name>` (CERT PL / PhishTank corpus) |

Run specs (`--corpus`): `nask` (default: nask + upload), `extended` (nask + upload + every ext:*), `all`
(everything), or explicit names separated by commas (`ext:foo,public:certpl`, `nask,ext:foo`).
"""

from __future__ import annotations

import sqlite3

NASK = "nask"
UPLOAD = "upload"
UPLOAD_SOURCES = {"upload", "url", "screenshot", "api", "cli"}


def corpus_of(source: str | None) -> str:
    s = (source or "").strip()
    if s.startswith(("ext:", "public:")):
        return s
    if s in UPLOAD_SOURCES:
        return UPLOAD
    return NASK  # nask-sample and anything organiser-like defaults to the protected NASK corpus


def is_external(corpus: str) -> bool:
    return corpus.startswith(("ext:", "public:"))


def matches(corpus: str, spec: str | None) -> bool:
    """Does an incident of `corpus` belong to a run with `spec`? `None` = default NASK mode."""
    spec = (spec or NASK).strip()
    if spec == "all":
        return True
    for part in (p.strip() for p in spec.split(",") if p.strip()):
        if part == NASK and corpus in (NASK, UPLOAD):
            return True
        if part == "extended" and (corpus in (NASK, UPLOAD) or corpus.startswith("ext:")):
            return True
        if part in ("ext", "ext:*", "ext:all") and corpus.startswith("ext:"):
            return True
        if part in ("public", "public:*", "public:all") and corpus.startswith("public:"):
            return True
        if part == corpus:
            return True
    return False


def incidents_in(conn: sqlite3.Connection, spec: str | None) -> list[str]:
    return sorted(r["id"] for r in conn.execute("SELECT id, source FROM incidents") if matches(corpus_of(r["source"]),
                                                                                              spec))


def corpus_counts(conn: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in conn.execute("SELECT source FROM incidents"):
        c = corpus_of(r["source"])
        out[c] = out.get(c, 0) + 1
    return dict(sorted(out.items()))
