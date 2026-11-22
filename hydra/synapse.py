"""M5 SYNAPSE: pair scoring with rarity bits per family, dependency groups, contradictions and reasons.

Default scorer `rarity_bits@1` (baseline B3 in PLAN_MAX): labels are too few in the sample to fit a model.
Per pair:
1. every shared discrete value → bits from `entropy.Rarity.discrete_bits` (pair excluded from df);
   near-duplicate hash kinds (pdq, phash…) match by Hamming distance;
   vector kinds → best cosine between the two incidents' vectors → background bits (multiple-comparison
   corrected);
2. bits are pooled per dependency group (`Gene.dependency_group`, default `FAMILY:kind`): the strongest
   gene counts fully, the rest at `REST_WEIGHT`, capped at `GROUP_CAP` (correlated genes never stack);
3. groups sum per family, capped at `FAMILY_CAP`; total = families + contradictions;
4. hypothesis: same_campaign / shared_template (only kit-level evidence) / shared_asset (only images,
   faces, logos) / independent / abstain;
5. missing families (no observed gene on one side) are listed and never penalised.

`FusionModel` (LR on per-family bits + missingness, isotonic calibration) is the supervised path, used only
when enough labelled pairs exist.

Gene tiers come from `dependency_group` = "<family>:<tier>:<name>" (INFRA_GENES.md): operator and phrase →
campaign level; platform and kit → kit level (they never count towards a campaign
link, uncapped for campaign-group links); asset and persona → asset level; boilerplate and label (CERT Polska
list hits) → 0 bits. Groups without a tier fall back to: prefix `kit`/`template` or a kind in `KIT_KINDS` →
kit level; VIS/ID families, logo/face kinds → asset level; anything else → campaign level.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache

import numpy as np

from hydra.contracts import (
    EvidencePointer,
    Family,
    Gene,
    GeneMatch,
    GeneStatus,
    Hypothesis,
    PairScore,
    Provenance,
)
from hydra.entropy import MIN_BITS, Rarity, is_generic_text, is_stop, kind_u0, usable
from hydra.labels_pl import num_pl, schemes_pl

MODEL_NAME = "rarity_bits@1"
REST_WEIGHT = 0.25
GROUP_CAP = 14.0
FAMILY_CAP = 24.0
CONTRADICTION_BITS = -4.0
PLATFORM_KIT_CAP = 0.0  # kit/platform bits towards a campaign link (was 4.0; 0 since 2026-11-21 14:20)
# Per-pair cap on campaign-level bits per family (below the default link threshold of 10 for every family
# except INF and META, whose campaign-level genes are operator-tier): one family alone cannot make a campaign
# link unless it is operator-tier INF/META or an identical file.
FAMILY_CAMPAIGN_CAP = {"INF": 24.0, "META": 24.0, "TXT": 8.0, "VIS": 8.0, "ID": 6.0, "NAR": 0.0}
ASSET_CAP = 8.0
# Near-duplicate standalone rules (architect 2026-11-21, after EVAL's ablation: text-only / visual-only F1 0.0).
NEARDUP_BITS = 12.0
TEXT_NEARDUP_JACCARD = 0.9      # MinHash Jaccard over the page channel (nav/footer/comments/legal excluded)
TEXT_NEARDUP_MIN_SHINGLES = 50  # word 5-shingles ≈ 300+ characters of body text
VIS_NEARDUP_PHASH = 6
VIS_NEARDUP_PDQ = 31
VIS_NEARDUP_COS = 0.95
VIS_CROP_PHASH = 6  # sub-window pHash vs whole image (crop variants); same bound as the whole-image rule
VIS_STANDALONE_CLASSES = {"creative", "desktop_screenshot", "region"}  # the ad itself; frames need >= 2 units
VIS_EXCLUDED_CLASSES = {"avatar", "icon", "logo", "pixel", "phone_screenshot"}
NEARDUP_KINDS = {"text_near_duplicate", "visual_near_duplicate", "video_segment", "cross_modal_text"}
# Rare multi-item evidence lifts a family cap: >= RARE_MIN_ITEMS distinct items held by <= 2 incidents.
RAISED_CAP = 12.0
RARE_MIN_ITEMS = 3
# Contact identities that are operator evidence when corroborated by rare text (CR-ASTRA-7).
CONTACT_KINDS = {"entity_phone", "entity_iban", "entity_email", "entity_crypto", "entity_wallet", "entity_btc",
                 "entity_account"}
CONTACT_MIN_RARE_TEXT = 2
# >= 5 near-identical page images = the same landing page (sample: same-campaign pairs share 11 and 6, the worst
# cross-campaign pair 1; CORE_LOG 16:10). Then VIS may link on its own, like a near-duplicate page text.
VIS_MULTI_STANDALONE = 5
SUM_FAMILIES = {"INF", "META"}
# A same-campaign link needs operator-tier INF/META (or identical-file) evidence >= OPERATOR_STRONG_BITS, or
# >= STRONG_FAMILY_BITS (= the TXT/VIS caps) in two families (architect rule 2026-11-21, measured on the
# sample: umbrella pairs reach INF 7.3 + TXT 8 when a sibling campaign is held out; true pairs INF >= 24).
OPERATOR_STRONG_BITS = 8.0
STRONG_FAMILY_BITS = 8.0
STYLE_CAP = 3.0
# Fuzzy template/style kit genes: shared by unrelated pages built from similar templates or tracker products
# (sample: CSS class tokens and DOM shingles chained nutra, gambling and investment pages). They add at most
# STYLE_CAP bits to kit-level (group) evidence; identity kit genes (asset/build hashes, observed endpoints,
# POST field sets, favicon, linked hosts) are not capped.
STYLE_KINDS = {
    "dom_token", "dom_shingle", "dom_shingles", "html_comment", "class_vocab", "dom_id", "data_attrs",
    "cloak", "cloak_feature", "script_name", "fake_comment", "fake_comment_block", "comment_names",
    "comment_names_set", "code_endpoint", "tds_signature", "dom_skeleton", "inline_js", "lib", "libs",
    "param_set", "url_param_set", "utm_pattern", "param_shape", "url_path_skeleton", "media_brand",
}


@dataclass
class Thresholds:
    link: float = 10.0      # same-campaign link (campaign-level bits)
    kit: float = 6.0        # shared template / campaign-group link (kit-level bits)
    low: float = 4.0        # below: independent
    asset: float = 6.0      # shared asset (VIS/ID only)
    max_reasons: int = 12


KIT_KINDS = {
    "dom_skeleton", "dom_shingle", "dom_shingles", "class_vocab", "dom_id", "data_attrs", "js_file_hash",
    "inline_js", "css_hash", "js_hash", "lib", "libs", "endpoint", "endpoints", "form_endpoint", "form_sig",
    "form_sigs", "post_endpoint", "post_fields", "param_set", "param_shape", "click_params", "tds", "tds_param",
    "cloak", "url_skeleton", "host_skeleton", "path_skeleton", "req_path_skeleton", "html_comment",
    "comment_names", "sdk", "kit", "template", "asset_sha256_code",
}
ASSET_FAMILIES = {Family.VIS.value, Family.ID.value}
ASSET_KINDS_PREFIX = ("logo", "face")
CONTRADICTION_KINDS = {"scheme"}
# Hex-hash kinds compared by Hamming distance: prefix → (bits, max distance for a match)
NEAR_HASH = {"pdq": (256, 31), "phash_tile": (64, 6), "phash": (64, 10), "dhash": (64, 10), "ahash": (64, 6)}

KIND_PL = {
    "host": "Wspólny host", "domain": "Wspólna domena", "regdom": "Wspólna domena rejestrowalna",
    "registrable_domain": "Wspólna domena rejestrowalna", "url_skeleton": "Ten sam szkielet adresu URL",
    "host_skeleton": "Ten sam szkielet nazwy hosta", "path_skeleton": "Ten sam szkielet ścieżki URL",
    "redirect_chain": "Ten sam łańcuch przekierowań", "ip": "Ten sam adres IP serwera",
    "ip24": "Ta sama podsieć /24 serwera", "asn": "Ten sam operator sieci (ASN)", "ns": "Te same serwery DNS",
    "cert_san": "Wspólny certyfikat TLS", "tracker_id": "Ten sam identyfikator śledzący (piksel)",
    "trackers": "Ten sam identyfikator śledzący (piksel)", "tds_param": "Ten sam parametr systemu TDS",
    "tds_values": "Ta sama wartość parametru TDS", "dom_skeleton": "Podobny szkielet DOM strony",
    "asset_sha256": "Identyczny plik zasobu strony", "asset_sha256_code": "Identyczny plik kodu strony (JS/CSS)",
    "artifact_sha256": "Identyczny plik materiału (duplikat)", "favicon_mmh3": "Ta sama ikona strony (favicon)",
    "title": "Ten sam tytuł strony", "og_title": "Ten sam tytuł Open Graph", "phrase": "Wspólna fraza tekstu",
    "minhash": "Bardzo podobny tekst", "entity_phone": "Ten sam numer telefonu", "entity_iban": "Ten sam numer konta",
    "entity_email": "Ten sam adres e-mail", "person_named": "Ta sama wymieniona osoba", "face": "Ta sama twarz",
    "logo": "To samo logo", "scheme": "Ten sam typ oszustwa", "platform_ad_id": "Ten sam identyfikator reklamy",
    "pdq": "Niemal identyczny obraz (PDQ)", "phash": "Podobny obraz (pHash)", "phash_tile": "Wspólny fragment obrazu",
    "dinov2": "Wizualnie podobny obraz (DINOv2)", "kit_uuid": "Ten sam identyfikator zestawu stron (kit)",
    "landing_uuid": "Ten sam identyfikator strony docelowej", "endpoint": "Ten sam punkt końcowy API",
    "form_endpoint": "Ten sam adres wysyłki formularza", "post_endpoint": "Ten sam adres wysyłki danych",
    "param_set": "Ten sam zestaw parametrów kliknięcia", "cloak": "Ta sama technika maskowania (cloaking)",
    "linked_host": "Ten sam powiązany host (w kodzie lub linkach strony)",
    "landing_host": "Ten sam host strony docelowej", "operator_id": "Ten sam identyfikator operatora",
    "tds_value": "Ta sama wartość parametru śledzącego/TDS", "tds_signature": "To samo oprogramowanie TDS/trackera",
    "code_endpoint": "Ten sam adres API w kodzie strony", "tracking_endpoint": "Ten sam punkt śledzenia (POST)",
    "lead_endpoint": "Ten sam adres przechwytywania leadów", "post_fields": "Te same pola wysyłanych danych",
    "css_build_hash": "Ten sam build CSS", "js_build_hash": "Ten sam build JavaScript",
    "inline_script_hash": "Ten sam skrypt osadzony w stronie", "dom_token": "Wspólne klasy/identyfikatory DOM",
    "dom_shingle": "Wspólny fragment struktury DOM", "dom_skeleton_hash": "Identyczny szkielet DOM",
    "fake_comment": "Ten sam fałszywy komentarz", "page_title": "Ten sam tytuł strony",
    "entry_url": "Ten sam adres wejściowy", "host_folded": "Ta sama nazwa domeny po rozwinięciu homoglifów/IDN",
    "brand_lookalike": "Podszycie pod tę samą markę w nazwie domeny", "idn_host": "Ta sama domena IDN",
    "chat_persona": "Ta sama persona w rozmowie", "chat_url_host": "Ten sam host linkowany w rozmowie",
    "url_label_skeleton": "Ten sam szkielet nazwy domeny",
    "url_label_path_skeleton": "Ten sam szkielet domeny i ścieżki", "host_contacted": "Ten sam kontaktowany host",
    "cloak_feature": "Ta sama technika maskowania (cloaking)", "form_fields": "Te same pola formularza",
    "lead_form_kit": "Ten sam zestaw formularza leadowego",
    "registration_month": "Domeny zarejestrowane w tym samym miesiącu",
    "media_brand": "Podszywanie pod tę samą markę medialną", "url_param_set": "Ten sam zestaw parametrów URL",
    "utm_pattern": "Ten sam wzorzec parametrów UTM",
    "audio_track_reuse": "Ta sama ścieżka dźwiękowa (ponowne użycie głosu/ścieżki)",
    "asr_transcript": "Transkrypcja mowy (ASR)",
}
FAMILY_PL = {"VIS": "obraz", "TXT": "tekst", "INF": "infrastruktura", "ID": "tożsamość", "NAR": "narracja",
             "META": "metadane platformy"}


ASSET_TYPE_PL = [((".woff2", ".woff", ".ttf", ".otf", ".eot"), "Identyczny plik czcionki strony"),
                 ((".css",), "Identyczny arkusz stylów strony (CSS)"),
                 ((".js", ".mjs"), "Identyczny skrypt strony (JS)"),
                 ((".html", ".htm"), "Identyczny dokument HTML (ramka/fragment strony)"),
                 ((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif", ".bmp", ".ico"),
                  "Identyczny plik graficzny strony"),
                 ((".mp4", ".webm", ".ts", ".m3u8"), "Identyczny plik wideo strony"),
                 ((".json",), "Identyczny plik danych strony (JSON)")]


def asset_label_pl(kind: str, ev: EvidencePointer | None) -> str | None:
    """asset_sha256 explanations named by file type (from the evidence file name), not always 'plik graficzny'."""
    if not kind.startswith("asset_sha256") or ev is None:
        return None
    name = (ev.note or ev.dom_path or "").lower().split("?")[0]
    for suf in (".pobrane", ".pobierz", ".download"):
        name = name.removesuffix(suf)
    for exts, label in ASSET_TYPE_PL:
        if name.endswith(exts):
            return label
    return "Identyczny plik zasobu strony"


def kind_label_pl(kind: str) -> str:
    if kind in KIND_PL:
        return KIND_PL[kind]
    for k, v in KIND_PL.items():
        if kind.startswith(k):
            return v
    return f"Wspólna cecha „{kind}”"


def _short(v: str | None, n: int = 70) -> str:
    if v is None:
        return ""
    return v if len(v) <= n else v[: n - 1] + "…"


def _group(g: Gene) -> str:
    return g.dependency_group or f"{g.family}:{g.kind}"


TIERS = {"operator", "platform", "kit", "boilerplate", "asset", "persona", "phrase", "label"}
LABEL_KIND_PREFIX = ("cert_pl", "certpl")  # list membership: a campaign attribute, never link evidence


def gene_tier(g: Gene) -> str | None:
    """Tier from `dependency_group` = "<family>:<tier>:<name>" (INFRA_GENES.md), else None."""
    parts = (g.dependency_group or "").lower().split(":")
    if len(parts) >= 3 and parts[1] in TIERS:
        return parts[1]
    if g.kind.lower().startswith(LABEL_KIND_PREFIX):
        return "label"
    return None


def is_kit(g: Gene) -> bool:
    tier = gene_tier(g)
    if tier is not None:
        return tier in ("platform", "kit")
    grp = (g.dependency_group or "").lower()
    return grp.startswith(("kit", "template")) or g.kind in KIT_KINDS


NONE_GROUPS = ("vis_boilerplate", "vis_app_ui", "vis_brand", "id_face_boilerplate")
NONE_KINDS = {"image_class", "person_candidate", "language", "brand_mention", "commenter_name", "chat_platform",
              "document_type"}
STYLE_EXTRA_KINDS = {"entity_price", "entity_discount", "logo_strip", "registrar", "registration_month",
                     "registration_day", "cert_first_month", "cert_first_day", "cert_issuer", "capture_date",
                     "ad_start_date", "upload_date", "release_date", "upload_time"}
# Date coincidences (domain registration / first certificate / capture in the same month) and registrars are
# weak: on the sample they tied gambling incident 10 to nutra incident 14.
STYLE_SUFFIXES = ("_month", "_day", "_date", "_week")


def level(g: Gene) -> str:
    """campaign | kit | asset | none — how a matching gene counts (see module docstring).

    Stream rules (docs/genes/VIS.md, TXT.md): VIS app-UI / boilerplate / brand groups and image classes count 0;
    VIS image units, SigLIP, faces and named personas are asset level; `persona_renamed` is campaign level
    (one face under several names); `person_candidate` (SFace 0.50–0.60) counts 0; NAR genes are style kit."""
    grp = (g.dependency_group or "").lower()
    if grp.startswith(NONE_GROUPS) or g.kind in NONE_KINDS:
        return "none"
    if g.kind == "artifact_sha256" or grp.endswith("duplicate_file"):
        return "campaign"  # identical original file (re-posted creative, copied page): may link on its own
    if g.kind == "persona_renamed":
        return "campaign"
    if grp.startswith(("vis_unit:", "vis_txt_semantic", "id_person:")) or g.kind in ("face", "person_named"):
        return "asset"
    if grp.startswith("vis_template") or str(g.family) == "NAR" or g.kind in STYLE_EXTRA_KINDS:
        return "kit"
    tier = gene_tier(g)
    if tier in ("boilerplate", "label"):
        return "none"
    if tier in ("platform", "kit"):
        return "kit"
    if tier in ("asset", "persona"):
        return "asset"
    if tier in ("operator", "phrase"):
        return "campaign"
    if is_kit(g):
        return "kit"
    if str(g.family) in ASSET_FAMILIES or g.kind.startswith(ASSET_KINDS_PREFIX):
        return "asset"
    return "campaign"


FACE_MIN_COS = 0.60
IMAGE_NEAR_DUP_COS = 0.95
WEAK_VECTOR_BITS = 2.0


def vector_rule(kind: str, model: str) -> str:
    k, m = kind.lower(), model.lower()
    if k == "face" or "sface" in m:
        return "face"
    if any(x in k or x in m for x in ("dinov2", "siglip", "clip")):
        return "image"
    if k.startswith(("emb", "charsig", "text", "e5")) or "e5" in m or "mmlw" in m:
        return "text"
    return "other"


def near_hash_spec(kind: str) -> tuple[int, int] | None:
    for prefix, spec in NEAR_HASH.items():
        if kind == prefix or kind.startswith(prefix + "_") or kind.startswith(prefix):
            return spec
    return None


NOTE_ROWS = 3  # 0-bit explanation rows kept per pair
LEVEL_RANK = {"none": 0, "kit": 1, "asset": 2, "campaign": 3}  # evidential strength, most conservative first
QUALITY_FULL = 0.5  # observations with prior quality >= this count fully; below it bits scale linearly (CR-ASTRA-3)
QUALITY_MIN = 0.25  # below: the observation is not evidence at all (review M3)
SCHEME_MIN_QUALITY = 0.5  # a scheme guessed below this quality cannot contradict (review M4)
UNKNOWN_SCHEMES = {"other", "unknown", "none", ""}
SCHEME_PL = {"investment": "inwestycje", "health_supplement": "suplementy i zdrowie", "gambling": "hazard",
             "fake_news_bait": "fałszywy artykuł prasowy", "giveaway": "fałszywa loteria / rozdawnictwo",
             "marketplace_phishing": "phishing na platformie ogłoszeniowej", "parcel_fee": "dopłata do przesyłki",
             "gov_refund": "fałszywy zwrot od instytucji", "bank_impersonation": "podszywanie się pod bank",
             "fake_shop": "fałszywy sklep", "account_takeover": "przejęcie konta", "loan": "fałszywa pożyczka",
             "fake_charity": "fałszywa zbiórka", "other": "nieokreślony"}
TIER_PL = {"operator": "operator", "phrase": "fraza", "platform": "platforma", "kit": "zestaw narzędzi",
           "boilerplate": "element wspólny (boilerplate)", "asset": "zasób", "persona": "persona",
           "label": "etykieta listy"}


# Account identities: one value = one advertiser/operator account (review M1). Their df counts known campaigns, so
# the k members of a campaign are one observation. Hosts, endpoints and asset hashes are infrastructure that sibling
# campaigns of one kit share; they keep incident-level df (sample: the umbrella {1,2,3} ~ {48,61} linked hosts).
# `operator_id`, kit and landing uuids are excluded: on the sample the umbrella campaigns {1,2,3} and {48,61} share an
# operator_id of their common kit, and campaign units sent held-out {1,2,3} to the sibling at 19.7 bits.
# The landing domain the operator registered (`landing_host`, `registrable_domain`) is an account too (CR-E2E-4: a
# URL-only input on the exclusive landing host of a 4-member campaign got 6.0 bits and became a new pattern).
ACCOUNT_KIND_PREFIXES = ("tracker", "pixel", "platform_ad_id", "platform_page_id", "ad_account", "ad_id", "page_id",
                         "entity_phone", "entity_iban", "entity_email", "gtm", "ga_", "landing_host",
                         "registrable_domain")


def is_account_kind(kind: str) -> bool:
    return kind.startswith(ACCOUNT_KIND_PREFIXES)


HOST_KINDS = {"host", "landing_host", "registrable_domain", "regdom", "domain", "host_contacted", "linked_host",
              "canonical_host", "lead_endpoint_host"}


TEXT_DOMAIN_KINDS = {"domain_in_text", "url_in_text"}  # the same domain read by OCR / from page text


@lru_cache(maxsize=65536)
def _registrable(host: str) -> str:
    h = host.strip().lower()
    h = h.split("://", 1)[-1].rstrip(".").split("/")[0].split("?")[0].split(":")[0]
    try:
        from hydra.enrich.domains import registrable_domain

        rd = registrable_domain(h)
    except Exception:  # noqa: BLE001 — offline PSL missing: last two labels
        rd = None
    return rd or ".".join(h.split(".")[-2:])


def domain_group(g: Gene, lvl: str) -> str | None:
    """One dependency group per registrable domain for campaign-level host genes (review M7): the same landing domain
    seen as landing host, contacted host, linked host, or read from the page text / OCR (`domain_in_text`,
    `url_in_text`) is one observation, not several summed identities or a second family."""
    if lvl != "campaign" or not g.value:
        return None
    if not ((str(g.family) == "INF" and g.kind in HOST_KINDS) or g.kind in TEXT_DOMAIN_KINDS):
        return None
    return f"INF:operator:domain:{_registrable(g.value)}"


def scheme_pl(value: str) -> str:
    return SCHEME_PL.get(value, value)


def quality_weight(*genes: Gene) -> float:
    """Reliability weight of a match: the weaker observation decides; q >= QUALITY_FULL counts fully, q = 0 gives
    0 bits. Extractors set q as a prior reliability (OCR confidence, detector score, warninglist demotion)."""
    q = min((float(g.quality) for g in genes), default=1.0)
    if q < QUALITY_MIN:
        return 0.0
    return max(0.0, min(1.0, q / QUALITY_FULL))


def _gene_sort_key(g: Gene) -> tuple:
    ev = g.evidence
    return (g.dependency_group or "", -float(g.quality), g.extractor or "", ev.artifact_id or "", ev.note or "",
            str(ev.char_range or ""), str(ev.bbox or ""))


@dataclass
class Reconciled:
    """One (kind, value) shared by both incidents, reconciled over every observation of it on both sides."""

    ga: Gene
    gb: Gene
    level: str
    tiers: tuple[str, ...]  # distinct annotated tiers (or levels) when the observations disagree, else ()


def reconcile(obs_a: list[Gene], obs_b: list[Gene]) -> Reconciled:
    """Deterministic tier reconciliation (CR-ASTRA-4): when observations of the same value carry different tiers
    (operator vs boilerplate from two extractors), the most conservative level wins on both sides, whatever the
    input or extractor completion order. Representatives at that level are ordered by dependency group, then quality,
    then stable evidence keys, so evidence pointers do not depend on order either."""
    both = [*obs_a, *obs_b]
    levels = {id(g): level(g) for g in both}
    lvl = min(levels.values(), key=lambda x: LEVEL_RANK[x])
    pick_a = min((g for g in obs_a if levels[id(g)] == lvl), key=_gene_sort_key, default=None)
    pick_b = min((g for g in obs_b if levels[id(g)] == lvl), key=_gene_sort_key, default=None)
    ga = pick_a or min(obs_a, key=_gene_sort_key)
    gb = pick_b or min(obs_b, key=_gene_sort_key)
    tiers = sorted({gene_tier(g) or levels[id(g)] for g in both})
    return Reconciled(ga, gb, lvl, tuple(tiers) if len({levels[id(g)] for g in both}) > 1 else ())


# --------------------------------------------------------------------------------------- genome


@dataclass
class IncidentGenome:
    incident_id: str
    genes: list[Gene]
    discrete: dict[tuple[str, str], list[Gene]] = field(default_factory=dict)
    near: dict[str, tuple[np.ndarray, list[Gene]]] = field(default_factory=dict)       # kind → uint8 bits
    vectors: dict[tuple[str, str], tuple[np.ndarray, list[Gene]]] = field(default_factory=dict)
    families: set[str] = field(default_factory=set)  # families with ≥ 1 observed gene
    units: dict[str, dict] = field(default_factory=dict)      # VIS unit → class, phash, pdq_canon, dinov2, gene
    minhash: dict[str, tuple[np.ndarray, int, Gene]] = field(default_factory=dict)  # channel → (sig, shingles)
    audio: list[tuple[np.ndarray, int, Gene, list[str]]] = field(default_factory=list)  # (sig, hashes, gene, bands)
    label: str | None = None
    title: str | None = None
    observed_at: str | None = None

    @property
    def schemes(self) -> set[str]:
        """Detected scheme types; `other` (no lexicon scheme reached the threshold) is unknown, not a scheme, and a
        scheme observed below SCHEME_MIN_QUALITY is a guess, so neither can contradict (review M4)."""
        return {v for (k, v), gs in self.discrete.items() if k in CONTRADICTION_KINDS and v not in UNKNOWN_SCHEMES
                and max(float(g.quality) for g in gs) >= SCHEME_MIN_QUALITY}


_CLASS_RE = re.compile(r"class=([a-z_]+)")
_SHINGLES_RE = re.compile(r"shingles=(\d+)")


def _hex_bits(value: str, nbits: int) -> np.ndarray | None:
    try:
        raw = bytes.fromhex(value)
    except ValueError:
        return None
    if len(raw) * 8 != nbits:
        return None
    return np.frombuffer(raw, dtype=np.uint8)


def build_genome(incident_id: str, genes: list[Gene], vectors: dict[str, tuple[str, np.ndarray]],
                 label: str | None = None, title: str | None = None, observed_at: str | None = None
                 ) -> IncidentGenome:
    """`vectors`: vector_ref → (model, 1-D float32 L2-normalised)."""
    gen = IncidentGenome(incident_id, genes, label=label, title=title, observed_at=observed_at)
    near_rows: dict[str, list[tuple[np.ndarray, Gene]]] = defaultdict(list)
    vec_rows: dict[tuple[str, str], list[tuple[np.ndarray, Gene]]] = defaultdict(list)
    for g in genes:
        if not usable(g):
            continue
        gen.families.add(str(g.family))
        grp = g.dependency_group or ""
        unit = gen.units.setdefault(grp[len("vis_unit:"):], {}) if grp.startswith("vis_unit:") else None
        if unit is not None and "class" not in unit:
            m = _CLASS_RE.search(g.evidence.note or "")
            unit["class"] = m.group(1) if m else None
        if g.vector_ref is not None:
            v = vectors.get(g.vector_ref)
            if v is not None:
                vec_rows[(g.kind, v[0])].append((v[1], g))
                if unit is not None and g.kind == "dinov2":
                    unit["dinov2"] = v[1]
                    unit.setdefault("gene", g)
            continue
        if g.value is None:
            continue
        gen.discrete.setdefault((g.kind, g.value), []).append(g)
        if unit is not None and g.kind in ("phash", "pdq_canon", "pdq"):
            arr = _hex_bits(g.value, 64 if g.kind == "phash" else 256)
            if arr is not None:
                unit[g.kind] = arr
                unit.setdefault("values", {})[g.kind] = g.value
                unit.setdefault("gene", g)
        if unit is not None and g.kind == "phash_multicrop":
            parts = [_hex_bits(x, 64) for x in g.value.split(",")]
            if parts and all(x is not None for x in parts):
                unit["multicrop"] = np.stack(parts)  # [windows, 8], `vis.multicrop_windows` order
        if unit is not None and g.kind == "pdq_dihedral":
            parts = [_hex_bits(x, 256) for x in g.value.split(",")]
            if len(parts) == 8 and all(x is not None for x in parts):
                unit["pdq_dihedral"] = np.stack(parts)  # [8, 32]: original orientation first
                unit.setdefault("gene", g)
        if g.kind == "minhash_sig" and ":" in g.value:
            ch, hexsig = g.value.split(":", 1)
            try:
                sig = np.frombuffer(bytes.fromhex(hexsig), dtype=">u4").astype(np.uint32)
            except ValueError:
                sig = None
            m = _SHINGLES_RE.search(g.evidence.note or "")
            if sig is not None and len(sig):
                gen.minhash[ch] = (sig, int(m.group(1)) if m else 0, g)
        if g.kind == "audio_fp_sig":
            from hydra.genome.aud import lsh_bands, parse_sig

            parsed = parse_sig(g.value)
            if parsed is not None:
                gen.audio.append((parsed[1], parsed[0], g, lsh_bands(parsed[1])))
        spec = near_hash_spec(g.kind)
        if spec is not None:
            arr = _hex_bits(g.value, spec[0])
            if arr is not None:
                near_rows[g.kind].append((arr, g))
    for kind, rows in near_rows.items():
        gen.near[kind] = (np.stack([r[0] for r in rows]), [r[1] for r in rows])
    for key, rows in vec_rows.items():
        gen.vectors[key] = (np.stack([r[0] for r in rows]).astype(np.float32), [r[1] for r in rows])
    return gen


# --------------------------------------------------------------------------------------- scoring


@dataclass
class PairEval:
    score: PairScore
    campaign_bits: float
    kit_bits: float
    asset_bits: float
    campaign_by_family: dict[str, float] = field(default_factory=dict)
    operator_bits: float = 0.0
    support: float = 0.0  # bits behind a same-campaign decision (batch and open-set use the same number)
    robustness: dict | None = None  # leave-one-family-out result (`robustness()`), set for linked pairs
    kit_groups: int = 0          # independent identity-kit dependency groups behind kit_bits (style excluded)
    kit_min_df: int | None = None  # rarest identity-kit value of the pair (incidents holding it)


POPCOUNT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint16)


def hamming_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Pairwise Hamming distances between rows of packed uint8 arrays (a: [n, B], b: [m, B])."""
    return POPCOUNT[np.bitwise_xor(a[:, None, :], b[None, :, :])].sum(axis=2)


