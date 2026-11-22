"""G13 lure-domain facts: a media/bank brand mark outside the brand's own domains, and registration windows.

Own implementation from docs/research/GAPS_PAPERS.md §12 (reference-based brand/domain inconsistency, P31/P34;
registration-time patterns, P25/P26). No third-party code, no popularity list.

- `brand_offdomain` (value = brand name from assets/brands.json): a media or bank brand is marked in the page title,
  og:site_name or masthead, and the landing host is neither one of the brand's official domains nor a subdomain of
  one. Polish note "znak TVN24 na domenie spoza TVN24". An explanation fact: group `inf:boilerplate:brand_offdomain`
  (0 bits), because every fake TVN24 page shares it and it says nothing about one kit or operator (CR-VP-G3 asks
  for a style-kit level instead). Landing host unknown: status `unavailable`.
- `registrar_window` (value = `<registrar>|<offset>:<bucket>`): registration date in 3-day buckets on three grids
  offset by 0, 1 and 2 days. Two registrations of one registrar share at least one value exactly when they are at
  most 2 days apart (3 values on the same day, 2 at one day, 1 at two days). Small registrars: operator tier in the
  `inf:operator:registration` budget (shared with `registrar_day`); large retail registrars (`LARGE_REGISTRARS`):
  boilerplate, 0 bits. The ±2-day window and the registrar list are assumptions, not measured on NASK data.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

from hydra.contracts import GeneStatus

OFFDOMAIN_KINDS = ("media", "bank")
OFFDOMAIN_GROUP = "inf:boilerplate:brand_offdomain"
WINDOW_DAYS = 3
WINDOW_GROUP_SMALL = "inf:operator:registration"
WINDOW_GROUP_LARGE = "inf:boilerplate:registration_window"
# Large retail registrars / registrar groups (own short list): thousands of unrelated customers register there
# every day, so "same registrar, same days" is not operator evidence.
LARGE_REGISTRARS = re.compile(
    r"godaddy|namecheap|tucows|\benom\b|network solutions|web\.com|register\.com|publicdomainregistry|"
    r"\bpdr\b|squarespace|google|cloudflare|amazon|ionos|1&1|\bovh|hostinger|namesilo|dynadot|porkbun|gandi|"
    r"alibaba|hichina|\bgmo\b|key-systems|home\.pl|nazwa\.pl|cyber_?folks|aftermarket",
    re.I,
)


@lru_cache(maxsize=1)
def brand_table() -> list[tuple[str, str, re.Pattern, tuple[str, ...]]]:
    d = json.loads((Path(__file__).parent / "assets" / "brands.json").read_text(encoding="utf-8"))
    out = []
    for name, v in d["brands"].items():
        if v.get("kind") in OFFDOMAIN_KINDS and v.get("domains"):
            rx = re.compile(r"(?<![a-z0-9])(?:" + "|".join(v["patterns"]) + r")", re.I)
            out.append((name, v["kind"], rx, tuple(x.lower() for x in v["domains"])))
    return out


def brand_marks(text: str) -> list[str]:
    low = (text or "").lower().replace("_", " ").replace("-", " ")
    return [name for name, _, rx, _ in brand_table() if rx.search(low)]


def on_official_domain(host: str, domains: tuple[str, ...]) -> bool:
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in domains)


def emit_brand_offdomain(sink, zones: dict[str, str], host: str | None, aid: str | None) -> None:
    """`zones`: zone name -> text (title, og, masthead). `sink`: webparse.GeneSink."""
    seen: set[str] = set()
    table = {name: doms for name, _, _, doms in brand_table()}
    for zone, text in zones.items():
        for brand in brand_marks(text):
            if brand in seen:
                continue
            seen.add(brand)
            doms = table[brand]
            if not host:
                sink.add(
                    "brand_offdomain",
                    None,
                    OFFDOMAIN_GROUP,
                    artifact_id=aid,
                    dom_path=zone,
                    status=GeneStatus.UNAVAILABLE,
                    note=f"znak {brand}: domena strony docelowej nieznana",
                )
                continue
            if on_official_domain(host, doms):
                continue
            sink.add(
                "brand_offdomain",
                brand,
                OFFDOMAIN_GROUP,
                artifact_id=aid,
                dom_path=zone,
                quality=0.8,
                note=f"znak {brand} na domenie spoza {brand} ({host}; oficjalne: {', '.join(doms)})",
            )


def registrar_scale(registrar: str) -> str:
    return "large" if LARGE_REGISTRARS.search(registrar or "") else "small"


def _day(d: date | datetime) -> int:
    return (d.date() if isinstance(d, datetime) else d).toordinal()


def registrar_window_values(registrar: str, reg: date | datetime) -> list[str]:
    r = re.sub(r"\s+", " ", registrar.strip().lower())[:100]
    n = _day(reg)
    return [f"{r}|{o}:{(n + o) // WINDOW_DAYS}" for o in range(WINDOW_DAYS)]


def emit_registrar_window(sink, registrar: str, reg: date | datetime, *, domain: str, key: str | None) -> None:
    scale = registrar_scale(registrar)
    group = WINDOW_GROUP_LARGE if scale == "large" else WINDOW_GROUP_SMALL
    day = (reg.date() if isinstance(reg, datetime) else reg).isoformat()
    note = f"{domain}: {registrar} {day}, okno ±2 dni ({'duży' if scale == 'large' else 'mały'} rejestrator)"
    for v in registrar_window_values(registrar, reg):
        sink.add("registrar_window", v, group, enrichment_key=key, note=note, quality=0.1 if scale == "large" else 0.6)
