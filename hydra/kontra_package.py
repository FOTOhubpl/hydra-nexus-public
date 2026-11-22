"""KONTRA evidence package: one versioned, redacted bundle per decision (JSON + Markdown + static HTML), and the
delivery to the LOCAL demonstration receiver (`hydra.kontra_receiver`, loopback only).

The package states facts and assessments separately: "materiał podejrzany, wspólne ślady z kampanią X, do
weryfikacji", never "udowodniony sprawca". `package_id` is the sha256 of the canonical content (without the
creation time), so the same experiment yields the same package and the receiver deduplicates it.
Packages live under `<state>/kontra/packages/<package_id>/` (state, never git).
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from hydra import __version__
from hydra.config import Settings
from hydra.events import canonical_json, utcnow
from hydra.export_safe import safe_filename, strip_controls
from hydra.kontra import CLASS_PL, DECISION_PL, valid_id
from hydra.netguard import literal_ip
from hydra.redact import redact_obj

PACKAGE_SCHEMA = "hydra.kontra.package.v1"
DEFAULT_RECEIVER = "http://127.0.0.1:8765/receive"
DISCLAIMER_PL = ("Pakiet laboratorium hipotez (tryb shadow): scenariusze są symulacją na kopii danych i nie zmieniają"
                 " decyzji produktu. Bity są miarą siły dowodów, nie prawdopodobieństwem. Zgłoszenie oznacza materiał"
                 " podejrzany ze wspólnymi śladami, do weryfikacji, a nie udowodnionego sprawcę.")


def _assessment_pl(b: dict[str, Any]) -> str:
    if b["decision"] == "assign":
        return f"Materiał podejrzany, wspólne ślady z kampanią {b['campaign_id']}, do weryfikacji."
    if b["decision"] == "abstain":
        return "Materiał podejrzany; dowody niejednoznaczne, wymaga decyzji analityka."
    if b.get("group_id"):
        return f"Materiał podejrzany; nowy wzorzec ze wspólnym zestawem narzędzi grupy {b['group_id']}, do weryfikacji."
    return "Materiał podejrzany; nowy wzorzec bez wystarczającej podstawy do przypisania kampanii."


def build_package(exp: dict[str, Any]) -> dict[str, Any]:
    """The package content (redacted). Facts (evidence pointers, scenario results) and the assessment are
    separate fields."""
    b = exp["baseline"]
    body = {
        "schema_version": PACKAGE_SCHEMA,
        "manifest": {"product": "hydra-nexus", "product_version": __version__,
                     "product_commit": exp["context"].get("product_commit"),
                     "experiment_id": exp["experiment_id"], "experiment_schema": exp["schema_version"],
                     "incident_id": exp["incident_id"], "base_run": exp["context"]["base_run"],
                     "fingerprint": exp["context"]["fingerprint"],
                     "corpus_signature": exp["context"]["corpus_signature"], "scorer": exp["context"]["scorer"],
                     "query_genes_sha256": exp["context"].get("query_genes_sha256"), "mode": exp["mode"],
                     "frozen_identity": (exp["context"].get("frozen") or {}).get("identity"),
                     "redaction": "hydra.redact.redact_obj + export_safe.strip_controls",
                     "recipient": "lokalny odbiornik demonstracyjny (loopback)", "attachments": [],
                     "review_status": "nie przejrzano (pakiet nie zastępuje decyzji analityka)"},
        "assessment_pl": _assessment_pl(b),
        "decision": {k: b.get(k) for k in ("decision", "campaign_id", "group_id", "reason_pl", "support_bits",
                                           "link_threshold", "margin_bits", "link_rule_met", "winner")},
        "hypotheses": exp["hypotheses"],
        "counterfactuals": [{k: r.get(k) for k in ("scenario_id", "class", "label_pl", "decision", "campaign_id",
                                                    "support_bits", "link_threshold", "margin_bits",
                                                    "decision_changed", "campaign_changed", "removed")}
                            for r in exp["scenarios"]],
        "flip_set": exp.get("flip_set"),
        "next_observations": exp.get("next_observations"),
        "retrieval_scope": exp.get("retrieval_scope"),
        "limits": exp.get("limits"),
        "disclaimer_pl": DISCLAIMER_PL,
    }
    body = redact_obj(body)
    digest = hashlib.sha256(canonical_json(body).encode()).hexdigest()
    return {"package_id": "kp-" + digest[:24], "content_sha256": digest, "created_at": utcnow(), **body}


def _e(x: Any) -> str:
    return html.escape(strip_controls("" if x is None else str(x)))


def _bits(x: Any) -> str:
    return "—" if x is None else f"{float(x):.1f} bit".replace(".", ",")


def render_markdown(p: dict[str, Any]) -> str:
    d, m = p["decision"], p["manifest"]

    def c(x: Any) -> str:  # one Markdown table cell: no pipes, no line breaks
        return strip_controls("" if x is None else str(x)).replace("|", "/").replace("\n", " ")

    out = [f"# Pakiet dowodowy {p['package_id']}", "", f"**Ocena:** {c(p['assessment_pl'])}", "",
           f"_{c(p['disclaimer_pl'])}_", "", "## Decyzja (wynik analizy)", "",
           f"- decyzja: {DECISION_PL.get(d['decision'], d['decision'])}; kampania: {c(d['campaign_id'])};"
           f" grupa: {c(d['group_id'])}",
           f"- wsparcie {_bits(d['support_bits'])} wobec progu {_bits(d['link_threshold'])}; margines"
           f" {_bits(d['margin_bits'])}", f"- uzasadnienie: {c(d['reason_pl'])}", "", "## Hipotezy", ""]
    for h in p["hypotheses"]:
        out.append(f"### {c(h['label_pl'])}{' (wiodąca)' if h.get('leading') else ''}")
        out.append(f"wsparcie {_bits(h.get('support_bits'))}; kit {_bits(h.get('kit_bits'))}; {c(h.get('note_pl'))}")
        for r in h.get("reasons") or []:
            ev = r.get("evidence") or {}
            out.append(f"- {c(r['explanation_pl'])} ({_bits(r['bits'])}; rodzina {c(r['family'])}; artefakt"
                       f" {c(ev.get('artifact_id'))})")
        out.append("")
    out += ["## Scenariusze kontrfaktyczne (symulacja)", "",
            "| scenariusz | klasa | decyzja | kampania | wsparcie | zmiana |", "|---|---|---|---|---|---|"]
    for r in p["counterfactuals"]:
        out.append(f"| {c(r['label_pl'])} | {CLASS_PL.get(r['class'], r['class'])} | {c(r['decision'])} |"
                   f" {c(r['campaign_id'])} | {_bits(r['support_bits'])} |"
                   f" {'tak' if r['decision_changed'] or r['campaign_changed'] else 'nie'} |")
    fs = p.get("flip_set") or {}
    out += ["", "## Zestaw zmieniający decyzję", "",
            (f"- {', '.join(c(x) for x in fs.get('labels_pl') or [])} → {c(fs.get('decision'))}"
             if fs.get("found") else "- nie znaleziono w budżecie przeliczeń"),
            f"- {c(fs.get('note_pl'))}", "", "## Co by rozstrzygnęło (ranking heurystyczny)", ""]
    for r in (p.get("next_observations") or {}).get("ranking") or []:
        out.append(f"- {c(r['label_pl'])} (koszt {c(r['cost'])}, priorytet {r['priority']}): {c(r['why_pl'])}")
    out += ["", "## Zakres wyszukiwania", "", "```", json.dumps(p.get("retrieval_scope"), ensure_ascii=False,
                                                                  indent=1), "```", "",
            "## Manifest", "", "```", json.dumps(m, ensure_ascii=False, indent=1), "```",
            "", f"content_sha256: {p['content_sha256']}", ""]
    return "\n".join(out)


def render_html(p: dict[str, Any]) -> str:
    """Static HTML: every value escaped, no script, no external resource (CSP blocks everything but inline style)."""
    d = p["decision"]
    rows = "".join(
        f"<tr><td>{_e(r['label_pl'])}</td><td>{_e(CLASS_PL.get(r['class'], r['class']))}</td><td>{_e(r['decision'])}"
        f"</td><td>{_e(r['campaign_id'])}</td><td>{_e(_bits(r['support_bits']))}</td>"
        f"<td>{'tak' if r['decision_changed'] or r['campaign_changed'] else 'nie'}</td></tr>"
        for r in p["counterfactuals"])
    hyps = "".join(
        f"<section><h3>{_e(h['label_pl'])}{' (wiodąca)' if h.get('leading') else ''}</h3><p>wsparcie"
        f" {_e(_bits(h.get('support_bits')))}; {_e(h.get('note_pl'))}</p><ul>"
        + "".join(f"<li>{_e(r['explanation_pl'])} ({_e(_bits(r['bits']))}; artefakt"
                  f" {_e((r.get('evidence') or {}).get('artifact_id'))})</li>" for r in h.get("reasons") or [])
        + "</ul></section>" for h in p["hypotheses"])
    nxt = "".join(f"<li>{_e(r['label_pl'])} (koszt {_e(r['cost'])}): {_e(r['why_pl'])}</li>"
                  for r in (p.get("next_observations") or {}).get("ranking") or [])
    fs = p.get("flip_set") or {}
    flip = (_e(", ".join(fs.get("labels_pl") or [])) + " → " + _e(fs.get("decision"))) if fs.get("found") else \
        "nie znaleziono w budżecie przeliczeń"
    return ("<!doctype html><html lang=\"pl\"><head><meta charset=\"utf-8\">"
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            f"<title>Pakiet {_e(p['package_id'])}</title><style>body{{font-family:sans-serif;max-width:60rem;"
            "margin:2rem auto}}table{{border-collapse:collapse}}td,th{{border:1px solid #999;padding:.2rem .4rem}}"
            "</style></head><body>"
            f"<h1>Pakiet dowodowy {_e(p['package_id'])}</h1><p><strong>Ocena:</strong> {_e(p['assessment_pl'])}</p>"
            f"<p><em>{_e(p['disclaimer_pl'])}</em></p><h2>Decyzja</h2><p>{_e(DECISION_PL.get(d['decision']))};"
            f" kampania {_e(d['campaign_id'])}; wsparcie {_e(_bits(d['support_bits']))} wobec progu"
            f" {_e(_bits(d['link_threshold']))}</p><p>{_e(d['reason_pl'])}</p><h2>Hipotezy</h2>{hyps}"
            "<h2>Scenariusze kontrfaktyczne (symulacja)</h2><table><tr><th>scenariusz</th><th>klasa</th>"
            f"<th>decyzja</th><th>kampania</th><th>wsparcie</th><th>zmiana</th></tr>{rows}</table>"
            f"<h2>Zestaw zmieniający decyzję</h2><p>{flip}</p><p>{_e(fs.get('note_pl'))}</p>"
            f"<h2>Co by rozstrzygnęło (ranking heurystyczny)</h2><ul>{nxt}</ul><h2>Manifest</h2>"
            f"<pre>{_e(json.dumps(p['manifest'], ensure_ascii=False, indent=1))}</pre>"
            f"<p>content_sha256: {_e(p['content_sha256'])}</p></body></html>")


def package_dir(settings: Settings, package_id: str) -> Path:
    return Path(settings.state_dir) / "kontra" / "packages" / safe_filename(package_id, "package")


def save_package(settings: Settings, exp: dict[str, Any]) -> dict[str, Any]:
    p = build_package(exp)
    d = package_dir(settings, p["package_id"])
    d.mkdir(parents=True, exist_ok=True)
    if (d / "package.json").is_file():  # immutable: the first write wins (same content by construction)
        p = json.loads((d / "package.json").read_text(encoding="utf-8"))
    else:
        (d / "package.json").write_text(json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")
        (d / "report.md").write_text(render_markdown(p), encoding="utf-8")
        (d / "report.html").write_text(render_html(p), encoding="utf-8")
    files = {n: hashlib.sha256((d / n).read_bytes()).hexdigest() for n in ("package.json", "report.md", "report.html")}
    base = f"/v1/lab/packages/{p['package_id']}"
    return {"package_id": p["package_id"], "sha256": p["content_sha256"], "created_at": p["created_at"],
            "experiment_id": p["manifest"]["experiment_id"], "file_sha256": files,
            "files": {"json": base, "markdown": f"{base}/report.md", "html": f"{base}/report.html"}}


def load_package(settings: Settings, package_id: str) -> dict[str, Any] | None:
    if not valid_id(package_id):
        return None
    f = package_dir(settings, package_id) / "package.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


def receiver_url() -> str:
    return os.environ.get("HYDRA_KONTRA_RECEIVER", DEFAULT_RECEIVER)


def is_loopback_url(url: str) -> bool:
    """Strict loopback target (ASTRA-DELTA-04, CR-KONTRA-04): `http`, a LITERAL loopback IP (127.0.0.0/8 or ::1;
    no host names, so no resolver or hosts file decides), no user info, a valid port, no fragment."""
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises on a malformed port
    except ValueError:
        return False
    if parts.scheme != "http" or not parts.hostname or parts.username is not None or parts.password is not None:
        return False
    if parts.fragment or any(c in url for c in ("\\", "\r", "\n", "\t", " ")):
        return False
    ip = literal_ip(parts.hostname)
    return ip is not None and ip.is_loopback


class RedirectRefused(urllib.error.HTTPError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the demonstration channel is one loopback POST, nothing else (ASTRA-DELTA-04)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201 — urllib signature
        raise RedirectRefused(req.full_url, code, f"redirect refused ({code})", headers, fp)


def validate_ack(ack: Any, package_id: str) -> str | None:
    """None when `ack` is a valid receipt for `package_id`, else the reason (Polish) it is not (ASTRA-DELTA-03,
    CR-KONTRA-03). Required: a JSON object, `package_id` equal to the sent one, a non-empty string `receipt_id`,
    a boolean `duplicate`."""
    if not isinstance(ack, dict):
        return f"odpowiedź nie jest obiektem JSON ({type(ack).__name__})"
    if ack.get("package_id") != package_id:
        return "odpowiedź nie dotyczy wysłanego pakietu (package_id niezgodny lub brak)"
    rid = ack.get("receipt_id")
    if not isinstance(rid, str) or not rid.strip():
        return "brak identyfikatora pokwitowania (receipt_id)"
    if not isinstance(ack.get("duplicate"), bool):
        return "pole duplicate nie jest wartością logiczną"
    for k in ("received_at", "synthetic_case_id", "note"):
        if ack.get(k) is not None and not isinstance(ack.get(k), str):
            return f"pole {k} ma nieprawidłowy typ"
    return None


ACK_FIELDS = ("receipt_id", "package_id", "received_at", "duplicate", "synthetic_case_id", "note")


def send_package(pkg: dict[str, Any], url: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """POST the package to the local demonstration receiver. Refuses any non-loopback URL and never follows a
    redirect. Status: `acknowledged` / `duplicate` only for a valid receipt of THIS package (`validate_ack`);
    `unconfirmed` when the request was sent but no valid receipt came back (invalid JSON, an empty or foreign
    object, a wrong type, a timeout); `unavailable` when nothing could be sent; `rejected` for an HTTP error,
    a redirect or a refused URL."""
    url = url or receiver_url()
    out: dict[str, Any] = {"status": "unavailable", "receiver_url": url, "ack": None, "reason": None}
    if not is_loopback_url(url):
        out.update(status="rejected",
                   reason="adres odbiornika musi być literalnym adresem loopback (127.0.0.1 lub ::1)")
        return out
    data = json.dumps(pkg, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Idempotency-Key": pkg["package_id"]})
    # never through a proxy, never along a redirect
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
    except RedirectRefused as exc:
        out.update(status="rejected", reason=f"odbiornik zwrócił przekierowanie (HTTP {exc.code}); nie podążono")
        return out
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            out.update(status="rejected", reason=f"odbiornik zwrócił przekierowanie (HTTP {exc.code}); nie podążono")
        else:
            out.update(status="rejected", reason=f"odbiornik odrzucił pakiet: HTTP {exc.code}")
        return out
    except TimeoutError:
        out.update(status="unconfirmed", reason="przekroczony czas oczekiwania po wysłaniu; odbiór niepotwierdzony")
        return out
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            out.update(status="unconfirmed", reason="przekroczony czas oczekiwania; odbiór niepotwierdzony")
            return out
        out["reason"] = f"lokalny odbiornik niedostępny ({type(exc.reason).__name__})"
        return out
    except (ConnectionError, OSError) as exc:
        out["reason"] = f"lokalny odbiornik niedostępny ({type(exc).__name__})"
        return out
    try:
        ack = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        out.update(status="unconfirmed", reason="odpowiedź odbiornika nie jest poprawnym JSON; odbiór niepotwierdzony")
        return out
    bad = validate_ack(ack, pkg["package_id"])
    if bad is not None:
        out.update(status="unconfirmed", reason=bad + "; odbiór niepotwierdzony")
        return out
    out.update(status="duplicate" if ack["duplicate"] else "acknowledged",
               ack={k: ack.get(k) for k in ACK_FIELDS})
    return out