def _hamming_min(a: np.ndarray, b: np.ndarray) -> tuple[int, int, int]:
    """Minimum Hamming distance between rows of packed uint8 arrays → (dist, i, j)."""
    best = (10**9, -1, -1)
    for s in range(0, len(a), 64):
        d = hamming_matrix(a[s : s + 64], b)
        i, j = np.unravel_index(int(d.argmin()), d.shape)
        if d[i, j] < best[0]:
            best = (int(d[i, j]), s + int(i), int(j))
    return best


SHARED_TEXT_KINDS = {"phrase", "ocr_line", "library_ad_text", "headline", "ad_headline", "lead", "page_title"}


def _longest_shared_text(shared: list[tuple[str, str]], recon: dict, rarity: Rarity, n: int) -> GeneMatch | None:
    """ADOPT #8: the longest verbatim text both incidents share (readable evidence next to a similarity number),
    with its corpus document frequency. 0 bits: it explains TXT evidence, it is not extra evidence."""
    best = None
    for kind, value in shared:
        rc = recon.get((kind, value))
        if kind not in SHARED_TEXT_KINDS or rc is None or rc.level == "none" or is_stop(kind, value):
            continue
        text = (rc.ga.evidence.note or value) if kind != "phrase" else value
        cand = (len(value), value, kind, text, rc)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None:
        return None
    length, value, kind, text, rc = best
    df = rarity.df(kind, value)
    return GeneMatch(family=Family.TXT, kind="longest_shared_text", value_a=_short(text, 200),
                     value_b=_short(rc.gb.evidence.note or value, 200) if kind != "phrase" else _short(value, 200),
                     bits=0.0, doc_freq=df, provenance=Provenance.INF, evidence_a=rc.ga.evidence,
                     evidence_b=rc.gb.evidence,
                     explanation_pl=(f"Najdłuższy wspólny fragment tekstu: „{_short(text, 160)}” ({length} znaków po"
                                     f" normalizacji; występuje w {df} z {n} incydentów korpusu) (+0.0 bit)"))


