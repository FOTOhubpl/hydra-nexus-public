"""Graph `ArchivedAd` row -> normalised, redacted content and its content hash.

The hash (`normalized_content_sha256`) covers what makes a creative: page id and name, the per-card text arrays
(bodies, link titles, captions, descriptions, kept as arrays in source order: cards are never re-paired), landing
URLs with their provenance and the digests of acquired media. Observation times, the snapshot URL (it embeds a
token), delivery dates, platforms, languages and EU transparency values are metadata: a change there never creates a
revision and never triggers another extraction.

A link caption is display text chosen by the advertiser ("EXAMPLE.COM"), not a confirmed destination: it is kept as
`display_domains` with provenance `link_caption`, never as a landing URL. Landing URLs come from URLs written in the
ad text (`url_in_text`), a confirmed link in an opt-in snapshot (`snapshot_link`) or an analyst attachment.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from hydra.meta import contract
from hydra.redact import redact_text, redact_url

URL_RE = re.compile(r"\bhttps?://[^\s<>\"'()]{3,300}", re.I)
DOMAIN_RE = re.compile(r"\b((?:[a-z0-9-]{1,63}\.)+[a-z]{2,24})\b", re.I)
TEXT_LISTS = ("ad_creative_bodies", "ad_creative_link_titles", "ad_creative_link_captions",
              "ad_creative_link_descriptions")


def _lst(v: Any) -> list[str]:
    if v is None:
        return []
    vals = v if isinstance(v, list) else [v]
    return [redact_text(str(x)) or "" for x in vals if x is not None][:50]


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFC", s)
    return " ".join(s.split())


def landing_urls(bodies: list[str], titles: list[str], descriptions: list[str]) -> list[dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for field_name, texts in (("ad_creative_bodies", bodies), ("ad_creative_link_titles", titles),
                              ("ad_creative_link_descriptions", descriptions)):
        for t in texts:
            for m in URL_RE.findall(t or ""):
                u = redact_url(m.rstrip(".,;:!?")) or ""
                if u and u not in out:
                    out[u] = {"url": u, "provenance": "url_in_text", "field": field_name}
    return list(out.values())[:20]


def display_domains(captions: list[str]) -> list[dict[str, str]]:
    out = []
    for c in captions:
        for d in DOMAIN_RE.findall(c or ""):
            out.append({"domain": d.lower(), "provenance": "link_caption",
                        "note_pl": "Podpis linku wybrany przez reklamodawcę; nie jest potwierdzonym adresem "
                                   "docelowym."})
    seen, uniq = set(), []
    for d in out:
        if d["domain"] not in seen:
            seen.add(d["domain"])
            uniq.append(d)
    return uniq[:20]


def optional_values(row: dict[str, Any], observed_at: str, countries: list[str],
                    requested: list[str]) -> dict[str, Any]:
    """EU transparency fields: present -> value with declared/estimate flags and scope; requested but absent ->
    `unknown` with a reason; not requested (unsupported for this token/version) -> `not_requested`. A missing
    reach is never 0."""
    out: dict[str, Any] = {}
    for f in contract.OPTIONAL_FIELDS:
        if f in row and row[f] is not None:
            out[f] = {"status": "available", "value": _cap(row[f]), "declared_by_advertiser": f in
                      contract.DECLARED_FIELDS, "estimate": f in contract.ESTIMATE_FIELDS,
                      "scope": {"countries_requested": countries, "region": "EU" if f.startswith("eu_") else None},
                      "observed_at": observed_at}
        elif f in requested:
            out[f] = {"status": "unknown", "reason_pl": "Pole żądane, ale nieobecne w odpowiedzi dla tej reklamy."}
        else:
            out[f] = {"status": "not_requested",
                      "reason_pl": "Pole nieżądane: diagnostyka nie potwierdziła dostępności dla tego tokenu i "
                                   "wersji API."}
    return out


def _cap(v: Any, depth: int = 0) -> Any:
    if depth > 4:
        return None
    if isinstance(v, dict):
        return {str(k)[:60]: _cap(x, depth + 1) for k, x in list(v.items())[:30]}
    if isinstance(v, list):
        return [_cap(x, depth + 1) for x in v[:30]]
    if isinstance(v, str):
        return (redact_text(v) or "")[:300]
    return v


def normalise(row: dict[str, Any], *, observed_at: str, countries: list[str], graph_version: str | None = None,
              requested_optional: list[str] | None = None, media_digests: list[str] | None = None) -> dict[str, Any]:
    """One Graph row -> {"creative": hashed part, "meta": volatile part}. `id` is always a string."""
    ad_id = str(row.get("id") or "").strip()
    bodies, titles = _lst(row.get("ad_creative_bodies")), _lst(row.get("ad_creative_link_titles"))
    captions, descs = _lst(row.get("ad_creative_link_captions")), _lst(row.get("ad_creative_link_descriptions"))
    page_id = row.get("page_id")
    creative = {
        "source_ad_id": ad_id,
        "source_page_id": str(page_id) if page_id not in (None, "") else None,
        "source_page_name": redact_text(str(row["page_name"])) if row.get("page_name") else None,
        "creative_bodies": bodies,
        "creative_link_titles": titles,
        "creative_link_captions": captions,
        "creative_link_descriptions": descs,
        "landing_urls": landing_urls(bodies, titles, descs),
        "media_digests": sorted(set(media_digests or [])),
    }
    missing = [f for f in contract.REQUIRED_FIELDS if f not in row]
    meta = {
        "source_delivery_start": row.get("ad_delivery_start_time"),
        "source_delivery_stop": row.get("ad_delivery_stop_time"),
        "ad_creation_time": row.get("ad_creation_time"),
        "publisher_platforms": _lst(row.get("publisher_platforms")),
        "languages": _lst(row.get("languages")),
        "snapshot_url_redacted": redact_url(str(row["ad_snapshot_url"])) if row.get("ad_snapshot_url") else None,
        "display_domains": display_domains(captions),
        "countries_requested": list(countries),
        "graph_api_version": graph_version,
        "eu_transparency": optional_values(row, observed_at, countries, list(requested_optional or [])),
        "missing_fields": missing,
        "library_link": contract.library_link(ad_id) if ad_id else None,
    }
    return {"creative": creative, "meta": meta}


def content_sha256(creative: dict[str, Any]) -> str:
    def norm(v: Any) -> Any:
        if isinstance(v, str):
            return _fold(v)
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, dict):
            return {k: norm(x) for k, x in sorted(v.items())}
        return v

    blob = json.dumps(norm(creative), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def text_of(creative: dict[str, Any]) -> str:
    parts = [*creative.get("creative_bodies", []), *creative.get("creative_link_titles", []),
             *creative.get("creative_link_descriptions", []), *creative.get("creative_link_captions", [])]
    return "\n".join(p for p in parts if p).strip()


def hosts(creative: dict[str, Any], meta: dict[str, Any] | None = None) -> list[str]:
    from urllib.parse import urlsplit

    out = []
    for u in creative.get("landing_urls") or []:
        h = (urlsplit(u.get("url") or "").hostname or "").lower()
        if h:
            out.append(h)
    for d in (meta or {}).get("display_domains") or []:
        out.append(d["domain"])
    return list(dict.fromkeys(out))
