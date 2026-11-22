"""Read-only aggregates that make the pipeline visible ("Jak to działa", variant diff, provenance, revisions,
lifecycle, gene explainer, corpus stats). Mounted under /v1 by hydra.api_task. Every number is counted from the
stored tables or copied from runs.metrics; nothing is estimated here.
"""

from __future__ import annotations

import sqlite3
from collections import Counter, defaultdict
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from hydra.api_task import (
    FAMILY_ORDER,
    _assignment_runs,
    _dominant_family,
    _gene_level,
    _groups,
    _in,
    _json,
    _latest_assignments,
    _membership,
    _run_row,
    _st,
    resolve_run,
    run_incidents,
    safe_ref,
)
from hydra.redact import redact_obj

router = APIRouter(tags=["visual"])

TEXT_KINDS = ("ad_headline", "page_title", "headline", "lead", "library_ad_text", "ad_text", "cta", "promise",
              "product_name", "byline", "ocr_line")
INFRA_KINDS = ("registrable_domain", "landing_host", "registrar", "ns_set", "tracker_id", "cert_issuer", "asn",
               "as_org", "dom_skeleton_hash", "css_build_hash", "js_build_hash", "tlsh_html_skeleton",
               "favicon_mmh3", "url_label_skeleton", "entity_phone", "operator_id")
VISUAL_KINDS = ("artifact_sha256", "pdq", "pdq_canon", "phash", "phash_tiles", "logo", "image_class", "face",
                "person_named", "persona_renamed")


def _corpus_of(source: str | None) -> str:
    try:
        from hydra.corpus import corpus_of  # type: ignore[attr-defined]

        return corpus_of(source)
    except ImportError:
        s = source or ""
        return s if s.startswith(("ext:", "public:")) else "nask"


# --------------------------------------------------------------------------------------- 1 system flow

ASSIGN_HINT_PL = ("Brak przypisań w tym przebiegu: uruchom analizę ponownie (każdy przebieg przypisuje swoje incydenty"
                  " metodą leave-one-out) albo dodaj nowy materiał na ekranie Analiza lub `hydra assign`.")
REVIEW_HINT_PL = ("Brak recenzji: zaakceptuj lub odrzuć powiązanie w kolejce analityka (Kolejka) albo na karcie pary;"
                  " każda recenzja tworzy nową rewizję kampanii.")
EXPORT_HINT_PL = ("Brak eksportów tego przebiegu: pobierz JSON/CSV z karty kampanii, STIX lub pakiet abuse, eksport"
                  " Neo4j / podgrafu z ekranu Graf albo `hydra export-dataset`.")


def _extractor_counts(conn: sqlite3.Connection, scope: list[str]) -> dict[str, dict[str, Any]]:
    """Runs without metrics.extractors (older CORE): genes by extractor tag and status from the genes table."""
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(f"SELECT extractor, status, COUNT(*) FROM genes WHERE incident_id IN ({_in(scope)})"
                          " GROUP BY 1, 2", scope):
        row = out.setdefault(str(r[0]).split("@")[0], {"observed": 0, "unavailable": 0, "failed": 0})
        if r[1] in row:
            row[r[1]] += r[2]
    return out



