"""Offline host/URL helpers shared by the INF genes and the enrichment layer.

- Registrable domain with the Public Suffix List *including private suffixes* (netlify.app, pages.dev, ...),
  so free-hosting tenants are separate domains (PLAN_MAX §1: registrable domain alone gave precision 0.54).
- URL label skeleton: the shape of each host label (letters/digits/separators with lengths), the strongest
  INF gene in the scale lab (same-target precision 0.95).
- Boilerplate host list: CDNs, fonts, analytics and big platforms that say nothing about the operator.
- Redaction of secrets found in URLs (tokens, API keys, local user paths) before anything is persisted.
"""

from __future__ import annotations

import ipaddress
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit, urlunsplit

from hydra.contracts import REDACTED

HOST_OK = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")

# Third-party infrastructure that tells nothing about the operator (matched as suffix on label boundary).
BOILERPLATE_SUFFIXES: tuple[str, ...] = (
    # Google
    "google.com",
    "google.pl",
    "googleapis.com",
    "gstatic.com",
    "googletagmanager.com",
    "google-analytics.com",
    "doubleclick.net",
    "googlesyndication.com",
    "googleadservices.com",
    "googleusercontent.com",
    "youtube.com",
    "ytimg.com",
    "ggpht.com",
    "gstatic.cn",
    "recaptcha.net",
    "withgoogle.com",
    # Meta and other social
    "facebook.com",
    "facebook.net",
    "fbcdn.net",
    "instagram.com",
    "cdninstagram.com",
    "whatsapp.com",
    "wa.me",
    "tiktok.com",
    "tiktokcdn.com",
    "ttwstatic.com",
    "twitter.com",
    "x.com",
    "twimg.com",
    "ads-twitter.com",
    "linkedin.com",
    "licdn.com",
    "pinterest.com",
    "snapchat.com",
    "t.me",
    "telegram.org",
    # Microsoft / Apple / Yandex
    "microsoft.com",
    "bing.com",
    "bing.net",
    "clarity.ms",
    "apple.com",
    "mzstatic.com",
    "icloud.com",
    "yandex.ru",
    "yandex.net",
    "yastatic.net",
    "mc.yandex.ru",
    # CDNs and libraries
    "cloudflare.com",
    "cloudflareinsights.com",
    "cdnjs.com",
    "jsdelivr.net",
    "jsdelivr.com",
    "unpkg.com",
    "jquery.com",
    "bootstrapcdn.com",
    "fontawesome.com",
    "fontawesomemedia.com",
    "fonts.net",
    "typekit.net",
    "akamaihd.net",
    "akamaized.net",
    "cloudfront.net",
    "fastly.net",
    "gravatar.com",
    "wp.com",
    "polyfill.io",
    "hcaptcha.com",
    "unsplash.com",
    "pexels.com",
    "pixabay.com",
    # web standards / docs found in bundles
    "w3.org",
    "schema.org",
    "ogp.me",
    "mozilla.org",
    "mozilla.net",
    "github.com",
    "githubusercontent.com",
    "wikipedia.org",
    "wikimedia.org",
    "apache.org",
    "reactjs.org",
    "react.dev",
    "vuejs.org",
    "nextjs.org",
    "npms.io",
    "openstreetmap.org",
    "jwplayer.com",
    "videojs.com",
    "swiperjs.com",
    "ionicframework.com",
    "fpjs.dev",
    "er-api.com",
    "exchangerate-api.com",
    "ipapi.co",
    "ip-api.com",
    "ipinfo.io",
    "ipwho.is",
    "api.country.is",
    "sentry.io",
    "aomedia.org",
    "broofa.com",
    "quora.com",
    "ims.ca",
    "conversionsapigateway.com",
    "smpte-ra.org",
    "dashif.org",
    "binom.org",
    "sweetalert2.github.io",
    "exchangerate.host",
    "sentry-cdn.com",
    "hotjar.com",
    "goo.gl",
    "goo.gle",
    "g.co",
    "bit.ly",
    # shop-builder CDNs / storefront APIs: every tenant shop loads them (SCAMLINKS; genome/adshop.py names the builder)
    "cdn.shopify.com",
    "shopifycdn.com",
    "shopifycdn.net",
    "shopifysvc.com",
    "shopifycloud.com",
    "shoplineapp.com",
    "shoplineimg.com",
    "img.myshopline.com",
    "cdn.myshopline.com",
    "cdn.shoplazza.com",
    "wixstatic.com",
    "parastorage.com",
    "squarespace-cdn.com",
)

