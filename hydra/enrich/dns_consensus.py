"""Wildcard-aware DNS with a three-resolver majority (ADOPT #11, docs/cleanroom/dns_wildcard_consensus.md).

    resp, summ = check(enr, "x1.wild.test")     # summ["class"]: wildcard | current | not_current | disputed | None

A hostname seen in the material is asked on three resolvers (A and AAAA; the CNAME chain comes with the answer).
Random control names (prefix `hx` + 32 hex) are asked at the registrable domain and at every parent level between
it and the host. If the host's answer equals a control answer at the deepest level, the zone answers any label:
the host's addresses are a property of the zone, not of the host (class `wildcard`). Otherwise two of three
resolvers decide `current` (has an address) or `not_current` (clean negative); anything else is `disputed`.

One cache record per host (source `dns_consensus`) holds every individual answer and the control names used, so a
replay is offline and deterministic. With no cached record and no live mode the class is None (unknown): callers
keep the single-resolver `dns` answers. Live queries need HYDRA_ENRICH=1 and HYDRA_OFFLINE=0 (Enricher.live).
Budget: HYDRA_DNS_BUDGET queries per process (default 20000); a host whose probe ran out of budget is not cached
(retried next live run) and reads as `disputed`.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from hydra.enrich.client import DELAY, Enricher, Response
from hydra.enrich.dns import NAME_OK
from hydra.enrich.domains import is_ip, registrable_domain

SOURCE = "dns_consensus"
RESOLVERS3 = ("1.1.1.1", "8.8.8.8", "9.9.9.9")
RTYPES = ("A", "AAAA")
CONTROLS_PER_DEPTH = 3
MAJORITY = 2
NEGATIVE_CODES = {"NOERROR", "NXDOMAIN", "NODATA"}
WILDCARD, CURRENT, NOT_CURRENT, DISPUTED = "wildcard", "current", "not_current", "disputed"
CLASSES = (WILDCARD, CURRENT, NOT_CURRENT, DISPUTED)
DELAY.setdefault(SOURCE, 0.0)  # pacing happens per query inside the probe

# (name, rtype, resolver) -> {"rcode": str, "addrs": [...], "cnames": [...], "error": str | None}
Resolve = Callable[[str, str, str], dict[str, Any]]


class Budget:
    """Query budget shared by all probes of one run (thread-safe)."""

    def __init__(self, total: int):
        self.left = int(total)
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            if self.left <= 0:
                return False
            self.left -= 1
            return True


_BUDGET = Budget(int(os.environ.get("HYDRA_DNS_BUDGET", "20000") or 20000))


def control_depths(host: str, apex: str) -> list[str]:
    """Apex first, then each parent of `host` strictly between the apex and the host (deepest last)."""
    if host == apex or not host.endswith("." + apex):
        return [apex]
    labels = host.split(".")
    n_apex = len(apex.split("."))
    return [".".join(labels[-k:]) for k in range(n_apex, len(labels))]


def new_label(rng: Callable[[], str] | None = None) -> str:
    return "hx" + (rng() if rng else secrets.token_hex(16))


def dig_resolve(name: str, rtype: str, resolver: str, *, timeout_s: int = 4) -> dict[str, Any]:
    """One query on one resolver via the system `dig` (4 s, 1 try, as hydra.enrich.dns). Never raises."""
    if not NAME_OK.match(name) or rtype not in RTYPES:
        return {"rcode": "ERROR", "addrs": [], "cnames": [], "error": "invalid"}
    from hydra.netguard import offline

    if offline():  # `dig` is a subprocess: the in-process socket guard does not cover it (hydra.netguard §2)
        return {"rcode": "ERROR", "addrs": [], "cnames": [], "error": "offline"}
    dig = shutil.which("dig")
    if not dig:
        return {"rcode": "ERROR", "addrs": [], "cnames": [], "error": "no_dig"}
    try:
        p = subprocess.run(
            [dig, "+noall", "+answer", "+comments", f"+time={timeout_s}", "+tries=1", f"@{resolver}", "-t", rtype,
             "-q", name],
            capture_output=True, text=True, timeout=timeout_s * 3, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"rcode": "ERROR", "addrs": [], "cnames": [], "error": type(exc).__name__}
    return parse_dig(p.stdout, rtype)


def parse_dig(out: str, rtype: str) -> dict[str, Any]:
    m = re.search(r"status: ([A-Z]+)", out)
    rcode = m.group(1) if m else "ERROR"
    addrs: set[str] = set()
    cnames: set[str] = set()
    for line in out.splitlines():
        if not line or line.startswith(";"):
            continue
        parts = line.split(None, 4)
        if len(parts) != 5:
            continue
        if parts[3] == rtype and is_ip(parts[4].strip()):
            addrs.add(parts[4].strip())
        elif parts[3] == "CNAME":
            cnames.add(parts[4].strip().lower().rstrip("."))
    if rcode == "NOERROR" and not addrs and not cnames:
        rcode = "NODATA"
    err = None if rcode in NEGATIVE_CODES else (rcode.lower() if m else "noresp")
    return {"rcode": rcode, "addrs": sorted(addrs), "cnames": sorted(cnames), "error": err}


def _norm(ans: dict[str, Any]) -> dict[str, Any]:
    import ipaddress

    addrs = set()
    for a in ans.get("addrs") or []:
        try:
            addrs.add(str(ipaddress.ip_address(str(a).strip())))
        except ValueError:
            continue
    cnames = {str(c).lower().rstrip(".") for c in ans.get("cnames") or [] if c}
    rcode = str(ans.get("rcode") or "ERROR").upper()
    if rcode == "NOERROR" and not addrs and not cnames:
        rcode = "NODATA"
    err = ans.get("error") or (None if rcode in NEGATIVE_CODES else rcode.lower())
    return {"rcode": rcode, "addrs": sorted(addrs), "cnames": sorted(cnames), "error": err}


def probe(
    host: str,
    apex: str,
    *,
    resolvers: tuple[str, ...] = RESOLVERS3,
    resolve: Resolve | None = None,
    budget: Budget | None = None,
    rng: Callable[[], str] | None = None,
    pace_s: float = 0.0,
) -> dict[str, Any]:
    """Ask the candidate and the controls on every resolver; return the raw evidence (what is cached)."""
    resolve = resolve or dig_resolve
    budget = budget or _BUDGET
    controls = {d: [f"{new_label(rng)}.{d}" for _ in range(CONTROLS_PER_DEPTH)] for d in control_depths(host, apex)}
    out: dict[str, Any] = {"host": host, "apex": apex, "resolvers": list(resolvers), "controls": controls,
                           "answers": [], "budget_exhausted": False}
    jobs = [(host, None)] + [(n, d) for d, names in controls.items() for n in names]
    for name, depth in jobs:
        for res in resolvers:
            for t in RTYPES:
                if not budget.take():
                    out["budget_exhausted"] = True
                    return out
                try:
                    ans = _norm(resolve(name, t, res))
                except Exception as exc:  # noqa: BLE001 — a failing resolver is evidence, never a crash
                    ans = {"rcode": "ERROR", "addrs": [], "cnames": [], "error": type(exc).__name__}
                out["answers"].append({"resolver": res, "name": name, "depth": depth, "rtype": t, **ans})
                if pace_s:
                    time.sleep(pace_s)
    return out


def _sig(a: dict[str, Any]) -> frozenset[str]:
    return frozenset(a["addrs"]) | frozenset("cname:" + c for c in a["cnames"])


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Equal record sets, or (CDN rotation) the same non-empty CNAME targets."""
    return _sig(a) == _sig(b) or (bool(a["cnames"]) and a["cnames"] == b["cnames"])


