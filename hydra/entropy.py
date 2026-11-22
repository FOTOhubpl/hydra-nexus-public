"""M2 ENTROPY: rarity of gene values over incidents, turned into evidence bits.

Discrete genes (any `kind` with a `value`): Fellegi–Sunter agreement weight `log2(m / u)`, where `u` is the
chance that an unrelated incident carries the same value. `u` is estimated from document frequency over the
*other* incidents of the corpus (the pair itself is excluded), smoothed towards a per-kind background prior
`u0`, so a 10-incident corpus does not pretend a value seen twice is common, and a 50k corpus is dominated by
its own counts:

    u = (df_others + A * u0) / (N_others + A)        bits = clip(log2(m / u), 0, cap)

Vector genes (`vector_ref` set): the background is the empirical distribution of cosine similarity between
vectors of *different* incidents for the same (kind, model). A best-of-k*l match is corrected for multiple
comparisons: `u_pair = min(1, k * l * P_bg(sim >= s))`. Raw cosine is never thresholded (SPEC_ERRATA).

Units: members of one campaign must not make each other's values look common. `Rarity.unit_of` maps
incidents to units (campaigns of a first clustering pass, or the known campaigns during assignment); df then
counts distinct units, and the pair's own units are excluded. Without `unit_of` every incident is a unit.

Stop-genes: platform and boilerplate values (Cloudflare, Google Fonts, jQuery, FB pixel loader, generic
icons) score 0 bits whatever their frequency. Values equal to the redaction marker never match.

Bits are heuristic evidence weights, not probabilities (CLAUDE.md).
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from hydra.contracts import REDACTED, Gene, GeneStatus

# Background priors per kind (u0 = chance an unrelated incident shares the value). Matched by exact kind,
# then by prefix / substring. Unknown kinds get DEFAULT_U0. These are assumptions, documented as such.
KIND_U0: list[tuple[str, float]] = [
    # operator payment / contact ids (ADOPT_PLAN item 3, hydra/genome/operator_ids.py): exact kinds first
    ("crypto_wallet", 2**-16), ("messenger_handle", 2**-14), ("bank_code", 2**-4), ("payment_method", 2**-2),
    ("text_template_key", 2**-9),  # GAPS_PAPERS G02, like phrase (assumed)
    ("asset_nameset_hash", 2**-10), ("asset_name_token", 2**-3),  # kit name set; one shared name alone is weak
    # kit exfiltration ids (hydra/genome/kit_exfil.py): bound to one operator account; kit markers shared per kit
    ("exfil_telegram_bot", 2**-16), ("exfil_telegram_botid", 2**-15), ("exfil_telegram_chat", 2**-16),
    ("exfil_discord_webhook", 2**-16), ("exfil_discord_token", 2**-16), ("mail_drop", 2**-15),
    ("captcha_sitekey", 2**-16), ("kit_author", 2**-9), ("kit_panel_path", 2**-7), ("kit_rule", 2**-9),
    # toll-fraud chain ids (hydra/genome/tollfraud.py, CERT Polska 2026-09-23; assumed): one store package /
    # developer / per-customer LB / leaf certificate per operation; a premium code serves many billing customers
    ("app_package_id", 2**-14), ("app_developer_id", 2**-13), ("premium_sms", 2**-12), ("premium_sms_code", 2**-5),
    ("carrier_billing_marker", 2**-3), ("cloud_lb_host", 2**-3), ("cloud_lb_name", 2**-14),
    ("tls_cert_sha256", 2**-14),
    ("chat_widget_id", 2**-14),("payment_processor_id", 2**-14), ("ad_publisher_id", 2**-14),  # G07 (assumed)
    ("tracker", 2**-14), ("pixel", 2**-14), ("platform_ad_id", 2**-16), ("ad_id", 2**-14), ("page_id", 2**-12),
    ("phone", 2**-14), ("iban", 2**-16), ("email", 2**-14), ("account", 2**-12),
    ("asset_sha256", 2**-12), ("sha256", 2**-12), ("favicon", 2**-10), ("kit_uuid", 2**-14), ("uuid", 2**-14),
    ("landing", 2**-12), ("host", 2**-12), ("domain", 2**-11), ("regdom", 2**-11), ("redirect", 2**-11),
    ("url_skeleton", 2**-7), ("path_skeleton", 2**-6), ("skeleton", 2**-7), ("endpoint", 2**-8),
    ("form", 2**-8), ("tds", 2**-8), ("param", 2**-5), ("ip24", 2**-6), ("ip", 2**-9), ("asn", 2**-3),
    ("ns", 2**-5), ("cert", 2**-9), ("registrar", 2**-3), ("title", 2**-10), ("og_", 2**-9),
    ("phrase", 2**-9), ("minhash", 2**-9), ("person", 2**-6), ("face", 2**-8), ("logo", 2**-5),
    ("scheme", 2**-2), ("lang", 2**-1), ("cdn", 2**-1), ("server", 2**-1), ("lib", 2**-2), ("cloak", 2**-3),
    ("dom", 2**-6), ("pdq", 2**-12), ("phash", 2**-10),
]
DEFAULT_U0 = 2**-8
DEFAULT_M = 0.5          # P(agree | same campaign); conservative, no labels to fit it yet
PRIOR_STRENGTH = 20.0    # pseudo-incidents behind u0
DISCRETE_CAP = 14.0
VECTOR_CAP = 10.0
MIN_BITS = 0.5           # weaker agreements are not reasons
# Hub cap (ADOPT #2): a value held by more than max(HUB_MIN, HUB_FRAC x corpus units) units is boilerplate (0 bits),
# like MISP's over-correlation limit. HUB_FRAC equals the retrieval cap (`retrieval.candidates` max_df), so a value
# too common to generate candidates is also too common to score.
HUB_MIN = 5
HUB_FRAC = 0.2
# With incident units (batch scoring: campaigns are not known yet) the k members of one campaign are k holders, so
# a 20 % cap erased a 13-member campaign in a 60-incident corpus (ASTRA pipeline audit growth control); there the
# hub cap is half the corpus.
HUB_FRAC_INCIDENTS = 0.5
SPREAD_MIN_CAMPAIGNS = 3  # operator-tier value held by members of >= 3 known campaigns: platform, 0 bits. Two campaigns
# sharing one is the analyst-bridge case (ADOPT #14), already discounted by the campaign-unit df.
_POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)

# Boilerplate: (kind regex, value regex). Matched case-insensitively against the full value.
STOP_GENES: list[tuple[str, str]] = [
    (r".*", r"(.*\.)?(cloudflare|cloudflare-dns|cloudflareinsights)\.(com|net)"),
    (r".*", r"(.*\.)?(googleapis|gstatic|google|googletagmanager|google-analytics|doubleclick|youtube|ytimg|"
            r"googlesyndication|googleadservices|googleusercontent)\.com"),
    (r".*", r"(.*\.)?(facebook|fbcdn|instagram|whatsapp)\.(com|net)|connect\.facebook\.net|fb\.com|wa\.me"),
    # messenger front doors: the account is the evidence (`messenger_handle`), never the shared domain
    (r".*", r"(t|telegram)\.(me|dog|org)|signal\.(me|group|org)|(.*\.)?viber\.com|line\.me"),
    (r".*", r"(.*\.)?(jsdelivr|cdnjs\.cloudflare|unpkg|jquery|bootstrapcdn|fontawesome|tiktok|tiktokcdn|"
            r"schema|w3|apple|microsoft|bing|github|unsplash|gravatar|wp|wordpress|amazonaws|akamaihd)\.(com|net|org|io)"),
    (r".*", r"(cloudflare|nginx|apache|litespeed|openresty|gws|esf|sffe|proxygen-bolt|envoy|ats)"),
    # web standards / SDK reference hosts that appear in every bundled JS (not infrastructure)
    (r".*", r"(.*\.)?(aomedia|w3|whatwg|schema|ietf|ecma-international|mozilla|json-schema|ogp|purl|xmlsoap|"
            r"adobe|apple|android|chromium|webkit|npmjs|npms|fpjs|conversionsapigateway|ims|broofa|jquery|github|"
            r"gitlab|stackoverflow|wikipedia|creativecommons|opensource|tc39|whatwg|polyfill|feross|zloirock)\.(org|com|io|ca|dev|net)"),
    # Meta/Google standard UTM values and ad-manager macros: identical for every advertiser
    (r"tds_value|tds_param|utm.*|url_param.*",
     r"utm_(source|medium|placement|campaign|content|term)=(paid|ig|fb|facebook|instagram|cpc|cpm|social|"
     r"others|feed|an|msg|story|reels|google|\{.*\})"),
    (r"lib.*|.*library.*|.*script_name.*", r"(jquery|bootstrap|react|vue|lodash|swiper|slick|fontawesome|gsap).*"),
    (r"lang.*", r".*"),
    (r".*", r"(favicon\.ico|index\.html?|/|about:blank|null|none|undefined|true|false|0|1|-)?"),
]
_STOP_RE = [(re.compile(k, re.I), re.compile(v, re.I)) for k, v in STOP_GENES]

# Placeholder / SDK-default identities (CR-ASTRA-1): documentation and template values that every copied snippet
# carries (`G-XXXXXXXXXX`, `UA-XXXXX-Y`, `YOUR_PIXEL_ID`, `{{PIXEL_ID}}`, `ca-pub-0000000000000000`). Corpus rarity
# says nothing about them: two unrelated pages that both left the snippet unfilled share the value. They are
# boilerplate whatever their frequency. Only identifier-like kinds are checked, so ordinary text is unaffected.
PLACEHOLDER_KINDS = re.compile(r"(tracker.*|pixel.*|.*_ids?|.*uuid|account.*|gtm.*|ga4?_.*|analytics.*|"
                               r"entity_(phone|iban|email)|tds_value|tds_param|url_param.*)", re.I)
_PLACEHOLDER_WORDS = re.compile(r"(^|[^a-z])(your|insert|replace|enter|example|sample|placeholder|dummy|changeme|"
                                r"todo)[_\- ]?(pixel|tracking|tracker|measurement|property|container|account|app|"
                                r"site|ga|gtm|client|publisher|ad|id|key|code|number)", re.I)
_TEMPLATE_SYNTAX = re.compile(r"(\{\{.*\}\}|\$\{.*\}|<%.*%>|%[A-Z_]{3,}%|\[[A-Z_]{3,}\]|__[A-Z_]{3,}__|<[A-Z_]{3,}>)")
_ID_PREFIX = re.compile(r"^(ca-pub|pub|gtm|ua|aw|dc|gt|mc|fb|px|tt|ym|g)(?=[-_:])", re.I)
_ASCENDING = "01234567890"


# Generic-duplicate guard (ADOPT #8, QA-3 finding): a form whose every field is a standard contact/consent field is
# the default lead form of every landing builder ("name,phone" gave an unrelated synthetic upload 7.3 kit bits to a
# known campaign in a 10-incident corpus). Such a field set identifies no kit. Field names are compared lower-case
# without separators.
GENERIC_FORM_KINDS = {"form_fields", "lead_form_kit", "funnel_capture_fields"}
GENERIC_FORM_FIELDS = {
    "name", "firstname", "lastname", "fname", "lname", "surname", "fullname", "yourname", "imie", "imię", "nazwisko",
    "phone", "tel", "telephone", "phonenumber", "mobile", "mobilephone", "cell", "telefon", "numertelefonu", "numer",
    "email", "mail", "emailaddress", "youremail", "country", "countrycode", "phonecc", "phonecode", "prefix",
    "dialcode", "city", "address", "zip", "zipcode", "postcode", "message", "comment", "comments", "subject",
    "submit", "agree", "terms", "consent", "privacy", "rodo", "zgoda", "gdpr", "policy", "newsletter", "subscribe",
    "csrf", "token", "csrftoken", "csrfmiddlewaretoken", "gtoken", "grecaptcharesponse", "recaptcha", "honeypot",
    "website", "url", "search", "s", "q", "query", "password", "login", "username", "user", "remember", "age",
    "gender", "lang", "language", "locale",
}


def is_generic_form(kind: str, value: str | None) -> bool:
    if value is None or kind not in GENERIC_FORM_KINDS:
        return False
    body = value.split(":", 1)[1] if value.startswith(("hidden:", "fields:")) else value
    fields = [re.sub(r"[^0-9a-ząćęłńóśźż]", "", f.lower()) for f in body.split(",")]
    fields = [f for f in fields if f]
    return bool(fields) and all(f in GENERIC_FORM_FIELDS for f in fields)


# Short whole-text lines (ADOPT #8, QUT rule): an ad line or headline of fewer than GENERIC_MIN_TOKENS word tokens
# ("Zarejestruj się teraz", "Kup teraz") is generic call-to-action copy, not campaign text. Only whole-line kinds;
# phrase shingles are fixed-length samples of longer texts and keep their rarity bits.
GENERIC_TEXT_KINDS = {"ocr_line", "library_ad_text", "headline", "ad_headline", "lead"}
GENERIC_MIN_TOKENS = 5
_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def is_generic_text(kind: str, value: str | None) -> bool:
    return value is not None and kind in GENERIC_TEXT_KINDS and len(_WORD.findall(value)) < GENERIC_MIN_TOKENS


def is_placeholder(kind: str, value: str | None) -> bool:
    """An SDK default / documentation placeholder in an identifier-like gene (no operator evidence):
    template syntax, "your/insert/example ... id" words, or after a known prefix (`G-`, `UA-`, `GTM-`, `ca-pub-`)
    a filler body: an X run with at most one trailing symbol (`XXXXX-Y`), one repeated symbol (`0000`, `####`),
    or an ascending digit run (`123456789`)."""
    if value is None or not PLACEHOLDER_KINDS.fullmatch(kind or ""):
        return False
    tail = value.strip().split("=", 1)[-1].strip()  # `key=value` encodings (tds_value, url params)
    if not tail:
        return False
    if _TEMPLATE_SYNTAX.search(tail) or _PLACEHOLDER_WORDS.search(tail):
        return True
    body = re.sub(r"[-_.:\s]", "", _ID_PREFIX.sub("", tail, count=1)).lower()
    if len(body) < 3:
        return False
    if body.count("x") >= 3 and body.count("x") >= len(body) - 1:
        return True
    if len(set(body)) == 1 and (len(body) >= 4 or body[0] in "#*?"):
        return True
    return len(body) >= 6 and body.isdigit() and body.startswith(("0123", "1234")) and body in _ASCENDING


def kind_u0(kind: str) -> float:
    k = kind.lower()
    for key, u0 in KIND_U0:
        if k == key:
            return u0
    for key, u0 in KIND_U0:
        if k.startswith(key) or key in k:
            return u0
    return DEFAULT_U0


_NULL_FPS: set[str] | None = None


def _null_fingerprints() -> set[str]:
    """Fingerprints of the redaction marker and of empty strings (both hash schemes in use): a hashed
    `[REDACTED]` shared by two incidents is not evidence (sample: one value linked incidents 2, 3, 14)."""
    global _NULL_FPS
    if _NULL_FPS is None:
        from hydra.redact import secret_fingerprint

        fps = {secret_fingerprint(v) for v in (REDACTED, "", "%5BREDACTED%5D")}
        try:
            from hydra.genome.webparse import pii_hash  # GENES-INF scheme

            fps |= {pii_hash(v) for v in (REDACTED, "", "%5BREDACTED%5D")}
        except ImportError:
            pass
        _NULL_FPS = fps
    return _NULL_FPS


def is_stop(kind: str, value: str | None) -> bool:
    if value is None:
        return True
    v = value.strip()
    tail = v.split("=", 1)[-1]
    if not v or v == REDACTED or tail == REDACTED or tail in _null_fingerprints() or v in _null_fingerprints():
        return True
    if is_placeholder(kind, v) or is_generic_form(kind, v):
        return True
    return any(kr.fullmatch(kind) and vr.fullmatch(v) for kr, vr in _STOP_RE)


def usable(g: Gene) -> bool:
    return g.status == GeneStatus.OBSERVED or g.status == "observed"


@dataclass
class Background:
    """Cosine similarities between vectors of different incidents for one (kind, model)."""

    sims: np.ndarray  # sorted ascending
    n_vectors: int
    _quantiles: dict[float, float] = field(default_factory=dict, repr=False, compare=False)
    _sims64: np.ndarray | None = field(default=None, repr=False, compare=False)

    def quantile(self, q: float) -> float:
        """`np.quantile(sims, q)`, memoised (candidate generation asks once per query incident)."""
        if q not in self._quantiles:
            self._quantiles[q] = float(np.quantile(self.sims, q))
        return self._quantiles[q]

    def tail(self, s: float) -> float:
        n = len(self.sims)
        if n == 0:
            return 1.0
        sims = self.sims
        if isinstance(s, float) and sims.dtype == np.float32:  # Python float and np.float64, not np.float32
            # numpy compares a float32 array with a Python / float64 scalar in float64 by casting the whole array on
            # every call (O(n), ~0.2 ms at 400k values: 19 % of score_pair, SCALE 2026-11-21). The same comparison
            # on a cached float64 copy gives the identical index in O(log n).
            if self._sims64 is None:
                self._sims64 = sims.astype(np.float64)
            sims = self._sims64
        ge = n - int(np.searchsorted(sims, s, side="left"))
        return (ge + 0.5) / (n + 1.0)


@dataclass
class BackgroundPrior:
    """Document frequencies from an external corpus (`ext:*` / `public:*`), used only when a run enables them.

    `df[(kind, value)]` = external incidents holding the value; `n` = external incidents with at least one gene
    of that kind (`n_by_kind`), so a kind the external corpus never observes does not dilute rarity."""

    corpus: str
    n: int
    n_by_kind: dict[str, int]
    df: dict[tuple[str, str], int]

    def lookup(self, kind: str, value: str) -> tuple[int, int]:
        n = self.n_by_kind.get(kind, 0)
        return (self.df.get((kind, value), 0), n) if n else (0, 0)

    def to_json(self) -> dict:
        return {"corpus": self.corpus, "n": self.n, "n_by_kind": self.n_by_kind,
                "df": [[k, v, c] for (k, v), c in self.df.items()]}

    @classmethod
    def from_json(cls, d: dict) -> BackgroundPrior:
        return cls(d["corpus"], int(d["n"]), {k: int(v) for k, v in d["n_by_kind"].items()},
                   {(k, v): int(c) for k, v, c in d["df"]})


@dataclass
class CompositePrior:
    """Several background priors (e.g. the shipped hashed prior + a `--prior ext:...` corpus) as one: counts and
    kind sizes add up (disjoint corpora assumed; the shipped prior is built without the eval corpora)."""

    parts: list

    @property
    def corpus(self) -> str:
        return "+".join(p.corpus for p in self.parts)

    @property
    def n(self) -> int:
        return sum(p.n for p in self.parts)

    def lookup(self, kind: str, value: str) -> tuple[int, int]:
        df = n = 0
        for p in self.parts:
            d, k = p.lookup(kind, value)
            df, n = df + d, n + k
        return df, n


def build_prior(genes_by_incident: dict[str, list[Gene]], corpus: str) -> BackgroundPrior:
    df: dict[tuple[str, str], int] = defaultdict(int)
    by_kind: dict[str, set[str]] = defaultdict(set)
    for iid, genes in genes_by_incident.items():
        seen: set[tuple[str, str]] = set()
        for g in genes:
            if g.value is None or g.vector_ref is not None or not usable(g):
                continue
            by_kind[g.kind].add(iid)
            key = (g.kind, g.value)
            if key not in seen:
                seen.add(key)
                df[key] += 1
    return BackgroundPrior(corpus, len(genes_by_incident), {k: len(v) for k, v in by_kind.items()}, dict(df))


@dataclass
class Rarity:
    n_incidents: int
    postings: dict[tuple[str, str], set[str]]
    backgrounds: dict[tuple[str, str], Background] = field(default_factory=dict)
    m: float = DEFAULT_M
    prior_strength: float = PRIOR_STRENGTH
    unit_of: dict[str, str] | None = None
    n_units: int = 0
    kit_unit_of: dict[str, str] | None = None  # units for kit-level genes only (assignment: campaign groups)
    prior: Any = None  # BackgroundPrior / shipped prior / CompositePrior: external document frequencies
    ignored: set[tuple[str, str]] = field(default_factory=set)  # analyst "ignore value" (kind, value): 0 bits
    # Known-campaign units for operator-tier genes (open-set assignment, review M1): a tracker shared by the k
    # members of one campaign is one observation of that operator, not k independent ones.
    operator_unit_of: dict[str, str] | None = None
    # Analyse / batch grouping (no known campaigns yet): pass-0 units from shared account-kind operator values
    # (`operator_seed_units`), so a k-member unknown campaign sharing one tracker/phone/host is one holder as in
    # assignment, not k (SPEC_ERRATA, finding C1). Used only when `operator_unit_of` is None.
    seed_operator_unit_of: dict[str, str] | None = None
    # HYDRA_OPERATOR_UNITS=smooth only: (kind, value) -> weight of the one-unit regime for operator values above the
    # seed-unit limit (`operator_seed_soft`); None in the default mode
    seed_operator_soft: dict[tuple[str, str], float] | None = None

    def __post_init__(self) -> None:
        if not self.n_units:
            self.n_units = self.n_incidents

    def _all_ids(self) -> set[str]:
        if not hasattr(self, "_ids_cache"):
            self._ids_cache = {i for hs in self.postings.values() for i in hs}
        return self._ids_cache

    def unit(self, iid: str) -> str:
        return self.unit_of.get(iid, iid) if self.unit_of else iid

    def df(self, kind: str, value: str) -> int:
        return len(self.postings.get((kind, value), ()))

    def _near_index(self, kind: str, nbytes: int) -> tuple[np.ndarray, list[set[str]]] | None:
        cache = self.__dict__.setdefault("_near_index_cache", {})
        if (kind, nbytes) not in cache:
            rows, holders = [], []
            for (_k, v), hs in sorted(((k, v), hs) for (k, v), hs in self.postings.items()
                                     if k == kind and len(v) == 2 * nbytes):
                try:
                    raw = bytes.fromhex(v)
                except ValueError:
                    continue
                rows.append(np.frombuffer(raw, dtype=np.uint8))
                holders.append(hs)
            cache[(kind, nbytes)] = (np.stack(rows), holders) if rows else None
        return cache[(kind, nbytes)]

    def near_df(self, kind: str, value: str, max_d: int) -> int:
        """Incidents holding a hash within `max_d` bits of `value` (the value's own holders included). Re-encoded
        or slightly edited copies of one stock image have different exact hashes; their frequency is the size of
        this Hamming ball, not the exact-value count (CR-ASTRA-8). With `operator_unit_of` set (assignment) holders
        are counted as known-campaign units. Falls back to `df` for unparsable values."""
        exact = self.df(kind, value)
        try:
            q = np.frombuffer(bytes.fromhex(value), dtype=np.uint8)
        except ValueError:
            return exact
        if not len(q):
            return exact
        cache = self.__dict__.setdefault("_near_df_cache", {})
        key = (kind, value, max_d, len(self.operator_unit_of or ()))
        if key not in cache:
            idx = self._near_index(kind, len(q))
            if idx is None:
                cache[key] = exact
            else:
                mat, holders = idx
                d = _POPCOUNT[np.bitwise_xor(mat, q[None, :])].sum(axis=1)
                hit: set[str] = set()
                for k in np.flatnonzero(d <= max_d).tolist():
                    hit |= holders[k]
                # known-campaign units during assignment (review M1 applied to images): the members of one campaign
                # holding its creative are one holder, so a campaign's own image is not "stock"
                if self.operator_unit_of:
                    umap = {**(self.unit_of or {}), **self.operator_unit_of}
                    hit = {umap.get(h, h) for h in hit}
                    cache[key] = len(hit)
                else:
                    cache[key] = max(exact, len(hit))
        return cache[key]

    def minhash_near_df(self, value: str, min_jaccard: float) -> int:
        """Incidents (known-campaign units during assignment) whose MinHash signature of the same channel
        ("<channel>:<hex>") agrees with `value` on at least `min_jaccard` of the positions."""
        ch, _, hexsig = value.partition(":")
        try:
            q = np.frombuffer(bytes.fromhex(hexsig), dtype=">u4")
        except ValueError:
            return self.df("minhash_sig", value)
        cache = self.__dict__.setdefault("_mh_df_cache", {})
        key = (value, min_jaccard, len(self.operator_unit_of or ()))
        if key in cache:
            return cache[key]
        idx = self.__dict__.setdefault("_mh_index", {})
        if (ch, len(q)) not in idx:
            rows, holders = [], []
            for (_k, v), hs in sorted(((k, v), hs) for (k, v), hs in self.postings.items()
                                      if k == "minhash_sig" and v.startswith(ch + ":")):
                try:
                    arr = np.frombuffer(bytes.fromhex(v.split(":", 1)[1]), dtype=">u4")
                except ValueError:
                    continue
                if len(arr) == len(q):
                    rows.append(arr)
                    holders.append(hs)
            idx[(ch, len(q))] = (np.stack(rows), holders) if rows else None
        entry = idx[(ch, len(q))]
        hit: set[str] = set()
        if entry is not None:
            mat, holders = entry
            for k in np.flatnonzero((mat == q[None, :]).mean(axis=1) >= min_jaccard).tolist():
                hit |= holders[k]
        if self.operator_unit_of:
            umap = self._merged_operator_units()
            hit = {umap.get(h, h) for h in hit}
        cache[key] = max(len(hit), 1)
        return cache[key]

    def _merged_operator_units(self, op_units: dict[str, str] | None = None) -> dict[str, str]:
        op = self.operator_unit_of if op_units is None else op_units
        key = (id(self.unit_of), id(op))
        cache = self.__dict__.setdefault("_op_units_cache", {})
        if key not in cache:
            cache.clear()
            cache[key] = {**(self.unit_of or {}), **(op or {})}
        return cache[key]

    def _n_units_for(self, umap: dict[str, str]) -> int:
        cache = self.__dict__.setdefault("_n_units_cache", {})
        if id(umap) not in cache:
            cache[id(umap)] = (umap, len({umap.get(i, i) for i in self._all_ids()}))
        return cache[id(umap)][1]

    def hub_limit(self) -> int:
        """Holders above which a value is a hub (over-correlating): max(HUB_MIN, HUB_FRAC x units)."""
        return max(HUB_MIN, math.ceil(HUB_FRAC * self.n_units))

    def is_hub(self, kind: str, value: str) -> bool:
        return self.df(kind, value) > self.hub_limit()

    def discrete_bits(self, kind: str, value: str, pair: Iterable[str] = (), kit: bool = False,
                      operator: bool = False) -> tuple[float, int]:
        """Bits for both incidents sharing `value`; `pair` members are excluded from the frequency.
        `kit=True` counts units by `kit_unit_of` when set (related campaigns share one kit). `operator=True` counts
        units by `operator_unit_of` when set (known campaigns), and a value held by members of two or more known
        campaigns is a shared platform value, not one operator's identity (spread rule): 0 bits."""
        if is_stop(kind, value) or is_generic_text(kind, value) or (kind, value) in self.ignored:
            return 0.0, self.df(kind, value)  # generic short text stays an observation (lineage, display), 0 bits
        holders = self.postings.get((kind, value), set())
        df = len(holders)
        pair = tuple(pair)
        umap = self.kit_unit_of if (kit and self.kit_unit_of) else self.unit_of
        campaign_units = bool(kit and self.kit_unit_of)
        seed_units_ = getattr(self, "seed_operator_unit_of", None)
        soft = getattr(self, "seed_operator_soft", None)
        if operator and soft and not self.operator_unit_of and (kind, value) in soft:
            return self._soft_operator_bits(kind, value, holders, pair, soft[(kind, value)]), df
        if operator and self.operator_unit_of:
            known = {self.operator_unit_of[h] for h in holders if h in self.operator_unit_of}
            if len(known) >= SPREAD_MIN_CAMPAIGNS:
                return 0.0, df
            umap, campaign_units = self._merged_operator_units(), True
        elif operator and seed_units_:
            umap, campaign_units = self._merged_operator_units(seed_units_), True
        if umap:
            units = {umap.get(h, h) for h in holders}
            pair_units = {umap.get(p, p) for p in pair}
            n_units = self._n_units_for(umap)
        else:
            units, pair_units, n_units = set(holders), set(pair), self.n_incidents
        # kit values keep the 20 % cap with incident units too: a kit part used across a fifth of the corpus is a
        # generic template part (EVAL: sample+syn group F1 0.846 with the half-corpus cap, 1.0 with 20 %)
        frac = HUB_FRAC if (campaign_units or kit) else HUB_FRAC_INCIDENTS
        if len(units) > max(HUB_MIN, math.ceil(frac * n_units)):
            return 0.0, df  # hub value held by too many units: boilerplate (ADOPT #2, MISP over-correlation)
        df_o = len(units - pair_units)
        n_o = max(0, n_units - len(pair_units))
        u = self.u_estimate(kind, value, df_o, n_o)
        bits = math.log2(self.m / max(u, 1e-12))
        return float(min(DISCRETE_CAP, max(0.0, bits))), df

    def _soft_operator_bits(self, kind: str, value: str, holders: set[str], pair: tuple[str, ...],
                            w: float) -> float:
        """HYDRA_OPERATOR_UNITS=smooth (SCORE-GROWTH, CR-SCORE-GROWTH-1): an operator value held by more incidents
        than the seed-unit limit but at most the incident hub limit gets `w x bits(holders as one unit) +
        (1 - w) x bits(holders as incidents)`, `w` falling linearly with the holder count (`operator_seed_soft`).
        The value's holders are merged for this value only; no other value sees that union."""
        base = self._merged_operator_units(self.seed_operator_unit_of) if self.seed_operator_unit_of else {}

        def bits_for(units: set[str], pair_units: set[str], n_units: int, frac: float) -> float:
            if len(units) > max(HUB_MIN, math.ceil(frac * n_units)):
                return 0.0
            u = self.u_estimate(kind, value, len(units - pair_units), max(0, n_units - len(pair_units)))
            return float(min(DISCRETE_CAP, max(0.0, math.log2(self.m / max(u, 1e-12)))))

        def bu(i: str) -> str:
            return base.get(i, i)

        n_base = self._n_units_for(base) if base else self.n_incidents
        inc_units = {bu(h) for h in holders}
        b_inc = bits_for(inc_units, {bu(p) for p in pair}, n_base, HUB_FRAC_INCIDENTS)
        rep = min(inc_units)
        joined = {rep if u in inc_units else u for u in ({bu(p) for p in pair})}
        b_unit = bits_for({rep}, joined, n_base - len(inc_units) + 1, HUB_FRAC)
        return w * b_unit + (1.0 - w) * b_inc

    def u_estimate(self, kind: str, value: str, df_o: int, n_o: int) -> float:
        """Chance that an unrelated incident carries `value`.

        Local: `u_loc = (df_o + A*u0) / (n_o + A)` (Beta prior with mean u0 and strength A pseudo-incidents).
        Background (`self.prior`, external corpora: the shipped hashed prior and/or `--prior`): when the value is
        seen there (`p_df > 0` of `p_n` background units observing the kind), its units are pooled with the local
        ones as further unrelated observations, `u_pool = (df_o + p_df + A*u0) / (n_o + p_n + A)`, and
        `u = max(u_loc, u_pool)`. One-sided on purpose: the background population (web pages, global phishing)
        differs from the local one, so it may only say "this value is common" (CDN, cookie banner, CMS template),
        never "rarer than the local corpus says". Values unseen in the background keep the local estimate."""
        u0 = kind_u0(kind)
        a = self.prior_strength
        u_loc = (df_o + a * u0) / (n_o + a)
        if self.prior is None:
            return u_loc
        p_df, p_n = self.prior.lookup(kind, value)
        if p_df <= 0 or p_n <= 0:
            return u_loc
        return max(u_loc, (df_o + p_df + a * u0) / (n_o + p_n + a))

    def background_df(self, kind: str, value: str) -> tuple[int, int]:
        """(units holding the value, units observing the kind) in the background prior; (0, 0) without one."""
        return self.prior.lookup(kind, value) if self.prior is not None else (0, 0)

    def vector_bits(self, kind: str, model: str, sim: float, comparisons: int = 1) -> float:
        bg = self.backgrounds.get((kind, model))
        if bg is None or len(bg.sims) < 20:
            # No usable background yet: a conservative fixed curve (near-duplicates only).
            u = 2 ** (-max(0.0, (sim - 0.85) / 0.15 * 8.0)) if sim > 0.85 else 1.0
        else:
            u = bg.tail(sim)
        u_pair = min(1.0, max(comparisons, 1) * u)
        bits = math.log2(self.m / max(u_pair, 1e-12))
        return float(min(VECTOR_CAP, max(0.0, bits)))


def build_postings(genes_by_incident: dict[str, list[Gene]]) -> dict[tuple[str, str], set[str]]:
    post: dict[tuple[str, str], set[str]] = defaultdict(set)
    for iid, genes in genes_by_incident.items():
        for g in genes:
            if g.value is not None and g.vector_ref is None and usable(g):
                post[(g.kind, g.value)].add(iid)
    return dict(post)


def build_background(vectors: dict[str, np.ndarray], max_vectors: int = 3000, seed: int = 0,
                     unit_of: dict[str, str] | None = None) -> Background:
    """`vectors`: incident id → [n_i, d] matrix. Cosine similarities across different units (sampled)."""
    owners, mats = [], []
    for iid, m in vectors.items():
        if len(m):
            owners.extend([(unit_of or {}).get(iid, iid)] * len(m))
            mats.append(m)
    if not mats:
        return Background(np.zeros(0, np.float32), 0)
    x = np.concatenate(mats).astype(np.float32)
    own = np.array(owners)
    if len(x) > max_vectors:
        idx = np.random.default_rng(seed).choice(len(x), max_vectors, replace=False)
        x, own = x[idx], own[idx]
    sims = x @ x.T
    mask = own[:, None] != own[None, :]
    iu = np.triu_indices(len(x), k=1)
    vals = sims[iu][mask[iu]]
    return Background(np.sort(vals.astype(np.float32)), len(x))


def build_rarity(genes_by_incident: dict[str, list[Gene]],
                 vectors_by_kind: dict[tuple[str, str], dict[str, np.ndarray]],
                 unit_of: dict[str, str] | None = None) -> Rarity:
    n_units = len({(unit_of or {}).get(i, i) for i in genes_by_incident})
    r = Rarity(n_incidents=len(genes_by_incident), postings=build_postings(genes_by_incident), unit_of=unit_of,
               n_units=n_units)
    for key, per_inc in vectors_by_kind.items():
        r.backgrounds[key] = build_background(per_inc, unit_of=unit_of)
    return r


def duplicate_units(genes_by_incident: dict[str, list[Gene]]) -> dict[str, str] | None:
    """Exact-duplicate rarity units (review M9, CR-VP-1): incidents whose non-empty sets of original-file hashes
    (`artifact_sha256`) are identical are one unit, named by the lexicographically first incident id. N copies of
    one incident then add 1, not N, to the document frequency of every value they share. None when no duplicates."""
    by_set: dict[frozenset[str], list[str]] = defaultdict(list)
    for iid, genes in genes_by_incident.items():
        arts = frozenset(g.value for g in genes if g.kind == "artifact_sha256" and g.value and usable(g))
        if arts:
            by_set[arts].append(iid)
    out = {m: min(ids) for ids in by_set.values() if len(ids) > 1 for m in ids}
    return out or None


def _operator_holders(genes_by_incident: dict[str, list[Gene]],
                      ignored: Iterable[tuple[str, str]] = ()) -> dict[tuple[str, str], set[str]]:
    """Holders of every account-kind operator-tier campaign-level value (observed, not stop-listed or generic)."""
    from hydra.synapse import gene_tier, is_account_kind, level

    ign = set(ignored)
    holders: dict[tuple[str, str], set[str]] = defaultdict(set)
    for iid, genes in genes_by_incident.items():
        for g in genes:
            if g.value is None or g.vector_ref is not None or not usable(g) or not is_account_kind(g.kind):
                continue
            key = (g.kind, g.value)
            if (gene_tier(g) != "operator" or level(g) != "campaign" or key in ign or is_stop(*key)
                    or is_generic_text(*key)):
                continue
            holders[key].add(iid)
    return holders


def _union_units(holders: dict[tuple[str, str], set[str]], keys: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Union-find over the holders of `keys`; only incidents joined to another one are in the result."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for key in sorted(keys):
        first, *rest = sorted(holders[key])
        for h in rest:
            ra, rb = find(first), find(h)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    return {i: find(i) for i in sorted(parent)}


def operator_seed_units(genes_by_incident: dict[str, list[Gene]],
                        ignored: Iterable[tuple[str, str]] = ()) -> dict[str, str] | None:
    """Pass-0 operator units for analyse and batch grouping (finding C1): incidents that share one account-kind
    operator-tier value (`synapse.is_account_kind`, tier `operator`, campaign level; observed, not stop-listed or
    generic) are one unit (union-find, `seed_units` with min_values=1 restricted to those kinds). Values held by
    more than the campaign-unit hub limit max(HUB_MIN, ceil(HUB_FRAC x incidents)) do not join units (they stay
    hub/boilerplate under the incident count). Only `Rarity.discrete_bits(operator=True)` reads these units: the
    same regime as assignment, where operator values count known campaigns (`operator_unit_of`)."""
    limit = max(HUB_MIN, math.ceil(HUB_FRAC * len(genes_by_incident)))
    holders = _operator_holders(genes_by_incident, ignored)
    return _union_units(holders, [k for k, hs in holders.items() if 2 <= len(hs) <= limit]) or None


def operator_units_mode() -> str:
    """`HYDRA_OPERATOR_UNITS`: "smooth" (default since 2026-11-22, operator decision; SCORE-GROWTH) or "legacy"
    (hard seed-unit limit of SCORE-C1, kept for comparison)."""
    import os

    mode = os.environ.get("HYDRA_OPERATOR_UNITS", "").strip().lower()
    return "legacy" if mode == "legacy" else "smooth"


def operator_seed_soft(genes_by_incident: dict[str, list[Gene]], ignored: Iterable[tuple[str, str]] = ()
                       ) -> tuple[dict[str, str] | None, dict[tuple[str, str], float] | None, set[tuple[str, str]]]:
    """HYDRA_OPERATOR_UNITS=smooth (SCORE-GROWTH, ASTRA-DELTA-07, CR-SCORE-GROWTH-1). Returns (units, soft, spread).

    1. Spread across operations: a value whose holders fall into >= SPREAD_MIN_CAMPAIGNS distinct multi-incident
       units formed by the *other* operator values (each such unit = one operation evidenced by its own identifier)
       is a cross-operator value (vendor SDK, shared platform account): it joins no unit and gets no soft weight,
       the analyse counterpart of the assignment spread rule.
    2. Units: the remaining values held by 2..L incidents, L = max(HUB_MIN, ceil(HUB_FRAC x n)), are unioned as in
       the default mode.
    3. Graded attenuation instead of the hard cut-off: a remaining value held by h incidents, L < h <= U,
       U = max(2 x HUB_MIN, ceil(HUB_FRAC_INCIDENTS x n)), gets weight w = (U + 1 - h) / (U + 1 - L) of the one-unit
       regime (`Rarity._soft_operator_bits`); it does not join units, so no other value inherits its union."""
    n = len(genes_by_incident)
    lim = max(HUB_MIN, math.ceil(HUB_FRAC * n))
    upper = max(2 * HUB_MIN, math.ceil(HUB_FRAC_INCIDENTS * n))
    holders = _operator_holders(genes_by_incident, ignored)
    small = [k for k, hs in holders.items() if 2 <= len(hs) <= lim]
    spread: set[tuple[str, str]] = set()
    for key, hs in holders.items():
        if len(hs) < 2 * SPREAD_MIN_CAMPAIGNS or len(hs) > upper:
            continue
        # units formed by other values only: does this value cut across several independently evidenced operations?
        others = _union_units(holders, [k for k in small if k != key and holders[k] & hs])
        sizes: dict[str, int] = defaultdict(int)
        for root in others.values():
            sizes[root] += 1
        if len({others[h] for h in hs if h in others and sizes[others[h]] >= 2}) >= SPREAD_MIN_CAMPAIGNS:
            spread.add(key)
    units = _union_units(holders, [k for k in small if k not in spread]) or None
    soft = {k: (upper + 1 - len(hs)) / (upper + 1 - lim) for k, hs in holders.items()
            if lim < len(hs) <= upper and k not in spread}
    return units, soft or None, spread


def apply_operator_seed_units(rarity: Rarity, genes_by_incident: dict[str, list[Gene]],
                              ignored: Iterable[tuple[str, str]] = ()) -> None:
    """Set the pass-0 operator units on `rarity` for the active HYDRA_OPERATOR_UNITS mode."""
    if operator_units_mode() == "smooth":
        rarity.seed_operator_unit_of, rarity.seed_operator_soft, _spread = operator_seed_soft(genes_by_incident,
                                                                                               ignored)
    else:
        rarity.seed_operator_unit_of = operator_seed_units(genes_by_incident, ignored)


def seed_units(genes_by_incident: dict[str, list[Gene]], postings: dict[tuple[str, str], set[str]], *,
               specific_u0: float = 2**-9, min_values: int = 2) -> dict[str, str]:
    """Pass-0 rarity units: incidents that share ≥ `min_values` specific values (kind prior ≤ `specific_u0`,
    not boilerplate, held by few incidents) from *different* dependency groups are one unit (union-find).
    Prevents the first scoring pass from treating campaign siblings as independent background."""
    n = len(genes_by_incident)
    max_df = max(3, int(0.15 * n))
    group_of: dict[tuple[str, str], str] = {}
    for genes in genes_by_incident.values():
        for g in genes:
            if g.value is not None and g.vector_ref is None:
                group_of.setdefault((g.kind, g.value), g.dependency_group or f"{g.family}:{g.kind}")
    shared: dict[tuple[str, str], set[str]] = defaultdict(set)  # pair → dependency groups
    for key, holders in postings.items():
        if len(holders) < 2 or len(holders) > max_df or kind_u0(key[0]) > specific_u0 or is_stop(*key):
            continue
        hs = sorted(holders)
        for i, a in enumerate(hs):
            for b in hs[i + 1:]:
                shared[(a, b)].add(group_of.get(key, key[0]))
    parent = {i: i for i in genes_by_incident}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b), groups in shared.items():
        if len(groups) >= min_values and a in parent and b in parent:
            parent[find(a)] = find(b)
    return {i: find(i) for i in genes_by_incident}