# Hosting platforms whose tenants are separate sites: the tenant label carries the signal, the platform not.
PLATFORM_SUFFIXES: tuple[str, ...] = (
    "netlify.app",
    "b-cdn.net",
    "github.io",
    "vercel.app",
    "pages.dev",
    "workers.dev",
    "web.app",
    "firebaseapp.com",
    "herokuapp.com",
    "onrender.com",
    "glitch.me",
    "wixsite.com",
    "weebly.com",
    "blogspot.com",
    "wordpress.com",
    "tilda.ws",
    "carrd.co",
    "framer.website",
    "webflow.io",
    "000webhostapp.com",
    "azurewebsites.net",
    "appspot.com",
    "r2.dev",
    "surge.sh",
    "replit.app",
    "translate.goog",
    # added 2026-11-21 after the public corpus (PUBLIC_CORPUS.md: largest false clusters were tenants of these)
    "lovable.app",
    "cfolks.pl",
    "repl.co",
    "replit.dev",
    "framer.app",
    "framer.ai",
    "webflow.io",
    "square.site",
    "godaddysites.com",
    "mystrikingly.com",
    "jimdosite.com",
    "site123.me",
    "webnode.page",
    "notion.site",
    "softr.app",
    "fly.dev",
    "deno.dev",
    "myshopify.com",
    "wpcomstaging.com",
    "ct8.pl",
    "mydevil.net",
    "github.dev",
    "gitlab.io",
    "bitbucket.io",
    "ngrok.io",
    "ngrok-free.app",
    "trycloudflare.com",
    "sites.google.com",
    "business.site",
    "my.canva.site",
    "canva.site",
    "wixstudio.io",
    "bolt.host",
    "myshopline.com",
    "myshoplaza.com",
    "mybigcommerce.com",
    "myshopyy.com",
)


def _host_match(host: str, suffixes: tuple[str, ...]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == s or host.endswith("." + s) for s in suffixes)


def is_boilerplate(host: str | None) -> bool:
    return bool(host) and _host_match(host, BOILERPLATE_SUFFIXES)  # type: ignore[arg-type]


def is_platform(host: str | None) -> bool:
    """Multi-tenant hosting/site-builder host: hand list or any PSL *private* suffix (tenants are separate
    customers, so the parent's RDAP/NS/MX/TXT/certificates are shared and never operator evidence)."""
    if not host:
        return False
    if _host_match(host, PLATFORM_SUFFIXES):
        return True
    if is_ip(host):
        return False
    try:
        return bool(getattr(split_host(host), "is_private", False))
    except Exception:  # noqa: BLE001 — malformed host: not a platform
        return False


def is_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s.strip("[]"))
        return True
    except ValueError:
        return False


def valid_host(host: str | None) -> bool:
    return bool(host) and bool(HOST_OK.match(host))  # type: ignore[arg-type]


def psl_path() -> Path | None:
    env = os.environ.get("HYDRA_PSL_PATH")
    cands = [Path(env)] if env else []
    root = Path(os.environ.get("HYDRA_DATA_DIR", Path.home() / "HydraNexus" / "data"))
    cands.append(root / "external" / "psl" / "public_suffix_list.dat")
    return next((p for p in cands if p.is_file()), None)


@lru_cache(maxsize=1)
def _extractor():
    import tldextract

    p = psl_path()
    # Offline: a local PSL file if present, otherwise the snapshot bundled with tldextract. Never fetches.
    urls = (p.as_uri(),) if p else ()
    return tldextract.TLDExtract(
        suffix_list_urls=urls, cache_dir=None, include_psl_private_domains=True, fallback_to_snapshot=True
    )


def split_host(host: str):
    return _extractor()(host.lower().rstrip("."))


def _top(e) -> str:
    return e.top_domain_under_public_suffix if hasattr(e, "top_domain_under_public_suffix") else e.registered_domain


def registrable_domain(host: str | None) -> str | None:
    if not host or is_ip(host):
        return None
    e = split_host(host)
    rd = _top(e)
    return rd or None


def icann_domain(host: str | None) -> str | None:
    """Registrable domain under ICANN suffixes only: what RDAP and crt.sh know (netlify.app, not x.netlify.app)."""
    if not host or is_ip(host):
        return None
    e = _extractor()(host.lower().rstrip("."), include_psl_private_domains=False)
    rd = _top(e)
    return rd or None


def digit_skeleton(host: str) -> str | None:
    """Host labels with digit runs masked, letters kept: 'allegrolokalnie.pe-7492104.sbs' -> 'allegrolokalnie.pe-#.sbs'.

    The rotated-domain pattern of one kit (scale lab: pair precision 0.95 on real phishing). Same algorithm as
    `hydra/adapters/public_feeds.digit_skeleton`, so both emit comparable `url_digit_skeleton` values."""
    if not host or is_ip(host):
        return None
    e = split_host(host)
    if not e.suffix or not e.domain:
        return None
    labels = [x for x in e.subdomain.split(".") if x and x != "www"] + [e.domain]
    return ".".join(re.sub(r"\d+", "#", x) for x in labels) + "." + e.suffix