def _quality_note(weight: float, raw: float | None, ga: Gene | None, gb: Gene | None) -> str:
    if weight >= 1.0 or raw is None:
        return ""
    q = min(float(g.quality) for g in (ga, gb) if g is not None)
    return f"; niska jakość obserwacji q={num_pl(q, '.2f')}: {num_pl(raw)} bit × {num_pl(weight, '.2f')}"


def _match(fam: str, kind: str, va: str | None, vb: str | None, bits: float, *, sim: float | None = None,
           df: int | None = None, ga: Gene | None = None, gb: Gene | None = None, n: int = 0,
           expl: str | None = None, weight: float = 1.0, raw: float | None = None) -> GeneMatch:
    if expl is None:
        what = asset_label_pl(kind, ga.evidence if ga else None) or kind_label_pl(kind)
        shown = scheme_pl(va) if kind in CONTRADICTION_KINDS and va is not None else _short(va)
        qn = _quality_note(weight, raw, ga, gb)
        if df is not None:
            expl = (f"{what}: „{shown}” — u obu incydentów; występuje w {df} z {n} incydentów korpusu"
                    f"{qn} ({num_pl(bits, '+.1f')} bit)")
        else:
            expl = f"{what} — podobieństwo {num_pl(sim, '.3f')}{qn} ({num_pl(bits, '+.1f')} bit)"
    return GeneMatch(family=fam, kind=kind, value_a=_short(va, 200), value_b=_short(vb, 200), similarity=sim,
                     doc_freq=df, bits=round(bits, 3), provenance=Provenance.INF,
                     evidence_a=ga.evidence if ga else None, evidence_b=gb.evidence if gb else None,
                     explanation_pl=expl)