def classify(data: dict[str, Any]) -> dict[str, Any]:
    """Class + usable records (candidate answers only) + per-address resolver agreement. Pure and deterministic."""
    host, apex = data.get("host"), data.get("apex")
    answers = data.get("answers") or []
    resolvers = list(data.get("resolvers") or [])
    cand = [a for a in answers if a.get("depth") is None]
    addrs = sorted({x for a in cand for x in a["addrs"]})
    cnames = sorted({x for a in cand for x in a["cnames"]})
    agree = {x: len({a["resolver"] for a in cand if x in a["addrs"]}) for x in addrs}
    per_res: dict[str, dict[str, Any]] = {}
    for a in cand:
        r = per_res.setdefault(a["resolver"], {"addr": False, "err": False, "n": 0})
        r["addr"] |= bool(a["addrs"])
        r["err"] |= bool(a.get("error")) or a["rcode"] not in NEGATIVE_CODES
        r["n"] += 1
    with_addr = sum(1 for r in per_res.values() if r["addr"])
    clean_neg = sum(1 for r in per_res.values() if not r["addr"] and not r["err"] and r["n"] == len(RTYPES))
    depths = list((data.get("controls") or {}).keys())
    deepest = depths[-1] if depths else None
    ctrl = [a for a in answers if a.get("depth") == deepest and (a["addrs"] or a["cnames"])]
    # a candidate answer equal to a control answer of the same resolver and type; two resolvers must agree, so one
    # filtering resolver that answers a block-page IP for every name cannot make a zone look wildcarded
    wild_res = {
        c["resolver"] for c in cand if (c["addrs"] or c["cnames"])
        and any(k["resolver"] == c["resolver"] and k["rtype"] == c["rtype"] and _same(c, k) for k in ctrl)
    }
    wildcard = len(wild_res) >= MAJORITY
    # shallower depths are evidence only; note them so the analyst sees a zone-wide wildcard
    wild_depths = sorted({a["depth"] for a in answers if a.get("depth") and (a["addrs"] or a["cnames"])},
                         key=lambda d: depths.index(d))
    if data.get("budget_exhausted"):
        cls = DISPUTED
    elif wildcard:
        cls = WILDCARD
    elif with_addr >= MAJORITY:
        cls = CURRENT
    elif clean_neg >= MAJORITY:
        cls = NOT_CURRENT
    else:
        cls = DISPUTED
    return {
        "class": cls,
        "host": host,
        "apex": apex,
        "wildcard_zone": deepest if cls == WILDCARD else None,
        "answering_control_depths": wild_depths,
        "addrs": addrs,
        "cnames": cnames,
        "agreement": f"{with_addr}/{len(resolvers) or 3}",
        "wildcard_resolvers": sorted(wild_res),
        "addr_agreement": agree,
        "clean_negatives": clean_neg,
        "controls": data.get("controls") or {},
        "budget_exhausted": bool(data.get("budget_exhausted")),
    }


