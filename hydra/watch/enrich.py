"""Stage B: metadata enrichment of high-scoring watch events through `hydra.enrich` (never touches the page).

RDAP (registrar, registration date, NS), DNS NS/A (public resolvers), crt.sh (only from `CRT_CUT` bits), CERT
Polska snapshot (offline) and MISP warninglists (offline). Live lookups only with HYDRA_ENRICH=1; otherwise the
replay cache answers and a miss is `unavailable`. `RateLimit` caps the number of enriched events per minute on
top of the per-source delays of `hydra.enrich.client`.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime
from typing import Any

from hydra.enrich import cert_pl, crtsh, dns, get_enricher, rdap
from hydra.enrich.domains import icann_domain, is_platform

CRT_CUT = 8.0
PER_MINUTE = 20


class RateLimit:
    def __init__(self, per_minute: int = PER_MINUTE) -> None:
        self.per_minute = per_minute
        self._t: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._t and now - self._t[0] > 60:
                    self._t.popleft()
                if len(self._t) < self.per_minute:
                    self._t.append(now)
                    return
                delay = 60 - (now - self._t[0]) + 0.05
            time.sleep(max(0.05, delay))


def _age_days(reg: str | None, seen: str | None) -> float | None:
    if not reg or not seen:
        return None
    try:
        a = datetime.fromisoformat(str(reg).replace("Z", "+00:00"))
        b = datetime.fromisoformat(str(seen).replace("Z", "+00:00"))
    except ValueError:
        return None
    if a.tzinfo is None or b.tzinfo is None:
        return None
    return round((b - a).total_seconds() / 86400, 1)


def enrich_host(host: str, first_seen: str | None, bits: float, settings=None) -> dict[str, Any]:
    enr = get_enricher(None, settings)
    out: dict[str, Any] = {"live": enr.live, "sources": {}}
    d = icann_domain(host)
    out["domain"] = d
    if d and not is_platform(host):
        r, s = rdap.domain(enr, d)
        out["sources"]["rdap"] = r.status
        if s:
            out["rdap"] = {k: s.get(k) for k in ("registration", "registrar", "nameservers", "expiration")}
            out["age_days"] = _age_days(s.get("registration"), first_seen)
        rn = dns.query(enr, d, "NS")
        out["sources"]["dns_ns"] = rn.status
        out["ns"] = sorted(x.lower().rstrip(".") for x in dns.answers(rn))
        if bits >= CRT_CUT:
            rc, sc = crtsh.lookup(enr, d)
            out["sources"]["crtsh"] = rc.status
            if sc:
                out["crtsh"] = {k: sc.get(k) for k in ("n_certs", "issuers", "first_cert")}
                out["crtsh"]["sans"] = (sc.get("sans") or [])[:20]
    ra = dns.query(enr, host, "A")
    out["sources"]["dns_a"] = ra.status
    out["a"] = [x for x in dns.answers(ra) if x.count(".") == 3]
    out["cert_pl"] = cert_pl.lookup(host)
    out["warninglists"] = _warn(out)
    out["stage_b"] = stage_b(out)
    return out


def _warn(e: dict[str, Any]) -> list[dict[str, str]]:
    try:
        from hydra.enrich import warninglists
    except ImportError:  # pragma: no cover
        return []
    hits = []
    for ns in e.get("ns") or []:
        h = warninglists.classify("ns_host", ns)
        if h is not None:
            hits.append({"value": ns, "list": h.list_name, "effect": h.effect})
    for ip in e.get("a") or []:
        h = warninglists.classify("dns_ip", ip)
        if h is not None:
            hits.append({"value": ip, "list": h.list_name, "effect": h.effect})
    return hits


def stage_b(e: dict[str, Any]) -> list[dict[str, Any]]:
    """Stage B reasons in bits (REALTIME_DISCOVERY.md §5.3): domain age; CERT list as a label only."""
    out = []
    age = e.get("age_days")
    if age is not None and age <= 7:
        out.append({"code": "young_domain", "bits": 2.0, "label_pl": "Domena zarejestrowana ≤ 7 dni przed wykryciem",
                    "detail": f"{age} dni"})
    elif age is not None and age <= 30:
        out.append({"code": "young_domain", "bits": 1.0, "label_pl": "Domena zarejestrowana ≤ 30 dni przed wykryciem",
                    "detail": f"{age} dni"})
    cp = e.get("cert_pl") or {}
    if cp.get("host_listed") or cp.get("parent_listed"):
        out.append({"code": "cert_pl_listed", "bits": 0.0, "label_pl": "Na liście ostrzeżeń CERT Polska (etykieta)",
                    "detail": cp.get("parent_listed") or "host"})
    return out


def infra_keys(e: dict[str, Any] | None) -> list[str]:
    """Operator-tier values that may join live campaigns: a non-shared, non-warninglisted NS set."""
    if not e:
        return []
    from hydra.genome.inf import SHARED_NS

    keys = []
    ns = sorted(set(e.get("ns") or (e.get("rdap") or {}).get("nameservers") or []))
    if ns and not any(any(x in n for x in SHARED_NS) for n in ns):
        shared = ("boilerplate", "label", "parked_domain", "platform")
        if not any(w.get("effect") in shared for w in e.get("warninglists") or []):
            keys.append("ns:" + ",".join(ns))
    # registrar + registration day is NOT used here: on the live feed one big registrar registers hundreds of
    # unrelated lure domains per day; it stays an assigner gene (registrar_day) weighted by corpus rarity.
    return keys