def score_pair(a: IncidentGenome, b: IncidentGenome, rarity: Rarity, th: Thresholds | None = None,
               candidate_sources: list[str] | None = None, drop: frozenset[str] = frozenset()) -> PairEval:
    """`drop`: families whose evidence is ignored (leave-one-family-out robustness).

    The pair is put in canonical order (a < b) BEFORE scoring, so every side-dependent field (value_a/b,
    evidence_a/b with bbox, char_range, har_entry, timecode) belongs to `PairScore.a` / `.b` (audit A01)."""
    if b.incident_id < a.incident_id:
        a, b = b, a
    th = th or Thresholds()
    pair = (a.incident_id, b.incident_id)
    n = rarity.n_incidents
    # group → list of (bits, GeneMatch, level, family)
    groups: dict[str, list[tuple[float, GeneMatch, str, str]]] = defaultdict(list)

    shared = sorted(a.discrete.keys() & b.discrete.keys())  # sorted: set order varies with PYTHONHASHSEED
    notes: list[GeneMatch] = []  # 0-bit explanation rows (tier conflicts), listed after the evidence
    recon: dict[tuple[str, str], Reconciled] = {}
    for kind, value in shared:
        rc = recon[(kind, value)] = reconcile(a.discrete[(kind, value)], b.discrete[(kind, value)])
        if near_hash_spec(kind) is not None:
            continue  # handled by the Hamming path below (exact match = distance 0)
        ga, gb, lvl = rc.ga, rc.gb, rc.level
        if rc.tiers:
            notes.append(GeneMatch(
                family=ga.family, kind="tier_conflict", value_a=_short(value, 200), value_b=_short(value, 200),
                bits=0.0, provenance=Provenance.INF, evidence_a=ga.evidence, evidence_b=gb.evidence,
                explanation_pl=(f"{kind_label_pl(kind)}: „{_short(value)}” ma sprzeczne oznaczenia poziomu"
                                f" ({', '.join(TIER_PL.get(t, t) for t in rc.tiers)}); przyjęto najostrożniejszy"
                                f" poziom „{lvl}” (+0.0 bit)")))
        if lvl == "none":
            continue
        operator = lvl == "campaign" and gene_tier(ga) == "operator" and is_account_kind(kind)
        bits, df = rarity.discrete_bits(kind, value, pair, kit=(lvl == "kit"), operator=operator)
        w = quality_weight(ga, gb)
        if bits * w < MIN_BITS:
            continue
        m = _match(str(ga.family), kind, value, value, bits * w, df=df, ga=ga, gb=gb, n=n, weight=w, raw=bits)
        groups[domain_group(ga, lvl) or _group(ga)].append((bits * w, m, lvl, str(ga.family)))

    for kind in sorted(a.near.keys() & b.near.keys()):
        nbits, max_d = near_hash_spec(kind) or (64, 0)
        (xa, gas), (xb, gbs) = a.near[kind], b.near[kind]
        d, i, j = _hamming_min(xa, xb)
        if d > max_d:
            continue
        ga, gb = gas[i], gbs[j]
        if is_stop(kind, ga.value) or level(ga) == "none" or level(gb) == "none":
            continue
        # frequency of near-identical hashes (CR-ASTRA-8), not of the exact value: re-encoded stock variants differ
        df = max(rarity.near_df(kind, ga.value or "", max_d), rarity.near_df(kind, gb.value or "", max_d))
        top = min(GROUP_CAP, math.log2(rarity.m / kind_u0(kind)))
        bits = top * (1.0 - d / (max_d + 1.0))
        if df > 2 and n > 2:  # a near-hash shared by many incidents is a template image
            bits *= max(0.0, 1.0 - (df - 2) / max(1.0, n - 2))
        w = quality_weight(ga, gb)
        raw, bits = bits, bits * w
        if bits < MIN_BITS:
            continue
        lvl, grp = level(ga), _group(ga)
        if image_hash_campaign(kind, d, df, ga, gb, lvl):
            lvl, grp = "campaign", f"{grp}{IMAGE_HASH_SUFFIX}"
        expl = (f"{kind_label_pl(kind)}: odległość Hamminga {d}/{nbits} (próg {max_d}); obrazów niemal identycznych"
                f" w korpusie: {df}{_quality_note(w, raw, ga, gb)}"
                + ("; ten sam rzadki obraz: dowód kampanii (rodzina obrazu, limit 8 bit, wymaga drugiej rodziny)"
                   if lvl == "campaign" else "") + f" ({num_pl(bits, '+.1f')} bit)")
        m = _match(str(ga.family), kind, ga.value, gb.value, bits, sim=1.0 - d / nbits, ga=ga, gb=gb, expl=expl)
        groups[grp].append((bits, m, lvl, str(ga.family)))

    for key in sorted(a.vectors.keys() & b.vectors.keys()):
        kind, model = key
        (xa, gas), (xb, gbs) = a.vectors[key], b.vectors[key]
        ma = np.array([level(x) != "none" for x in gas])
        mb = np.array([level(x) != "none" for x in gbs])
        if not ma.any() or not mb.any():
            continue
        sims = xa @ xb.T
        sims[~ma, :] = -2.0
        sims[:, ~mb] = -2.0
        i, j = np.unravel_index(int(sims.argmax()), sims.shape)
        s = float(sims[i, j])
        n_cmp = int(ma.sum() * mb.sum())
        rule = vector_rule(kind, model)
        if rule == "face" and s < FACE_MIN_COS:
            continue  # SFace: strong only at >= 0.60, never chain through 0.50-0.60 (VIS.md)
        bits = rarity.vector_bits(kind, model, s, comparisons=n_cmp)
        if rule == "image" and s < IMAGE_NEAR_DUP_COS:
            bits = min(bits, WEAK_VECTOR_BITS)  # background-normalised similarity: weak evidence at most
        elif rule == "text":
            bits = min(bits, WEAK_VECTOR_BITS)  # text embeddings are evidence, never a linking rule
        elif rule == "other":
            bits = min(bits, 4.0)
        ga, gb = gas[i], gbs[j]
        w = quality_weight(ga, gb)
        raw, bits = bits, bits * w
        if bits < MIN_BITS:
            continue
        expl = (f"{kind_label_pl(kind)}: najlepsze dopasowanie cos={num_pl(s, '.3f')} spośród {sims.size} porównań,"
                f" na tle rozkładu par niepowiązanych{_quality_note(w, raw, ga, gb)} ({num_pl(bits, '+.1f')} bit)")
        m = _match(str(ga.family), kind, ga.evidence.artifact_id, gb.evidence.artifact_id, bits, sim=s,
                   ga=ga, gb=gb, expl=expl)
        groups[_group(ga)].append((bits, m, level(ga), str(ga.family)))

    rare_items: dict[str, int] = defaultdict(int)
    rare_sources: set[str | None] = set()
    rare_contacts: list[tuple[str, str]] = []
    rare_text = 0  # rare TXT items that are not contact identities
    for (kind, value) in shared:
        rc = recon[(kind, value)]
        ga = rc.ga
        if (rc.level == "campaign" and rarity.df(kind, value) <= 2 and str(ga.family) == "TXT"
                and quality_weight(rc.ga, rc.gb) >= 1.0 and not is_stop(kind, value)
                and not is_generic_text(kind, value)):
            rare_items["TXT"] += 1
            rare_sources.add(ga.evidence.artifact_id)
            if kind in CONTACT_KINDS:
                rare_contacts.append((kind, value))
            else:
                rare_text += 1
    if len(rare_sources) < 2:  # rare phrases must come from >= 2 distinct artifacts (one OCR'd image is one item)
        rare_items["TXT"] = min(rare_items["TXT"], RARE_MIN_ITEMS - 1)
    contact_corroborated = (bool(rare_contacts) and rare_text >= CONTACT_MIN_RARE_TEXT
                            and rare_items["TXT"] >= RARE_MIN_ITEMS and "TXT" not in drop)
    _text_near_duplicate(a, b, groups, rarity, notes)
    from hydra.crossmodal import cross_modal_text  # screenshot OCR vs saved page text (CR-OPGENES-12, E-XM-1)

    if (m_xm := cross_modal_text(a, b, rarity)) is not None:
        groups["TXT:asset:near_duplicate"].append((m_xm.bits, m_xm, "campaign", "TXT"))
    vis_multi = _visual_near_duplicate(a, b, groups, rarity)
    from hydra.vis_geom import SKIPPED_GROUP  # CR-ASTRA-SW-01: budget-skipped geometry checks carry no bits

    geom_skipped = groups.pop(SKIPPED_GROUP) if SKIPPED_GROUP in groups and "VIS" not in drop else []
    _audio_reuse(a, b, groups, rarity)
    _video_segment(a, b, groups)
    if vis_multi >= RARE_MIN_ITEMS:
        rare_items["VIS"] = vis_multi
    if drop:
        groups = defaultdict(list, {k: kept for k, v in groups.items() if (kept := [t for t in v if t[3] not in drop])})
        notes = [x for x in notes if str(x.family) not in drop]
    _settle_partial_audio(groups, notes)
    _settle_template_text(groups, notes)
    family_bits: dict[str, float] = defaultdict(float)
    campaign_bits = kit_bits = asset_bits = style_bits = dup_bits = 0.0
    kit_groups, kit_min_df = 0, None
    contact_bits = 0.0
    image_hash_groups = vis_other_groups = 0
    camp_by_family: dict[str, float] = {}
    camp_groups: dict[str, list[float]] = defaultdict(list)
    reasons: list[GeneMatch] = []
    pos_families_campaign: set[str] = set()
    for _grp, items in groups.items():
        items.sort(key=lambda t: -t[0])
        gbits = min(GROUP_CAP, items[0][0] + REST_WEIGHT * sum(t[0] for t in items[1:]))
        fam = items[0][3]
        lvl = items[0][2]
        family_bits[fam] += gbits
        if lvl == "kit":
            if all(is_style_match(t[1]) for t in items):
                style_bits += gbits
            else:
                kit_bits += gbits
                kit_groups += 1
                dfs = [t[1].doc_freq for t in items if t[1].doc_freq is not None]
                if dfs:
                    kit_min_df = min(dfs) if kit_min_df is None else min(kit_min_df, min(dfs))
        elif lvl == "asset":
            asset_bits += gbits
        elif (_grp.lower().endswith("duplicate_file") or items[0][1].kind == "artifact_sha256"
              or items[0][1].kind in NEARDUP_KINDS):
            dup_bits += gbits  # identical original file: may link on its own
            pos_families_campaign.add(fam)
        else:
            camp_groups[fam].append(gbits)
            pos_families_campaign.add(fam)
            if _grp.endswith(IMAGE_HASH_SUFFIX):
                image_hash_groups += 1
            elif fam == "VIS":
                vis_other_groups += 1
            if fam == "TXT" and items[0][1].kind in CONTACT_KINDS:
                contact_bits = max(contact_bits, gbits)
        reasons.extend(t[1] for t in items)
    for f, gl in camp_groups.items():
        gl.sort(reverse=True)
        # INF operator genes are independent identities (sum); in other families the groups are correlated
        # views of the same content (page text / OCR / MinHash; one image unit per group): max + 0.5 x second
        v = sum(gl) if f in SUM_FAMILIES else gl[0] + 0.5 * (gl[1] if len(gl) > 1 else 0.0)
        cap = FAMILY_CAMPAIGN_CAP.get(f, FAMILY_CAP)
        if rare_items.get(f, 0) >= RARE_MIN_ITEMS:
            cap = max(cap, RAISED_CAP)
        camp_by_family[f] = min(cap, v)
    campaign_bits = dup_bits + sum(camp_by_family.values())
    operator_bits = dup_bits + sum(v for f, v in camp_by_family.items() if f in SUM_FAMILIES)
    if contact_corroborated and contact_bits > 0:
        # CR-ASTRA-7: a rare, normalised contact identity (phone, IBAN, e-mail) is how the operator is reached or
        # paid, so it is operator evidence, but only when >= CONTACT_MIN_RARE_TEXT other rare text items from another
        # artifact corroborate it. It adds no bits: it only lets the TXT campaign bits (cap raised by the rare items)
        # satisfy the operator rule. One phrase, or a contact held by more than two incidents, never qualifies.
        contact_op = min(contact_bits, camp_by_family.get("TXT", 0.0))
        operator_bits += contact_op
        kinds = ", ".join(sorted({kind_label_pl(k).lower() for k, _v in rare_contacts}))
        notes.insert(0, GeneMatch(
            family=Family.TXT, kind="contact_operator", bits=0.0, provenance=Provenance.INF,
            value_a=_short(rare_contacts[0][1], 200), value_b=_short(rare_contacts[0][1], 200),
            explanation_pl=(f"Rzadki kontakt operatora ({kinds}) potwierdzony {rare_text} rzadkimi frazami tekstu z"
                            f" innego źródła: liczony jako dowód operatora ({num_pl(contact_op)} bit z puli tekstu, bez"
                            f" dodatkowych bitów) (+0.0 bit)")))
    strong_families = sum(1 for v in camp_by_family.values() if v >= STRONG_FAMILY_BITS)
    asset_bits = min(ASSET_CAP, asset_bits)
    for fam in list(family_bits):
        family_bits[fam] = round(min(FAMILY_CAP, family_bits[fam]), 3)

    if family_bits.get("TXT", 0.0) > 0 and "TXT" not in drop:
        lsp = _longest_shared_text(shared, recon, rarity, n)
        if lsp is not None:
            notes.insert(0, lsp)

    # VIS campaign bits that come only from shared rare image hashes: a shared asset unless another family joins
    image_only_vis = camp_by_family.get("VIS", 0.0) if image_hash_groups and not vis_other_groups else 0.0
    contradictions: list[GeneMatch] = []
    sa, sb = a.schemes, b.schemes
    # CR-AQ-1 (B): the same rare image outweighs a scheme guessed from little text, so no contradiction then
    if (sa and sb and not (sa & sb) and campaign_bits < th.link and "NAR" not in drop
            and image_only_vis < IMAGE_LED_BITS and camp_by_family.get("VIS", 0.0) < IMAGE_LED_BITS):
        contradictions.append(GeneMatch(
            family=Family.NAR, kind="scheme", value_a=",".join(sorted(sa)), value_b=",".join(sorted(sb)),
            bits=CONTRADICTION_BITS, provenance=Provenance.INF,
            explanation_pl=(f"Sprzeczność: różny typ oszustwa ({schemes_pl(sa)} vs"
                            f" {schemes_pl(sb)})"
                            f" i brak wspólnej rzadkiej cechy kampanii ({num_pl(CONTRADICTION_BITS, '+.1f')} bit)")))

    all_fams = {f.value for f in Family}
    missing = sorted(f for f in all_fams if f not in a.families or f not in b.families)
    # Style/NAR kit evidence (DOM tokens, class vocabularies, urgency/persona motifs) is shown in the family bits but no
    # longer counts towards kit_bits: a group link needs identity-kit evidence (EVAL 2026-11-21: one 6.87-bit bridge =
    # asset_name_token 3.87 + 3.0 style bits dropped sample+syn group F1 0.952 -> 0.846). STYLE_CAP stays for display.
    style_kit = min(STYLE_CAP, style_bits)
    # Kit/platform bits count only towards campaign-GROUP links, never towards a campaign link, not even capped
    # (architect 2026-11-21: nask-1 vs nask-61, two aio accounts, reached 10.5 bits on a capped kit residual).
    contra = sum(c.bits for c in contradictions)
    total = campaign_bits + asset_bits + contra
    # Same campaign: campaign-level bits (asset-level counts as support once some campaign evidence exists)
    support = campaign_bits + (0.5 * asset_bits if campaign_bits > 0 else 0.0)
    correction = total - contra - sum(min(FAMILY_CAP, v) for v in family_bits.values())
    if correction < -1e-6:  # family_bits show raw pooled evidence; this row reconciles them with total_bits
        reasons.append(GeneMatch(
            family=Family.INF, kind="cap_correction", bits=round(correction, 3), provenance=Provenance.INF,
            explanation_pl=(f"Limit dla dowodów platformy/zestawu narzędzi i limitów rodzin cech"
                            f" ({num_pl(correction, '+.1f')} bit; zestaw narzędzi {num_pl(kit_bits)} bit liczy się"
                            f" tylko do"
                            f" grup"
                            f" kampanii; cechy stylu/szablonu {num_pl(style_kit)} bit nie łączą grup)")))
    if contradictions:
        hyp = Hypothesis.ABSTAIN if total >= th.low else Hypothesis.INDEPENDENT
    elif support >= th.link and (operator_bits >= OPERATOR_STRONG_BITS or strong_families >= 2):
        hyp = Hypothesis.SAME_CAMPAIGN
    elif (asset_bits + image_only_vis >= th.asset and campaign_bits - image_only_vis < th.low
          and asset_bits + image_only_vis >= kit_bits):
        hyp = Hypothesis.SHARED_ASSET
    elif kit_bits >= th.kit:
        hyp = Hypothesis.SHARED_TEMPLATE
    elif total >= th.low:
        hyp = Hypothesis.ABSTAIN
    else:
        hyp = Hypothesis.INDEPENDENT
    if geom_skipped:  # a required check did not run: incomplete, never a silent `independent`
        notes.insert(0, geom_skipped[0][1])
        if hyp == Hypothesis.INDEPENDENT:
            hyp = Hypothesis.ABSTAIN
    reasons.sort(key=lambda m: (-abs(m.bits), str(m.family), m.kind, m.value_a or "", m.value_b or ""))
    prob = 1.0 / (1.0 + 2.0 ** (-(support - th.link)))
    a_id, b_id = sorted(pair)
    ps = PairScore(
        a=a_id, b=b_id, family_bits=dict(family_bits), missing_families=missing, contradictions=contradictions,
        reasons=[r for r in reasons if r.kind != "cap_correction"][: th.max_reasons]
        + [r for r in reasons if r.kind == "cap_correction"] + notes[:NOTE_ROWS], total_bits=round(total, 3),
        probability=round(prob, 4),
        calibrated=False, hypothesis=hyp, candidate_sources=sorted(candidate_sources or []), model=MODEL_NAME,
        meta={"geometry_incomplete": len(geom_skipped)} if geom_skipped else {},
    )
    return PairEval(ps, round(campaign_bits, 3), round(kit_bits, 3), round(asset_bits, 3),
                    {f: round(v, 3) for f, v in camp_by_family.items()}, round(operator_bits, 3),
                    round(support + sum(c.bits for c in contradictions), 3), kit_groups=kit_groups,
                    kit_min_df=kit_min_df)


