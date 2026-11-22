"""INF genes from landing URLs and HAR network logs (static parsing, no network).

Extractors:
- `inf_url`: landing URLs from every source (HAR entry, `saved from url=` comment, capture manifest, URL input):
  entry_url, landing_host, registrable_domain, url_label_skeleton, url_path_skeleton, utm_pattern (kit),
  click/TDS parameter names and campaign-identifying values.
- `inf_har`: the request graph: redirect chain + skeleton, hosts contacted (boilerplate removed), server IPs and
  /24, header fingerprints, tracker IDs, TDS signatures, operator IDs, lead/API endpoints, POST field sets,
  cloaking features, JS/meta-refresh redirects found in bodies.

Secrets (access tokens, API keys, local user names) are redacted before persisting; secret parameter values
that still identify a campaign become `h:<sha256[:16]>`.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from hydra.contracts import REDACTED, Family, GeneStatus
from hydra.enrich.domains import (
    digit_skeleton,
    is_boilerplate,
    is_ip,
    is_platform,
    is_secret_param,
    path_skeleton,
    redact_text,
    redact_url,
    registrable_domain,
    shape_is_lexical,
    url_label_skeleton,
    valid_host,
    value_shape,
)
from hydra.genome import adv_f01
from hydra.genome.adshop import ad_genes, host_builder, product_path, shop_from_params
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.tollfraud import shared_cert_reason
from hydra.genome.webparse import (
    CAMPAIGN_VALUE_PARAMS,
    CLICK_PARAMS,
    FILE_TOO_LARGE,
    OPID_PLATFORM,
    GeneSink,
    Scan,
    ad_campaign_ids,
    bundle,
    is_text_mime,
    pii_hash,
    scan_text,
)

# Shared CDN edge ranges carry little evidence (CAMPAIGNS.md: Cloudflare IPs appear in 7 of 10 incidents).
CDN_IP_PREFIXES = (
    "104.16.",
    "104.17.",
    "104.18.",
    "104.19.",
    "104.20.",
    "104.21.",
    "104.22.",
    "104.24.",
    "104.25.",
    "104.26.",
    "104.27.",
    "172.64.",
    "172.65.",
    "172.66.",
    "172.67.",
    "172.68.",
    "172.69.",
    "188.114.96.",
    "188.114.97.",
    "188.114.98.",
    "188.114.99.",
    "162.158.",
    "141.101.",
    "108.162.",
    "190.93.",
    "197.234.",
    "198.41.",
    "2606:4700:",
    "2a06:98c1:",
    "2803:f800:",
)


def _ip24(ip: str) -> str | None:
    if ":" in ip:
        parts = ip.split(":")
        return ":".join(parts[:3]) + "::/48" if len(parts) >= 3 else None
    p = ip.split(".")
    return ".".join(p[:3]) + ".0/24" if len(p) == 4 else None


def _decode_idna(host: str) -> str:
    from hydra.watch.names import decode_idna

    return decode_idna(host)


def lookalike_genes(sink: GeneSink, host: str, aid: str | None, note: str) -> None:
    """IDN / homoglyph decoding and brand look-alike checks of a landing host (offline, public brand list of
    `hydra.watch.lexicon`). The impersonated brand is the scam's *target*, shared by unrelated campaigns, so
    `brand_lookalike` / `brand_official` are boilerplate (shown, never linking). The folded ASCII skeleton of an
    IDN or homoglyph host is kit level: look-alike variants of one kit fold to the same skeleton."""
    try:
        from hydra.watch.names import decode_idna, parts
        from hydra.watch.score import brand_hits, is_official
    except ImportError:  # pragma: no cover - watch module is part of the package
        return
    try:
        p = parts(host)
    except Exception:  # noqa: BLE001 — malformed host: no look-alike genes
        return
    if p.idn:
        sink.add("idn_host", decode_idna(host), "boilerplate", artifact_id=aid, quality=0.2,
                 note=f"{note}; punycode {host} → {decode_idna(host)}")
    if (p.idn or p.homoglyph) and p.folded and p.folded != host:
        sink.add("host_folded", p.folded, "kit", artifact_id=aid, quality=0.6,
                 note=f"{note}; ASCII skeleton{' (homoglyphs folded)' if p.homoglyph else ''}")
    official = is_official(p)
    if official is not None:
        sink.add("brand_official", official.name, "boilerplate", artifact_id=aid, quality=0.2,
                 note=f"{note}; host under the brand's own domain")
        return
    for h in brand_hits(p):
        kind = "homoglyph" if p.homoglyph else h.kind
        detail = f"{h.token} ≈ {h.brand.name} (distance {h.distance})" if h.kind == "typo" else h.token
        sink.add("brand_lookalike", f"{h.brand.name}|{kind}|{h.where}", "boilerplate", artifact_id=aid,
                 quality=0.2, note=f"{note}; {detail}")


def url_genes(sink: GeneSink, url: str, src: str, aid: str | None) -> None:
    raw_query = urlsplit(url).query  # raw values: secret ones are only ever hashed (pii_hash), never stored
    url = redact_url(url)
    sp = urlsplit(url)
    host = (sp.hostname or "").lower()
    if host and not host.isascii():  # a Unicode host typed or OCR'd from an ad: compare in its wire (punycode) form
        from hydra.watch.names import normalise_host

        host = normalise_host(host)
    if not valid_host(host):
        return
    note = f"source={src}"
    sink.add("entry_url", f"{host}{sp.path}", "url", artifact_id=aid, note=note)
    if is_boilerplate(host):
        return
    sink.add("landing_host", host, "url", artifact_id=aid, note=note)
    lookalike_genes(sink, host, aid, note)
    rd = registrable_domain(host)
    if rd:
        sink.add(
            "registrable_domain", rd, "url", artifact_id=aid, note=note + (" platform" if is_platform(host) else "")
        )
    # punycode digits ('xn--80ak6aa92e') are an encoding artefact, not a rotated-domain counter
    dsk = digit_skeleton(_decode_idna(host)) if "xn--" in host else digit_skeleton(host)
    if dsk and "#" in dsk:  # without digits it equals the host itself (already a landing gene)
        sink.add("url_digit_skeleton", dsk, None, artifact_id=aid, note=note, quality=0.8)
    sk = None if "xn--" in host else url_label_skeleton(host)  # punycode shape is an encoding artefact
    if sk:
        # shape-only skeletons ('L9.com') chained unrelated random-name domains on the public corpus (top reason of
        # 33,087 of 37,661 links, PUBLIC_CORPUS.md): no lexical content -> boilerplate; structured shapes -> kit
        lexical = shape_is_lexical(sk)
        sink.add(
            "url_label_skeleton",
            sk,
            None if lexical else "boilerplate",
            artifact_id=aid,
            note=note,
            quality=0.4 if lexical else 0.05,
        )
        psk = path_skeleton(sp.path)
        sink.add(
            "url_label_path_skeleton",
            f"{sk}{psk}",
            None if (lexical or len(psk) > 1) else "boilerplate",
            artifact_id=aid,
            note=note,
            quality=0.5 if (lexical or len(psk) > 1) else 0.05,
        )
    sink.add("url_path_skeleton", path_skeleton(sp.path), "url", artifact_id=aid, note=note, quality=0.5)
    params = parse_qsl(raw_query, keep_blank_values=True)
    builders = [b for b, _ in shop_from_params(params)] or [x for x in [host_builder(host)] if x]
    pskel, pid = product_path(sp.path, builders[0] if builders else None)
    if pskel:
        sink.add("product_path_skeleton", pskel, None, artifact_id=aid, note=note, quality=0.5)
    if pid:
        sink.add("shop_product_id", pid, None, artifact_id=aid, note=f"{note}; numeric store product id", quality=0.9)
    if not params:
        return
    _, ad_ids = ad_campaign_ids(params)
    ad_params = {p for _, p, _ in ad_ids}  # carried as ad_campaign_id: never also a tds_value (one observation)
    ad_genes(sink, url, src, aid)
    names = sorted({k for k, _ in params})
    sink.add("url_param_set", ",".join(names)[:400], None, artifact_id=aid, note=note)
    sink.add(
        "utm_pattern",
        "&".join(sorted({f"{k}={value_shape(v)}" for k, v in params}))[:400],
        None,
        artifact_id=aid,
        note=note,
    )
    for k, v in params:
        kl = k.lower()
        if re.fullmatch(r"\{\{?[A-Za-z_.]+\}?\}", v):
            sink.add("unexpanded_macro", f"{kl}={v}", "tds", artifact_id=aid, note=note, quality=0.6)
        if kl in CLICK_PARAMS or kl.startswith(("sub", "utm_", "aff")):
            sink.add("tds_param", kl, "tds", artifact_id=aid, note=note, quality=0.4)
        if v and kl in CAMPAIGN_VALUE_PARAMS and kl not in ad_params:
            val = pii_hash(v) if is_secret_param(k) else redact_text(v)[:80]
            if v != REDACTED and REDACTED not in val:
                sink.add("tds_value", f"{kl}={val}", "tds", artifact_id=aid, note=note)
        if v and kl == "access_token" and v != REDACTED:
            sink.add("capi_token_hash", pii_hash(v), "tracker", artifact_id=aid, note=note)


@register("inf_url", family=Family.INF, version=3)  # 2: SCAMLINKS ad ids, shop paths; 3: path w/o builder
def inf_url(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, "inf_url@3")
    b = bundle(inc)
    raw_urls = []
    for h in b.hars:  # unredacted entry URLs first, so secret values can be hashed instead of lost
        if h.entry_url and not h.local_replay:
            raw_urls.append((h.entry_url, "har_entry", h.artifact_id))
    for d in b.htmls:
        if d.saved_from:
            raw_urls.append((d.saved_from, "saved_from", d.artifact_id))
    raw_urls += [x for x in b.landing_urls if x[1] not in ("har_entry", "saved_from")]
    for u, src, aid in raw_urls:
        url_genes(sink, u, src, aid)
    for h in b.hars:
        if h.local_replay:
            sink.add(
                "har_local_replay",
                "1",
                "url",
                artifact_id=h.artifact_id,
                quality=0.1,
                note="HAR recorded a file:// replay; only third-party requests",
            )
    if not sink.genes:
        sink.missing("entry_url", GeneStatus.UNAVAILABLE, "no landing URL in the material", "url")
    return sink.result()


# 2: enrich-plus; 3: + operator-genes shared-cert tiers; 4: ADV-F01 (HYDRA_ADV_F01=1 only)
@register("inf_har", family=Family.INF, version=adv_f01.version("inf_har", 3))
def inf_har(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, f"inf_har@{adv_f01.version('inf_har', 3)}")
    enr = _enricher(ctx)
    b = bundle(inc)
    if not b.hars:
        sink.missing("host_contacted", GeneStatus.UNAVAILABLE, "no HAR in the incident", "hosts")
        return sink.result()
    for h in b.hars:
        aid = h.artifact_id
        if h.error:
            st = GeneStatus.UNAVAILABLE if h.error == FILE_TOO_LARGE else GeneStatus.FAILED
            sink.add("host_contacted", None, "hosts", artifact_id=aid, status=st, note=h.error)
            continue
        if h.creator:
            sink.add("har_creator", h.creator, None, artifact_id=aid, quality=0.1)
        if h.fetch is not None:  # live fetch by hydra.enrich.fetch: cloaking signals + TLS of each hop
            for sig in h.fetch.get("white_page") or []:
                sink.add(
                    "white_page_signal",
                    sig,
                    "cloak",
                    artifact_id=aid,
                    quality=0.3,
                    note=f"fetched {h.fetch.get('fetched_at')}",
                )
            if h.fetch.get("white_page"):
                sink.add(
                    "white_page_suspicion",
                    "1",
                    "cloak",
                    artifact_id=aid,
                    quality=0.2,
                    note="the bot fetch probably received a decoy page; the real landing may differ",
                )
        for entry, host, t in h.tls:
            if t.get("issuer"):
                sink.add(
                    "tls_issuer",
                    str(t["issuer"])[:150],
                    "inf_cert",
                    artifact_id=aid,
                    har_entry=entry,
                    quality=0.1,
                    note=host,
                )
            if t.get("sha256"):
                # CDN / PaaS / multi-tenant certificates name the provider, not the operator (tollfraud.py)
                shared = shared_cert_reason(host, t)
                sink.add("tls_cert_sha256", str(t["sha256"]).lower().replace(":", ""),
                         "boilerplate" if shared else "inf_cert", artifact_id=aid, har_entry=entry,
                         note=f"{host} [{shared}]" if shared else host, quality=0.1 if shared else 1.0)
            for san in t.get("sans") or []:
                san = san.lower().removeprefix("*.")
                if san != host and registrable_domain(san) and registrable_domain(san) != registrable_domain(host):
                    sink.add(
                        "cert_san_sibling",
                        san,
                        "inf_cert",
                        artifact_id=aid,
                        har_entry=entry,
                        note=f"live certificate of {host}",
                    )
            if t.get("verified") is False:
                sink.add("tls_invalid", "1", "inf_cert", artifact_id=aid, har_entry=entry, quality=0.2, note=host)
        # ---- hosts, IPs, headers
        for host, hi in h.hosts.items():
            if not (valid_host(host) or is_ip(host)):
                continue
            boiler = is_boilerplate(host)
            if not boiler:
                sink.add("host_contacted", host, "hosts", artifact_id=aid, har_entry=hi.first_entry)
                rd = registrable_domain(host)
                if rd and rd != host:
                    sink.add("domain_contacted", rd, "hosts", artifact_id=aid, har_entry=hi.first_entry)
            else:
                sink.add("thirdparty_host", host, "thirdparty", artifact_id=aid, har_entry=hi.first_entry, quality=0.1)
            if boiler:
                continue
            for ip in sorted(hi.ips):
                cdn = ip.startswith(CDN_IP_PREFIXES) or _edge(enr, ip)
                q = 0.1 if cdn else 1.0
                sink.add(
                    "server_ip",
                    ip,
                    "boilerplate" if cdn else None,
                    artifact_id=aid,
                    har_entry=hi.first_entry,
                    quality=q,
                    note=f"host={host}" + (" shared CDN edge" if cdn else ""),
                )
                n24 = _ip24(ip)
                if n24:
                    sink.add(
                        "server_ip24",
                        n24,
                        "boilerplate" if cdn else None,
                        artifact_id=aid,
                        har_entry=hi.first_entry,
                        quality=q * 0.6,
                        note=f"host={host}",
                    )
            for v in sorted(hi.server):
                sink.add("http_server", v.lower(), "headers", artifact_id=aid, har_entry=hi.first_entry, quality=0.3)
            for v in sorted(hi.powered_by):
                sink.add("x_powered_by", v, "headers", artifact_id=aid, har_entry=hi.first_entry, quality=0.5)
            for v in sorted(hi.cookies):
                sink.add("cookie_name", v, "headers", artifact_id=aid, har_entry=hi.first_entry, quality=0.5)
            for v in sorted(hi.cdn):
                sink.add("cdn_provider", v, "headers", artifact_id=aid, har_entry=hi.first_entry, quality=0.1)
        # ---- redirect chain (3xx + JS/meta found in first-party bodies)
        hops = [r for r in h.redirects if not is_boilerplate(urlsplit(r["from"]).hostname or "")]
        adv = adv_f01.enabled()
        if adv:  # ADV-F01 (CR-F01-2): + followed meta-refresh / JS / Refresh-header hops; chain by following
            hops = adv_f01.all_hops(h, hops)
        if hops:
            chain_hops = adv_f01.follow_chain(hops) if adv else hops
            chain_hosts = [(urlsplit(hops[0]["from"]).hostname or "?").lower()]
            skel = []
            for r in hops:
                to = r["to"]
                th = (urlsplit(to).hostname or chain_hosts[-1]).lower()
                fh = (urlsplit(r["from"]).hostname or "?").lower()
                sink.add(
                    "redirect_hop", f"{fh}>{th}", "redirect", artifact_id=aid, har_entry=r["entry"], note=r["kind"]
                )
                if adv and not any(r is c for c in chain_hops):
                    continue
                if th != chain_hosts[-1]:
                    chain_hosts.append(th)
                skel.append(f"{r['kind']}:{url_label_skeleton(th) or th}{path_skeleton(urlsplit(to).path)}")
            sink.add("redirect_chain", ">".join(chain_hosts), "redirect", artifact_id=aid, har_entry=hops[0]["entry"])
            sink.add("redirect_skeleton", ">".join(skel)[:500], "redirect", artifact_id=aid, har_entry=hops[0]["entry"])
        # ---- POST / API endpoints
        for p in h.posts:
            if is_boilerplate(p["host"]):
                continue
            sink.add("lead_endpoint", f"{p['host']}{p['path_skel']}", "endpoint", artifact_id=aid, har_entry=p["entry"])
            sink.add(
                "tracking_endpoint",
                f"POST {p['path_skel']}",
                None,
                artifact_id=aid,
                har_entry=p["entry"],
                note=f"host={p['host']}",
            )
            if p["fields"]:
                sink.add(
                    "post_fields",
                    f"{p['path_skel']}:{','.join(sorted(p['fields'])[:30])}",
                    None,
                    artifact_id=aid,
                    har_entry=p["entry"],
                )
        for r in h.requests:
            if is_boilerplate(r["host"]) or r["type"] not in ("xhr", "fetch", "ping", "beacon", None):
                continue
            path = urlsplit(r["url"]).path
            if "/api/" in path or r["type"] in ("xhr", "fetch"):
                if r["mime"].startswith(("image/", "font/", "text/css")):
                    continue
                sink.add(
                    "api_path",
                    path_skeleton(path),
                    "endpoint",
                    artifact_id=aid,
                    har_entry=r["entry"],
                    note=f"host={r['host']}",
                    quality=0.7,
                )
        # ---- body scans: trackers, TDS, operator ids, cloaking, JS redirects
        acc = Scan()
        for body in h.bodies:
            if not is_text_mime(body.mime) and not body.url.endswith("#post"):
                continue
            host = (urlsplit(body.url).hostname or "").lower()
            txt = body.data.decode("utf-8", errors="replace")
            # library/boilerplate bodies: trackers yes (pixel IDs live in fbevents config), redirects no
            scan_text(txt, f"{body.entry}", acc, redirects=not is_boilerplate(host))
        for r in h.requests:  # tracker IDs and TDS signatures in request URLs (e.g. facebook.com/tr?id=)
            scan_text(r["url"], f"{r['entry']}", acc, redirects=False)
        _emit_scan(sink, acc, aid, har=True)
    from hydra.genome import parked

    v = parked.incident_verdict(inc, ctx)
    if v is not None:  # parked capture: the contacted servers belong to the parking provider
        parked.downgrade_ips(sink.genes, v, ("server_ip", "server_ip24"))
    return sink.result()


def _enricher(ctx: ExtractContext):
    try:
        from hydra.enrich import get_enricher

        return get_enricher(ctx.enrich, ctx.settings)
    except Exception:  # noqa: BLE001 — without enrichment the hand-made CDN prefixes still apply
        return None


def _edge(enr, ip: str) -> bool:
    """IP inside a CDN/cloud edge ASN (Team Cymru record in the enrichment cache)."""
    if enr is None or not _public(ip):
        return False
    from hydra.enrich.asn import is_edge_ip

    try:
        return is_edge_ip(enr, ip)
    except Exception:  # noqa: BLE001 — lookup problems are unknown, not evidence
        return False


def _public(ip: str) -> bool:
    import ipaddress

    try:
        return ipaddress.ip_address(ip.strip("[]")).is_global
    except ValueError:
        return False


def _entry(src: str) -> int | None:
    return int(src) if src.isdigit() else None


def _emit_scan(sink: GeneSink, acc: Scan, aid: str | None, *, har: bool) -> None:
    def ptr(src: str) -> dict:
        return {"har_entry": _entry(src)} if har else {"dom_path": src[:200]}

    for v, src in sorted(acc.trackers.items()):
        sink.add("tracker_id", v, "tracker", artifact_id=aid, **ptr(src))
    for v, src in sorted(acc.opids.items()):
        if v.startswith(("postback_host:", "crm_host:")) and is_boilerplate(v.split(":", 1)[1]):
            continue
        sub_kind = v.split(":", 1)[0]
        tier = "platform" if sub_kind in OPID_PLATFORM else "operator"
        sink.add("operator_id", v, f"inf:{tier}:{sub_kind}", artifact_id=aid, **ptr(src))
    for v, src in sorted(acc.tds.items()):
        sink.add("tds_signature", v, "tds", artifact_id=aid, quality=0.6, **ptr(src))
    for v, src in sorted(acc.cloak.items()):
        sink.add("cloak_feature", v, "cloak", artifact_id=aid, quality=0.3, **ptr(src))
    for v, src in sorted(acc.cloak_impl.items()):  # G05: same cloaking implementation (kit tier, weak)
        sink.add("cloak_impl", v, None, artifact_id=aid, quality=0.3, **ptr(src))
    for (kind, target), src in sorted(acc.js_redirects.items()):
        t = redact_url(target)
        th = urlsplit(t).hostname
        if th and is_boilerplate(th):
            continue
        sink.add(
            "js_redirect",
            f"{kind}:{(th or '') + path_skeleton(urlsplit(t).path)}"[:300],
            "redirect",
            artifact_id=aid,
            quality=0.6,
            **ptr(src),
        )
    for v, src in sorted(acc.endpoints.items()):
        sp = urlsplit(v)
        if sp.hostname and is_boilerplate(sp.hostname):
            continue
        sink.add(
            "code_endpoint",
            path_skeleton(sp.path if sp.scheme else v.split("?")[0]),
            "endpoint",
            artifact_id=aid,
            quality=0.5,
            **ptr(src),
        )
    for v, src in sorted(acc.linked_hosts.items()):
        if not is_boilerplate(v) and registrable_domain(v):
            sink.add("linked_host", v, "hosts", artifact_id=aid, quality=0.6, **ptr(src))
    for v, src in sorted(acc.libs.items()):
        sink.add("script_lib", v, "libs", artifact_id=aid, quality=0.2, **ptr(src))


# HTTP header fingerprints (`inf_http`) live in their own module; importing it registers the extractor.
from hydra.genome import http_fp as _http_fp  # noqa: E402,F401
