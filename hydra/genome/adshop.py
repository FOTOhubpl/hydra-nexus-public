"""Ad-platform campaign ids and shop-builder fingerprints (INF family, SCAMLINKS 2026-11-21).

Scam shops advertised through Google Ads / Meta / TikTok often run on a hosted shop builder under a random-letter
domain. What one operator chooses (the ad account, campaign, ad group, ad; the store behind several domains) is
operator evidence; the builder and the ad platform are shared by every customer and never link alone.

Pure helpers (also used by `har.url_genes` for landing URLs):
- `shop_from_params`: `utm_origin=<builder>` style markers in a URL;
- `product_path`: `/products/<id>` skeleton (kit tier) and, for long numeric ids, the store-wide product id
  (operator tier: one product id names one store, whatever domain fronts it).

Extractor `inf_adshop` (non-landing URLs and page/HAR markers; landing URLs are `inf_url`'s):
- `ad_campaign_id` / `ad_platform` from every other URL of the incident with a query: HAR request URLs, links,
  script and stylesheet URLs of saved pages, URLs written in plain-text artifacts;
- `shop_platform` from builder hosts contacted in the HAR or referenced by the page (scripts, stylesheets, icons,
  links), builder-specific JS globals, the generator meta tag, and `utm_origin`-style URL markers.

Mere mentions of a builder name are not used: tracking SDKs bundle lists of every builder (measured on the
2026-11-21 organiser material: one bundled script names Shopify, WooCommerce, PrestaShop and Magento together).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

from hydra.contracts import Family
from hydra.enrich.domains import redact_url, segment_shape
from hydra.genome.base import ExtractContext, ExtractResult, ResolvedIncident, register
from hydra.genome.webparse import AD_LEVEL_PL, AD_PLATFORM_PL, GeneSink, ad_campaign_ids, bundle, is_text_mime

VERSION = 1
EX = f"inf_adshop@{VERSION}"

# builder -> host suffixes of its tenant domains, CDNs and storefront APIs
SHOP_HOSTS: dict[str, tuple[str, ...]] = {
    "shopline": ("myshopline.com", "shoplineapp.com", "shoplineimg.com", "shopline.com"),
    "shopify": ("myshopify.com", "cdn.shopify.com", "shopifycdn.com", "shopifycdn.net", "shopifysvc.com",
                "shopifycloud.com"),
    "shoplazza": ("myshoplaza.com", "shoplazza.com", "shoplazza.net"),
    "wix": ("wixstatic.com", "parastorage.com", "wixsite.com"),
    "bigcommerce": ("bigcommerce.com", "mybigcommerce.com"),
    "ecwid": ("ecwid.com",),
    "squarespace": ("squarespace.com", "squarespace-cdn.com", "sqspcdn.com"),
    "shopyy": ("shopyy.com", "myshopyy.com"),
}
# builder-specific code markers: assignments / loader paths, not names in a list
SHOP_CODE: dict[str, re.Pattern[str]] = {
    "shopify": re.compile(r"\bShopify\.(?:shop|theme|routes|locale)\s*=|/cdn/shop/|Shopify\.PaymentButton"),
    "shopline": re.compile(r"\bwindow\.(?:SHOPLINE|Shopline)\b\s*="),
    "shoplazza": re.compile(r"\bwindow\.(?:SHOPLAZZA|Shoplazza)\b\s*="),
    "woocommerce": re.compile(r"/wp-content/plugins/woocommerce/|\bwoocommerce_params\s*="),
    "prestashop": re.compile(r"\bvar\s+prestashop\s*=|\bprestashop\s*=\s*\{"),
    "magento": re.compile(r"text/x-magento-init|/static/version\d+/frontend/"),
    "wix": re.compile(r"\bwixBiSession\b|static\.parastorage\.com"),
}
SHOP_GENERATOR = re.compile(r"(shopify|shopline|shoplazza|woocommerce|prestashop|magento|wix\.com|squarespace|"
                            r"bigcommerce|ecwid|opencart)", re.I)
# URL parameters whose value names the builder that generated the ad link (`utm_origin=shopline`)
SHOP_PARAMS = ("utm_origin",)
PRODUCT_PATH = re.compile(r"^(/[a-z]{2}(?:-[a-z]{2})?)?/(products?|goods|item)/([^/]+)/?$", re.I)
CLASS_WOO = re.compile(r"^woocommerce(-page|-cart|-checkout)?$")


def host_builder(host: str) -> str | None:
    host = host.lower().rstrip(".")
    for name, sufs in SHOP_HOSTS.items():
        if any(host == s or host.endswith("." + s) for s in sufs):
            return name
    return None


def shop_from_params(params: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """[(builder, marker)] from URL parameters such as utm_origin=shopline (known builder names only)."""
    out = []
    for k, v in params:
        vl = (v or "").strip().lower()
        if k.lower() in SHOP_PARAMS and vl in SHOP_HOSTS | {"woocommerce": (), "prestashop": (), "magento": ()}:
            out.append((vl, f"{k.lower()}={vl}"))
    return out


def product_path(path: str, builder: str | None) -> tuple[str | None, str | None]:
    """(product_path_skeleton, shop_product_id) of a URL path. The skeleton keeps the id's digit count (a builder's
    id format); it is emitted only for builder shops or long numeric ids. Store product ids need >= 10 digits."""
    m = PRODUCT_PATH.match(path or "")
    if not m:
        return None, None
    seg, pid = m.group(2).lower(), m.group(3)
    numeric = pid.isdigit()
    shape = f"{{num{len(pid)}}}" if numeric else segment_shape(pid)
    if builder is None and not (numeric and len(pid) >= 10):
        return None, None
    skel = f"/{seg}/{shape}"  # no builder prefix: a builder is detected on only some links of one ad template
    pid_val = f"{builder or 'shop'}:{pid}" if numeric and len(pid) >= 10 else None
    return skel, pid_val


def ad_genes(sink: GeneSink, url: str, src: str, aid: str | None, *, quality: float = 1.0,
             har_entry: int | None = None) -> None:
    """ad_platform + ad_campaign_id genes of one URL (raw or redacted: ids are numeric, never secret)."""
    try:
        params = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return
    if not params:
        return
    plats, ids = ad_campaign_ids(params)
    for plat, marker in plats.items():
        sink.add("ad_platform", plat, None, artifact_id=aid, har_entry=har_entry, quality=0.2 * quality,
                 note=f"source={src}; {AD_PLATFORM_PL[plat]} ({marker}); platforma wspólna, sama nie łączy")
    for val, param, level in ids:
        plat = val.split(":", 1)[0]
        sink.add("ad_campaign_id", val, None, artifact_id=aid, har_entry=har_entry, quality=quality,
                 note=f"source={src}; {AD_LEVEL_PL[level]} {AD_PLATFORM_PL.get(plat, AD_PLATFORM_PL['ads'])} "
                      f"({param})")


_TEXT_URL = re.compile(r"https?://[^\s'\"<>()\\]+", re.I)


def _other_urls(b, arts) -> list[tuple[str, str, str | None, int | None]]:
    out: list[tuple[str, str, str | None, int | None]] = []
    for h in b.hars:
        for r in h.requests:
            u = str(r.get("url") or "")
            if "?" in u and u.startswith("http"):
                out.append((u, "har_request", h.artifact_id, r.get("entry")))
    for d in b.htmls:
        for u in list(d.links) + list(d.script_src) + list(d.stylesheets):
            if "?" in u and u.startswith("http"):
                out.append((u, "page_link", d.artifact_id, None))
    for a in arts:
        if a.meta.size <= 2_000_000 and a.meta.role != "url" and is_text_mime(a.meta.mime or "") and \
                a.meta.path_in_incident.lower().endswith((".txt", ".csv", ".md", ".url")):
            try:
                txt = a.path.read_text(errors="replace")
            except OSError:
                continue
            for u in _TEXT_URL.findall(txt):
                if "?" in u:
                    out.append((u, "text_url", a.meta.artifact_id, None))
    return out[:5000]


@register("inf_adshop", family=Family.INF, version=VERSION)
def inf_adshop(inc: ResolvedIncident, ctx: ExtractContext) -> ExtractResult:
    sink = GeneSink(inc.incident.id, EX)
    b = bundle(inc)
    landing = {redact_url(u) for u, _, _ in b.landing_urls}
    for u, src, aid, entry in _other_urls(b, inc.artifacts):
        if redact_url(u) in landing:
            continue  # landing URLs: inf_url (har.url_genes) emits the same kinds
        ad_genes(sink, u, src, aid, quality=0.8, har_entry=entry)
    # ---- shop builder
    found: dict[str, tuple[str, str | None]] = {}

    def hit(name: str, why: str, aid: str | None) -> None:
        found.setdefault(name, (why, aid))

    for u, src, aid in b.landing_urls:
        try:
            sp = urlsplit(u)
            params = parse_qsl(sp.query, keep_blank_values=True)
        except ValueError:
            continue
        name = host_builder(sp.hostname or "")
        if name:
            hit(name, f"{src}: tenant/builder host", aid)
        for name, marker in shop_from_params(params):
            hit(name, f"{src}: {marker}", aid)
    for h in b.hars:
        if h.local_replay:
            continue
        for host in h.hosts:
            name = host_builder(host)
            if name:
                hit(name, "HAR: builder host contacted", h.artifact_id)
    for d in b.htmls:
        for u in list(d.script_src) + list(d.stylesheets) + list(d.icons) + list(d.links):
            try:
                name = host_builder(urlsplit(u).hostname or "")
            except ValueError:
                name = None
            if name:
                hit(name, "page: builder asset/link host", d.artifact_id)
        gen = d.meta.get("generator") or ""
        m = SHOP_GENERATOR.search(gen)
        if m:
            hit(m.group(1).lower().removesuffix(".com"), "page: generator meta", d.artifact_id)
        code = (d.raw or "")[:3_000_000]
        for name, rx in SHOP_CODE.items():
            if rx.search(code) or any(rx.search(s[:500_000]) for s in d.inline_scripts):
                hit(name, "page: builder code marker", d.artifact_id)
        if any(CLASS_WOO.match(c) for c in d.classes):
            hit("woocommerce", "page: WooCommerce body/DOM class", d.artifact_id)
    for name, (why, aid) in sorted(found.items()):
        sink.add("shop_platform", name, None, artifact_id=aid, quality=0.3,
                 note=f"{why}; kreator sklepów wspólny dla wielu sklepów, sam nie łączy")
    return sink.result()
