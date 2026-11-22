"""Watchlists: operator indicators derived from analysed campaigns (genes of member incidents) plus manual entries,
and the in-memory exact-match index the engine uses for every new feed item.

Derivation reads the latest analyse run (read-only connection) and never changes any scoring table. Campaign
derived values stay local: they are never sent to any source (the CT source queries manual keywords only).
`df` / `n_incidents` are the number of incidents carrying the value / incidents with genes, for the informational
rarity `bits = log2((N + 1) / (df + 1))` shown next to a match (heuristic, not a probability, not a linking rule).
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from hydra.watch.rt import indicators as ind

# gene kind -> watch kind (only kinds that identify an operator asset or its landing infrastructure)
GENE_MAP: dict[str, str] = {
    "landing_host": "host", "cert_pl_listed_host": "host", "host_contacted": "host", "lead_endpoint": "url",
    "entry_url": "url", "registrable_domain": "domain", "domain_in_text": "domain",
    "entity_phone": "phone", "entity_iban": "iban", "crypto_wallet": "wallet", "tracker_id": "tracker",
    "ad_campaign_id": "ad_id", "platform_ad_id": "ad_id", "platform_page_id": "page_id", "server_ip": "ip",
}
MAX_ENTRIES = 50_000


def _is_platform(host: str) -> bool:
    try:
        from hydra.enrich.domains import is_boilerplate, is_platform

        return bool(is_platform(host) or is_boilerplate(host))
    except Exception:  # noqa: BLE001
        return False


def latest_run(conn: sqlite3.Connection) -> str | None:
    try:
        from hydra.api_task import resolve_run

        return resolve_run(conn, None)
    except Exception:  # noqa: BLE001
        r = conn.execute("SELECT id FROM runs WHERE kind='analyse' ORDER BY created_at DESC LIMIT 1").fetchone()
        return r[0] if r else None


def derive(conn: sqlite3.Connection, run_id: str | None = None) -> list[dict[str, Any]]:
    """Campaign-derived entries of one run (default: the latest analyse run). Read-only."""
    run = run_id or latest_run(conn)
    if not run:
        return []
    labels = {r[0]: r[1] for r in conn.execute("SELECT id, label FROM campaigns WHERE run_id=?", (run,))}
    members: dict[str, str] = {}
    for cid, iid in conn.execute("SELECT campaign_id, incident_id FROM campaign_members WHERE run_id=?", (run,)):
        members.setdefault(iid, cid)
    if not members:
        return []
    kinds = tuple(GENE_MAP)
    q = ",".join("?" * len(kinds))
    n_inc = conn.execute("SELECT COUNT(DISTINCT incident_id) FROM genes").fetchone()[0] or 0
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    df: dict[tuple[str, str], set[str]] = defaultdict(set)
    rows = conn.execute(f"SELECT incident_id, kind, value, dependency_group FROM genes WHERE kind IN ({q}) "
                        "AND value IS NOT NULL", kinds).fetchall()
    for iid, gkind, value, dep in rows:
        wkind = GENE_MAP[gkind]
        if gkind == "entity_phone" and dep and ":operator:" not in str(dep) and ":operator" not in str(dep):
            continue  # hotlines / platform numbers are boilerplate, never an operator indicator
        key = ind.normalise(wkind, value)
        if not key:
            continue
        if wkind in ("host", "domain") and _is_platform(key):
            continue
        if wkind == "url" and _is_platform(ind.norm_host(key) or ""):
            continue
        df[(wkind, key)].add(iid)
        cid = members.get(iid)
        if cid is None:
            continue
        e = by_key.setdefault((wkind, key, cid), {
            "kind": wkind, "value": key, "display": ind.display(wkind, key), "origin": "campaign",
            "campaign_id": cid, "run_id": run, "label": labels.get(cid), "incident_ids": set()})
        e["incident_ids"].add(iid)
    out = []
    for (wkind, key, _cid), e in by_key.items():
        e["incident_ids"] = sorted(e["incident_ids"])[:50]
        e["df"] = len(df[(wkind, key)])
        e["n_incidents"] = n_inc
        out.append(e)
        if len(out) >= MAX_ENTRIES:
            break
    return out


def rarity_bits(df: int | None, n: int | None) -> float | None:
    if not df or not n:
        return None
    return round(math.log2((n + 1) / (df + 1)), 2)


@dataclass
class Entry:
    id: int
    kind: str
    value: str
    origin: str
    campaign_id: str
    run_id: str | None
    label: str | None
    incident_ids: list[str]
    df: int | None
    n_incidents: int | None


@dataclass
class Index:
    """(kind, value) -> entries. Rebuilt from watch_watchlist after every change (tens of thousands of rows: ms)."""

    by_key: dict[tuple[str, str], list[Entry]] = field(default_factory=lambda: defaultdict(list))
    keywords: list[str] = field(default_factory=list)
    size: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def load(self, conn: sqlite3.Connection) -> None:
        by: dict[tuple[str, str], list[Entry]] = defaultdict(list)
        kws: list[str] = []
        n = 0
        for r in conn.execute("SELECT id, kind, value, origin, campaign_id, run_id, label, incident_ids, df, "
                              "n_incidents FROM watch_watchlist"):
            e = Entry(r[0], r[1], r[2], r[3], r[4], r[5], r[6], json.loads(r[7] or "[]"), r[8], r[9])
            if e.kind == "keyword":
                if e.origin == "manual":  # only operator-typed terms ever leave the machine (crt.sh query)
                    kws.append(e.value)
                continue
            by[(e.kind, e.value)].append(e)
            n += 1
        with self.lock:
            self.by_key, self.keywords, self.size = by, sorted(set(kws)), n

    def match(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str, Entry]]:
        out = []
        by = self.by_key
        for kind, value in pairs:
            for e in by.get((kind, value), ()):
                out.append((kind, value, e))
            if kind == "phone" and not value.startswith("h:"):
                # inf_dom phone genes are stored as the salted hash of the E.164 number: look that key up too
                for e in by.get((kind, _phone_hash(value)), ()):
                    out.append((kind, value, e))
        return out


def _phone_hash(e164: str) -> str:
    from hydra.genome.webparse import pii_hash

    return pii_hash(e164)


def strength(kind: str, entry: Entry) -> str:
    """strong: the value names one operator asset (host, URL, account, wallet, tracker, ad/page id). weak: a
    registrable domain or an IP (shared hosting, free subdomain services)."""
    return "strong" if kind in ind.STRONG_KINDS else "weak"
