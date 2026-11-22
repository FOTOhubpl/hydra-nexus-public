"""JARM-like active TLS fingerprint built on the standard `ssl` module (analyst-triggered, cached, replayable).

    resp, summ = probe(enr, "landing.example")   # live only with HYDRA_ENRICH=1 and HYDRA_OFFLINE=0
    resp, summ = cached(enr, "landing.example")  # replay only: what inf_enrich reads; never touches the network

Idea (public description of JARM, Salesforce): send several different TLS ClientHellos and hash what the server
chooses; servers with the same TLS stack and configuration answer alike. JARM crafts raw ClientHello packets
(exact cipher and extension orders) that the Python `ssl` module cannot produce, so this is NOT JARM and its values
are not comparable with JARM databases. It is our own lower-resolution variant: six handshakes that vary the
protocol version range, the offered cipher order (forward / reversed: does the server impose its own preference?),
a CBC-only offer, and the ALPN list. Per probe we record (negotiated version, cipher, ALPN) or the failure class;
`tlsp1:` + the first 32 hex of sha256 over the six results is the fingerprint. No HTTP request is sent.

Active contact with the landing server, so it never runs inside the pipeline: `scripts/tls_probe.py` fills the
cache on an analyst's request (live mode), and `inf_enrich` reads only the cache (gene `tls_probe_hash`,
platform tier: a server stack/configuration hint; boilerplate behind a CDN edge). Only public IPs are contacted.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import ssl
from collections.abc import Callable
from typing import Any

from hydra.enrich.client import DELAY, Enricher, Response
from hydra.enrich.dns import NAME_OK

SOURCE = "tls_probe"
DELAY.setdefault(SOURCE, 1.0)
TIMEOUT_S = 5.0
_CIPHERS = (
    "ECDHE-ECDSA-AES128-GCM-SHA256", "ECDHE-RSA-AES128-GCM-SHA256", "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-RSA-AES256-GCM-SHA384", "ECDHE-ECDSA-CHACHA20-POLY1305", "ECDHE-RSA-CHACHA20-POLY1305",
    "ECDHE-RSA-AES128-SHA", "ECDHE-RSA-AES256-SHA", "AES128-GCM-SHA256", "AES256-GCM-SHA384", "AES128-SHA",
    "AES256-SHA",
)
_CBC = ("ECDHE-RSA-AES128-SHA", "ECDHE-RSA-AES256-SHA", "AES128-SHA", "AES256-SHA")
T12, T13 = ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3
# (name, min version, max version, TLS<=1.2 cipher list or None for the library default, ALPN list or None)
PROBES: tuple[tuple[str, ssl.TLSVersion, ssl.TLSVersion, tuple[str, ...] | None, tuple[str, ...] | None], ...] = (
    ("t12_fwd", T12, T12, _CIPHERS, ("h2", "http/1.1")),
    ("t12_rev", T12, T12, _CIPHERS[::-1], ("http/1.1",)),
    ("t12_cbc", T12, T12, _CBC, None),
    ("t13_h2", T13, T13, None, ("h2", "http/1.1")),
    ("t13_plain", T13, T13, None, None),
    ("any_rev_alpn", T12, T13, _CIPHERS[::-1], ("http/1.1", "h2")),
)

# (ip, sni, probe spec) -> "version|cipher|alpn" or "fail:<class>"
Handshake = Callable[[str, str, tuple], str]


def _public_ip(host: str, port: int) -> str | None:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return None
    ips = [i[4][0] for i in infos]
    good = [ip for ip in ips if ipaddress.ip_address(ip).is_global]
    return good[0] if good and len(good) == len(ips) else None  # any non-public answer: refuse (SSRF guard)


def handshake(ip: str, sni: str, spec: tuple, port: int = 443) -> str:
    _name, lo, hi, ciphers, alpn = spec
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # fingerprinting, not trusting
    try:
        ctx.minimum_version, ctx.maximum_version = lo, hi
        if ciphers:
            ctx.set_ciphers(":".join(ciphers))
        if alpn:
            ctx.set_alpn_protocols(list(alpn))
    except (ValueError, ssl.SSLError):
        return "fail:client_unsupported"
    try:
        with socket.create_connection((ip, port), timeout=TIMEOUT_S) as raw, \
                ctx.wrap_socket(raw, server_hostname=sni) as tls:
            c = tls.cipher()
            return f"{tls.version()}|{c[0] if c else '?'}|{tls.selected_alpn_protocol() or '-'}"
    except ssl.SSLError as exc:
        return f"fail:{(exc.reason or 'ssl').lower()[:40]}"
    except TimeoutError:
        return "fail:timeout"
    except OSError as exc:
        return f"fail:{type(exc).__name__.lower()}"


def fingerprint(results: list[str]) -> str | None:
    if not results or all(r.startswith("fail:") for r in results):
        return None
    return "tlsp1:" + hashlib.sha256(",".join(results).encode()).hexdigest()[:32]


def _key(host: str, port: int) -> str:
    return f"{host}_{port}"


def _summary(r: Response) -> dict[str, Any]:
    d = r.data if r.ok and isinstance(r.data, dict) else {}
    return {"fingerprint": d.get("fingerprint"), "results": d.get("results") or {}, "ip": d.get("ip")}


def cached(enr: Enricher, host: str, port: int = 443) -> tuple[Response, dict[str, Any]]:
    """Replay only (no fetch callable): what the gene extractor reads."""
    r = enr.get(SOURCE, _key(host.lower().rstrip("."), port), None)
    return r, _summary(r)


def probe(enr: Enricher, host: str, port: int = 443, *, shake: Handshake | None = None,
          resolve: Callable[[str, int], str | None] | None = None) -> tuple[Response, dict[str, Any]]:
    """Live (Enricher.live) or replay. Unresolvable or non-public hosts are `error:*` and not cached."""
    host = host.lower().rstrip(".")
    if not NAME_OK.match(host):
        return Response(SOURCE, _key(host, port), "error:invalid", None, None, None), _summary_empty()

    def fetch() -> tuple[str | None, str, Any]:
        ip = (resolve or _public_ip)(host, port)
        if not ip:
            return f"tls://{host}:{port}", "error:no_public_ip", None
        res = {spec[0]: (shake or handshake)(ip, host, spec) for spec in PROBES}
        fp = fingerprint(list(res.values()))
        if fp is None:
            return f"tls://{host}:{port}", "error:no_tls", None
        return f"tls://{host}:{port}", "ok", {"ip": ip, "results": res, "fingerprint": fp, "scheme": "tlsp1"}

    r = enr.get(SOURCE, _key(host, port), fetch)
    return r, _summary(r)


def _summary_empty() -> dict[str, Any]:
    return {"fingerprint": None, "results": {}, "ip": None}
