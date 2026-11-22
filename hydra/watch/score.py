"""Stage A name score of HYDRA Watch: evidence bits with named Polish reasons (triage, not a probability).

Clean-room: behaviour from docs/cleanroom specs (phishing_catcher.md, opensquat.md, streamingphish.md) and
REALTIME_DISCOVERY.md §5.2-5.3 only. Differences from the studied tools (by design):
- brand tokens are matched on label/hyphen token boundaries (never as free substrings for short brands);
- fuzzy (Damerau distance 1) only for brand tokens of length >= 5, distance 2 only for length >= 9;
- every label is compared, and the brand's placement (subdomain, combo with other words, foreign suffix) matters;
- the brand's own registrable domains are recognised and score strongly negative;
- entropy is a small bonus, never a baseline; the free-CA signal is not used;
- name-pattern families (digit templates) and known-campaign genes link a name to a campaign.

`D = sum of reason bits`. Weights (`W`) are initial priors (REALTIME_DISCOVERY.md §5.3), documented in
docs/watch/WATCH.md and pinned by tests; they are not yet fitted lifts.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from hydra.watch.lexicon import BAIT_STEMS, BRANDS, FAKE_SUFFIX_TOKENS, GENERIC_STEMS, TLD_PRIOR_BITS, Brand
from hydra.watch.names import HostParts, alpha_tokens, family_key, osa_distance, parts, shannon_bits, tokens

MODEL = "watch_name_bits@1"

# Reason weights in bits (prior; see module docstring). Caps are applied per reason group.
W: dict[str, float] = {
    "official_domain": -8.0,      # host is under the brand's own registrable domain
    "brand_subdomain": 4.0,       # brand label left of a foreign registrable domain: allegro.xyz123.sbs
    "brand_combo": 4.0,           # brand + other words in the registrable label: pkobp-weryfikacja.pl
    "brand_foreign_suffix": 3.0,  # registrable label == brand on a foreign suffix: allegro.sbs
    "brand_typo": 4.0,            # one edit (two for long brands) from a brand token: allegr0, inpots
    "brand_extra": 1.0,           # each additional distinct brand in the same name (cap 2)
    "homoglyph": 2.0,             # IDN look-alike characters folded to reveal a brand
    "idn": 0.5,                   # punycode without a brand
    "fake_suffix": 1.5,           # 'pl'/'com'/'gov' token left of the real suffix: allegro.pl-oferta.top
    "bait": 1.0,                  # each Polish bait stem (cap 3)
    "generic_bait": 0.5,          # each generic English credential word (cap 1)
    "tld_prior": 0.0,             # per-suffix prior from lexicon.TLD_PRIOR_BITS (cap 2)
    "many_hyphens": 1.0,          # >= 3 hyphens left of the suffix (1.5 from 5)
    "deep_name": 1.0,             # >= 3 labels left of the suffix
    "digit_run": 1.0,             # >= 5 consecutive digits in the registrable label (generated sequence)
    "random_label": 0.5,          # long high-entropy registrable label
    "family": 3.0,                # name template shared by >= 3 listed registrable domains (4 from 10)
    "listed_label": 2.0,          # subdomain label seen on >= 5 listed registrable domains (alebilet., cap 2)
    "campaign_skeleton": 6.0,     # url_label_skeleton of a known campaign (rarity-scaled, cap 6)
    "campaign_token": 6.0,        # invented platform/product name of a known campaign
    "campaign_domain": 8.0,       # host or registrable domain already seen in a known campaign
    "campaign_domain_typo": 6.0,  # one edit from a known campaign's lure registrable label (len >= 6)
    "campaign_family": 6.0,       # same generated-name template as a known campaign's lure domain ('?-offertanr#')
}
CAPS = {"listed_label": 2.0, "brand_extra": 2.0, "bait": 3.0, "generic_bait": 1.0, "tld_prior": 2.0, "family": 4.0,
        "campaign_skeleton": 6.0}
A_CUT = 6.0  # review/enrichment cut (bits)

THREE_LETTER_TAILS = {"pl", "polska", "bp", "pay", "bank", "online", "app", "mobile", "eu", "com"}


@dataclass
class Reason:
    code: str
    bits: float
    label_pl: str
    detail: str = ""
    campaign_id: str | None = None


@dataclass
class NameScore:
    host: str
    registrable: str
    family: str
    bits: float
    reasons: list[Reason] = field(default_factory=list)
    brands: list[str] = field(default_factory=list)
    campaign_hints: list[str] = field(default_factory=list)
    model: str = MODEL

    @property
    def high(self) -> bool:
        return self.bits >= A_CUT

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["bits"] = round(self.bits, 2)
        d["high"] = self.high
        for r in d["reasons"]:
            r["bits"] = round(r["bits"], 2)
        return d


class FamilyStats(Protocol):
    def family_size(self, key: str) -> int: ...
    def skeleton_freq(self, skeleton: str) -> tuple[int, int]: ...
    def label_size(self, label: str) -> int: ...


class CampaignGenes(Protocol):
    def skeleton_campaigns(self, skeleton: str) -> list[str]: ...
    def token_campaigns(self, token: str) -> list[str]: ...
    def domain_campaigns(self, host: str, registrable: str) -> list[str]: ...
    def lure_labels(self) -> dict[str, list[str]]: ...
    def family_campaigns(self, family: str) -> list[str]: ...


# --------------------------------------------------------------------------------------- brand matching


@dataclass
class BrandHit:
    brand: Brand
    token: str
    kind: str      # exact | prefix | contains | typo
    where: str     # sub | domain
    distance: int = 0


def _match_token(tok: str, brand: Brand) -> tuple[str, str, int] | None:
    for bt in brand.tokens:
        if tok == bt:
            return "exact", bt, 0
        if len(bt) >= 4 and tok.startswith(bt):
            return "prefix", bt, 0
        if len(bt) == 3 and tok.startswith(bt) and tok[3:] in THREE_LETTER_TAILS:
            return "prefix", bt, 0
        if len(bt) >= 6 and bt in tok:
            return "contains", bt, 0
    for bt in brand.tokens:
        if len(bt) >= 5 and len(tok) >= 5:
            limit = 2 if len(bt) >= 9 else 1
            d = osa_distance(tok, bt, limit)
            if 0 < d <= limit:
                return "typo", bt, d
    return None


def brand_hits(p: HostParts) -> list[BrandHit]:
    hits: list[BrandHit] = []
    places = [("sub", lab) for lab in (p.f_sub_labels or [])] + [("domain", p.f_domain)]
    for where, label in places:
        toks = alpha_tokens(label)
        cands = set(toks) | {label.replace("-", "")}
        cands |= {a + b for a, b in zip(toks, toks[1:], strict=False) if b == "pl"}  # gov-pl, olx-pl, ...
        for brand in BRANDS:
            best: BrandHit | None = None
            for tok in cands:
                m = _match_token(tok, brand)
                if m is None:
                    continue
                kind, bt, dist = m
                h = BrandHit(brand, tok, kind, where, dist)
                if best is None or (best.kind == "typo" and kind != "typo"):
                    best = h
            if best:
                hits.append(best)
    # one hit per brand: prefer exact over typo, domain placement over sub
    by_brand: dict[str, BrandHit] = {}
    for h in hits:
        cur = by_brand.get(h.brand.name)
        if cur is None or (cur.kind == "typo" and h.kind != "typo"):
            by_brand[h.brand.name] = h
    # Allegro Lokalnie subsumes Allegro
    if "Allegro Lokalnie" in by_brand and "Allegro" in by_brand:
        by_brand.pop("Allegro")
    return list(by_brand.values())


def is_official(p: HostParts) -> Brand | None:
    reg = p.registrable
    for b in BRANDS:
        for off in b.official:
            if p.host == off or p.host.endswith("." + off) or reg == off:
                return b
    return None


# --------------------------------------------------------------------------------------- scorer


def _add(reasons: list[Reason], code: str, bits: float, label: str, detail: str = "", cid: str | None = None) -> None:
    if bits:
        reasons.append(Reason(code, bits, label, detail, cid))


def _cap(reasons: list[Reason], code: str) -> None:
    cap = CAPS.get(code)
    if cap is None:
        return
    total = 0.0
    for r in reasons:
        if r.code != code:
            continue
        room = max(0.0, cap - total)
        r.bits = min(r.bits, room)
        total += r.bits
    reasons[:] = [r for r in reasons if r.code != code or r.bits > 0]


def score_host(host: str, families: FamilyStats | None = None, campaigns: CampaignGenes | None = None) -> NameScore:
    p = parts(host)
    fam = family_key(p)
    reasons: list[Reason] = []
    out = NameScore(host=p.host, registrable=p.registrable, family=fam, bits=0.0, reasons=reasons)

    official = is_official(p)
    if official is not None:
        _add(reasons, "official_domain", W["official_domain"], "Oficjalna domena marki",
             f"{official.name}: {p.registrable}")
        out.bits = sum(r.bits for r in reasons)
        return out

    # ---- brand placement / typo / homoglyph
    hits = brand_hits(p)
    hits.sort(key=lambda h: (h.kind == "typo", h.where != "sub"))
    for i, h in enumerate(hits):
        out.brands.append(h.brand.name)
        if i > 0:
            _add(reasons, "brand_extra", W["brand_extra"], "Kolejna marka w tej samej nazwie", h.brand.name)
            continue
        if h.kind == "typo":
            _add(reasons, "brand_typo", W["brand_typo"], "Literówka nazwy marki",
                 f"{h.token} ≈ {h.brand.name} (odległość {h.distance})")
        elif h.where == "sub":
            _add(reasons, "brand_subdomain", W["brand_subdomain"], "Marka w subdomenie obcej domeny",
                 f"{h.brand.name} w {'.'.join(p.sub_labels)} → {p.registrable}")
        elif alpha_tokens(p.f_domain) != [h.token] or "-" in p.f_domain or any(c.isdigit() for c in p.f_domain):
            _add(reasons, "brand_combo", W["brand_combo"], "Marka z dopiskami w nazwie domeny",
                 f"{h.brand.name}: {p.f_domain}")
        else:
            _add(reasons, "brand_foreign_suffix", W["brand_foreign_suffix"], "Marka pod obcą końcówką domeny",
                 f"{h.brand.name}: {p.registrable}")
    _cap(reasons, "brand_extra")
    if p.homoglyph and hits:
        _add(reasons, "homoglyph", W["homoglyph"], "Znaki-sobowtóry (homoglify) udające markę",
             f"{p.host} → {p.folded}")
    elif p.idn:
        _add(reasons, "idn", W["idn"], "Nazwa IDN (punycode)", p.host)

    # ---- fake suffix boundary: allegro.pl-oferta.top, gov-pl.info, pl.secure.xyz
    left = p.f_labels
    fake = [t for lab in left for t in tokens(lab) if t in FAKE_SUFFIX_TOKENS]
    if fake and p.suffix:
        _add(reasons, "fake_suffix", W["fake_suffix"], "Udawana końcówka domeny w nazwie",
             f"„{fake[0]}” przed prawdziwą końcówką .{p.suffix}")

    # ---- bait words
    all_tokens = [t for lab in left for t in alpha_tokens(lab)]
    seen_stems: set[str] = set()
    for t in all_tokens:
        for stem in BAIT_STEMS:
            if t.startswith(stem) and stem not in seen_stems and len(t) >= 4:
                seen_stems.add(stem)
                _add(reasons, "bait", W["bait"], "Słowo-przynęta", t)
                break
        for stem in GENERIC_STEMS:
            if t.startswith(stem) and stem not in seen_stems:
                seen_stems.add(stem)
                _add(reasons, "generic_bait", W["generic_bait"], "Słowo z phishingu kont (EN)", t)
                break
    _cap(reasons, "bait")
    _cap(reasons, "generic_bait")

    # ---- suffix prior, shape
    prior = TLD_PRIOR_BITS.get(p.tld, 0.0)
    if prior and "." not in p.suffix:
        _add(reasons, "tld_prior", min(prior, CAPS["tld_prior"]), "Końcówka często nadużywana", f".{p.tld}")
    hyphens = sum(lab.count("-") for lab in p.labels if not lab.startswith("xn--"))
    if hyphens >= 3:
        _add(reasons, "many_hyphens", W["many_hyphens"] + (0.5 if hyphens >= 5 else 0.0), "Dużo myślników",
             f"{hyphens} myślników")
    if len(p.labels) >= 3:
        _add(reasons, "deep_name", W["deep_name"], "Głęboko zagnieżdżona nazwa", f"{len(p.labels)} poziomów")
    if any(len(t) >= 5 and t.isdigit() for t in tokens(p.f_domain)):
        _add(reasons, "digit_run", W["digit_run"], "Generowana sekwencja cyfr", p.f_domain)
    letters = "".join(alpha_tokens(p.f_domain))
    if len(letters) >= 10 and shannon_bits(letters) >= 3.5 and not hits:
        _add(reasons, "random_label", W["random_label"], "Losowo wyglądająca nazwa",
             f"{shannon_bits(letters):.2f} bit/znak")

    # ---- live name-pattern family
    if families is not None and "#" in fam:
        n = families.family_size(fam)
        if n >= 3:
            _add(reasons, "family", W["family"] + (1.0 if n >= 10 else 0.0), "Rodzina nazw z listy CERT",
                 f"{fam}: {n} domen")
            _cap(reasons, "family")
    if families is not None:
        brand_tokens = {h.token for h in hits}
        for lab in p.f_sub_labels or []:
            if len(lab) < 3 or lab in brand_tokens or lab.replace("-", "") in brand_tokens:
                continue
            n = families.label_size(lab)
            if n >= 5:
                _add(reasons, "listed_label", W["listed_label"], "Etykieta znana z listy CERT",
                     f"{lab}: {n} domen")
        _cap(reasons, "listed_label")

    # ---- known campaign genes
    if campaigns is not None:
        _campaign_reasons(p, out, families, campaigns)

    out.bits = sum(r.bits for r in reasons)
    out.campaign_hints = sorted({r.campaign_id for r in reasons if r.campaign_id})
    return out


def _campaign_reasons(p: HostParts, out: NameScore, families: FamilyStats | None, campaigns: CampaignGenes) -> None:
    from hydra.enrich.domains import url_label_skeleton

    reasons = out.reasons
    for cid in campaigns.domain_campaigns(p.host, p.registrable)[:2]:
        _add(reasons, "campaign_domain", W["campaign_domain"], "Domena znana z kampanii", p.registrable, cid)
    if any(r.code == "campaign_domain" for r in reasons):
        return
    fam_cids = campaigns.family_campaigns(out.family)
    if fam_cids:
        _add(reasons, "campaign_family", W["campaign_family"], "Rodzina nazw jak w znanej kampanii",
             f"{out.family} ({len(fam_cids)} kampanii)", fam_cids[0])
    sk = url_label_skeleton(p.host)
    if sk:
        cids = campaigns.skeleton_campaigns(sk)
        if cids:
            from hydra.watch.link import shape_is_rare

            n_sk, n_all = families.skeleton_freq(sk) if families is not None else (0, 0)
            # rarity on the live stream: only shapes rarer than 1 in 2^8 names count; k bits of rarity -> k-4
            if families is None:
                bits = W["campaign_skeleton"]
            elif shape_is_rare(n_sk, n_all):
                bits = min(CAPS["campaign_skeleton"], math.log2(n_all / (n_sk + 1)) - 4.0)
            else:
                bits = 0.0
            if bits >= 0.5:
                _add(reasons, "campaign_skeleton", bits, "Szkielet nazwy jak w znanej kampanii",
                     f"{sk} ({n_sk}/{n_all} w strumieniu)", cids[0])
    toks = {t for lab in p.f_labels for t in alpha_tokens(lab)} | {lab.replace("-", "") for lab in p.f_labels}
    for t in sorted(toks):
        cids = campaigns.token_campaigns(t)
        if cids:
            _add(reasons, "campaign_token", W["campaign_token"], "Nazwa platformy/produktu ze znanej kampanii", t,
                 cids[0])
            break
    if not any(r.code == "campaign_token" for r in reasons):
        lab = p.f_domain
        if len(lab) >= 6:
            for lure, cids in campaigns.lure_labels().items():
                if lure != lab and osa_distance(lab, lure, 1) == 1:
                    _add(reasons, "campaign_domain_typo", W["campaign_domain_typo"],
                         "Literówka domeny ze znanej kampanii", f"{lab} ≈ {lure}", cids[0])
                    break
