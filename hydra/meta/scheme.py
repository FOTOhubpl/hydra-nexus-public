"""P1 "nowa reklama, ten sam schemat": what the stored analyses say about one ad, side by side with a look-alike
that HYDRA did not merge. Reads stored rows only (revisions, artifacts, analyses); no scoring, no new decision.

- `versions`: the ad's own revisions with the text/URL/media differences and a landing-domain change;
- `same_scheme`: other ads of the same data space whose latest analysis was assigned to the same HYDRA campaign, with
  the identifiers they share (landing host, phone-like tokens, media digest) and the assigner's evidence;
- `lookalike_not_merged`: ads sharing a media file (same bytes) or the page name but NOT assigned to the same
  campaign, with the reason it was kept apart (the assigner's own decision): a shared picture is not a link.
Hypotheses only: none of this confirms a common operator.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from hydra.meta import normalize, store

PHONE_RE = re.compile(r"\+?\d[\d \-]{7,}\d")


def _hosts(cr: dict[str, Any]) -> set[str]:
    return {(urlsplit(u.get("url") or "").hostname or "").lower() for u in cr.get("landing_urls") or []} - {""}


def _phones(cr: dict[str, Any]) -> set[str]:
    return {re.sub(r"[ \-]", "", p) for p in PHONE_RE.findall(normalize.text_of(cr))}


def _media(conn: sqlite3.Connection, rid: int) -> set[str]:
    return {r[0] for r in conn.execute("SELECT artifact_id FROM meta_artifacts WHERE revision_id=? AND "
                                       "status='asset_available' AND artifact_id IS NOT NULL", (rid,))}


def _latest(conn: sqlite3.Connection, mode: str) -> list[dict[str, Any]]:
    out = []
    for ad_id, rid, content in conn.execute(
            "SELECT a.ad_id, a.current_rev_id, r.content FROM meta_ads a JOIN meta_revisions r ON r.id=a.current_rev_id"
            " WHERE a.mode=?", (mode,)):
        an = conn.execute("SELECT id, decision, hydra_campaign_id, correlation FROM meta_analyses WHERE revision_id=? "
                          "ORDER BY id DESC LIMIT 1", (rid,)).fetchone()
        cr = json.loads(content)["creative"]
        out.append({"ad_id": ad_id, "revision_id": rid, "creative": cr, "hosts": _hosts(cr), "phones": _phones(cr),
                    "media": _media(conn, rid), "analysis_id": an[0] if an else None,
                    "decision": an[1] if an else None, "campaign_id": an[2] if an else None,
                    "evidence": (json.loads(an[3]) if an else {}).get("evidence") or []})
    return out


def compare(conn: sqlite3.Connection, mode: str, ad_id: str) -> dict[str, Any] | None:
    det = store.ad_detail(conn, mode, ad_id)
    if det is None:
        return None
    revs = sorted(det["revisions"], key=lambda r: r["rev"])
    versions = []
    for prev, cur in zip(revs, revs[1:], strict=False):
        a, b = prev["content"]["creative"], cur["content"]["creative"]
        ha, hb = _hosts(a), _hosts(b)
        versions.append({"from_rev": prev["rev"], "to_rev": cur["rev"], "observed_at": cur["observed_at"],
                         "text_changed": normalize.text_of(a) != normalize.text_of(b),
                         "domain_change": {"removed": sorted(ha - hb), "added": sorted(hb - ha)} if ha != hb else None,
                         "media_changed": _media(conn, prev["id"]) != _media(conn, cur["id"]),
                         "kept": {"phones": sorted(_phones(a) & _phones(b))}})
    ads = _latest(conn, mode)
    me = next((x for x in ads if x["ad_id"] == ad_id), None)
    same, apart = [], []
    for o in ads:
        if me is None or o["ad_id"] == ad_id:
            continue
        shared = {"hosts": sorted(me["hosts"] & o["hosts"]), "phones": sorted(me["phones"] & o["phones"]),
                  "media_sha256": sorted(me["media"] & o["media"])}
        row = {"ad_id": o["ad_id"], "decision": o["decision"], "campaign_id": o["campaign_id"], "shared": shared,
               "analysis_id": o["analysis_id"]}
        if me["campaign_id"] and o["campaign_id"] == me["campaign_id"] and me["decision"] == o["decision"] == "assign":
            row["evidence"] = o["evidence"][:6]
            same.append(row)
        elif shared["media_sha256"] or (o["creative"].get("source_page_name") and o["creative"].get(
                "source_page_name") == me["creative"].get("source_page_name")):
            row["why_not_merged_pl"] = (
                "Wspólny obraz lub nazwa strony nie są samodzielnym dowodem wspólnej kampanii; decyzja HYDRY dla tej "
                f"reklamy: {store.DECISION_PL.get(o['decision'], o['decision'])}"
                + (f" ({o['campaign_id']})" if o["campaign_id"] else "") + ".")
            apart.append(row)
    return {"mode": mode, "mode_label_pl": store.MODE_PL[mode], "ad_id": ad_id,
            "decision": me["decision"] if me else None, "campaign_id": me["campaign_id"] if me else None,
            "versions": versions, "same_scheme": same[:10], "lookalike_not_merged": apart[:10],
            "note_pl": "Powiązania z publicznych reklam to hipotezy do weryfikacji, nie potwierdzenie operatora."}
