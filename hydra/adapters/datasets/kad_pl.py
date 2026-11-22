"""KAD - STOP Przekrętom (github.com/FiltersHeroes/KAD, CC BY-SA 4.0 → EVAL-ONLY: share-alike): community uBlock /
AdGuard filter list against threats on the Polish web (fake shops, SMS-subscription traps, scam investment,
redirectors to scams), with entries from CERT Polska, legalniewsieci.pl and KNF. The list is split into sections;
the section is the category the maintainers gave the domain. The `przekrety_CERT` section (a mirror of the CERT
Polska list, already in `ext:certpl-warnings`) is not fetched.

Layout: `sections/<section>.txt` with Adblock rules. Only network rules `||host[/path]^$...` are read
(cosmetic rules, regex rules and wildcards other than a leading `*` are skipped). One host (or host/path) = one
incident: `url.txt` with `http://<entry>` (synthesised, flagged in meta). No per-entry campaign field:
`label = None`, `group_label = ext:kad-pl:<section category>`. Every entry targets Polish users, so
`meta.lang = "pl"` (targeting, not page language, as for certpl-warnings). Domain strings only; pages are never fetched.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    prefixed,
)

INFO = DatasetInfo(
    name="kad-pl", title="KAD - STOP Przekrętom (Polish scam / fake-shop filter list, by section)",
    homepage="https://github.com/FiltersHeroes/KAD",
    licence="CC BY-SA 4.0 (LICENSE, README, list header)", verdict="EVAL-ONLY", modality="url",
    label_kind="none", dir="kad-pl", citation="FiltersHeroes, KAD - STOP Przekrętom",
)
# section file → category; order = precedence when an entry appears in several sections
SECTIONS = {
    "przekrety": "scam",
    "przekrety-sciezki-parametry": "scam_path",
    "przekierowujace_do_przekretow": "redirect_to_scam",
    "podejrzane_inne_oszustwa": "suspicious_other_fraud",
    "natretne_lub_wprowadzajace_w_blad": "intrusive_or_misleading",
    "szybko_wygaszajace": "short_lived",
    "blogspot": "blogspot_scam",
}
RULE_RE = re.compile(r"^\|\|\*?([a-z0-9][a-z0-9.-]*\.[a-z0-9-]{2,}(?:/[^\s^$*|]*)?)\^?(?:\$|$)", re.I)


def _entries(path: Path) -> Iterator[str]:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = RULE_RE.match(line.strip())
        if m:
            yield m.group(1).lower().rstrip("/")


def index(root: Path) -> list[Row]:
    seen: dict[str, Row] = {}
    known = [(s, root / "sections" / f"{s}.txt") for s in SECTIONS]
    extra = [(p.stem, p) for p in sorted((root / "sections").glob("*.txt")) if p.stem not in SECTIONS
             and "cert" not in p.stem.lower() and not p.stem.lower().endswith(("_ag", "_ubo", "_legacy", "wildcard"))]
    for sec, p in known + extra:
        if not p.is_file():
            continue
        cat = SECTIONS.get(sec, sec)
        for e in _entries(p):
            if e not in seen:
                seen[e] = Row(e, None, cat, {"lang": "pl", "section": sec, "has_path": "/" in e})
    return list(seen.values())


def where(category: str | None = None, hosts_only: bool = False):
    if not (category or hosts_only):
        return None
    return lambda r: (category is None or r.group == category) and (not hosts_only or not r.extra.get("has_path"))


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        url = f"http://{r.key}" + ("" if r.extra.get("has_path") else "/")
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={URL_FILE: (url + "\n").encode()},
            label=None, group_label=prefixed(INFO.name, r.group),
            meta={"entry": r.key, "url": url, "url_synthesised": True, "class": r.group,
                  "section": r.extra.get("section"), "lang": "pl", "label_kind": INFO.label_kind},
        )
