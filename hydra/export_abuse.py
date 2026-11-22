"""Abuse / takedown evidence package per campaign: XARF v4 reports + Polish cover notes, as a ZIP. Never sent.

One XARF v4 report (category `content`, type `fraud` or `phishing`) per reported host, following the XARF v4
schemas (https://github.com/xarf/xarf-spec, MIT, v4.2.0 core + content-base + content-fraud/phishing; studied
2026-11-21, not vendored). `validate_xarf` checks the subset this module emits (required fields, formats, enums,
evidence items ≤ 5 MB with `sha256:` hashes); the full JSON-Schema validator is not in the lock.

Evidence per host: the landing screenshot / ad creative (images, original bytes), the saved landing HTML
(redacted copy: secrets in forms and URLs replaced, so its hash differs from the CAS original — both hashes are
stated), a HAR excerpt for that host (URL redacted, status, server IP, time), the RDAP snapshot summary with
`fetched_at`, and the CERT Polska list status. Abuse contacts come from the RDAP cache (`entities[abuse]` of the
domain and of the hosting network). Reporter identity comes from HYDRA_REPORTER_ORG / _EMAIL / _DOMAIN; the defaults
are placeholders the analyst must replace before sending manually.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import uuid
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from hydra.campaign_facts import (
    CampaignView,
    campaign_hosts,
    enricher,
    evidence_artifacts,
    load_campaign,
    rdap_domain,
    rdap_ip,
    summary_line,
)
from hydra.config import Settings
from hydra.db import connect
from hydra.redact import redact_obj, redact_text, redact_url
from hydra.storage import ContentStore

XARF_VERSION = "4.2.0"
MAX_EVIDENCE = 5 * 1024 * 1024
FRAUD_TYPE = {"investment": "investment", "fake_news_bait": "investment", "gambling": "other",
              "health_supplement": "shopping", "giveaway": "lottery", "crypto": "cryptocurrency"}
IMAGE_ROLES = ("landing_screenshot", "ad_creative", "feed_screenshot")
CATEGORIES = {"messaging", "content", "copyright", "connection", "vulnerability", "infrastructure", "reputation"}
FRAUD_TYPES = {"investment", "romance", "tech_support", "lottery", "advance_fee", "cryptocurrency", "shopping",
               "charity", "employment", "government_impersonation", "other"}
EVIDENCE_KEYS = {"content_type", "description", "payload", "hash", "size"}
CONTACT_KEYS = {"org", "contact", "domain"}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
_DOMAIN = re.compile(r"^([a-z0-9]+(-[a-z0-9]+)*\.)+[a-z]{2,}$")
_HASH = re.compile(r"^(md5|sha1|sha256|sha512):[a-fA-F0-9]+$")


def reporter() -> dict[str, str]:
    return {"org": os.environ.get("HYDRA_REPORTER_ORG", "UZUPEŁNIJ: organizacja zgłaszająca"),
            "contact": os.environ.get("HYDRA_REPORTER_EMAIL", "abuse-reports@example.invalid"),
            "domain": os.environ.get("HYDRA_REPORTER_DOMAIN", "example.invalid")}


def _ev(content_type: str, data: bytes, description: str) -> dict[str, Any]:
    return {"content_type": content_type, "description": description[:500],
            "payload": base64.b64encode(data).decode("ascii"),
            "hash": "sha256:" + hashlib.sha256(data).hexdigest(), "size": len(data)}


def _read(store: ContentStore, digest: str) -> bytes | None:
    """CAS bytes through the symlink-refusing reader (CR-ASTRA-SEC-6); a tampered entry is skipped."""
    try:
        return store.read_bytes(digest)
    except OSError:
        return None


def _host_matches(url: str, host: str) -> bool:
    h = (urlsplit(url).hostname or "").lower()
    return h == host or h.endswith("." + host)


def _har_rows(store: ContentStore, arts: list[dict], host: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for a in arts:
        if a["role"] != "har" or a["size"] > 64 * 1024 * 1024 or not store.exists(a["artifact_id"]):
            continue
        raw = _read(store, a["artifact_id"])
        try:
            har = json.loads(raw.decode("utf-8", errors="replace")) if raw is not None else None
        except ValueError:
            continue
        if not isinstance(har, dict):
            continue
        for e in (har.get("log") or {}).get("entries") or []:
            url = str((e.get("request") or {}).get("url", ""))
            if _host_matches(url, host):
                rows.append({"startedDateTime": e.get("startedDateTime"), "method": (e.get("request") or {}).get(
                    "method"), "url": redact_url(url), "status": (e.get("response") or {}).get("status"),
                    "serverIPAddress": e.get("serverIPAddress"), "har_artifact_sha256": a["artifact_id"]})
            if len(rows) >= 200:
                break
    return rows


def _har_excerpt(store: ContentStore, arts: list[dict], host: str) -> bytes | None:
    rows = _har_rows(store, arts, host)
    return json.dumps({"host": host, "entries": rows}, ensure_ascii=False, indent=1).encode() if rows else None


_SAVED_FROM = re.compile(r"<!--\s*saved from url=\(\d+\)(https?://[^\s>]+?)\s*-->", re.I)


def observed_urls(store: ContentStore, arts: list[dict], host: dict[str, Any],
                  har_rows: list[dict[str, Any]] | None = None) -> list[str]:
    """Observed URLs on one reported host, redacted, in order: URL genes with a scheme, HAR request URLs, the
    `saved from url=` comment of saved landing pages (CR-E2E-3: the normalised `entry_url` gene has no scheme,
    so the XARF `url` must come from what was actually observed, never from an invented `http://host/`)."""
    out: list[str] = list(host["urls"])
    rows = har_rows if har_rows is not None else _har_rows(store, arts, host["host"])
    out += [str(r["url"]) for r in rows if str(r.get("url", "")).startswith(("http://", "https://"))]
    for a in [x for x in arts if x["role"] == "landing_html" and x["size"] <= MAX_EVIDENCE][:5]:
        page = _read(store, a["artifact_id"])
        m = _SAVED_FROM.search(page[:4096].decode("utf-8", errors="replace")) if page is not None else None
        if m and _host_matches(m.group(1), host["host"]):
            out.append(redact_url(m.group(1)))
    return list(dict.fromkeys(out))


def build_report(view: CampaignView, host: dict[str, Any], arts: list[dict], store: ContentStore, enr,
                 now: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(XARF report, side info: abuse contacts, hoster, cert status, evidence list)."""
    from hydra.enrich import cert_pl

    dom = host["domain"]
    rd = rdap_domain(enr, dom)
    nets = [rdap_ip(enr, ip) for ip in host["ips"][:3]]
    abuse = rd["abuse"] + [c for n in nets for c in n["abuse"]]
    hoster = next((n["name"] for n in nets if n.get("name")), None)
    cert = cert_pl.lookup(host["host"]) if cert_pl.available() else {"available": False}
    inc_ids = set(host["incidents"])
    mine = [a for a in arts if a["incident_id"] in inc_ids]
    evidence: list[dict[str, Any]] = []
    for a in [x for x in mine if x["role"] in IMAGE_ROLES and x["mime"].startswith("image/")][:3]:
        if a["size"] <= MAX_EVIDENCE and (img := _read(store, a["artifact_id"])) is not None:
            evidence.append(_ev(a["mime"], img,
                                f"{a['role']} incydentu {a['incident_id']} (oryginał sha256:{a['artifact_id']})"))
    for a in [x for x in mine if x["role"] == "landing_html" and x["mime"] == "text/html"][:1]:
        if a["size"] <= MAX_EVIDENCE and (page := _read(store, a["artifact_id"])) is not None:
            html = redact_text(page.decode("utf-8", errors="replace"))
            evidence.append(_ev("text/html", (html or "").encode(), f"Zapisana strona docelowa (kopia z redakcją"
                                f" sekretów; oryginał w CAS sha256:{a['artifact_id']})"))
    har_rows = _har_rows(store, mine, host["host"])
    har = json.dumps({"host": host["host"], "entries": har_rows}, ensure_ascii=False, indent=1).encode() \
        if har_rows else None
    if har and len(har) <= MAX_EVIDENCE:
        evidence.append(_ev("application/json", har, f"Fragment logu HAR dla {host['host']} (URL z redakcją)"))
    snap = {"domain": dom, "rdap": {k: rd[k] for k in ("status", "fetched_at")} | {"summary": rd["summary"]},
            "hosting_networks": [{k: n[k] for k in ("name", "country", "fetched_at", "status")} for n in nets],
            "cert_pl": cert}
    evidence.append(_ev("application/json", json.dumps(redact_obj(snap), ensure_ascii=False, indent=1).encode(),
                        "Migawka RDAP/sieci hostingu i status listy CERT Polska (z datą pobrania)"))
    scheme = view.campaign.scheme or "other"
    rtype = "phishing" if scheme == "phishing" else "fraud"
    seen = observed_urls(store, mine, host, har_rows)
    first_url = seen[0] if seen else f"http://{host['host']}/"  # fallback only when nothing was observed
    rep: dict[str, Any] = {
        "xarf_version": XARF_VERSION, "report_id": str(uuid.uuid4()), "timestamp": now,
        "reporter": reporter(), "sender": reporter(), "source_identifier": host["host"],
        "category": "content", "type": rtype, "evidence_source": "automated_analysis",
        "url": first_url, "description": summary_line(view)[:1000],
        "tags": [f"campaign:{view.campaign.id}", f"scheme:{scheme}", f"incidents:{len(inc_ids)}"],
        "evidence": evidence,
    }
    if _DOMAIN.match(host["host"]):
        rep["domain"] = host["host"]
    if rd["summary"].get("registrar"):
        rep["registrar"] = str(rd["summary"]["registrar"])
    if rd["summary"].get("nameservers"):
        rep["nameservers"] = list(rd["summary"]["nameservers"])
    if hoster:
        rep["hosting_provider"] = hoster
    if rtype == "fraud":
        rep["fraud_type"] = FRAUD_TYPE.get(scheme, "other")
    side = {"host": host["host"], "domain": dom, "abuse_contacts": abuse, "hosting_provider": hoster,
            "cert_pl": cert, "rdap_fetched_at": rd["fetched_at"], "incidents": sorted(inc_ids),
            "evidence": [{"content_type": e["content_type"], "hash": e["hash"], "size": e["size"],
                          "description": e["description"]} for e in evidence]}
    return rep, side