def shape_is_lexical(skeleton: str) -> bool:
    """True when a shape skeleton carries structure beyond plain letter runs ('L4-D2L5.xyz'), False for
    'L9.com' / 'L3.L9.com' (every label one letter run: no lexical content, collides at scale)."""
    labels = skeleton.split(".")[:-1] if "." in skeleton else [skeleton]
    return any(not re.fullmatch(r"L\d+", x) for x in labels if x)


def label_shape(label: str) -> str:
    """'przykladoferta' -> 'L14', 'shop-42deals' -> 'L4-D2L5'."""
    out = []
    for m in re.finditer(r"[a-z]+|[0-9]+|[^a-z0-9]+", label.lower()):
        t = m.group(0)
        if t[0].isalpha():
            out.append(f"L{len(t)}")
        elif t[0].isdigit():
            out.append(f"D{len(t)}")
        else:
            out.append(t)
    return "".join(out)


def url_label_skeleton(host: str) -> str | None:
    """Shape of every label left of the public suffix, plus the suffix: 'www.shop-42deals.test' -> 'L3.L4-D2L5.test'."""
    if not host or is_ip(host):
        return None
    e = split_host(host)
    if not e.suffix or not e.domain:
        return None
    labels = [x for x in e.subdomain.split(".") if x] + [e.domain]
    return ".".join(label_shape(x) for x in labels) + "." + e.suffix


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def segment_shape(s: str) -> str:
    if not s:
        return ""
    if _UUID.match(s):
        return "{uuid}"
    if re.fullmatch(r"\d+", s):
        return "{num}"
    if re.fullmatch(r"[0-9a-f]{16,}", s, re.I):
        return f"{{hex{len(s)}}}"
    if re.fullmatch(r"[A-Za-z0-9_\-]{6,}", s) and re.search(r"\d", s) and re.search(r"[A-Za-z]", s):
        return f"{{tok{len(s)}}}"
    return s.lower()[:60]


def path_skeleton(path: str) -> str:
    return "/" + "/".join(segment_shape(s) for s in path.split("/")[1:])


def value_shape(v: str) -> str:
    if v == REDACTED:
        return "redacted"
    if _UUID.match(v):
        return "uuid-v" + v[14]
    if re.fullmatch(r"\d+", v):
        return f"num{len(v)}"
    if re.fullmatch(r"[A-Za-z0-9]+", v):
        return f"alnum{len(v)}"
    if not v:
        return "empty"
    return "str"


# ------------------------------------------------------------------------------------- redaction

SECRET_PARAM = re.compile(
    r"^(access_?token|token|api_?key|apikey|key|secret|password|passwd|pwd|auth|authorization|session|sessid|"
    r"sid_token|signature|sig|fbp|fbc|email|phone|tel|e-mail|name|first_?name|last_?name)$",
    re.I,
)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
# FB access tokens (EAA...) and long mixed-case secrets; pure hex (sha256 digests, ids) is left alone.
LONG_TOKEN = re.compile(
    r"\b(?:EAA[A-Za-z0-9]{20,}|(?=[A-Za-z0-9_\-]*[A-Z])(?=[A-Za-z0-9_\-]*[a-z])"
    r"(?=[A-Za-z0-9_\-]*[0-9])[A-Za-z0-9_\-]{48,})\b"
)
LOCAL_USER = re.compile(r"(?i)(/(?:Users|home)/|[A-Z]:[\\/]+Users[\\/]+|file:/+[A-Z]:/Users/)([^/\\]+)")


def redact_text(s: str) -> str:
    s = JWT.sub(REDACTED, s)
    s = LONG_TOKEN.sub(REDACTED, s)
    return LOCAL_USER.sub(lambda m: m.group(1) + REDACTED, s)


def redact_url(url: str) -> str:
    """Drop secret query values (tokens, API keys, PII params), JWTs and local user names; keep everything else."""
    try:
        sp = urlsplit(url)
    except ValueError:
        return redact_text(url)
    if sp.scheme == "file":
        return redact_text(url)
    q = []
    for k, v in parse_qsl(sp.query, keep_blank_values=True):
        if v and v != REDACTED and SECRET_PARAM.match(k):
            v = REDACTED
        q.append(f"{quote(k, safe='[]_-.')}={quote(redact_text(v), safe='[]_-.:/@')}" if v else quote(k, safe="_-."))
    return redact_text(urlunsplit((sp.scheme, sp.netloc, sp.path, "&".join(q), "")))


def is_secret_param(name: str) -> bool:
    return bool(SECRET_PARAM.match(name))
