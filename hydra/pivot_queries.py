"""Copyable pivot queries per campaign for external tools. Strings only: HYDRA never calls these services.

From the campaign's reported hosts and core genes: crt.sh (certificate transparency), urlscan.io search syntax,
Shodan and Censys search syntax, plus a DNS / RDAP lookup list. The analyst pastes them by hand under each
service's own terms of use (COMPETITIVE_GAPS.md §5: no urlscan/Censys/Shodan API in the shipped path).
"""

from __future__ import annotations

from contextlib import closing
from typing import Any
from urllib.parse import quote

from hydra.campaign_facts import CampaignView, campaign_hosts, load_campaign
from hydra.config import Settings
from hydra.contracts import REDACTED
from hydra.db import connect
from hydra.entropy import is_stop

NOTE_PL = ("Zapytania do ręcznego wklejenia w serwisach zewnętrznych (zgodnie z ich regulaminami). HYDRA NEXUS nie"
           " wywołuje tych serwisów.")


def _ok(v: str | None) -> bool:
    return bool(v) and REDACTED not in str(v)


def pivots_for(view: CampaignView) -> dict[str, Any]:
    hosts = campaign_hosts(view)
    domains = sorted({h["domain"] for h in hosts})
    core = view.campaign.core_genome
    hashes = sorted({g.value_a for g in core if g.kind in ("asset_sha256", "artifact_sha256") and _ok(g.value_a)
                     and len(g.value_a or "") == 64})
    favicons = sorted({g.value for gs in view.genes.values() for g in gs if g.kind == "favicon_mmh3" and _ok(g.value)
                       and not is_stop(g.kind, g.value)})
    trackers = sorted({g.value for gs in view.genes.values() for g in gs if g.kind in ("tracker_id", "operator_id")
                       and _ok(g.value) and len(g.value or "") <= 80})
    q: dict[str, list[dict[str, str]]] = {"crtsh": [], "urlscan": [], "shodan": [], "censys": [], "dns_rdap": []}
    for d in domains:
        q["crtsh"].append({"query": f"%.{d}", "url": f"https://crt.sh/?q={quote('%.' + d)}",
                           "why": "certyfikaty subdomen (CT)"})
        q["urlscan"].append({"query": f"domain:{d}", "why": "skany stron w domenie"})
        q["shodan"].append({"query": f"hostname:{d}", "why": "usługi z nazwą hosta w domenie"})
        q["censys"].append({"query": f"dns.names: {d}", "why": "hosty z tą nazwą DNS"})
        q["dns_rdap"] += [{"query": f"dig +short A {d}", "why": "adresy IP"},
                          {"query": f"dig +short NS {d}", "why": "serwery nazw"},
                          {"query": f"https://rdap.org/domain/{d}", "why": "rejestracja i kontakt abuse (RDAP)"}]
    for h in hosts:
        q["urlscan"].append({"query": f"page.domain:{h['host']}", "why": "strony serwowane z hosta"})
        q["shodan"].append({"query": f"ssl.cert.subject.cn:{h['host']}", "why": "certyfikat z tą nazwą"})
        q["censys"].append({"query": f'services.tls.certificates.leaf_data.subject.common_name: "{h["host"]}"',
                            "why": "certyfikat z tą nazwą"})
        for ip in h["ips"][:3]:
            q["dns_rdap"].append({"query": f"https://rdap.org/ip/{ip}", "why": "sieć hostingu i kontakt abuse"})
    for f in favicons:
        q["shodan"].append({"query": f"http.favicon.hash:{f}", "why": "ta sama ikona strony (mmh3)"})
    for hsh in hashes:
        q["urlscan"].append({"query": f"hash:{hsh}", "why": "ten sam plik zasobu strony (sha256)"})
    for t in trackers:
        v = t.split(":", 1)[-1]
        q["urlscan"].append({"query": f'"{v}"', "why": "ten sam identyfikator śledzący/operatora (pełny tekst)"})
    return {"campaign_id": view.campaign.id, "run_id": view.run_id, "note_pl": NOTE_PL,
            "counts": {k: len(v) for k, v in q.items()}, "queries": q}


def campaign_pivots(settings: Settings, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        view = load_campaign(conn, campaign_id, run_id)
    if view is None:
        raise KeyError(f"campaign {campaign_id} not found")
    return pivots_for(view)
