"""Observation reports: one representation (`ad_entry`) for the single-ad report and the period report.

This is an observation report stored and shown in HYDRA, not an abuse report: nothing is sent anywhere. An
unreviewed ad does not meet the semantics of a confirmed abuse submission (XARF/STIX exports stay with confirmed
campaigns, hydra.export_abuse); the JSON here can be attached to a later submission package by an analyst.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from hydra.meta import contract, store

FORMAT = "hydra.meta.report.v1"
LIMITS_PL = [
    "Biblioteka reklam zwraca reklamy, które dotarły do wskazanego kraju (PL = miejsce dotarcia, nie siedziba "
    "reklamodawcy); reklamy spoza UE tylko w kategoriach społecznych/politycznych (S1).",
    "Publiczna biblioteka nie udostępnia wewnętrznych kampanii reklamodawcy: kampanie HYDRY są rozpoznawane metodą "
    "HYDRY z treści i infrastruktury.",
    "Okno dat z nakładką: wyszukiwanie nie jest kompletnym strumieniem przyrostowym; brak wyniku nie oznacza braku "
    "reklam.",
    "Zasięg (eu_total_reach) to estymacja Meta dla UE, nie liczba unikalnych osób; nie jest sumowany.",
    "Brak zwalidowanego detektora manipulacji (deepfake): status „niedostępny” nie oznacza autentyczności.",
]


def _code_version() -> str | None:
    try:
        root = Path(__file__).resolve().parents[2]
        return subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _models() -> dict[str, str]:
    try:
        from hydra.pipeline import model_revisions

        return model_revisions()
    except Exception:  # noqa: BLE001
        return {}


def ad_entry(conn: sqlite3.Connection, analysis_id: int) -> dict[str, Any] | None:
    a = store.analysis(conn, analysis_id)
    if a is None:
        return None
    det = store.ad_detail(conn, a["mode"], a["ad_id"]) or {}
    rev = next((r for r in det.get("revisions") or [] if r["id"] == a["revision_id"]), None) or {}
    content = rev.get("content") or {"creative": {}, "meta": {}}
    return {
        "report_id": a["report_id"],
        "analysis_id": a["id"],
        "mode": a["mode"], "mode_label_pl": a["mode_label_pl"],
        "source": {"source": "ext:meta-adlibrary", "source_ad_id": a["ad_id"],
                   "source_page_id": content["creative"].get("source_page_id"),
                   "source_page_name": content["creative"].get("source_page_name"),
                   "library_link": contract.library_link(a["ad_id"]),
                   "graph_api_version": rev.get("graph_api_version"),
                   "countries_requested": content["meta"].get("countries_requested"),
                   "queries": det.get("queries")},
        "timeline": {"first_observed_at": (det.get("ad") or {}).get("first_observed_at"),
                     "last_observed_at": (det.get("ad") or {}).get("last_observed_at"),
                     "revision_observed_at": rev.get("observed_at"),
                     "source_delivery_start": content["meta"].get("source_delivery_start"),
                     "source_delivery_stop": content["meta"].get("source_delivery_stop"),
                     "analysed_at": a["updated_at"],
                     "reviewed_at": ((a.get("review") or {}).get("history") or [{}])[-1].get("created_at"),
                     "note_pl": "Czasy obserwacji HYDRY różnią się od dat emisji podanych przez źródło."},
        "revision": {"id": rev.get("id"), "rev": rev.get("rev"), "content_sha256": rev.get("content_sha256"),
                     "creative": content["creative"], "eu_transparency": content["meta"].get("eu_transparency"),
                     "display_domains": content["meta"].get("display_domains"),
                     "missing_fields": content["meta"].get("missing_fields")},
        "materials": {"collection_status": rev.get("collection_status"),
                      "collection_status_pl": rev.get("collection_status_pl"), "artifacts": rev.get("artifacts")},
        "correlation": a["correlation"],
        "fraud_indications": a["triage"],
        "manipulation": a["manipulation"],
        "analyst": a["review"],
        "analysis": {"status": a["status"], "status_pl": a["status_pl"], "coverage_pl": a["reason_pl"],
                     "incident_id": a["incident_id"], "base_run_id": a["base_run_id"],
                     "context_version": a["context_version"], "assign_run_id": a["assign_run_id"]},
    }


def _envelope(kind: str, mode: str, cfg: dict[str, Any]) -> dict[str, Any]:
    return {"format": FORMAT, "kind": kind, "mode": mode, "mode_label_pl": store.MODE_PL.get(mode, mode),
            "synthetic": mode == "synthetic",
            "versions": {"code": _code_version(), "extract": store.EXTRACT_VERSION,
                         "graph_api_version": contract.graph_version(), "contract_verified_on": contract.VERIFIED_ON,
                         "config_version": cfg.get("config_version"), "models": _models()},
            "limitations_pl": LIMITS_PL,
            "semantics_pl": "Raport obserwacji HYDRY (nie zgłoszenie nadużycia); nic nie zostało wysłane."}


def ad_report(conn: sqlite3.Connection, analysis_id: int) -> dict[str, Any] | None:
    e = ad_entry(conn, analysis_id)
    if e is None:
        return None
    return {**_envelope("ad", e["mode"], store.config(conn)), "report_id": e["report_id"], "entries": [e]}


def period_report(conn: sqlite3.Connection, mode: str, since: str | None = None, until: str | None = None,
                  limit: int = 500) -> dict[str, Any]:
    where, args = ["mode=?"], [mode]
    if since:
        where.append("updated_at>=?")
        args.append(since)
    if until:
        where.append("updated_at<=?")
        args.append(until)
    ids = [r[0] for r in conn.execute(f"SELECT id FROM meta_analyses WHERE {' AND '.join(where)} ORDER BY id DESC "
                                      "LIMIT ?", (*args, limit))]
    entries = [e for e in (ad_entry(conn, i) for i in ids) if e]
    rep = {**_envelope("period", mode, store.config(conn)), "window": {"since": since, "until": until},
           "counters": store.counters(conn, mode), "polls": store.last_polls(conn, mode, 50), "entries": entries}
    return rep


def to_markdown(rep: dict[str, Any]) -> str:
    L = [f"# Raport obserwacji reklam Meta — {rep['mode_label_pl']}", "",
         f"- Format: `{rep['format']}`, rodzaj: {rep['kind']}", f"- {rep['semantics_pl']}",
         f"- Wersja kodu: {rep['versions'].get('code')}, ekstrakcja {rep['versions']['extract']}, Graph API "
         f"{rep['versions']['graph_api_version']} (kontrakt sprawdzony {rep['versions']['contract_verified_on']}), "
         f"konfiguracja v{rep['versions'].get('config_version')}", ""]
    if rep["mode"] != "live":
        L += [f"> **{rep['mode_label_pl']}** — wyniki nie pochodzą z bieżącej obserwacji Meta Ad Library.", ""]
    if rep.get("counters"):
        L += ["## Liczniki (osobno, bez sumowania)", ""] + [f"- {k}: {v}" for k, v in rep["counters"].items()] + [""]
    for e in rep["entries"]:
        c, t, m = e["correlation"], e["fraud_indications"], e["manipulation"]
        L += [f"## Reklama {e['source']['source_ad_id']} (raport {e['report_id']})", "",
              f"1. Źródło: strona {e['source']['source_page_name']} ({e['source']['source_page_id']}), "
              f"{e['source']['library_link']}; obserwacja HYDRY {e['timeline']['first_observed_at']} – "
              f"{e['timeline']['last_observed_at']}; emisja wg źródła {e['timeline']['source_delivery_start']}.",
              f"2. Materiały: {e['materials']['collection_status_pl']}"]
        for x in e["materials"]["artifacts"] or []:
            L.append(f"   - {x['role']}: {x['status_pl']} {('sha256 ' + x['sha256'][:16]) if x.get('sha256') else ''}")
        L += [f"3. Korelacja: {c.get('decision_pl')} {c.get('campaign_id') or ''} — {c.get('reason_pl') or ''}",
              f"   - bazowy przebieg {c.get('base_run_id')}, przebieg przypisania {c.get('assign_run_id')}"]
        for ev in (c.get("evidence") or [])[:8]:
            L.append(f"   - {ev['family']}/{ev['kind']}: {ev['value']} ({ev['bits']} bit)")
        L += [f"4. Przesłanki oszustwa: priorytet {t.get('triage_priority_pl')}, status {e['analyst']['status']}"]
        for r in t.get("reasons") or []:
            L.append(f"   - {r['code']}: {r['description_pl']} [{r['rule_version']}]")
        L.append(f"   - {t.get('note_pl')}")
        L.append("5. Sygnały manipulacji:")
        for x in m:
            L.append(f"   - {x['modality']}: {x['status_pl']} — {x['limitations_pl']}")
        L.append(f"6. Decyzja analityka: {e['analyst']['status']}; historia: "
                 + ", ".join(f"{h['decision']} ({h['analyst']}, {h['created_at']})" for h in e["analyst"]["history"]))
        L.append(f"7. Analiza: {e['analysis']['status_pl']}; incydent {e['analysis']['incident_id']}; "
                 f"{e['analysis']['coverage_pl']}")
        L.append("")
    L += ["## Ograniczenia", ""] + [f"- {x}" for x in rep["limitations_pl"]]
    return "\n".join(L) + "\n"


def dumps(rep: dict[str, Any]) -> str:
    return json.dumps(rep, ensure_ascii=False, indent=1, default=str)
