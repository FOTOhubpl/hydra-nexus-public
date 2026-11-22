"""HTTP response fingerprints from stored HARs (offline): header order, security headers, CSP (extractor `inf_http`).

- `http_header_order_hash`: HHHash-style hash of the response header *names* in received order, `hhh:1:` + sha256
  of the names joined with `:`. Own implementation from the public HHHash description (CIRCL); one deviation:
  names are lower-cased, because HTTP/2 captures (browser HARs) lower-case them and HTTP/1.1 fetches do not, and
  HTTP/2 pseudo-headers (`:status`) are dropped. The order reflects the server stack and its configuration
  (nginx/Apache/PHP framework, reverse proxy), so it is platform tier: a kit-level hint, never a campaign link.
  Responses through a CDN edge carry the edge's headers and are boilerplate.
- `http_security_headers`: which security headers the landing document sends (HSTS, CSP, XFO, ...): boilerplate
  (descriptive; common combinations), shown to the analyst.
- `http_csp_hash`: hash of the normalised Content-Security-Policy (directives and sources sorted). A non-trivial
  policy is written by whoever deployed the kit; kit tier.
Values only describe header names and policies; header values with cookies or tokens are never stored.
Favicon hashes (Shodan-compatible mmh3) are `favicon_mmh3` genes of `inf_dom`.
"""

from __future__ import annotations

import hashlib

from hydra.contracts import Family, GeneStatus
from hydra.enrich.domains import is_boilerplate
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.webparse import GeneSink, bundle, h16

SECURITY_HEADERS = {
    "strict-transport-security": "hsts",
    "content-security-policy": "csp",
    "content-security-policy-report-only": "csp-ro",
    "x-frame-options": "xfo",
    "x-content-type-options": "xcto",
    "referrer-policy": "referrer",
    "permissions-policy": "permissions",
    "cross-origin-opener-policy": "coop",
    "cross-origin-embedder-policy": "coep",
    "cross-origin-resource-policy": "corp",
    "x-xss-protection": "xxss",
}
MIN_HEADERS = 3  # fewer names carry no ordering information


def header_order_hash(names: list[str]) -> str | None:
    keys = [n.strip().lower() for n in names if n and n.strip() and not n.strip().startswith(":")]
    if len(keys) < MIN_HEADERS:
        return None
    return "hhh:1:" + hashlib.sha256(":".join(keys).encode("utf-8", "replace")).hexdigest()


def security_profile(names: list[str]) -> str:
    got = sorted({SECURITY_HEADERS[n.lower()] for n in names if n.lower() in SECURITY_HEADERS})
    return ",".join(got) if got else "none"


def csp_normalised(policy: str) -> str | None:
    """Directives sorted, sources sorted; nonces and hashes masked (they change per response)."""
    out = []
    for part in policy.split(";"):
        toks = part.strip().split()
        if not toks:
            continue
        srcs = sorted({"'nonce'" if t.lower().startswith("'nonce-") else "'hash'" if t.lower().startswith(
            ("'sha256-", "'sha384-", "'sha512-")) else t.lower() for t in toks[1:]})
        out.append(" ".join([toks[0].lower(), *srcs]))
    return "; ".join(sorted(out)) or None


@register("inf_http", family=Family.INF, version=1)
def inf_http(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, "inf_http@1")
    b = bundle(inc)
    hars = [h for h in b.hars if not h.local_replay]
    if not any(h.doc_headers for h in hars):
        sink.missing("http_header_order_hash", GeneStatus.UNAVAILABLE, "no HTML document response headers in a HAR",
                     "platform")
        return sink.result()
    for h in hars:
        for entry, host, headers in h.doc_headers:
            if is_boilerplate(host):
                continue
            names = [n for n, _ in headers]
            cdn = bool(h.hosts.get(host) and h.hosts[host].cdn)
            hhh = header_order_hash(names)
            if hhh:
                sink.add("http_header_order_hash", hhh, "boilerplate" if cdn else None, artifact_id=h.artifact_id,
                         har_entry=entry, quality=0.1 if cdn else 0.3,
                         note=f"{host}: {len(names)} headers" + (" (CDN edge)" if cdn else ""))
            sink.add("http_security_headers", security_profile(names), "boilerplate", artifact_id=h.artifact_id,
                     har_entry=entry, quality=0.1, note=host)
            for n, v in headers:
                if n.lower() == "content-security-policy" and len(v) > 40 and (norm := csp_normalised(v)):
                    sink.add("http_csp_hash", h16(norm), None, artifact_id=h.artifact_id, har_entry=entry,
                             quality=0.5, note=f"{host}: {norm[:160]}")
    from hydra.genome import parked

    v = parked.incident_verdict(inc, ctx)
    if v is not None:  # a parking provider's server answers for every parked domain
        parked.downgrade_template(sink.genes, v)
    return sink.result()