# --------------------------------------------------------------------------------------- supervised path

FAMILIES = [f.value for f in Family]


def pair_features(pe: PairEval) -> list[float]:
    s = pe.score
    feats = [s.family_bits.get(f, 0.0) for f in FAMILIES]
    feats += [1.0 if f in s.missing_families else 0.0 for f in FAMILIES]
    feats += [pe.campaign_bits, pe.kit_bits, pe.asset_bits, float(len(s.contradictions))]
    return feats


@dataclass
class FusionModel:
    """Logistic regression on per-family bits + missingness, isotonic-calibrated (`lr_isotonic@1`)."""

    min_pairs: int = 30
    min_per_class: int = 5
    _lr: object | None = None
    _iso: object | None = None
    name: str = "lr_isotonic@1"
    n_pairs: int = 0

    def can_fit(self, labels: list[int]) -> bool:
        pos = sum(labels)
        return len(labels) >= self.min_pairs and pos >= self.min_per_class and len(labels) - pos >= self.min_per_class

    def fit(self, pairs: list[PairEval], labels: list[int]) -> bool:
        if not self.can_fit(labels):
            return False
        from sklearn.isotonic import IsotonicRegression
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import StratifiedKFold, cross_val_predict

        x = np.array([pair_features(p) for p in pairs])
        y = np.array(labels)
        lr = LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced")
        # isotonic calibration on OUT-OF-FOLD predictions (audit §6): never on scores of the pairs it was fitted on
        folds = max(2, min(5, int(min(y.sum(), len(y) - y.sum()))))
        raw = cross_val_predict(lr, x, y, cv=StratifiedKFold(folds, shuffle=True, random_state=0),
                                method="predict_proba")[:, 1]
        lr.fit(x, y)
        self._iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(raw, y)
        self._lr = lr
        self.n_pairs = len(labels)
        return True

    def predict(self, pe: PairEval) -> float:
        assert self._lr is not None and self._iso is not None
        raw = self._lr.predict_proba(np.array([pair_features(pe)]))[:, 1]  # type: ignore[attr-defined]
        return float(self._iso.predict(raw)[0])  # type: ignore[attr-defined]

    def apply(self, pe: PairEval, th: Thresholds | None = None) -> None:
        p = self.predict(pe)
        pe.score.probability = round(p, 4)
        pe.score.calibrated = True
        pe.score.model = self.name
        if pe.score.hypothesis in (Hypothesis.SAME_CAMPAIGN, Hypothesis.ABSTAIN, Hypothesis.INDEPENDENT):
            pe.score.hypothesis = (Hypothesis.SAME_CAMPAIGN if p >= 0.5 else
                                   Hypothesis.ABSTAIN if p >= 0.2 else Hypothesis.INDEPENDENT)


