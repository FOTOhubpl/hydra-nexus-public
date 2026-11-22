"""RDAP (registration data) via the rdap.org bootstrap: registrar, dates, status, nameservers; IP networks.

Terms: registry RDAP is ICANN-mandated public data (registrant fields GDPR-redacted); each registry states its
terms in `notices` (query-based access only, no bulk harvesting). rdap.org is a free bootstrap redirector.
"""

from __future__ import annotations

from typing import Any

from hydra.enrich.client import Enricher, Response, http_json


def domain(enr: Enricher, d: str) -> tuple[Response, dict[str, Any]]:
    r = enr.get("rdap", d, lambda: http_json(f"https://rdap.org/domain/{d}"))
    return r, summarize_domain(r.data) if r.ok else {}


def summarize_domain(j: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(j, dict):
        return out
    for ev in j.get("events") or []:
        a = str(ev.get("eventAction", ""))
        if a in ("registration", "expiration", "last changed"):
            out[a.replace(" ", "_")] = ev.get("eventDate")
    out["status"] = j.get("status")
    out["nameservers"] = sorted(
        str(n.get("ldhName", "")).lower().rstrip(".") for n in j.get("nameservers") or [] if n.get("ldhName")
    )
    for ent in j.get("entities") or []:
        if "registrar" in (ent.get("roles") or []):
            name = None
            vc = ent.get("vcardArray") or [None, []]
            for v in vc[1] if len(vc) > 1 else []:
                if v and v[0] == "fn":
                    name = v[3]
            out["registrar"] = name or ent.get("handle")
            out["registrar_iana"] = next(
                (
                    p.get("identifier")
                    for p in ent.get("publicIds") or []
                    if str(p.get("type", "")).lower().startswith("iana")
                ),
                None,
            )
    return out


def ip(enr: Enricher, addr: str) -> tuple[Response, dict[str, Any]]:
    r = enr.get("rdap_ip", addr, lambda: http_json(f"https://rdap.org/ip/{addr}"))
    j = r.data if r.ok and isinstance(r.data, dict) else {}
    return r, (
        {
            "name": j.get("name"),
            "handle": j.get("handle"),
            "country": j.get("country"),
            "start": j.get("startAddress"),
            "end": j.get("endAddress"),
        }
        if j
        else {}
    )
