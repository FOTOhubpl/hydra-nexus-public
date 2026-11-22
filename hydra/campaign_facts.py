"""Facts about one campaign for exports (abuse package, STIX, lifecycle, pivot queries). Read-only.

Hosts come from the campaign members' operator-tier infrastructure genes (landing host, registrable domain, entry
URL, server IPs); platform/boilerplate hosts are never reported. Enrichment facts are read from the replay cache
only (`Enricher(live=False)`): exports never trigger a network call. Every value that leaves through an export goes
through `hydra.redact`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from hydra.config import Settings
from hydra.contracts import Campaign, Gene
from hydra.entropy import is_stop
from hydra.redact import redact_text, redact_url

HOST_KINDS = {"landing_host", "host", "first_party_host", "host_contacted"}
DOMAIN_KINDS = {"registrable_domain", "regdom", "domain"}
URL_KINDS = {"entry_url", "url", "url_in_text"}
IP_KINDS = {"server_ip", "dns_ip", "ip", "ip_firstparty"}
HASH_KINDS = {"artifact_sha256", "asset_sha256"}
NOT_OPERATOR = ("boilerplate", "platform", "label")


@dataclass
class CampaignView:
    run_id: str
    campaign: Campaign
    incidents: dict[str, dict[str, Any]]           # id → {title, observed_at, source, source_ref}
    genes: dict[str, list[Gene]] = field(default_factory=dict)


def _tier_ok(g: Gene) -> bool:
    grp = (g.dependency_group or "").lower()
    return not any(f":{t}:" in grp or grp.endswith(f":{t}") for t in NOT_OPERATOR)


def run_known(conn: sqlite3.Connection, run_id: str) -> bool:
    """An explicit run id names stored results: a `runs` row, or campaigns/pairs written under it
    (CR-ASTRA-PIPE-302: an unknown run is a 404, never an empty or global evidence package)."""
    return conn.execute("SELECT 1 FROM runs WHERE id=? UNION ALL SELECT 1 FROM campaigns WHERE run_id=?"
                        " UNION ALL SELECT 1 FROM pairs WHERE run_id=? LIMIT 1", (run_id, run_id, run_id)
                        ).fetchone() is not None


def load_campaign(conn: sqlite3.Connection, campaign_id: str, run_id: str | None = None) -> CampaignView | None:
    from hydra.pipeline import _row_gene, latest_run

    run_id = run_id or latest_run(conn)
    if run_id is None:
        return None
    row = conn.execute("SELECT detail FROM campaigns WHERE run_id = ? AND id = ?", (run_id, campaign_id)).fetchone()
    if row is None:
        return None
    camp = Campaign.model_validate(json.loads(row["detail"]))
    q = ",".join("?" * len(camp.members)) or "''"
    incs = {r["id"]: dict(r) for r in conn.execute(
        f"SELECT id, title, observed_at, source, source_ref FROM incidents WHERE id IN ({q})", camp.members)}
    genes: dict[str, list[Gene]] = {m: [] for m in camp.members}
    for r in conn.execute(f"SELECT * FROM genes WHERE incident_id IN ({q}) AND status = 'observed'", camp.members):
        genes[r["incident_id"]].append(_row_gene(r))
    return CampaignView(run_id, camp, incs, genes)


def campaign_hosts(view: CampaignView) -> list[dict[str, Any]]:
    """One entry per reported host: registrable domain, incidents, entry URLs (redacted), server IPs."""
    from hydra.enrich.domains import icann_domain, is_ip, is_platform

    hosts: dict[str, dict[str, Any]] = {}

    def entry(host: str) -> dict[str, Any] | None:
        host = host.lower().strip().rstrip(".")
        if not host or is_ip(host) or is_stop("host", host) or is_platform(host):
            return None
        return hosts.setdefault(host, {"host": host, "domain": icann_domain(host) or host, "incidents": set(),
                                       "urls": set(), "ips": set()})

    for iid, genes in view.genes.items():
        ips = {g.value for g in genes if g.kind in IP_KINDS and g.value and _tier_ok(g)}
        for g in genes:
            if not g.value or not _tier_ok(g):
                continue
            if g.kind in HOST_KINDS or g.kind in DOMAIN_KINDS:
                e = entry(g.value)
            elif g.kind in URL_KINDS and g.value.startswith(("http://", "https://")):
                e = entry(urlsplit(g.value).hostname or "")
                if e is not None:
                    e["urls"].add(redact_url(g.value))
            else:
                continue
            if e is not None:
                e["incidents"].add(iid)
                e["ips"] |= ips
    out = []
    for h in sorted(hosts.values(), key=lambda x: (-len(x["incidents"]), x["host"])):
        out.append({**h, "incidents": sorted(h["incidents"]), "urls": sorted(h["urls"])[:10],
                    "ips": sorted(h["ips"])[:10]})
    return out


def enricher(settings: Settings):
    from hydra.enrich.client import Enricher

    return Enricher(settings=settings, live=False)  # replay only: exports never call the network


def _cached(enr, source: str, query: str) -> tuple[Any, str | None, str | None, str]:
    r = enr.get(source, query, None)
    return (r.data if r.ok else None), r.fetched_at, (str(r.cache_path) if r.cache_path else None), r.status


def rdap_domain(enr, domain: str) -> dict[str, Any]:
    from hydra.enrich.rdap import summarize_domain

    raw, fetched_at, path, status = _cached(enr, "rdap", domain)
    return {"status": status, "fetched_at": fetched_at, "cache_path": path,
            "summary": summarize_domain(raw) if isinstance(raw, dict) else {},
            "abuse": abuse_contacts(raw) if isinstance(raw, dict) else []}


def crtsh_domain(enr, domain: str) -> dict[str, Any]:
    from hydra.enrich.crtsh import summarize

    raw, fetched_at, path, status = _cached(enr, "crtsh", domain)
    return {"status": status, "fetched_at": fetched_at, "cache_path": path,
            "summary": summarize(raw) if raw is not None else {}}


def rdap_ip(enr, ip: str) -> dict[str, Any]:
    raw, fetched_at, path, status = _cached(enr, "rdap_ip", ip)
    j = raw if isinstance(raw, dict) else {}
    return {"status": status, "fetched_at": fetched_at, "name": j.get("name"), "country": j.get("country"),
            "abuse": abuse_contacts(j) if j else []}


def abuse_contacts(raw: dict[str, Any]) -> list[dict[str, str]]:
    """E-mail addresses of RDAP entities with role `abuse` (nested entities included), e.g. registrar abuse."""
    out: list[dict[str, str]] = []

    def walk(ents: list, parent_roles: list[str]) -> None:
        for ent in ents or []:
            roles = [str(r) for r in ent.get("roles") or []]
            vc = ent.get("vcardArray") or [None, []]
            name, emails = None, []
            for v in vc[1] if len(vc) > 1 and isinstance(vc[1], list) else []:
                if v and v[0] == "fn":
                    name = v[3]
                if v and v[0] == "email":
                    emails.append(str(v[3]))
            if "abuse" in roles:
                for e in emails:
                    out.append({"email": e, "name": name or "", "of": ",".join(parent_roles) or "domain"})
            walk(ent.get("entities") or [], roles)

    walk(raw.get("entities") or [], [])
    seen, uniq = set(), []
    for c in out:
        if c["email"].lower() not in seen:
            seen.add(c["email"].lower())
            uniq.append(c)
    return uniq


def evidence_artifacts(conn: sqlite3.Connection, members: list[str]) -> list[dict[str, Any]]:
    q = ",".join("?" * len(members)) or "''"
    return [dict(r) for r in conn.execute(
        f"SELECT ia.incident_id, ia.artifact_id, ia.role, ia.path_in_incident, a.mime, a.size, a.created_at"
        f" FROM incident_artifacts ia JOIN artifacts a ON a.id = ia.artifact_id WHERE ia.incident_id IN ({q})"
        " ORDER BY ia.incident_id, ia.path_in_incident", members)]


def summary_line(view: CampaignView) -> str:
    c = view.campaign
    return redact_text(f"Kampania {c.id} „{c.label}”: {len(c.members)} incydentów, schemat {c.scheme or 'nieznany'},"
                       f" pierwsze wystąpienie {c.first_seen or 'brak daty'}, ostatnie {c.last_seen or 'brak daty'}")