def failed_or_missing(g: Gene) -> bool:
    return g.status in (GeneStatus.UNAVAILABLE, GeneStatus.FAILED, GeneStatus.NOT_APPLICABLE)


ROBUSTNESS_FAMILIES = [f.value for f in Family]


TEXT_TEMPLATE_MAX_DF = 2  # page text near-identical in more incidents (or known campaigns) is a template body
TEMPLATE_TEXT_GROUP = "TXT:asset:near_duplicate_common"


def _settle_template_text(groups: dict, notes: list[GeneMatch]) -> None:
    """A near-duplicate body held by more than TEXT_TEMPLATE_MAX_DF incidents links only with context evidence from
    another family (campaign-level bits outside TXT, or the same image by hash: a shared account, domain, image or
    file; embedding similarity alone is not context); otherwise it
    is a template shared by unrelated operators and is listed with 0 bits (CR-ASTRA-PIPE-203)."""
    items = groups.get(TEMPLATE_TEXT_GROUP)
    if not items:
        return
    context = any(g != TEMPLATE_TEXT_GROUP and its and its[0][3] != "TXT" and its[0][0] >= MIN_BITS
                  and (its[0][2] == "campaign" or (its[0][2] == "asset" and its[0][1].kind in IMAGE_HASH_KINDS))
                  for g, its in groups.items())
    if context:
        return
    del groups[TEMPLATE_TEXT_GROUP]
    for _bits, m, _lvl, _fam in items:
        notes.append(m.model_copy(update={
            "kind": "text_template", "bits": 0.0,
            "explanation_pl": m.explanation_pl.rsplit(" (", 1)[0]
            + "; brak dowodu spoza tekstu: szablon, nie dowód wspólnego operatora (+0.0 bit)"}))


def _text_near_duplicate(a: IncidentGenome, b: IncidentGenome, groups: dict, rarity: Rarity | None = None,
                         notes: list[GeneMatch] | None = None) -> None:
    """Page text MinHash Jaccard >= 0.9 over >= 50 shingles: a near-duplicate page, may link on its own, unless the
    same body is near-identical in more than TEXT_TEMPLATE_MAX_DF incidents (known campaigns during assignment):
    then it is a template text shared by unrelated operators, shown with 0 bits (CR-ASTRA-PIPE-203: ten independent
    operators with one generic body gave 45 false pairs and one campaign)."""
    for ch in ("page",):
        if ch not in a.minhash or ch not in b.minhash:
            continue
        (sa, na, ga), (sb, nb, gb) = a.minhash[ch], b.minhash[ch]
        if len(sa) != len(sb) or min(na, nb) < TEXT_NEARDUP_MIN_SHINGLES:
            continue
        j = float(np.mean(sa == sb))
        if j < TEXT_NEARDUP_JACCARD:
            continue
        df = rarity.minhash_near_df(ga.value or "", TEXT_NEARDUP_JACCARD) if rarity is not None else 2
        if df > TEXT_TEMPLATE_MAX_DF:
            # a common body: promoted only with non-text context evidence (`_settle_template_text`)
            m = _match("TXT", "text_near_duplicate", f"{ch}: {na} shingli", f"{ch}: {nb} shingli", NEARDUP_BITS,
                       sim=j, ga=ga, gb=gb,
                       expl=(f"Niemal identyczny tekst strony: podobieństwo Jaccarda (MinHash) {num_pl(j, '.3f')} przy"
                             f" {min(na, nb)} shinglach; ten sam tekst w {df} incydentach korpusu, więc liczy się tylko"
                             f" razem z dowodem spoza tekstu ({num_pl(NEARDUP_BITS, '+.1f')} bit)"))
            groups[TEMPLATE_TEXT_GROUP].append((NEARDUP_BITS, m, "campaign", "TXT"))
            continue
        m = _match("TXT", "text_near_duplicate", f"{ch}: {na} shingli", f"{ch}: {nb} shingli", NEARDUP_BITS, sim=j,
                   ga=ga, gb=gb, expl=("Niemal identyczny tekst strony: podobieństwo Jaccarda (MinHash)"
                                       f" {num_pl(j, '.3f')}"
                                       f" przy {min(na, nb)} shinglach treści (bez nawigacji, stopki i komentarzy)"
                                       f" ({num_pl(NEARDUP_BITS, '+.1f')} bit)"))
        groups["TXT:asset:near_duplicate"].append((NEARDUP_BITS, m, "campaign", "TXT"))


AUDIO_REUSE_JACCARD = 0.03      # MinHash Jaccard of the landmark-hash sets (unrelated synthetic audio: <= 0.02)
AUDIO_REUSE_CONTAINMENT = 0.15  # share of the shorter track's landmarks found in the other (crops, re-encodes)
AUDIO_VOICE_BITS = 8.0          # rare reused track with speech: campaign level, VIS cap 8, needs a 2nd family
AUDIO_MUSIC_BITS = 4.0          # rare reused track, no speech detected / ASR unknown: kit level (campaign groups)
_SPEECH_RE = re.compile(r"speech=([a-z01]+)")


AUDIO_VOICE_MIN_CONTAINMENT = 0.4
# A near-complete copy also needs a MinHash Jaccard clearly above the unrelated-track noise (GAP-AUDIT B9: synthetic
# speech matched a sample incident with +8 bits at Jaccard 0.04; 66 unrelated synthetic track pairs reach Jaccard
# <= 0.066, noisy full copies >= 0.242). With very different track lengths the containment estimate inflates
# MinHash noise by the length ratio, so containment alone is not enough.
AUDIO_STRONG_MIN_JACCARD = 0.08


