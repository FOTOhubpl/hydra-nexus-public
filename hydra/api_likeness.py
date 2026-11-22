"""GET /v1/incidents/{id}/likeness: the "Nadużycie wizerunku" analyst triage card (hydra/likeness.py).

Read-only. Genes come from the `genes` table; reuse of images / footage from the VIS reasons of stored pair scores in
the resolved run (and its assign runs) plus identical VIS hash values held by other incidents. Nothing is estimated
here and nothing is written back: the card never changes links, campaigns or assignments.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hydra.api_task import _assignment_runs, _json, _st, resolve_run
from hydra.likeness import VIS_HASH_KINDS, VIS_REUSE_KINDS, build_card

router = APIRouter(prefix="/v1", tags=["likeness"])

CARD_FAMILIES = ("TXT", "ID", "NAR", "VIS", "META")
CARD_KINDS = ("person_named", "persona_match", "persona_renamed", "promise", "scheme", "media_brand",
              "institution_brand", "logo", "c2pa_manifest", "c2pa_claim_generator", "c2pa_signer", "c2pa_validation",
              "c2pa_ai_generated", "c2pa_ai_declared_unverified", "c2pa_binding", "c2pa_trust", "wm_audio_local",
              "ai_metadata_marker", "wm_generator", "wm_check", "synth_avatar_eyes",
              "avatar_geometry_check", "wm_audio", "wm_audio_check", "av_sync_suspicion", "synthetic_image_suspicion",
              "synthetic_image_check", "synthetic_video_suspicion", "synthetic_video_check")
SKIP_GROUPS = ("vis_boilerplate", "vis_app_ui", "vis_brand")
MAX_REUSE = 40


def _card_genes(conn: sqlite3.Connection, incident_id: str) -> list[dict[str, Any]]:
    marks = ",".join("?" * len(CARD_KINDS))
    rows = conn.execute(
        f"SELECT family, kind, value, quality, status, provenance, evidence, dependency_group FROM genes"
        f" WHERE incident_id=? AND kind IN ({marks}) ORDER BY family, kind, id", (incident_id, *CARD_KINDS)).fetchall()
    genes = [dict(r) | {"evidence": _json(r["evidence"], {}) or {}} for r in rows]
    # family-level "did it run" markers (one row per family/status) so absent vs unknown can be told apart
    for r in conn.execute("SELECT family, status, COUNT(*) AS n FROM genes WHERE incident_id=? GROUP BY family, status",
                          (incident_id,)):
        if r["family"] in CARD_FAMILIES:
            genes.append({"family": r["family"], "kind": "_family_marker", "value": None, "status": r["status"],
                          "evidence": {}})
    return genes


def _pair_reuse(conn: sqlite3.Connection, run: str | None, incident_id: str) -> list[dict[str, Any]]:
    runs = _assignment_runs(conn, run)
    if not runs:
        return []
    marks = ",".join("?" * len(runs))
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in conn.execute(f"SELECT a, b, detail FROM pairs WHERE run_id IN ({marks}) AND (a=? OR b=?)",
                          (*runs, incident_id, incident_id)):
        mine_a = r["a"] == incident_id
        other = r["b"] if mine_a else r["a"]
        detail = _json(r["detail"], {}) or {}
        for g in detail.get("reasons") or []:
            if g.get("family") != "VIS" or (g.get("bits") or 0) <= 0:
                continue
            kind = str(g.get("kind") or "")
            if kind not in VIS_REUSE_KINDS and not kind.startswith(("pdq", "phash", "vis_")):
                continue
            ev = (g.get("evidence_a") if mine_a else g.get("evidence_b")) or {}
            key = (other, kind)
            if key not in out or (g.get("bits") or 0) > (out[key].get("bits") or 0):
                out[key] = {"other": other, "kind": kind, "value": g.get("value_a") if mine_a else g.get("value_b"),
                            "bits": g.get("bits"), "quality": g.get("similarity"), "evidence": ev,
                            "label_pl": g.get("explanation_pl")}
    return list(out.values())


def _hash_reuse(conn: sqlite3.Connection, incident_id: str) -> list[dict[str, Any]]:
    kinds = (*VIS_HASH_KINDS, "asset_sha256")
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        f"SELECT g1.kind, g1.value, g1.evidence, g1.dependency_group, g2.dependency_group AS other_group,"
        f" g2.incident_id AS other FROM genes g1"
        f" JOIN genes g2 ON g2.kind=g1.kind AND g2.value=g1.value AND g2.incident_id<>g1.incident_id"
        f" AND g2.status='observed'"
        f" WHERE g1.incident_id=? AND g1.status='observed' AND g1.value IS NOT NULL AND g1.kind IN ({marks})"
        f" LIMIT 500", (incident_id, *kinds)).fetchall()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if str(r["dependency_group"] or "").startswith(SKIP_GROUPS) or str(r["other_group"] or "").startswith(
                SKIP_GROUPS):
            continue
        out.setdefault((r["other"], r["kind"]), {
            "other": r["other"], "kind": r["kind"], "value": r["value"], "evidence": _json(r["evidence"], {}) or {},
            "label_pl": f"identyczny skrót obrazu ({r['kind']}) w {r['other']}"})
    return list(out.values())


@router.get("/incidents/{incident_id}/likeness")
def likeness_card(request: Request, incident_id: str, run_id: str | None = None) -> dict[str, Any]:
    with _st(request).reader() as conn:
        if conn.execute("SELECT 1 FROM incidents WHERE id=?", (incident_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="incident not found")
        run = resolve_run(conn, run_id)
        genes = _card_genes(conn, incident_id)
        reuse: dict[tuple[str, str], dict[str, Any]] = {}
        for r in _hash_reuse(conn, incident_id) + _pair_reuse(conn, run, incident_id):
            reuse[(r["other"], r["kind"])] = r
        items = sorted(reuse.values(), key=lambda r: (-(r.get("bits") or 0), r["other"], r["kind"]))[:MAX_REUSE]
    return build_card(incident_id, genes, items, run)
