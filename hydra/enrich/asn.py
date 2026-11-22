"""IP -> ASN, prefix, country and AS name via the Team Cymru DNS interface (free community service).

`<reversed-ip>.origin.asn.cymru.com TXT` and `AS<n>.asn.cymru.com TXT`; stored as DNS cache records.
Terms: https://www.team-cymru.com/ip-asn-mapping (free, no guarantees; bulk users should use the whois bulk port).
"""

from __future__ import annotations

import ipaddress
from typing import Any

from hydra.enrich import dns
from hydra.enrich.client import Enricher, Response

# CDN / cloud front-door networks: one edge IP serves thousands of unrelated sites, so an IP (or /24) inside them
# is boilerplate. Public corpus (PUBLIC_CORPUS.md): 647 same-campaign links held only by a shared Google/Fastly/
# Amazon address (45 firebaseapp.com tenants on one IP).
EDGE_ASNS: dict[int, str] = {
    13335: "cloudflare",
    209242: "cloudflare",
    16509: "amazon",
    14618: "amazon",
    8987: "amazon",
    15169: "google",
    396982: "google",
    19527: "google",
    36040: "google",
    8075: "microsoft",
    8068: "microsoft",
    54113: "fastly",
    20940: "akamai",
    16625: "akamai",
    21342: "akamai",
    32787: "akamai",
    35994: "akamai",
    12222: "akamai",
    60068: "cdn77",
    200325: "bunny",
    199524: "gcore",
    202422: "gcore",
    12989: "stackpath",
    33438: "stackpath",
    19551: "imperva",
    30148: "sucuri",
    32934: "facebook",
    45102: "alibaba",
    132203: "tencent",
    13414: "twitter",
    36459: "github",
    54825: "packet",
    397273: "render",
    394536: "vercel",
}
# Large hosters: the IP is still the operator's server (operator tier), but the ASN itself is shared.
HOSTER_ASNS: dict[int, str] = {
    24940: "hetzner",
    16276: "ovh",
    14061: "digitalocean",
    20473: "vultr",
    63949: "linode",
    60781: "leaseweb",
    12876: "scaleway",
    47583: "hostinger",
    51167: "contabo",
    44066: "first-colo",
    212238: "datacamp",
    197695: "reg.ru",
    43541: "cyberfolks",
    12824: "home.pl",
    29522: "kei.pl",
    15967: "nazwa.pl",
}


def is_edge_ip(enr: Enricher, ip: str) -> bool:
    """True when the replay cache (or a live lookup with HYDRA_ENRICH=1) puts the IP in an edge ASN."""
    _, s = lookup(enr, ip)
    return s.get("asn") in EDGE_ASNS


def lookup(enr: Enricher, ip: str) -> tuple[Response, dict[str, Any]]:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return Response("dns", ip, "error:invalid", None, None, None), {}
    if a.version == 6:
        q = a.reverse_pointer.replace(".ip6.arpa", "") + ".origin6.asn.cymru.com"
    else:
        q = ".".join(reversed(ip.split("."))) + ".origin.asn.cymru.com"
    r = dns.query(enr, q, "TXT")
    ans = dns.answers(r)
    if not ans:
        return r, {}
    f = [x.strip() for x in ans[0].strip('"').split("|")]
    try:
        asn = int(f[0].split()[0])
    except (ValueError, IndexError):
        return r, {}
    out: dict[str, Any] = {
        "asn": asn,
        "prefix": f[1] if len(f) > 1 else None,
        "cc": f[2] if len(f) > 2 else None,
        "rir": f[3] if len(f) > 3 else None,
    }
    n = dns.answers(dns.query(enr, f"AS{asn}.asn.cymru.com", "TXT"))
    if n:
        out["as_name"] = n[0].strip('"').split("|")[-1].strip()
    return r, out