def _audio_reuse(a: IncidentGenome, b: IncidentGenome, groups: dict, rarity: Rarity | None = None) -> None:
    """Same audio track in both incidents (signals of reused voice-over / soundtrack, never deepfake detection).

    Landmark-set containment >= 0.15 and Jaccard >= 0.03. Rarity decides the level: a track whose LSH bands are
    held by more than STOCK_AUDIO_MAX_DF incidents is stock music / a platform sound (0 bits); a rare track with
    speech (ASR) is a reused voice-over (campaign level, AUDIO_VOICE_BITS, VIS family cap, so it never links on its
    own); a rare track without detected speech, or with ASR unavailable, is kit level (AUDIO_MUSIC_BITS)."""
    if not a.audio or not b.audio:
        return
    from hydra.genome.aud import STOCK_AUDIO_MAX_DF, containment, jaccard

    best = None
    for sa, na, ga, ba in a.audio:
        for sb, nb, gb, bb in b.audio:
            j = jaccard(sa, sb)
            if j < AUDIO_REUSE_JACCARD:
                continue
            c = containment(j, na, nb)
            if c < AUDIO_REUSE_CONTAINMENT:
                continue
            if best is None or c > best[0]:
                best = (c, j, na, nb, ga, gb, [x for x, y in zip(ba, bb, strict=True) if x == y])
    if best is None:
        return
    c, j, na, nb, ga, gb, shared = best
    df = 2
    if rarity is not None and shared:
        dfs = sorted(rarity.df("audio_fp_band", v) for v in shared)
        df = max(2, dfs[len(dfs) // 2])
    if df > STOCK_AUDIO_MAX_DF:
        return  # stock music / platform jingle: boilerplate, 0 bits
    sp = {m.group(1) if (m := _SPEECH_RE.search(g.evidence.note or "")) else "unknown" for g in (ga, gb)}
    # Voice-over reuse is operator evidence only for a near-complete copy: a partial overlap (e.g. one shared
    # stock voice clip, measured 0.28 on two unrelated sample incidents) stays kit level. Below
    # AUDIO_VOICE_MIN_CONTAINMENT the track never adds campaign-level bits (integration review 2026-11-21).
    strong = c >= AUDIO_VOICE_MIN_CONTAINMENT and j >= AUDIO_STRONG_MIN_JACCARD
    voice = sp == {"1"} and strong
    scale = min(1.0, c / 0.3)  # full bits from 30 % shared landmarks (a noisy re-encode keeps ~40-55 %)
    bits = round((AUDIO_VOICE_BITS if voice else AUDIO_MUSIC_BITS) * scale, 3)
    if bits < MIN_BITS:
        return
    partial = not strong
    what = ("Ta sama ścieżka dźwiękowa z mową (ponowne użycie lektora/nagrania głosu)" if voice else
            "Częściowo lub słabo zgodna ścieżka dźwiękowa (wspólny fragment albo zgodność bliska szumu)" if partial else
            "Ta sama ścieżka dźwiękowa (muzyka/dźwięk; mowy nie wykryto lub brak transkrypcji)")
    m = _match("VIS", "audio_track_reuse", f"{na} hashy", f"{nb} hashy", bits, sim=round(c, 3), df=df, ga=ga, gb=gb,
               expl=(f"{what}: udział {num_pl(c, '.2f')} punktów charakterystycznych widma krótszego nagrania"
                     f" występuje w"
                     f" drugim (Jaccard MinHash {num_pl(j, '.2f')}, incydentów z tą ścieżką: {df}); sygnał ponownego"
                     f" użycia"
                     f" głosu/ścieżki, nie wykrywanie deepfake ({num_pl(bits, '+.1f')} bit)"))
    if voice:
        groups["VIS:operator:audio_voice_track"].append((bits, m, "campaign", "VIS"))
    elif partial:
        # A partial overlap (one shared clip, e.g. a stock voice line; sample: 0.28 between unrelated incidents 5 and
        # 14) is kit evidence only when another non-audio identity-kit gene corroborates it (`_settle_partial_audio`).
        groups[AUDIO_PARTIAL_GROUP].append((bits, m, "kit", "VIS"))
    else:
        groups["VIS:kit:audio_track"].append((bits, m, "kit", "VIS"))


AUDIO_PARTIAL_GROUP = "VIS:kit:audio_track_partial"
IMAGE_HASH_KINDS = {"pdq", "pdq_canon", "phash"}
IMAGE_HASH_SUFFIX = ":image_hash"
IMAGE_LED_BITS = 5.0  # image-hash campaign bits from which a text-guessed scheme mismatch is no contradiction


def _unit_class(g: Gene) -> str | None:
    m = _CLASS_RE.search(g.evidence.note or "")
    return m.group(1) if m else None


def image_hash_campaign(kind: str, d: int, df: int, ga: Gene, gb: Gene, lvl: str) -> bool:
    """CR-AQ-1 (C): the same rare ad image (pHash <= 6 or PDQ <= 31, held by <= STOCK_IMAGE_MAX_DF incidents within
    the radius, a creative / desktop screenshot / region on at least one side, no avatar/icon/logo/pixel/phone UI) is
    campaign-level content evidence, like a rare phrase. It stays in the VIS family cap (8 bits), so a campaign link
    still needs a second family or operator evidence."""
    if lvl != "asset" or str(ga.family) != "VIS" or kind not in IMAGE_HASH_KINDS or df > STOCK_IMAGE_MAX_DF:
        return False
    if d > (VIS_NEARDUP_PHASH if kind == "phash" else VIS_NEARDUP_PDQ):
        return False
    classes = {_unit_class(ga), _unit_class(gb)}
    # the ad itself on at least one side (a shared page image of a sibling landing page stays an asset: sample
    # held-out campaign {1,2,3} abstained against its sibling through one page image + template text)
    return not (classes & VIS_EXCLUDED_CLASSES) and bool(classes & VIS_STANDALONE_CLASSES)


def is_style_match_kind(kind: str, family: str) -> bool:
    return (kind in STYLE_KINDS or kind in STYLE_EXTRA_KINDS or family == "NAR" or kind.endswith(STYLE_SUFFIXES)
            or kind.startswith(("motif", "cta", "persona", "promise", "urgency", "scheme")))


def is_style_match(m: GeneMatch) -> bool:
    """Fuzzy template/style kit evidence (capped at STYLE_CAP), as opposed to identity kit genes."""
    return (m.kind in STYLE_KINDS or m.kind in STYLE_EXTRA_KINDS or m.family == "NAR"
            or m.kind.endswith(STYLE_SUFFIXES) or m.kind.startswith(("motif", "cta", "persona", "promise", "urgency",
                                                                     "scheme")))


def _settle_partial_audio(groups: dict, notes: list[GeneMatch]) -> None:
    """Partial shared audio counts as kit evidence only with a non-audio identity-kit group in the same pair;
    otherwise it is listed with 0 bits (architect review 2026-11-21: nask-5 and nask-14 joined one group)."""
    items = groups.get(AUDIO_PARTIAL_GROUP)
    if not items:
        return
    corroborated = any(
        g != AUDIO_PARTIAL_GROUP and its and its[0][2] == "kit" and not all(is_style_match(t[1]) for t in its)
        and not any(t[1].kind == "audio_track_reuse" for t in its) for g, its in groups.items())
    if corroborated:
        return
    del groups[AUDIO_PARTIAL_GROUP]
    for _bits, m, _lvl, _fam in items:
        notes.append(m.model_copy(update={
            "bits": 0.0, "explanation_pl": m.explanation_pl.rsplit(" (", 1)[0]
            + "; bez potwierdzenia innym dowodem zestawu narzędzi: tylko informacyjnie (+0.0 bit)"}))


STOCK_IMAGE_MAX_DF = 2  # a page image whose exact pHash/PDQ is held by more incidents is stock / template


def _unit_near(ra: list[tuple[str, dict]], rb: list[tuple[str, dict]]) -> tuple[np.ndarray, np.ndarray]:
    """(near, cos) between two lists of VIS units: pHash <= VIS_NEARDUP_PHASH or PDQ <= VIS_NEARDUP_PDQ, AND DINOv2
    cos >= VIS_NEARDUP_COS. PDQ is the minimum over the 8 dihedral variants when both units carry them
    (`pdq_dihedral`, ADOPT #1), else the canonical hash."""
    cos = np.stack([d["dinov2"] for _, d in ra]) @ np.stack([d["dinov2"] for _, d in rb]).T
    near = np.zeros_like(cos, dtype=bool)
    ia = [i for i, (_, d) in enumerate(ra) if "phash" in d]
    ib = [j for j, (_, d) in enumerate(rb) if "phash" in d]
    if ia and ib:
        h = hamming_matrix(np.stack([ra[i][1]["phash"] for i in ia]), np.stack([rb[j][1]["phash"] for j in ib]))
        near[np.ix_(ia, ib)] |= h <= VIS_NEARDUP_PHASH
    pdq = pdq_distance_matrix([d for _, d in ra], [d for _, d in rb])
    near |= pdq <= VIS_NEARDUP_PDQ
    near &= cos >= VIS_NEARDUP_COS
    return near, cos


def crop_matrix(ra: list[tuple[str, dict]], rb: list[tuple[str, dict]]) -> np.ndarray:
    """Local (sub-window) evidence for crop variants (CR-ASTRA-6): min pHash distance between a window of one unit
    (`phash_multicrop`, >= 64 % of its area) and the whole other unit, both directions. Large where unknown."""
    out = np.full((len(ra), len(rb)), 10**6, dtype=np.int64)
    for wins, whole, flip in ((ra, rb, False), (rb, ra, True)):
        iw = [i for i, (_u, d) in enumerate(wins) if "multicrop" in d]
        ih = [j for j, (_u, d) in enumerate(whole) if "phash" in d]
        if not iw or not ih:
            continue
        mc = [wins[i][1]["multicrop"] for i in iw]
        k = min(len(m) for m in mc)
        stack = np.stack([m[:k] for m in mc]).reshape(len(iw) * k, -1)  # [windows of every unit, 8]
        h = hamming_matrix(stack, np.stack([whole[j][1]["phash"] for j in ih])).reshape(len(iw), k, len(ih)).min(1)
        if flip:
            out[np.ix_(ih, iw)] = np.minimum(out[np.ix_(ih, iw)], h.T)
        else:
            out[np.ix_(iw, ih)] = np.minimum(out[np.ix_(iw, ih)], h)
    return out


def pdq_distance_matrix(da: list[dict], db: list[dict]) -> np.ndarray:
    """PDQ Hamming distance between units (large where unknown). Both with `pdq_dihedral`: min over the 8 dihedral
    variants of one unit against the original orientation of the other, in both directions (ThreatExchange PDQ
    recommendation; the lexicographic-min canonical hash flips variant under re-encoding). Otherwise `pdq_canon`."""
    out = np.full((len(da), len(db)), 10**6, dtype=np.int64)
    ia = [i for i, d in enumerate(da) if "pdq_dihedral" in d]
    ib = [j for j, d in enumerate(db) if "pdq_dihedral" in d]
    if ia and ib:
        xa = np.stack([da[i]["pdq_dihedral"] for i in ia])  # [n, 8, 32]
        xb = np.stack([db[j]["pdq_dihedral"] for j in ib])
        d1 = np.min(np.stack([hamming_matrix(xa[:, k], xb[:, 0]) for k in range(8)]), axis=0)
        d2 = np.min(np.stack([hamming_matrix(xa[:, 0], xb[:, k]) for k in range(8)]), axis=0)
        out[np.ix_(ia, ib)] = np.minimum(d1, d2)
    ca = [i for i, d in enumerate(da) if "pdq_canon" in d]
    cb = [j for j, d in enumerate(db) if "pdq_canon" in d]
    if ca and cb:
        h = hamming_matrix(np.stack([da[i]["pdq_canon"] for i in ca]), np.stack([db[j]["pdq_canon"] for j in cb]))
        both = np.zeros_like(out, dtype=bool)
        if ia and ib:
            both[np.ix_(ia, ib)] = True
        sub = out[np.ix_(ca, cb)]
        keep = both[np.ix_(ca, cb)]
        out[np.ix_(ca, cb)] = np.where(keep, sub, np.minimum(sub, h))
    return out


def _components(near_self: np.ndarray) -> list[int]:
    """Connected components of a symmetric boolean matrix (union-find): unit index -> component id."""
    n = len(near_self)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j in zip(*np.nonzero(near_self), strict=True):
        ri, rj = find(int(i)), find(int(j))
        if ri != rj:
            parent[max(ri, rj)] = min(ri, rj)
    return [find(i) for i in range(n)]


def _max_matching(edges: dict[int, set[int]]) -> list[tuple[int, int]]:
    """Maximum bipartite matching (Kuhn's augmenting paths; graphs here are small). Deterministic order."""
    match_b: dict[int, int] = {}

    def augment(u: int, seen: set[int]) -> bool:
        for v in sorted(edges.get(u, ())):
            if v in seen:
                continue
            seen.add(v)
            if v not in match_b or augment(match_b[v], seen):
                match_b[v] = u
                return True
        return False

    for u in sorted(edges):
        augment(u, set())
    return sorted((u, v) for v, u in match_b.items())


def _visual_near_duplicate(a: IncidentGenome, b: IncidentGenome, groups: dict, rarity: Rarity | None = None) -> int:
    """Same image content in both incidents: pHash <= 6 or PDQ <= 31 (dihedral minimum), AND DINOv2 >= 0.95; app UI,
    boilerplate, brand, avatars and icons excluded. A creative (or >= 2 video frames) may link on its own; >= 3
    matched contents of any other class make VIS campaign-level evidence with the raised cap.

    Counting (CR-ASTRA-2): units that are near-duplicates of each other inside one incident are ONE content item
    (five copies of a page image are one image), and the matched count is a maximum one-to-one matching between the
    distinct contents of A and of B, so repetition on one side and incident naming cannot change it. Stock content
    (CR-ASTRA-8): a unit is common when more than STOCK_IMAGE_MAX_DF incidents hold a near-identical hash (Hamming
    ball, `Rarity.near_df`), not only the exact value. Returns the number of matched distinct contents."""
    def common(d: dict) -> bool:
        if rarity is None:
            return False
        if d.get("footage_df", 0) > STOCK_IMAGE_MAX_DF:  # near-identical video frame in many incidents
            return True
        return any(rarity.near_df(k, v, VIS_NEARDUP_PHASH if k == "phash" else VIS_NEARDUP_PDQ)
                   > STOCK_IMAGE_MAX_DF for k, v in d.get("values", {}).items() if k in ("phash", "pdq_canon", "pdq"))

    def rows(g: IncidentGenome) -> list[tuple[str, dict]]:
        return sorted(((u, d) for u, d in g.units.items() if "dinov2" in d and d.get("class") not in
                       VIS_EXCLUDED_CLASSES and ("phash" in d or "pdq_canon" in d or "pdq_dihedral" in d)
                       and not common(d)), key=lambda t: t[0])

    ra, rb = rows(a), rows(b)
    if not ra or not rb:
        return 0
    near, cos = _unit_near(ra, rb)
    crop = (crop_matrix(ra, rb) <= VIS_CROP_PHASH) & (cos >= VIS_NEARDUP_COS) & ~near
    near |= crop
    from hydra import vis_geom  # ADV-VIS: HYDRA_ADV_GEOM / HYDRA_ADV_CROP, default off

    geo = vis_geom.scorer_hook(ra, rb, cos, near, groups) if vis_geom.active() else None
    if geo is not None:
        crop |= geo
        near |= geo
    if not near.any():
        return 0
    comp_a = _components(_unit_near(ra, ra)[0])
    comp_b = _components(_unit_near(rb, rb)[0])
    edges: dict[int, set[int]] = defaultdict(set)
    for i, j in zip(*np.nonzero(near), strict=True):
        edges[comp_a[int(i)]].add(comp_b[int(j)])
    matching = _max_matching(edges)
    n_matched = len(matching)
    matched_pairs = {(p, q) for p, q in matching}
    classes: list[tuple[str | None, str | None]] = []  # per matched content pair: (class in A, class in B)
    for p, q in matching:
        ca = {ra[i][1].get("class") for i in range(len(ra)) if comp_a[i] == p}
        cb = {rb[j][1].get("class") for j in range(len(rb)) if comp_b[j] == q}
        classes.append((min(ca, key=str), min(cb, key=str)))
    ii, jj = np.nonzero(near)
    best = max(((int(i), int(j)) for i, j in zip(ii, jj, strict=True) if (comp_a[i], comp_b[j]) in matched_pairs),
               key=lambda t: (cos[t], -t[0], -t[1]))
    (ua, da), (ub, db) = ra[best[0]], rb[best[1]]
    any_standalone = any(x in VIS_STANDALONE_CLASSES or y in VIS_STANDALONE_CLASSES for x, y in classes)
    frames = sum(1 for x, y in classes if "frame" in (x, y))
    standalone = any_standalone or frames >= 2 or n_matched >= VIS_MULTI_STANDALONE
    copies = (f"; powtórzone kopie w obrębie incydentu liczone raz ({len(ra)}/{len(rb)} jednostek,"
              f" {len(set(comp_a))}/{len(set(comp_b))} różnych obrazów)") if (
        len(set(comp_a)) < len(ra) or len(set(comp_b)) < len(rb)) else ""
    how = ("kadr/fragment: pHash okna o powierzchni ≥ 0.64 jednego obrazu w progu"
           f" {VIS_CROP_PHASH} wobec całego drugiego" if crop[best] else "pHash/PDQ w progu")
    if geo is not None and geo[best]:
        how = "kadr/fragment potwierdzony geometrycznie (ORB/RootSIFT + MAGSAC++, przekształcenie podobieństwa)"
    if standalone:
        what = "Niemal identyczna kreacja/obraz" if n_matched < VIS_MULTI_STANDALONE else \
            "Niemal identyczne obrazy strony (ta sama strona)"
        m = _match("VIS", "visual_near_duplicate", ua, ub, NEARDUP_BITS, sim=float(cos[best]), ga=da.get("gene"),
                   gb=db.get("gene"), expl=(f"{what}: {how} i DINOv2"
                                            f" cos={num_pl(float(cos[best]), '.3f')} ({n_matched} różnych obrazów"
                                            f" dopasowanych"
                                            f" jeden do jednego{copies}) ({num_pl(NEARDUP_BITS, '+.1f')} bit)"))
        groups["VIS:asset:near_duplicate"].append((NEARDUP_BITS, m, "campaign", "VIS"))
    elif n_matched >= RARE_MIN_ITEMS:
        bits = min(RAISED_CAP, 4.0 * n_matched)
        m = _match("VIS", "visual_multi_match", ua, ub, bits, sim=float(cos[best]), ga=da.get("gene"),
                   gb=db.get("gene"), expl=(f"{n_matched} różnych, niemal identycznych obrazów strony w obu"
                                            f" incydentach (dopasowanie jeden do jednego{copies})"
                                            f" ({num_pl(bits, '+.1f')} bit)"))
        groups["VIS:phrase:multi_image"].append((bits, m, "campaign", "VIS"))
    return n_matched


def _mmss(t: float) -> str:
    t = max(0.0, t)
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _video_segment(a: IncidentGenome, b: IncidentGenome, groups: dict) -> None:
    """Shared video footage (hydra/genome/vid_seq.py): the longest aligned run of near-identical frames between a
    video of A and a video of B, with its time range in both videos. Rare footage is near-duplicate evidence in the
    same dependency group as `visual_near_duplicate` (the two never stack beyond REST_WEIGHT); footage held by more
    than FOOTAGE_MAX_DF incidents (stock / news clip) is shown at 0 bits and never links."""
    from hydra.genome.vid_seq import match_genomes, segment_bits

    best = match_genomes(a, b)
    if best is None:
        return
    seg, fa, fb = best
    bits = segment_bits(seg, NEARDUP_BITS)
    ev_a = EvidencePointer(artifact_id=seg.artifact_a, timecode_s=seg.start_a, note=seg.note("a"))
    ev_b = EvidencePointer(artifact_id=seg.artifact_b, timecode_s=seg.start_b, note=seg.note("b"))
    how = (f"{seg.frames} dopasowanych klatek (PDQ ≤ 31 lub DINOv2 cos ≥ 0,95"
           + (f"; min. odległość Hamminga {seg.min_hamming}/256" if seg.min_hamming is not None else "")
           + (f"; DINOv2 śr. cos={num_pl(seg.mean_cos, '.3f')}" if seg.mean_cos is not None else "") + ")")
    where = (f"{_mmss(seg.start_a)}–{_mmss(seg.end_a)} w {a.incident_id} i {_mmss(seg.start_b)}–{_mmss(seg.end_b)}"
             f" w {b.incident_id} (przesunięcie {num_pl(seg.offset_s, '+.1f')} s)")
    if seg.common:
        expl = (f"Wspólny materiał wideo {where}, {how}; ten sam materiał występuje w {seg.footage_df} incydentach"
                f" korpusu: materiał stockowy lub telewizyjny, nie dowód wspólnego operatora (0 bit)")
        grp, lvl = "VIS:boilerplate:shared_footage", "asset"
    else:
        expl = f"Wspólny fragment wideo: {where}, {how} ({num_pl(bits, '+.1f')} bit)"
        grp, lvl = "VIS:asset:near_duplicate", "campaign"
    m = GeneMatch(family=Family.VIS, kind="video_segment",
                  value_a=f"{seg.artifact_a[:12]} {seg.start_a:.1f}-{seg.end_a:.1f}s",
                  value_b=f"{seg.artifact_b[:12]} {seg.start_b:.1f}-{seg.end_b:.1f}s",
                  similarity=seg.mean_cos, doc_freq=seg.footage_df, bits=round(bits, 3), provenance=Provenance.INF,
                  evidence_a=ev_a, evidence_b=ev_b, explanation_pl=expl)
    groups[grp].append((bits, m, lvl, "VIS"))


def is_linked(pe: PairEval, th: Thresholds) -> str | None:
    """'campaign' for a same-campaign link, 'group' for a campaign-group (kit) link, else None."""
    if pe.score.hypothesis == Hypothesis.SAME_CAMPAIGN:
        return "campaign"
    if pe.score.hypothesis == Hypothesis.SHARED_TEMPLATE or pe.kit_bits >= th.kit:
        return "group"
    return None


def robustness(a: IncidentGenome, b: IncidentGenome, rarity: Rarity, pe: PairEval, th: Thresholds | None = None
               ) -> dict:
    """Leave-one-family-out (ADVANCED_METHODS QW4): rescore the pair with each contributing family removed.
    A link is robust when it still holds at the same level without any single family; otherwise the family
    whose removal breaks it is critical. Families without evidence on this pair are skipped."""
    th = th or Thresholds()
    kind = is_linked(pe, th)
    present = {f for f, v in pe.score.family_bits.items() if v > 0}  # all pooled families, not only top reasons
    per: dict[str, dict] = {}
    for fam in ROBUSTNESS_FAMILIES:
        if fam not in present:
            continue
        alt = score_pair(a, b, rarity, th, drop=frozenset({fam}))
        still = is_linked(alt, th)
        holds = still == kind if kind == "campaign" else still in ("campaign", "group")
        per[fam] = {"total_bits_without": alt.score.total_bits, "support_without": alt.support,
                    "kit_bits_without": alt.kit_bits, "still_linked": bool(kind and holds)}
    critical = sorted(f for f, v in per.items() if not v["still_linked"])
    return {"link": kind, "families": per, "robust_link": bool(kind) and not critical, "critical_families": critical}