def _flow_heavy(conn: sqlite3.Connection, run: str, m: dict[str, Any], live: bool = True) -> dict[str, Any]:
    """The per-run part of the flow diagram (cached by hydra.api_cache): counts over genes / artifacts and one
    streaming pass over the run's pairs (orjson; SQLite json_extract over ~400 MB of pair JSON is ~10x slower)."""
    import orjson

    scope = run_incidents(conn, run, live=live)
    q = lambda sql, args=(): conn.execute(sql, args).fetchall()  # noqa: E731
    sources = Counter(_corpus_of(r[0]) for r in q(f"SELECT source FROM incidents WHERE id IN ({_in(scope)})", scope))
    roles = {r[0]: r[1] for r in q(f"SELECT role, COUNT(*) FROM incident_artifacts WHERE incident_id IN"
                                   f" ({_in(scope)}) GROUP BY 1", scope)}
    fam = {r[0]: r[1] for r in q(f"SELECT family, COUNT(*) FROM genes WHERE status='observed' AND incident_id IN"
                                 f" ({_in(scope)}) GROUP BY 1", scope)}
    per_ex = m.get("extractors") or _extractor_counts(conn, scope)
    # LIKE first: json_extract only runs on rows that can carry the key
    enr_genes = q(f"SELECT COUNT(*) FROM genes WHERE incident_id IN ({_in(scope)}) AND evidence LIKE"
                  " '%enrichment_key%' AND json_extract(evidence, '$.enrichment_key') IS NOT NULL", scope)[0][0]
    levels: Counter = Counter()
    for r in q(f"SELECT DISTINCT family, kind, value, dependency_group FROM genes WHERE status='observed' AND"
               f" value IS NOT NULL AND vector_ref IS NULL AND incident_id IN ({_in(scope)})", scope):
        levels[_gene_level(r)[0]] += 1
    idx: Counter = Counter()
    hyp: Counter = Counter()
    bits = {"campaign_bits": 0.0, "kit_bits": 0.0, "asset_bits": 0.0}
    n_pairs = 0
    for h, detail in conn.execute("SELECT hypothesis, detail FROM pairs WHERE run_id=?", (run,)):
        n_pairs += 1
        hyp[h] += 1
        try:
            d = orjson.loads(detail) if detail else {}
        except orjson.JSONDecodeError:
            d = {}
        if not isinstance(d, dict):
            continue
        for s in {str(x).split(":")[0] for x in d.get("candidate_sources") or []}:
            idx[s] += 1
        for k in bits:
            bits[k] += float(d.get(k) or 0)
    camps = q("SELECT COUNT(*), SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END) FROM (SELECT campaign_id, COUNT(*) n FROM"
              " campaign_members WHERE run_id=? GROUP BY 1)", (run,))[0]
    return {"n_scope": len(scope), "sources": dict(sources), "roles": roles, "fam": fam, "per_ex": per_ex,
            "enr_genes": enr_genes, "levels": dict(levels), "idx": dict(idx), "hyp": dict(hyp), "bits": bits,
            "n_pairs": n_pairs, "camps": [camps[0] or 0, camps[1] or 0]}