def check(
    enr: Enricher,
    host: str,
    *,
    resolvers: tuple[str, ...] = RESOLVERS3,
    resolve: Resolve | None = None,
    budget: Budget | None = None,
    rng: Callable[[], str] | None = None,
) -> tuple[Response, dict[str, Any]]:
    """Cached consensus for one host. Replay: cache only; class None when there is no record (unknown)."""
    if len(set(resolvers)) != 3:
        raise ValueError("dns consensus needs exactly three distinct resolvers")
    host = host.lower().rstrip(".")
    if not NAME_OK.match(host) or is_ip(host):
        return Response(SOURCE, host, "error:invalid", None, None, None), {"class": None}
    apex = registrable_domain(host) or ".".join(host.split(".")[-2:])  # reserved TLDs (.test) are not in the PSL

    def fetch() -> tuple[str | None, str, Any]:
        data = probe(host, apex, resolvers=resolvers, resolve=resolve, budget=budget, rng=rng,
                     pace_s=DELAY.get("dns", 0.05) if resolve is None else 0.0)
        if data["budget_exhausted"]:
            return f"dns-consensus://{host}", "error:budget", None
        return f"dns-consensus://{host}", "ok", data

    r = enr.get(SOURCE, host, fetch)
    if r.ok:
        return r, classify(r.data)
    if r.status == "error:budget":
        return r, {"class": DISPUTED, "host": host, "apex": apex, "budget_exhausted": True, "addrs": [], "cnames": []}
    return r, {"class": None}
