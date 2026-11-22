"""MISP warninglists (offline snapshot) as the data-driven boilerplate source for INF genes.

Snapshot: `$HYDRA_DATA_DIR/external/misp-warninglists/current/` (override `HYDRA_MISP_WL_DIR`), made by
`scripts/fetch_misp_warninglists.py` at a pinned commit with sha256 per list in `SNAPSHOT.json`. Licence: CC0-1.0
(repository README); the X4BNet datacenter/VPN lists keep their MIT notice.

Every list of the upstream repository at the pinned commit has an explicit effect in `EFFECT` (ADOPT_PLAN item 6):
- `boilerplate`: shared CDN / cloud front doors, big platforms' own infrastructure, public DNS, special-use
  address ranges and names, anonymiser egress (Tor, iCloud Private Relay), benign/empty hashes, reserved phone
  numbers (0 bits);
- `label`: an impersonated, blocked or sinkholed target (real bank or brand site, sinkhole IP, DNS block page,
  phishing simulator). A cluster attribute, never link evidence (0 bits, group `inf:label:<sub>`);
- `parked_domain`: parking-provider IPs / nameservers (a cluster label, never a link);
- `platform`: shared services an operator picks among many customers (URL shorteners, link-in-bio, dynamic DNS,
  free/disposable e-mail, trusted file hosts, regional clouds, VPN providers, top-10k popular sites);
- `info`: hosting providers and scanner sources (an IP at a hoster is still the operator's own server; scanner
  ranges are mostly rented cloud addresses that get recycled): note only, tier unchanged;
- `none`: deliberately not used; the reason is in `REASON`. These lists are not fetched.

`classify(kind, value) -> Hit | None` says how a gene value is affected; `classify_phone(e164)` is the phone entry
point. A list only applies to the HYDRA gene kinds that its `matching_attributes` cover (`ATTR_CATEGORY`).
Without a snapshot every lookup returns None and the hand-made lists in `hydra.enrich.domains` /
`hydra.genome.har.CDN_IP_PREFIXES` stay in force.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# ------------------------------------------------------------------------------------------- mapping
# Scanner / measurement source ranges. `info`: they are never a scam page's server, but many are rented cloud VPS
# addresses that get reassigned to other customers, so demoting them would hide operator servers.
SCANNERS = (
    "a10networks-scanning", "academyforinternetresearch-scanning", "adscore-scanning",
    "alphastrike-research-nt-scanning", "alphastrike-scanning", "binaryedge-scanning", "bitsight-scanning",
    "bufferover-nt-scanning", "bufferoverrun-scanning", "censys-scanning", "check-host-net",
    "circl-security-scanning", "circl-ssh-scanning", "coalition-signals-intelligence-nt-scanning",
    "criminalip-scanning", "crowdstrikefalconsurface-scanning", "cybcube-scanning", "cybergreen-nt-scanning",
    "cybergreen-scanning", "cyberok-scanning", "cyberresilience-nt-scanning", "cyberresilience-scanning",
    "cypex-nt-scanning", "cypex-scanning", "dataplane-scanning", "deepfield-scanning", "detectify-scanning",
    "driftnet", "edgewatch-scanning", "eset-scanning", "f6-nt-scanning", "fofa-scanning", "groupib-scanning",
    "hackertarget-scanning", "hadrian-scanning", "hunter-scanning", "infrawatch-scanning",
    "internet-census-nt-scanning", "internetcleanup-scanner", "internetmeasurementresearch-scanning",
    "internetmetrics-scanning", "internetresearchproject-scanning", "internettl-scanning", "intrinsec-nt-scanning",
    "intrinsec-scanning", "intruder-scanning", "ipinfo-nt-scanning", "ipinfo-scanning", "ipip-nt-scanning",
    "ipip-scanning", "ki3-scanning", "knoq-scanning", "leakix-scanning", "mandiantasm-scanning",
    "modat-nt-scanning", "modat-scanner", "netlas-scanning", "netscout-scanning", "netsecscan-nt-scanning",
    "netsecscan-scanning", "odin-scanning", "onlyscans-scanning", "onyphe-nt-scanning", "onyphe-scanner",
    "palo-alto-networks-cortex-xpanse", "pocnroll-scanning", "probethenet-nt-scanning", "probethenet-scanning",
    "quake-scanning", "rapid7-nt-scanning", "rapid7-scanning", "recordedfuture-scanning", "recyber-scanning",
    "research-scanner-nt-scanning", "researchscanner-scanning", "rootevidence-scanning",
    "securityresearch-scanning", "shadowforce-nt-scanning", "shadowforce-scanning", "shadowserver",
    "shadowserver-nt-scanning", "shodan-nt-scanning", "shodan-scanning", "skipa-nt-scanning",
    "stretchoid-nt-scanning", "stretchoid-scanning", "tenable-cloud-ipv4", "tenable-cloud-ipv6",
    "tenableasm-scanning", "umich-cse-connection-attempts", "university-scanning", "visionheight-scanning",
    "xmco-scanning", "zern-scanning", "zoomeye-scanning",
)

_GROUPS: dict[str, tuple[str, ...]] = {
    "boilerplate": (
        # CDN / edge / hyperscaler front doors (shared by every customer)
        "cloudflare", "akamai", "fastly", "amazon-aws", "google-gcp", "microsoft-azure", "microsoft-azure-china",
        "microsoft-azure-germany", "microsoft-azure-us-gov", "bunny-net", "cdn77", "gcore", "stackpath",
        "cachefly", "imperva", "sucuri", "zscaler", "salesforce",
        # big platforms' own infrastructure, crawlers and mail relays
        "google", "microsoft", "apple", "apple-domains", "apple-ipv4", "apple-ipv6", "github",
        "microsoft-office365", "microsoft-office365-cn", "microsoft-office365-ip",
        "microsoft-win10-connection-endpoints", "microsoft-win11-connection-endpoints", "microsoft-mdca-proxy",
        "telegram-ips", "wikimedia", "googlebot", "openai-gptbot", "google-gmail-sending-ips", "smtp-sending-ips",
        "smtp-receiving-ips", "automated-malware-analysis",
        # resolvers, revocation, IP echo, captive-portal probes
        "public-dns-v4", "public-dns-v6", "public-dns-hostname", "crl-hostname", "crl-ip", "whats-my-ip",
        "findip-host", "captive-portals",
        # special-use address ranges and names (never operator infrastructure)
        "rfc1918", "rfc3849", "rfc5735", "rfc6598", "rfc6761", "ipv6-linklocal", "multicast", "tlds",
        "second-level-tlds",
        # anonymiser egress shared by thousands of users
        "tor-exit-nodes", "icloud-private-relay",
        # benign / empty / test-file hashes and trusted CA fingerprints
        "empty-hashes", "common-ioc-false-positive", "eicar.com", "ti-falsepositives", "mozilla-CA",
        "mozilla-IntermediateCA",
        # reserved / fictional phone numbers (regex list)
        "phone_numbers",
    ),
    "label": (
        "sinkholes", "bank-website", "dax30", "university_domains", "microsoft-attack-simulator",
        "umbrella-blockpage-hostname", "umbrella-blockpage-v4", "umbrella-blockpage-v6",
    ),
    "parked_domain": ("parking-domain", "parking-domain-ns"),
    "platform": (
        "url-shortener", "link-in-bio", "dynamic-dns", "public-ipfs-gateways", "disposable-email",
        "personal-email-domains", "common-contact-emails", "lots-project", "digitalside",
        # regional clouds: shared IaaS/CDN of one provider; demote, do not zero (ADOPT_PLAN item 6)
        "alibaba-cloud", "huawei-cloud", "tencent-cloud", "oracle-oci", "ibm-cloud",
        # VPN provider networks: shared egress, but the same ASNs also rent VPS, so demote rather than zero
        "vpn-ipv4", "vpn-ipv6",
        # popularity rankings up to 10k, exact match only (EXACT_ONLY)
        "alexa", "cisco_top10k", "cloudflare-top10k", "majestic_million", "moz-top500", "netapi-top10k",
        "tranco10k",
    ),
    "info": (
        "hetzner", "ovh-cluster", "digitalocean", "vultr", "linode", "leaseweb", "scaleway", "rackspace",
        "godaddy", "coreweave", "palo-alto-networks-cortex-cloud", "datacenter-ipv4", "datacenter-ipv6",
        *SCANNERS,
    ),
}

REASON: dict[str, str] = {
    "cisco_top1000": "subset of cisco_top10k",
    "cisco_top5k": "subset of cisco_top10k",
    "cisco_top20k": "ranks 10k-20k of DNS popularity add mostly single-purpose hosts; 0.6 MB",
    "cisco_top1m": "1M DNS-popularity names (34 MB): deep ranks include abused and malicious domains",
    "cloudflare-top200": "subset of cloudflare-top10k",
    "cloudflare-top1k": "subset of cloudflare-top10k",
    "cloudflare-top2k": "subset of cloudflare-top10k",
    "cloudflare-top5k": "subset of cloudflare-top10k",
    "cloudflare-top20k": "ranks above 10k: popularity is no longer evidence of a shared site",
    "cloudflare-top50k": "ranks above 10k: popularity is no longer evidence of a shared site",
    "cloudflare-top100k": "ranks above 10k: popularity is no longer evidence of a shared site; 2 MB",
    "cloudflare-top200k": "ranks above 10k: popularity is no longer evidence of a shared site; 4 MB",
    "cloudflare-top500k": "deep ranks include abused and malicious domains; 11 MB",
    "cloudflare-top1m": "deep ranks include abused and malicious domains; 22 MB",
    "netapi-top1k": "subset of netapi-top10k",
    "netapi-top1m": "deep ranks include abused and malicious domains; 22 MB",
    "tranco": "Tranco top 1M (22 MB): deep ranks include abused and malicious domains; tranco10k is used",
    "google-chrome-crux-1million": "1M origins (25 MB): deep ranks include abused and malicious domains",
    "windows-binary-hashes": "Windows executables never appear as web-page assets; 57 MB",
    "nioc-filehash": "benign software file hashes (mostly executables); web assets practically never match; 10 MB",
    "microsoft-azure-appid": "Azure application ids: no HYDRA gene kind carries them",
    "covid": "2020 COVID-19 allow-list; stale and outside the scam-ad scope",
    "covid-19-cyber-threat-coalition-whitelist": "2020 COVID-19 allow-list; stale and outside the scam-ad scope",
    "covid-19-krassi-whitelist": "2020 COVID-19 allow-list; stale and outside the scam-ad scope",
    "security-provider-blogpost": "vendor blog domains incl. multi-tenant hosts (medium.com, sites.google.com); "
    "would demote tenant pages, no scam-ad value",
}

EFFECT: dict[str, str] = {n: eff for eff, names in _GROUPS.items() for n in names}
EFFECT.update(dict.fromkeys(REASON, "none"))
UPSTREAM_COMMIT = "40e2b570fda2e5443a2d6fa1d1db16c37ab0a956"
UPSTREAM_LIST_COUNT = 231  # lists/ at UPSTREAM_COMMIT; every one is in EFFECT (tests/genes_inf/test_inf_wl_full.py)

PRIORITY = {"boilerplate": 0, "label": 1, "parked_domain": 2, "platform": 3, "info": 4}
ZEROING = {"boilerplate", "label"}
LABEL_SUB = {
    "sinkholes": "sinkhole",
    "bank-website": "bank_website",
    "dax30": "brand_website",
    "university_domains": "institution_website",
    "microsoft-attack-simulator": "phishing_simulation",
    "umbrella-blockpage-hostname": "dns_blockpage",
    "umbrella-blockpage-v4": "dns_blockpage",
    "umbrella-blockpage-v6": "dns_blockpage",
}
# Popularity rankings list sites, not their subdomains: match the exact host only, whatever the list type.
# Lists whose single-label entries are whole special-use TLDs (".test", ".invalid", ".localhost", ".example"); a
# single-label entry in any other list is ignored, so a stray "com" can never swallow a TLD.
TLD_SUFFIX_LISTS = {"rfc6761"}
EXACT_ONLY = {"alexa", "cisco_top10k", "cloudflare-top10k", "majestic_million", "moz-top500", "netapi-top10k",
              "tranco10k"}

# ------------------------------------------------------------------------------------------- kinds
IP_KINDS = {"server_ip", "dns_ip", "dns_zone_ip"}  # dns_zone_ip: enrich-plus DNS consensus
NET_KINDS = {"server_ip24", "dns_ip24", "bgp_prefix"}
HOST_KINDS = {
    "host_contacted",
    "domain_contacted",
    "linked_host",
    "thirdparty_host",
    "landing_host",
    "registrable_domain",
    "ns_host",
    "mx_host",
    "dns_cname",
    "dns_zone_cname",
    "canonical_host",
    "entity_email_domain",
    "cert_san_sibling",
    "cert_san_domain",
    "form_action",
}
HASH_KINDS = {"asset_sha256", "asset_sha256_lib"}
CERT_KINDS = {"tls_cert_sha256"}
PHONE_KINDS = {"entity_phone"}
EMAIL_KINDS = {"entity_email", "entity_email_domain"}

# MISP matching_attributes -> HYDRA lookup category
ATTR_CATEGORY: dict[str, tuple[str, ...]] = {
    "ip-src": ("ip",), "ip-dst": ("ip",), "ip-src|port": ("ip",), "ip-dst|port": ("ip",),
    "domain|ip": ("ip", "host"), "hostname": ("host",), "domain": ("host",), "url": ("host",), "uri": ("host",),
    "hostname|port": ("host",),
    "email-src": ("email",), "email-dst": ("email",), "target-email": ("email",),
    "whois-registrant-email": ("email",), "dns-soa-email": ("email",),
    "phone-number": ("phone",), "whois-registrant-phone": ("phone",),
    "x509-fingerprint-md5": ("cert",), "x509-fingerprint-sha1": ("cert",), "x509-fingerprint-sha256": ("cert",),
}
for _h in ("md5", "sha1", "sha224", "sha256", "sha512", "ssdeep"):
    ATTR_CATEGORY[_h] = ATTR_CATEGORY[f"filename|{_h}"] = ("hash",)
# which categories each list type can serve (cidr lists name "domain|ip" too: only the IP half applies)
TYPE_CATEGORIES = {
    "cidr": {"ip"},
    "hostname": {"host", "email"},
    "substring": {"host", "email", "ip"},
    "string": {"host", "hash", "cert", "email", "phone"},
    "regex": {"host", "email", "phone"},
}
DEFAULT_CATEGORIES = {"cidr": {"ip"}, "hostname": {"host"}, "substring": {"host"}, "string": {"host", "hash"},
                      "regex": {"email", "phone"}}
MAX_PATTERN = 512  # regex entries longer than this are skipped
MAX_REGEX_INPUT = 320
_REGEX_FLAGS = {"i": re.I, "m": re.M, "s": re.S, "x": re.X}
_CC_HINT = re.compile(r"\(\?:\\\+\|00\)(\d{1,3})")  # "(?:\+|00)44" in a phone pattern -> country code 44


@dataclass(frozen=True)
class Hit:
    effect: str  # boilerplate | label | parked_domain | platform | info
    list_name: str


@lru_cache(maxsize=8)
def _data_dir(env_key: str | None) -> Path:
    from hydra.config import Settings

    return Path(Settings().data_dir or Path.home() / "HydraNexus" / "data")


def snapshot_dir() -> Path:
    """HYDRA_MISP_WL_DIR, else <Settings.data_dir>/external/misp-warninglists/current; when the `current` symlink
    is missing (e.g. copied without symlinks), the newest snapshot folder that contains lists/."""
    env = os.environ.get("HYDRA_MISP_WL_DIR")
    if env:
        return Path(env)
    base = _data_dir(os.environ.get("HYDRA_DATA_DIR")) / "external" / "misp-warninglists"
    cur = base / "current"
    if (cur / "lists").is_dir() or not base.is_dir():
        return cur
    snaps = sorted((p for p in base.iterdir() if (p / "lists").is_dir()), key=lambda p: p.stat().st_mtime)
    return snaps[-1] if snaps else cur


def _better(a: str | None, b: str) -> bool:
    return a is None or PRIORITY[EFFECT[b]] < PRIORITY[EFFECT[a]]


def _categories(j: dict) -> set[str]:
    typ = j.get("type")
    attrs = j.get("matching_attributes") or []
    cats = {c for a in attrs for c in ATTR_CATEGORY.get(str(a), ())}
    allowed = TYPE_CATEGORIES.get(typ, set())
    return (cats & allowed) or set(DEFAULT_CATEGORIES.get(typ, set()))


def _compile(entry: str) -> re.Pattern | None:
    """'/pattern/flags' (PCRE-style delimiters, as in the lists) or a bare pattern; None when too long or invalid."""
    src, flags = entry, 0
    if len(entry) >= 2 and entry[0] == "/" and "/" in entry[1:]:
        body, _, fl = entry[1:].rpartition("/")
        if all(c in "imsxuU" for c in fl):
            src = body
            for c in fl:
                flags |= _REGEX_FLAGS.get(c, 0)
    if not src or len(src) > MAX_PATTERN:
        return None
    try:
        return re.compile(src, flags)
    except (re.error, OverflowError, RecursionError):
        return None


class WarningLists:
    def __init__(self, root: Path):
        self.root = root
        self.nets: dict[tuple[int, int], dict[int, str]] = {}  # (version, prefixlen) -> {network int: list}
        self.suffix: dict[str, dict[str, str]] = {"host": {}, "email": {}}  # host/e-mail domain suffix -> list
        self.exact: dict[str, dict[str, str]] = {c: {} for c in ("host", "hash", "cert", "email", "phone")}
        self.regex: dict[str, list[tuple[str, re.Pattern, str | None]]] = {"host": [], "email": [], "phone": []}
        self.loaded: list[str] = []
        self.types: dict[str, str] = {}
        self.entries: dict[str, int] = {}
        self.regex_skipped: dict[str, int] = {}
        self.unmapped: list[str] = []
        try:
            self.meta = json.loads((root / "SNAPSHOT.json").read_text())
        except (OSError, ValueError):
            self.meta = {}
        for p in sorted(root.glob("lists/*/list.json")):
            name = p.parent.name
            eff = EFFECT.get(name)
            if eff is None:
                self.unmapped.append(name)
                continue
            if eff == "none":
                continue
            try:
                j = json.loads(p.read_text())
            except (OSError, ValueError):
                log.warning("MISP warninglist %s unreadable: skipped", name)
                continue
            typ = str(j.get("type"))
            if typ not in TYPE_CATEGORIES:
                log.warning("MISP warninglist %s has unsupported type %r: skipped", name, typ)
                continue
            self.loaded.append(name)
            self.types[name] = typ
            self.entries[name] = self._load_list(name, typ, _categories(j), j.get("list") or [])
        if self.unmapped:
            log.warning("MISP warninglists without a mapping (ignored): %s", ", ".join(self.unmapped))
        self.prefixes = sorted(self.nets)

    def _load_list(self, name: str, typ: str, cats: set[str], entries: list) -> int:
        n = 0
        bad = 0
        exact_hosts = name in EXACT_ONLY
        for raw in entries:
            v = str(raw).strip()
            if not v:
                continue
            n += 1
            if typ == "regex":
                pat = _compile(v)
                if pat is None:
                    bad += 1
                    continue
                hint = _CC_HINT.search(pat.pattern)
                for c in cats & {"host", "email", "phone"}:
                    self.regex[c].append((name, pat, hint.group(1) if hint else None))
                continue
            v = v.lower()
            if typ == "cidr" or (typ == "substring" and "ip" in cats and _is_ip_like(v)):
                if "ip" in cats:
                    self._put_net(v, name)
                continue
            if typ == "string" or exact_hosts:
                for c in cats & {"host", "hash", "cert", "email", "phone"}:
                    key = v.rstrip(".") if c == "host" else v
                    self._put(self.exact[c], key, name)
                continue
            # hostname / substring: domain suffix (".bank.pl", "*.bank.pl", "@mail.pl" -> bank.pl / mail.pl)
            key = v.lstrip("*.").lstrip("@").rstrip(".")
            if not key or ("." not in key and name not in TLD_SUFFIX_LISTS):
                continue
            for c in cats & {"host", "email"}:
                self._put(self.suffix[c], key, name)
        if bad:
            self.regex_skipped[name] = bad
            log.warning("MISP warninglist %s: %d regex entr%s skipped (invalid or longer than %d chars)", name, bad,
                        "y" if bad == 1 else "ies", MAX_PATTERN)
        return n

    def _put_net(self, v: str, name: str) -> None:
        try:
            net = ipaddress.ip_network(v, strict=False)
        except ValueError:
            return
        self._put(self.nets.setdefault((net.version, net.prefixlen), {}), int(net.network_address), name)

    @staticmethod
    def _put(d: dict, key, name: str) -> None:
        if _better(d.get(key), name):
            d[key] = name

    # ------------------------------------------------------------------------------------- matchers
    def match_ip(self, ip: str) -> str | None:
        try:
            a = ipaddress.ip_address(ip.strip("[]"))
        except ValueError:
            return None
        best = None
        x = int(a)
        bits = 32 if a.version == 4 else 128
        for ver, plen in self.prefixes:
            if ver != a.version:
                continue
            net = x >> (bits - plen) << (bits - plen)
            name = self.nets[(ver, plen)].get(net)
            if name and _better(best, name):
                best = name
        return best

    def match_host_hit(self, host: str, cat: str = "host") -> Hit | None:
        """Exact entries, domain-suffix entries and regex entries of category `cat`. A zeroing hit (boilerplate,
        label) on a public suffix above the host's registrable domain (`evil.web.app` under Google's `web.app`)
        is reported as `platform`: the tenant's own subdomain is the operator's choice, not the shared platform."""
        host = host.lower().strip().rstrip(".")
        if not host:
            return None
        best: Hit | None = None

        def consider(h: Hit) -> None:
            nonlocal best
            if best is None or PRIORITY[h.effect] < PRIORITY[best.effect]:
                best = h

        name = self.exact[cat].get(host)
        if name:
            consider(Hit(EFFECT[name], name))
        labels = host.split(".")
        rd_labels: int | None = None
        for i in range(len(labels)):
            name = self.suffix[cat].get(".".join(labels[i:]))
            if not name:
                continue
            eff = EFFECT[name]
            if i > 0 and eff in ZEROING:
                if rd_labels is None:
                    rd_labels = _registrable_labels(host)
                if len(labels) - i < rd_labels:
                    eff = "platform"
            consider(Hit(eff, name))
        if cat in self.regex and self.regex[cat] and len(host) <= MAX_REGEX_INPUT:
            for name, pat, _cc in self.regex[cat]:
                if (best is None or PRIORITY[EFFECT[name]] < PRIORITY[best.effect]) and pat.search(host):
                    consider(Hit(EFFECT[name], name))
        return best

    def match_host(self, host: str) -> str | None:
        h = self.match_host_hit(host)
        return h.list_name if h else None

    def match_string(self, v: str, cat: str = "hash") -> str | None:
        return self.exact[cat].get(v.strip().lower())

    def match_email(self, address: str) -> Hit | None:
        """Full address: regex lists (e.g. role accounts) and exact entries; then its domain part."""
        a = address.strip()
        best: Hit | None = None
        name = self.exact["email"].get(a.lower())
        if name:
            best = Hit(EFFECT[name], name)
        if len(a) <= MAX_REGEX_INPUT:
            for name, pat, _cc in self.regex["email"]:
                if (best is None or PRIORITY[EFFECT[name]] < PRIORITY[best.effect]) and pat.search(a):
                    best = Hit(EFFECT[name], name)
        _, at, dom = a.rpartition("@")
        if at:
            for h in (self.match_host_hit(dom, "email"), self.match_host_hit(dom, "host")):
                if h and (best is None or PRIORITY[h.effect] < PRIORITY[best.effect]):
                    best = h
        return best

    def match_phone(self, e164: str) -> Hit | None:
        cc, intl, national = phone_forms(e164)
        best: Hit | None = None
        for form in intl + national:
            name = self.exact["phone"].get(form)
            if name and (best is None or PRIORITY[EFFECT[name]] < PRIORITY[best.effect]):
                best = Hit(EFFECT[name], name)
        for name, pat, hint in self.regex["phone"]:
            if best is not None and PRIORITY[EFFECT[name]] >= PRIORITY[best.effect]:
                continue
            forms = intl + national if hint is not None and hint == cc else intl
            if any(pat.fullmatch(f) for f in forms):
                best = Hit(EFFECT[name], name)
        return best

    # ------------------------------------------------------------------------------------- reporting
    def summary(self) -> dict:
        by_effect = Counter(EFFECT[n] for n in self.loaded)
        by_type = Counter(self.types[n] for n in self.loaded)
        return {
            "available": True,
            "dir": str(self.root),
            "commit": self.meta.get("commit") or self.root.resolve().name,
            "upstream_lists": UPSTREAM_LIST_COUNT,
            "mapped": sum(1 for e in EFFECT.values() if e != "none"),
            "unused": sum(1 for e in EFFECT.values() if e == "none"),
            "loaded": len(self.loaded),
            "missing": sorted(n for n, e in EFFECT.items() if e != "none" and n not in self.types),
            "unmapped": list(self.unmapped),
            "by_effect": dict(sorted(by_effect.items())),
            "by_type": dict(sorted(by_type.items())),
            "entries": sum(self.entries.values()),
            "regex_patterns": {c: len(v) for c, v in self.regex.items()},
            "regex_skipped": dict(self.regex_skipped),
        }


def _is_ip_like(v: str) -> bool:
    try:
        ipaddress.ip_network(v, strict=False)
        return True
    except ValueError:
        return False


@lru_cache(maxsize=20_000)
def _registrable_labels(host: str) -> int:
    """Label count of the registrable domain (PSL incl. private suffixes); 0 when unknown."""
    try:
        from hydra.enrich.domains import registrable_domain

        rd = registrable_domain(host)
    except Exception:  # noqa: BLE001 — no PSL: no guard
        return 0
    return len(rd.split(".")) if rd else 0


@lru_cache(maxsize=20_000)
def phone_forms(e164: str) -> tuple[str | None, list[str], list[str]]:
    """(country code, international forms, national forms) of a phone number. International: "+CCNSN",
    "00CCNSN", "CCNSN". National (only tried against patterns written for that country code): the NSN, "0" + NSN
    and the national format digits (trunk prefix included)."""
    s = e164.strip()
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits = digits[2:]
    if not digits:
        return None, [], []
    intl = [f"+{digits}", f"00{digits}", digits]
    try:
        import phonenumbers

        num = phonenumbers.parse("+" + digits, None)
    except Exception:  # noqa: BLE001 — unparsable: international forms only
        return None, intl, []
    nsn = phonenumbers.national_significant_number(num)
    nat = re.sub(r"\D", "", phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.NATIONAL))
    national = list(dict.fromkeys(x for x in (nsn, "0" + nsn, nat) if x))
    return str(num.country_code), intl, national


@lru_cache(maxsize=2)
def _load(path: str) -> WarningLists | None:
    p = Path(path)
    if not (p / "lists").is_dir():
        log.warning(
            "MISP warninglists snapshot not found at %s: tier overrides from warninglists are OFF "
            "(run scripts/fetch_misp_warninglists.py or set HYDRA_MISP_WL_DIR); hand-made lists still apply",
            p,
        )
        return None
    wl = WarningLists(p)
    if not wl.loaded:
        log.warning("MISP warninglists at %s contain no known lists: overrides OFF", p)
        return None
    log.info("%s", _line(wl.summary()))
    return wl


def get() -> WarningLists | None:
    return _load(str(snapshot_dir()))


def available() -> bool:
    return get() is not None


def summary() -> dict:
    """Counts of the loaded snapshot by effect and by list type (plus mapping totals); `available` False without
    a snapshot."""
    wl = get()
    if wl is None:
        return {"available": False, "dir": str(snapshot_dir()), "upstream_lists": UPSTREAM_LIST_COUNT,
                "mapped": sum(1 for e in EFFECT.values() if e != "none"), "loaded": 0}
    return wl.summary()


def _line(s: dict) -> str:
    if not s.get("available"):
        return f"MISP warninglists: not loaded (no snapshot at {s.get('dir')}; {s['mapped']} lists mapped)"
    return (f"MISP warninglists: {s['loaded']} lists loaded ({s['mapped']} mapped, commit {str(s['commit'])[:12]})"
            + (f"; {len(s['missing'])} mapped lists missing from the snapshot" if s.get("missing") else ""))


def report_line() -> str:
    """"MISP warninglists: N lists loaded (M mapped, commit X)" for the enrichment report."""
    return _line(summary())


def fingerprint() -> str:
    """Snapshot commit + loaded lists + mapping digest: part of the INF gene-cache key (pipeline.enrich_state),
    so a new snapshot or a changed mapping re-extracts INF genes instead of reusing stale tiers."""
    mapping = hashlib.sha256(json.dumps([EFFECT, sorted(EXACT_ONLY), MAX_PATTERN], sort_keys=True).encode())
    s = summary()
    return f"{s.get('commit') or 'none'}:{s.get('loaded', 0)}:{mapping.hexdigest()[:12]}"


# ------------------------------------------------------------------------------------------- lookups
@lru_cache(maxsize=50_000)
def _classify(path: str, kind: str, value: str) -> Hit | None:
    wl = _load(path)
    if wl is None or not value:
        return None
    if kind in PHONE_KINDS:
        return None if value.startswith("h:") else wl.match_phone(value)
    if kind == "entity_email":
        return None if value.startswith("h:") or "@" not in value else wl.match_email(value)
    name = None
    if kind in IP_KINDS:
        name = wl.match_ip(value)
    elif kind in NET_KINDS:
        name = wl.match_ip(value.split("/")[0])  # network base address: 1.2.3.0/24 -> 1.2.3.0
    elif kind == "entity_email_domain":
        return wl.match_email("x@" + value)
    elif kind in HOST_KINDS:
        return wl.match_host_hit(value)
    elif kind in HASH_KINDS:
        name = wl.match_string(value, "hash")
    elif kind in CERT_KINDS:
        name = wl.match_string(value, "cert")
    elif kind == "ns_set":
        hits = [wl.match_host_hit(ns) for ns in value.split(",") if ns]
        if hits and all(h and h.effect == "parked_domain" for h in hits):
            return hits[0]
    return Hit(EFFECT[name], name) if name else None


def classify(kind: str, value: str | None) -> Hit | None:
    if value is None:
        return None
    return _classify(str(snapshot_dir()), kind, value)


def classify_phone(e164: str) -> Hit | None:
    """Phone number (E.164, with or without "+", or "00" prefix) against the regex/string phone lists
    (`phone_numbers`: reserved and fictional numbers). Each pattern is matched as a whole (fullmatch) against the
    international forms, and, when the pattern names the number's country code, also against the national forms."""
    return classify("entity_phone", e164)


def cache_clear() -> None:
    """Drop every cache of this module (tests, or after a new snapshot in a long-running process)."""
    for f in (_classify, _load, _data_dir, phone_forms, _registrable_labels):
        clear = getattr(f, "cache_clear", None)  # a test may have monkeypatched one of them
        if clear:
            clear()