def _flow_cached(st: Any, conn: sqlite3.Connection, run: str, m: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """DATAFLOW: with materialised aggregates the heavy part covers the immutable run only (never invalidated by
    new material) and the counters that new material changes are read from `agg_counts`."""
    from hydra import api_cache, dataflow

    if dataflow.ensure(st.settings.db_path, st.writer, conn, run):
        h, hit = api_cache.cached(st.settings, conn, "system-flow-base", run, {},
                                  lambda: _flow_heavy(conn, run, m, live=False), base_only=True)
        return {**h, **dataflow.flow_counts(conn, run)}, hit
    return api_cache.cached(st.settings, conn, "system-flow", run, {}, lambda: _flow_heavy(conn, run, m))


def warm_run(st: Any, conn: sqlite3.Connection, run: str) -> None:
    rr = _run_row(conn, run) or {}
    _flow_cached(st, conn, run, rr.get("metrics") or {})


@router.get("/system/flow")
def system_flow(request: Request, run_id: str | None = None) -> Any:
    """Live numbers per pipeline box for the "Jak to działa" diagram. Per-run counts come from the run cache;
    assignments, reviews, exports and the enrichment store are read live (they change after the run)."""
    st = _st(request)
    with st.reader() as conn:
        run = resolve_run(conn, run_id)
        if run is None:
            return {"run_id": None}
        rr = _run_row(conn, run) or {}
        m = rr.get("metrics") or {}
        params = rr.get("params") or {}
        h, hit = _flow_cached(st, conn, run, m)
        q = lambda sql, args=(): conn.execute(sql, args).fetchall()  # noqa: E731
        ex_seconds: Counter = Counter()
        ex_original: Counter = Counter()
        cached = run_cnt = 0
        n_names = len(params.get("extractors") or []) or 1
        for st_ in (m.get("extract") or {}).values():
            for k, v in (st_.get("extractor_seconds") or {}).items():
                ex_seconds[k] += float(v or 0)
            for k, v in (st_.get("extractor_original_seconds") or {}).items():
                ex_original[k] += float(v or 0)
            # older runs: a fully cached incident counted as 1; it reuses every extractor of the run
            cached += int(st_.get("extractors_cached") or (n_names if st_.get("cached") else 0))
            run_cnt += int(st_.get("extractors_run") or 0)
        enr = {r[0]: r[1] for r in q("SELECT source, COUNT(*) FROM enrichment GROUP BY 1")}
        latest = _latest_assignments(conn, run)
        assigns = Counter(r["decision"] for r in latest)
        a_runs = _assignment_runs(conn, run)
        a_rows = q(f"SELECT run_id, COUNT(*) FROM assignments WHERE run_id IN ({_in(a_runs)}) GROUP BY 1", a_runs) \
            if a_runs else []
        self_n = sum(r[1] for r in a_rows if r[0] == run)
        new_n = sum(r[1] for r in a_rows if r[0] != run)
        reviews = q("SELECT COUNT(*), SUM(CASE WHEN action='undo' THEN 1 ELSE 0 END) FROM reviews")[0]
        root = params.get("root_run") or run
        run_reviews = len(params.get("reviews_applied") or [])
        from hydra.export_log import export_counts

        exports = export_counts(conn, run)
        revisions = q("SELECT COUNT(*) FROM runs WHERE kind='analyse' AND json_extract(params, '$.revision_of')"
                      " IS NOT NULL")[0][0]
    try:
        from hydra.llm import is_enabled  # type: ignore[attr-defined]

        llm_on = bool(is_enabled(st.settings))
    except Exception:  # noqa: BLE001
        llm_on = False
    fam, roles, n_scope = h["fam"], h["roles"], h["n_scope"]
    from hydra import api_cache

    return api_cache.json_response({
        "run_id": run, "created_at": rr.get("created_at"), "revision_of": params.get("revision_of"),
        "timings_s": m.get("timings_s") or {},
        "boxes": {
            "input": {"incidents": n_scope, "by_corpus": h["sources"],
                      **({"new_since_analysis": h["new"]} if "new" in h else {})},
            "adapters": {"artifacts": sum(roles.values()), "by_role": roles},
            "genes": {"by_family": {f: fam.get(f, 0) for f in FAMILY_ORDER}, "total": sum(fam.values()),
                      "extractor_seconds": {k: round(v, 3) for k, v in ex_seconds.most_common()},
                      "extractor_original_seconds": {k: round(v, 3) for k, v in ex_original.most_common()},
                      "extractors": h["per_ex"], "extractors_cached": cached, "extractors_run": run_cnt},
            "enrichment": {"records_by_source": enr, "genes_with_enrichment": h["enr_genes"]},
            "rarity": {"distinct_values_by_level": h["levels"]},
            "candidates": {"by_index": h["idx"], "candidate_pairs": m.get("candidate_pairs"),
                           "all_pairs": n_scope * (n_scope - 1) // 2},
            "scoring": {"pairs_scored": h["n_pairs"], "hypotheses": h["hyp"],
                        "bits_by_level": {k: round(v, 1) for k, v in h["bits"].items()}, "scorer": m.get("scorer")},
            "clustering": {"campaigns": h["camps"][0], "multi_member": h["camps"][1],
                           "groups": len(m.get("campaign_groups") or [])},
            "assignment": {"by_decision": dict(assigns), "conformal": bool(m.get("conformal")),
                           "self": self_n, "new_material": new_n, "summary": m.get("assignment"),
                           "hint_pl": None if latest else ASSIGN_HINT_PL},
            "reviews": {"reviews": reviews[0] or 0, "undos": reviews[1] or 0, "revisions": revisions,
                        "constraints_in_run": len(params.get("constraints") or []),
                        "reviews_in_run": run_reviews, "root_run": root,
                        "hint_pl": None if reviews[0] else REVIEW_HINT_PL},
            "outputs": {"llm_enabled": llm_on, "exports": exports,
                        "hint_pl": None if exports.get("total") else EXPORT_HINT_PL},
        },
    }, hit=hit)


# --------------------------------------------------------------------------------------- 2 variant diff


def _values(conn: sqlite3.Connection, iid: str, kinds: tuple[str, ...]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute(f"SELECT kind, value FROM genes WHERE incident_id=? AND status='observed' AND value IS NOT"
                          f" NULL AND kind IN ({_in(list(kinds))}) ORDER BY kind, quality DESC, id",
                          (iid, *kinds)):
        if r["value"] not in out[r["kind"]]:
            out[r["kind"]].append(r["value"])
    return dict(out)


def _compare(a: dict[str, list[str]], b: dict[str, list[str]], kinds: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for k in kinds:
        va, vb = a.get(k, []), b.get(k, [])
        if not va and not vb:
            continue
        sa, sb = set(va), set(vb)
        shared = [v for v in va if v in sb]
        state = "same" if shared and sa == sb else "partly" if shared else ("changed" if va and vb else "one_side")
        rows.append({"kind": k, "state": state, "shared": shared[:20], "only_a": [v for v in va if v not in sb][:20],
                     "only_b": [v for v in vb if v not in sa][:20]})
    return rows


def _video_extent(conn: sqlite3.Connection, iid: str, artifact_id: str | None) -> float | None:
    """Last sampled frame time of a video artifact (the timeline length shown in the UI)."""
    if not artifact_id:
        return None
    r = conn.execute("SELECT MAX(CAST(json_extract(evidence, '$.timecode_s') AS REAL)) AS t FROM genes WHERE"
                     " incident_id=? AND json_extract(evidence, '$.artifact_id')=?", (iid, artifact_id)).fetchone()
    return float(r["t"]) if r and r["t"] is not None else None


def _video_segments(conn: sqlite3.Connection, lo: str, hi: str, reasons: list[dict]) -> list[dict[str, Any]]:
    """Shared-footage segments (reason kind `video_segment`, hydra/genome/vid_seq.py) with both time ranges."""
    from hydra.genome.vid_seq import parse_note

    out = []
    for m in reasons:
        if m.get("kind") != "video_segment":
            continue
        ea, eb = m.get("evidence_a") or {}, m.get("evidence_b") or {}
        pa, pb = parse_note(ea.get("note")), parse_note(eb.get("note"))
        if not pa or not pb:
            continue
        out.append({"artifact_a": ea.get("artifact_id"), "artifact_b": eb.get("artifact_id"),
                    "start_a": pa["start"], "end_a": pa["end"], "start_b": pb["start"], "end_b": pb["end"],
                    "offset_s": pa["offset"], "frames": pa["frames"], "footage_df": pa["footage_df"],
                    "length_a": _video_extent(conn, lo, ea.get("artifact_id")),
                    "length_b": _video_extent(conn, hi, eb.get("artifact_id")),
                    "bits": m.get("bits"), "explanation_pl": m.get("explanation_pl")})
    return out


@router.get("/pairs/{a}/{b}/diff")
def pair_diff(request: Request, a: str, b: str, run_id: str | None = None) -> dict[str, Any]:
    """Display boundary: the recognised-secret policy (`hydra.redact.redact_obj`) applies to the whole body
    (CR-ASTRA-PIPE-305)."""
    return redact_obj(_pair_diff(request, a, b, run_id))


def _pair_diff(request: Request, a: str, b: str, run_id: str | None = None) -> dict[str, Any]:
    """"Co się zmieniło": text, visual and infrastructure genes of two incidents, shared vs changed."""
    lo, hi = sorted((a, b))
    with _st(request).reader() as conn:
        for i in (lo, hi):
            if not conn.execute("SELECT 1 FROM incidents WHERE id=?", (i,)).fetchone():
                raise HTTPException(status_code=404, detail=f"incident {i} not found")
        run = resolve_run(conn, run_id)
        pair = None
        for rid in reversed(_assignment_runs(conn, run)):
            pair = conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a=? AND b=?", (rid, lo, hi)).fetchone()
            if pair:
                break
        detail = (_json(pair["detail"], {}) or {}) if pair else {}
        ta, tb = _values(conn, lo, TEXT_KINDS), _values(conn, hi, TEXT_KINDS)
        ia, ib = _values(conn, lo, INFRA_KINDS), _values(conn, hi, INFRA_KINDS)
        va, vb = _values(conn, lo, VISUAL_KINDS), _values(conn, hi, VISUAL_KINDS)
        # near-duplicate images as matched by the scorer (value + similarity / distance from the reasons)
        matches = [{"kind": m.get("kind"), "value_a": m.get("value_a"), "value_b": m.get("value_b"),
                    "similarity": m.get("similarity"), "bits": m.get("bits"), "explanation_pl": m.get("explanation_pl"),
                    "artifact_a": (m.get("evidence_a") or {}).get("artifact_id"),
                    "artifact_b": (m.get("evidence_b") or {}).get("artifact_id")}
                   for m in detail.get("reasons") or [] if m.get("family") == "VIS"]
        segments = _video_segments(conn, lo, hi, detail.get("reasons") or [])
        av_sync = [{"incident_id": r["incident_id"], "level": r["value"], "quality": r["quality"],
                    "artifact_id": (_json(r["evidence"], {}) or {}).get("artifact_id"),
                    "note": (_json(r["evidence"], {}) or {}).get("note")}
                   for r in conn.execute("SELECT incident_id, value, quality, evidence FROM genes WHERE incident_id IN"
                                         " (?, ?) AND kind='av_sync_suspicion' AND status='observed' ORDER BY"
                                         " incident_id, id", (lo, hi))]
        all_a = {(r["kind"], r["value"]) for r in conn.execute(
            "SELECT kind, value FROM genes WHERE incident_id=? AND status='observed' AND value IS NOT NULL", (lo,))}
        all_b = {(r["kind"], r["value"]) for r in conn.execute(
            "SELECT kind, value FROM genes WHERE incident_id=? AND status='observed' AND value IS NOT NULL", (hi,))}
        kinds_a, kinds_b = {k for k, _ in all_a}, {k for k, _ in all_b}
        both_kinds = kinds_a & kinds_b
        shared_vals = all_a & all_b
        changed_kinds = sorted(k for k in both_kinds if not any(kk == k for kk, _ in shared_vals))
        members = _membership(conn, run)
        core = []
        ca, cb = members.get(lo), members.get(hi)
        if ca and ca == cb:
            c = conn.execute("SELECT detail FROM campaigns WHERE run_id=? AND id=?", (run, ca)).fetchone()
            core = [{"family": g.get("family"), "kind": g.get("kind"), "value": g.get("value_a")}
                    for g in ((_json(c["detail"], {}) or {}).get("core_genome") or [])][:30] if c else []
    by_kind = Counter(k for k, _ in shared_vals)
    return {
        "a": lo, "b": hi, "same_campaign": bool(ca and ca == cb), "campaign_id": ca if ca == cb else None,
        "text": _compare(ta, tb, TEXT_KINDS), "infrastructure": _compare(ia, ib, INFRA_KINDS),
        "visual": _compare(va, vb, VISUAL_KINDS), "image_matches": matches, "video_segments": segments,
        "av_sync": av_sync,
        "summary": {"shared_values": len(shared_vals), "only_a": len(all_a - all_b), "only_b": len(all_b - all_a),
                    "invariant_kinds": by_kind.most_common(12), "changed_kinds": changed_kinds[:20],
                    "campaign_core": core},
        "hypothesis": detail.get("hypothesis"), "total_bits": detail.get("total_bits"),
    }


# --------------------------------------------------------------------------------------- 3 provenance flow


@router.get("/pairs/{a}/{b}/provenance")
def pair_provenance(request: Request, a: str, b: str, run_id: str | None = None) -> dict[str, Any]:
    """Display boundary: the recognised-secret policy (`hydra.redact.redact_obj`) applies to the whole body
    (CR-ASTRA-PIPE-305)."""
    return redact_obj(_pair_provenance(request, a, b, run_id))


def _pair_provenance(request: Request, a: str, b: str, run_id: str | None = None) -> dict[str, Any]:
    """artifact → extractor → gene kind → family → hypothesis, weighted by the bits of each stored reason."""
    lo, hi = sorted((a, b))
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        row = None
        for rid in reversed(_assignment_runs(conn, run)):
            row = conn.execute("SELECT hypothesis, detail FROM pairs WHERE run_id=? AND a=? AND b=?",
                               (rid, lo, hi)).fetchone()
            if row:
                break
        if row is None:
            raise HTTPException(status_code=404, detail="pair not scored")
        d = _json(row["detail"], {}) or {}
        roles = {r["artifact_id"]: (r["role"], r["path_in_incident"]) for r in conn.execute(
            "SELECT artifact_id, role, path_in_incident FROM incident_artifacts WHERE incident_id IN (?, ?)", (lo, hi))}
        links: Counter = Counter()
        items = []
        for m in d.get("reasons") or []:
            if m.get("kind") == "cap_correction" or not m.get("bits"):
                continue
            ev = m.get("evidence_a") or m.get("evidence_b") or {}
            art = ev.get("artifact_id")
            g = conn.execute("SELECT extractor, evidence FROM genes WHERE incident_id=? AND kind=? AND value=? LIMIT 1",
                             (lo, m.get("kind"), m.get("value_a") or m.get("value_b"))).fetchone()
            # reasons derived by the scorer itself (e.g. visual_near_duplicate) have no stored gene
            extractor = g["extractor"].split("@")[0] if g else "porównanie par (scorer)"
            role = roles.get(art, ("?", ""))[0] if art else "bez artefaktu"
            bits = abs(float(m["bits"]))
            fam = str(m.get("family"))
            for link in ((f"art:{role}", f"ext:{extractor}"), (f"ext:{extractor}", f"gene:{m.get('kind')}"),
                         (f"gene:{m.get('kind')}", f"fam:{fam}"), (f"fam:{fam}", f"hyp:{row['hypothesis']}")):
                links[link] += bits
            items.append({"family": fam, "kind": m.get("kind"), "value": m.get("value_a") or m.get("value_b"),
                          "bits": m.get("bits"), "extractor": extractor, "artifact_id": art, "role": role,
                          "path": safe_ref(roles.get(art, ("", ""))[1]) if art else None,
                          "pointer_a": m.get("evidence_a"), "pointer_b": m.get("evidence_b"),
                          "explanation_pl": m.get("explanation_pl")})
    return {"a": lo, "b": hi, "hypothesis": row["hypothesis"], "total_bits": d.get("total_bits"),
            "links": [{"source": s, "target": t, "bits": round(v, 2)} for (s, t), v in links.items()],
            "items": items}


# --------------------------------------------------------------------------------------- 4 revisions


@router.get("/revisions")
def revisions(request: Request, campaign_id: str | None = None) -> dict[str, Any]:
    """Review-driven revisions (runs.params.revision_of chain) with the membership diff each one caused."""
    with _st(request).reader() as conn:
        rows = conn.execute("SELECT id, params, created_at FROM runs WHERE kind='analyse' AND"
                            " json_extract(params, '$.revision_of') IS NOT NULL ORDER BY created_at").fetchall()
        reviews = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM reviews")}
        out = []
        for r in rows:
            p = _json(r["params"], {}) or {}
            before, after = _membership(conn, p.get("revision_of")), _membership(conn, r["id"])
            moved, renamed = membership_diff(before, after)
            rv = reviews.get(p.get("review_id") or "", {})
            entry = {"run_id": r["id"], "revision_of": p.get("revision_of"), "created_at": r["created_at"],
                     "review_id": p.get("review_id"), "action": p.get("action"), "analyst": rv.get("analyst"),
                     "note": rv.get("note"), "target": _json(rv.get("target"), {}) if rv else None,
                     "constraints": len(p.get("constraints") or []), "moved": moved, "renamed": renamed,
                     "before": _camp_sizes(before), "after": _camp_sizes(after)}
            if campaign_id is None or any(campaign_id in (x["from"], x["to"]) for x in moved) or \
                    campaign_id in (entry["target"] or {}).values():
                out.append(entry)
        latest = resolve_run(conn)
        constraints = (_run_row(conn, latest) or {}).get("params", {}).get("constraints") or [] if latest else []
    return {"items": out, "current_run": latest,
            "constraints": [{k: c.get(k) for k in ("kind", "a", "b", "review_id")} for c in constraints]}


def membership_diff(before: dict[str, str], after: dict[str, str]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Revisions may renumber campaigns: match each old campaign to the new one sharing most members (its
    successor); an incident moved only when it is not in its old campaign's successor."""
    old: dict[str, set[str]] = defaultdict(set)
    new: dict[str, set[str]] = defaultdict(set)
    for i, c in before.items():
        old[c].add(i)
    for i, c in after.items():
        new[c].add(i)
    succ = {}
    for c, members in old.items():
        best = max(new.items(), key=lambda kv: (len(members & kv[1]), -len(kv[1] ^ members), kv[0]), default=None)
        if best and members & best[1]:
            succ[c] = best[0]
    moved = [{"incident_id": i, "from": before.get(i), "to": after.get(i),
              "to_successor_of": next((o for o, n in succ.items() if n == after.get(i)), None)}
             for i in sorted(set(before) | set(after))
             if before.get(i) is None or after.get(i) is None or succ.get(before[i]) != after.get(i)]
    return moved, {o: n for o, n in succ.items() if o != n}


def _camp_sizes(m: dict[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = defaultdict(list)
    for i, c in m.items():
        out[c].append(i)
    return {c: sorted(v) for c, v in out.items() if len(v) > 1}


# --------------------------------------------------------------------------------------- 5 lifecycle + robustness


@router.get("/campaigns/{campaign_id}/lifecycle")
def campaign_lifecycle(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    """Display boundary: the recognised-secret policy (`hydra.redact.redact_obj`) applies to the whole body
    (CR-ASTRA-PIPE-305)."""
    out = _campaign_lifecycle(request, campaign_id, run_id)
    # Single owner of this path (CR-ASTRA-PIPE-401): the per-host domain lifecycle of `hydra.lifecycle`
    # (RDAP registration, certificates, CERT PL listing, lead times; replay cache only) is part of this body.
    from hydra.lifecycle import campaign_lifecycle as domain_lifecycle

    try:
        dom = domain_lifecycle(_st(request).settings, campaign_id, out["run_id"])
    except KeyError:
        dom = {}
    out |= {k: dom.get(k) for k in ("label", "domains", "campaign_first_events", "lead_times")}
    return redact_obj(out)


def _campaign_lifecycle(request: Request, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    """Gantt data: incidents by observed date, domain registration / certificate dates (RDAP / CT genes),
    first/last seen, shared-asset reuse; plus leave-one-family-out robustness of the campaign's links."""
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        ids = [r[0] for r in conn.execute("SELECT incident_id FROM campaign_members WHERE run_id=? AND campaign_id=?"
                                          " ORDER BY 1", (run, campaign_id))]
        if not ids:
            raise HTTPException(status_code=404, detail="campaign not in this run")
        incs = [{"id": r["id"], "observed_at": r["observed_at"], "title": r["title"]} for r in conn.execute(
            f"SELECT id, observed_at, title FROM incidents WHERE id IN ({_in(ids)}) ORDER BY observed_at, id", ids)]
        dates = []
        for r in conn.execute(
            f"SELECT incident_id, kind, value, evidence FROM genes WHERE incident_id IN ({_in(ids)}) AND status="
            f"'observed' AND kind IN ('registration_day','cert_first_day','cert_first_month','registration_month',"
            f"'ad_start_date','capture_date') ORDER BY value", ids
        ):
            e = _json(r["evidence"], {}) or {}
            dates.append({"incident_id": r["incident_id"], "kind": r["kind"], "date": r["value"],
                          "subject": str(e.get("note") or "").split(":")[0] or None})
        reuse = [{"value": r["value"], "incidents": sorted((r["ids"] or "").split(",")), "kind": r["kind"]}
                 for r in conn.execute(
            "SELECT kind, value, GROUP_CONCAT(DISTINCT incident_id) AS ids, COUNT(DISTINCT incident_id) AS n FROM genes"
            f" WHERE incident_id IN ({_in(ids)}) AND kind IN ('artifact_sha256','asset_sha256') AND status='observed'"
            f" GROUP BY kind, value HAVING n > 1 ORDER BY n DESC LIMIT 20", ids)]
        rob = []
        for r in conn.execute(f"SELECT a, b, hypothesis, detail FROM pairs WHERE run_id=? AND a IN ({_in(ids)}) AND"
                              f" b IN ({_in(ids)})", (run, *ids, *ids)):
            d = _json(r["detail"], {}) or {}
            if d.get("robustness"):
                rob.append({"a": r["a"], "b": r["b"], "hypothesis": r["hypothesis"], "robustness": d["robustness"],
                            "support": d.get("support"), "dominant_family": _dominant_family(d.get("family_bits", {}))})
        th = ((_run_row(conn, run) or {}).get("params") or {}).get("thresholds") or {}
    observed = [i["observed_at"] for i in incs if i["observed_at"]]
    return {"campaign_id": campaign_id, "run_id": run, "incidents": incs, "dates": dates, "asset_reuse": reuse,
            "first_seen": min(observed) if observed else None, "last_seen": max(observed) if observed else None,
            "robustness": rob, "link_threshold": th.get("link")}


# --------------------------------------------------------------------------------------- 7 gene explainer


@router.get("/genes/explain")
def gene_explain(request: Request, kind: str, value: str, run_id: str | None = None) -> dict[str, Any]:
    """Display boundary: the recognised-secret policy (`hydra.redact.redact_obj`) applies to the whole body
    (CR-ASTRA-PIPE-305)."""
    return redact_obj(_gene_explain(request, kind, value, run_id))


def _gene_explain(request: Request, kind: str, value: str, run_id: str | None = None) -> dict[str, Any]:
    """Why a gene counts: its document frequency vs the df distribution of its kind, level/tier, family cap and
    the bits it actually contributed to scored pairs of the run."""
    with _st(request).reader() as conn:
        run = resolve_run(conn, run_id)
        scope = run_incidents(conn, run)
        rows = conn.execute(f"SELECT value, COUNT(DISTINCT incident_id) AS df FROM genes WHERE kind=? AND status="
                            f"'observed' AND value IS NOT NULL AND incident_id IN ({_in(scope)}) GROUP BY value",
                            (kind, *scope)).fetchall()
        this = next((r["df"] for r in rows if r["value"] == value), 0)
        hist = Counter(r["df"] for r in rows)
        g = conn.execute("SELECT family, kind, dependency_group, provenance, extractor FROM genes WHERE kind=? AND"
                         " value=? LIMIT 1", (kind, value)).fetchone()
        if g is None:
            raise HTTPException(status_code=404, detail="gene value not found")
        level, tier = _gene_level(g)
        holders = [r[0] for r in conn.execute(f"SELECT DISTINCT incident_id FROM genes WHERE kind=? AND value=? AND"
                                              f" incident_id IN ({_in(scope)}) ORDER BY 1", (kind, value, *scope))]
        used = []
        for rid in _assignment_runs(conn, run):
            for r in conn.execute("SELECT a, b, detail FROM pairs WHERE run_id=?", (rid,)):
                for mm in (_json(r["detail"], {}) or {}).get("reasons") or []:
                    if mm.get("kind") == kind and value in (mm.get("value_a"), mm.get("value_b")):
                        used.append({"a": r["a"], "b": r["b"], "bits": mm.get("bits"),
                                     "explanation_pl": mm.get("explanation_pl")})
    try:
        from hydra.synapse import FAMILY_CAP, GROUP_CAP, PLATFORM_KIT_CAP  # type: ignore[attr-defined]
        caps = {"family_cap": FAMILY_CAP, "group_cap": GROUP_CAP, "platform_kit_cap": PLATFORM_KIT_CAP}
    except ImportError:
        caps = {}
    return {"kind": kind, "value": value, "family": g["family"], "extractor": g["extractor"],
            "provenance": g["provenance"], "dependency_group": g["dependency_group"], "level": level, "tier": tier,
            "df": this, "incidents_in_run": len(scope), "holders": holders,
            "df_histogram": [{"df": k, "values": v} for k, v in sorted(hist.items())],
            "caps": caps, "used_in_pairs": sorted(used, key=lambda x: -(x["bits"] or 0))[:20]}


# --------------------------------------------------------------------------------------- 8 corpus stats


@router.get("/corpora/stats")
def corpora_stats(request: Request) -> dict[str, Any]:
    """Small multiples per corpus (nask / upload / ext:* / public:*): incidents, genes by family, campaigns of the
    newest run that contains them."""
    with _st(request).reader() as conn:
        by: dict[str, list[str]] = defaultdict(list)
        for r in conn.execute("SELECT id, source FROM incidents"):
            by[_corpus_of(r["source"])].append(r["id"])
        latest = resolve_run(conn)
        members = _membership(conn, latest)
        groups = _groups(conn, latest) if latest else []
        out = []
        for corpus, ids in sorted(by.items()):
            fam = {r[0]: r[1] for r in conn.execute(f"SELECT family, COUNT(*) FROM genes WHERE status='observed' AND"
                                                    f" incident_id IN ({_in(ids)}) GROUP BY 1", ids)}
            camps = Counter(members[i] for i in ids if i in members)
            out.append({"corpus": corpus, "incidents": len(ids), "genes_by_family": {f: fam.get(f, 0)
                                                                                    for f in FAMILY_ORDER},
                        "campaigns_in_latest_run": len(camps), "multi_member": sum(1 for v in camps.values() if v > 1),
                        "in_latest_run": sum(camps.values())})
    return {"latest_run": latest, "groups": len(groups), "items": out}