_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(\.\d{1,9})?([Zz]|[+-]\d{2}:\d{2})$")


def validate_xarf(rep: dict[str, Any]) -> list[str]:
    """Errors against the XARF v4 subset emitted here (core + content-base + content-fraud/phishing)."""
    err: list[str] = []
    for k in ("xarf_version", "report_id", "timestamp", "reporter", "sender", "source_identifier", "category",
              "type"):
        if k not in rep:
            err.append(f"missing {k}")
    if not re.match(r"^4\.[0-9]+\.[0-9]+$", str(rep.get("xarf_version", ""))):
        err.append("xarf_version")
    try:
        uuid.UUID(str(rep.get("report_id")))
    except ValueError:
        err.append("report_id not uuid")
    ts = rep.get("timestamp")
    try:
        # RFC 3339 date-time: a date alone or a naive time is not valid (CR-ASTRA-SEC-17).
        if not isinstance(ts, str) or not _DATETIME.match(ts):
            raise ValueError(ts)
        datetime.fromisoformat(ts.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        err.append("timestamp not date-time")
    for who in ("reporter", "sender"):
        c = rep.get(who) or {}
        if set(c) != CONTACT_KEYS or not _EMAIL.match(str(c.get("contact"))) or not _HOST.match(str(c.get(
                "domain"))) or len(str(c.get("org"))) > 200:
            err.append(f"{who} contact_info")
    if rep.get("category") not in CATEGORIES:
        err.append("category")
    if rep.get("category") == "content" and not str(rep.get("url", "")).startswith(("http://", "https://")):
        err.append("url")
    if "domain" in rep and not (isinstance(rep["domain"], str) and _DOMAIN.match(rep["domain"])):
        err.append("domain pattern")
    if rep.get("type") == "fraud" and rep.get("fraud_type") not in FRAUD_TYPES:
        err.append("fraud_type")
    evidence = rep.get("evidence") or []
    if not isinstance(evidence, list):
        return [*err, "evidence not a list"]
    for i, e in enumerate(evidence):
        # Types are checked before any conversion: malformed input yields errors, never an exception.
        if not isinstance(e, dict):
            err.append(f"evidence[{i}] not an object")
            continue
        if not {"content_type", "payload"} <= set(e) or set(e) - EVIDENCE_KEYS:
            err.append(f"evidence[{i}] keys")
        if not isinstance(e.get("content_type", ""), str):
            err.append(f"evidence[{i}] content_type")
        h = e.get("hash")
        if "hash" in e and (not isinstance(h, str) or not _HASH.match(h)):
            err.append(f"evidence[{i}] hash")
            h = None
        size = e.get("size", 0)
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            err.append(f"evidence[{i}] size not a non-negative integer")
        elif size > MAX_EVIDENCE:
            err.append(f"evidence[{i}] size")
        payload = e.get("payload", "")
        if not isinstance(payload, str):
            err.append(f"evidence[{i}] payload not base64")
            continue
        try:
            raw = base64.b64decode(payload, validate=True)
            if isinstance(h, str) and h.startswith("sha256:") and hashlib.sha256(raw).hexdigest() != h[7:]:
                err.append(f"evidence[{i}] hash mismatch")
        except ValueError:
            err.append(f"evidence[{i}] payload not base64")
    return err


def cover_note_pl(view: CampaignView, side: dict[str, Any], report_file: str) -> str:
    to = ", ".join(c["email"] for c in side["abuse_contacts"]) or "BRAK — kontakt abuse nieznany (RDAP)"
    cert = side["cert_pl"]
    cert_line = ("na liście ostrzeżeń CERT Polska od " + str(cert.get("insert_date"))) if cert.get("host_listed") \
        else "nie figuruje na liście ostrzeżeń CERT Polska (stan migawki)"
    return redact_text(f"""Do: {to}
Temat: Zgłoszenie nadużycia – {side['host']} (kampania {view.campaign.id})

Dzień dobry,

zgłaszamy domenę {side['host']} (domena rejestrowalna {side['domain']}), wykorzystywaną w kampanii oszukańczych
reklam i stron docelowych. {summary_line(view)}. Host występuje w {len(side['incidents'])} incydentach; domena
{cert_line}. Dostawca hostingu (RDAP sieci): {side['hosting_provider'] or 'nieznany'}.

W załączeniu raport XARF v4 ({report_file}) z dowodami: zrzuty ekranu, zapis strony (z redakcją sekretów), fragment
logu HAR oraz migawka RDAP z datą pobrania ({side['rdap_fetched_at'] or 'brak'}). Sumy SHA-256 dowodów są w raporcie.

Prosimy o weryfikację i zablokowanie treści zgodnie z Państwa zasadami.

Z poważaniem
{reporter()['org']} ({reporter()['contact']})

---
EN: Abuse report for {side['host']} (fraud campaign {view.campaign.id}); XARF v4 report with evidence attached.

[Pakiet przygotowany automatycznie przez HYDRA NEXUS; nie został nigdzie wysłany. Analityk weryfikuje treść,
uzupełnia dane zgłaszającego i wysyła ręcznie.]
""")


def abuse_package(settings: Settings, campaign_id: str, run_id: str | None = None) -> tuple[bytes, dict[str, Any]]:
    """ZIP bytes + manifest. Reports, cover notes and the manifest; never sent anywhere."""
    with closing(connect(settings.db_path, readonly=True)) as conn:
        view = load_campaign(conn, campaign_id, run_id)
        if view is None:
            raise KeyError(f"campaign {campaign_id} not found")
        arts = evidence_artifacts(conn, view.campaign.members)
    store = ContentStore(settings.cas_dir)
    enr = enricher(settings)
    now = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    hosts = campaign_hosts(view)
    buf = io.BytesIO()
    entries = []
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for h in hosts:
            rep, side = build_report(view, h, arts, store, enr, now)
            errors = validate_xarf(rep)
            name = re.sub(r"[^a-z0-9.-]", "_", h["host"])
            rfile, nfile = f"reports/{name}.xarf.json", f"notes/{name}.txt"
            data = json.dumps(rep, ensure_ascii=False, indent=1).encode()
            zf.writestr(rfile, data)
            zf.writestr(nfile, cover_note_pl(view, side, rfile))
            entries.append({"host": h["host"], "domain": h["domain"], "report": rfile, "note": nfile,
                            "sha256": hashlib.sha256(data).hexdigest(), "xarf_valid": not errors, "errors": errors,
                            "abuse_contacts": side["abuse_contacts"], "hosting_provider": side["hosting_provider"],
                            "cert_pl_listed": bool(side["cert_pl"].get("host_listed")),
                            "incidents": side["incidents"], "evidence": side["evidence"]})
        with_contact = sum(1 for e in entries if e["abuse_contacts"])
        manifest = {
            "kind": "hydra-abuse-package", "created_at": now, "campaign_id": view.campaign.id, "run_id": view.run_id,
            "campaign": summary_line(view), "reports": entries, "hosts": len(entries),
            "hosts_with_abuse_contact": with_contact,
            "abuse_contact_coverage": round(with_contact / len(entries), 3) if entries else None,
            "xarf_valid": sum(1 for e in entries if e["xarf_valid"]), "xarf_version": XARF_VERSION,
            "sent": False,
            "note_pl": "Pakiet nie został wysłany. Wysyłka wyłącznie ręczna, po weryfikacji przez analityka.",
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
        zf.writestr("CZYTAJ_MNIE.txt", "Pakiet dowodowy HYDRA NEXUS (XARF v4). Nic nie zostało wysłane"
                    " automatycznie. Uzupełnij dane zgłaszającego (HYDRA_REPORTER_*) i wyślij ręcznie.\n")
    return buf.getvalue(), manifest
