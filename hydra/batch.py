"""Batch assignment of new material with cross-comparison inside the batch (GAP-AUDIT B3, B17 duplicates).

`hydra assign-batch` and `POST /v1/assign-batch` run every item through the single-item dispatcher
(`pipeline.analyse_input`, the same as `hydra assign`, the GUI and the scheduler inbox: each item is decided against
the fixed base run only, and gets the per-file `input_report`). Afterwards:

1. **Duplicates** (B17): an item whose incident has exactly the artefact set (sha256) of an earlier batch item or
   of a base-run incident gets `duplicate_of` (incident id) and `duplicate_of_item` (batch item name). A single
   file whose bytes were analysed before (any earlier run) is reused by `analyse_input` itself, which reports the
   same `duplicate_of` field (`duplicate_of_item` null); both rules write `Assignment.meta.duplicate_of`.
2. **Cross-comparison** (B3): the items that came back `new_campaign` are scored against each other
   (`synapse.score_pair`, rarity over base run + whole batch, kit units = base campaign groups, the base run's
   prior and fusion model) and clustered with the batch rule (`nexus.cluster`: connected components over
   `same_campaign` pairs). Every such item gets a `new_pattern_id` (`N-<sha8 of members>`); items of one unseen
   campaign share it, with the shared genes (core genome) and the linking pairs as reasons.
3. Known campaigns are never touched: `assign` / `abstain` decisions stay as the open-set path made them. A
   `new_campaign` item that links to an item assigned to a known campaign is only reported (`batch_links`), it is
   not moved into that campaign (no evidence against the campaign itself was found).

The batch is persisted as a run of kind `assign_batch`: pairs among the new items, the new patterns as campaigns
of that run (so `/v1/campaigns/{id}?run_id=` and the lineage view work on them), the report in `runs.metrics`;
the members' assignment rows get `meta.new_pattern_id`, `meta.new_pattern`, `meta.duplicate_of`, `meta.batch_run`.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from hydra import events
from hydra.config import Settings
from hydra.contracts import Assignment, Hypothesis
from hydra.db import Writer, connect
from hydra.labels_pl import num_pl
from hydra.redact import redact_obj, redact_text
from hydra.synapse import PairEval, Thresholds, is_linked, robustness, score_pair

RUN_KIND = "assign_batch"
MAX_LINK_REASONS = 5


def batch_items(folder: Path) -> list[Path]:
    """One item per incident subfolder and per top-level file (ZIP, image, video, HTML...); hidden entries skipped."""
    return sorted(p for p in Path(folder).iterdir() if not p.name.startswith((".", "__MACOSX")))


def pattern_id(members: list[str]) -> str:
    return "N-" + hashlib.sha256("|".join(sorted(members)).encode()).hexdigest()[:8]


def _row(item: str, base: str, run_id: str, a: Assignment) -> dict[str, Any]:
    top = a.candidates[0] if a.candidates else None
    return {"item": item, "base_run": base, "assign_run": run_id, "incident_id": a.incident_id,
            "decision": str(a.decision), "campaign_id": a.campaign_id, "group_id": a.group_id,
            "top_candidate": top.campaign_id if top else None,
            "top_bits": top.best_pair.total_bits if top and top.best_pair else None,
            "top_support": top.support if top else None, "reason_pl": a.reason_pl,
            "candidates": [{"campaign_id": c.campaign_id, "probability": c.probability, "support": c.support,
                            "total_bits": c.best_pair.total_bits if c.best_pair else None} for c in a.candidates]}


def assign_batch(items: list[Path | str], settings: Settings, *, base_run: str | None = None,
                 thresholds: Thresholds | None = None, only: list[str] | None = None, writer: Writer | None = None,
                 job_id: str | None = None,
                 on_item: Callable[[int, int, dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Assign every item against one fixed base run, then detect duplicates and group the new patterns of the
    batch among themselves. Returns the report (`items` rows, `new_patterns`, `duplicates`, counts)."""
    from hydra.pipeline import analyse_input, detect_input_kind, latest_run

    th = thresholds or Thresholds()
    settings.ensure_dirs()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        base = base_run or latest_run(conn)
    if base is None:
        raise ValueError("no analyse run yet: run `hydra analyse <known-base-folder>` first")
    own_writer = writer is None
    writer = writer or Writer(settings.db_path)
    t_all = time.monotonic()
    rows: list[dict[str, Any]] = []
    try:
        for i, item in enumerate(items, 1):
            t0 = time.monotonic()
            name = Path(item).name
            row: dict[str, Any] = {"item": name, "base_run": base}
            try:
                out = analyse_input(detect_input_kind(str(item)), str(item), settings, base_run=base,
                                    thresholds=th, only=only, writer=writer)
                if out.get("assignment") is None:  # e.g. a ZIP rejected before unpacking (input_report says why)
                    row.update({"decision": "error", "input_report": out.get("input_report"),
                                "reason_pl": str((out.get("input_report") or {}).get("summary_pl")
                                                 or "brak decyzji")[:500]})
                else:
                    row = _row(name, base, out["run_id"], Assignment.model_validate(out["assignment"]))
                    row["input_report"] = out.get("input_report")
                    if out.get("duplicate_of"):  # same bytes analysed before (pipeline._already_imported)
                        row.update({"duplicate_of": out["duplicate_of"], "duplicate_of_item": None,
                                    "duplicate_reason_pl": " ".join(out.get("notes") or [])[:500] or None})
            except Exception as exc:  # noqa: BLE001 — one broken item must not stop the batch
                row.update({"decision": "error",
                            "reason_pl": f"błąd przetwarzania: {type(exc).__name__}: {exc}"[:500]})
            row["seconds"] = round(time.monotonic() - t0, 2)
            rows.append(row)
            if on_item:
                on_item(i, len(items), row)
        report = cross_compare(rows, settings, writer, base, th, job_id=job_id)
    finally:
        if own_writer:
            writer.close()
    report["seconds"] = round(time.monotonic() - t_all, 1)
    return report


# --------------------------------------------------------------------------------------- duplicates


def _artifact_sets(conn: sqlite3.Connection, ids: list[str] | None = None) -> dict[str, frozenset[str]]:
    q = "SELECT incident_id, artifact_id FROM incident_artifacts"
    args: list[str] = []
    if ids is not None:
        if not ids:
            return {}
        q += f" WHERE incident_id IN ({','.join('?' * len(ids))})"
        args = ids
    acc: dict[str, set[str]] = {}
    for r in conn.execute(q, args):
        acc.setdefault(r["incident_id"], set()).add(r["artifact_id"])
    return {k: frozenset(v) for k, v in acc.items()}


def find_duplicates(rows: list[dict[str, Any]], conn: sqlite3.Connection, base_ids: list[str]
                    ) -> dict[int, dict[str, Any]]:
    """Row index → {duplicate_of, duplicate_of_item, reason_pl}. Same incident id as an earlier row (the same
    bytes or folder imported twice) or the identical artefact set (sha256) of an earlier row or a base incident."""
    batch_ids = [r["incident_id"] for r in rows if r.get("incident_id")]
    sets = _artifact_sets(conn, sorted(set(batch_ids)))
    base_sets = _artifact_sets(conn, base_ids)
    base_by_set: dict[frozenset[str], str] = {}
    for iid in sorted(base_sets):
        base_by_set.setdefault(base_sets[iid], iid)
    seen_id: dict[str, int] = {}
    seen_set: dict[frozenset[str], int] = {}
    out: dict[int, dict[str, Any]] = {}
    for i, r in enumerate(rows):
        iid = r.get("incident_id")
        if not iid:
            continue
        art = sets.get(iid, frozenset())
        if iid in seen_id:
            j = seen_id[iid]
            out[i] = {"duplicate_of": iid, "duplicate_of_item": rows[j]["item"],
                      "reason_pl": f"Duplikat materiału {rows[j]['item']} z tej partii (ten sam incydent {iid})."}
        elif art and art in seen_set:
            j = seen_set[art]
            out[i] = {"duplicate_of": rows[j]["incident_id"], "duplicate_of_item": rows[j]["item"],
                      "reason_pl": (f"Duplikat materiału {rows[j]['item']} z tej partii: identyczne pliki"
                                    f" (sha256, {len(art)})." )}
        elif art and art in base_by_set and base_by_set[art] != iid:
            out[i] = {"duplicate_of": base_by_set[art], "duplicate_of_item": None,
                      "reason_pl": (f"Duplikat znanego incydentu {base_by_set[art]}: identyczne pliki"
                                    f" (sha256, {len(art)}).")}
        seen_id.setdefault(iid, i)
        if art:
            seen_set.setdefault(art, i)
    return out


# --------------------------------------------------------------------------------------- cross-comparison


def _link(pe: PairEval) -> dict[str, Any]:
    return {"a": pe.score.a, "b": pe.score.b, "hypothesis": str(pe.score.hypothesis),
            "total_bits": pe.score.total_bits, "support": pe.support, "campaign_bits": pe.campaign_bits,
            "kit_bits": pe.kit_bits,
            "reasons": [{"family": str(r.family), "kind": r.kind, "bits": r.bits, "explanation_pl": r.explanation_pl}
                        for r in pe.score.reasons if r.bits > 0 and r.kind != "cap_correction"][:MAX_LINK_REASONS]}


def _pattern_reason(pid: str, items: list[str], core: list[dict[str, Any]], links: list[dict[str, Any]]) -> str:
    if len(items) < 2:
        return f"Nowy wzorzec {pid}: w tej partii żaden inny nowy materiał nie ma z nim dowodów na poziomie kampanii."
    genes = "; ".join(c["explanation_pl"].split(" — ")[0] for c in core[:3]) or "brak wspólnych genów rdzenia"
    weakest = min((lk["support"] for lk in links if lk["hypothesis"] == Hypothesis.SAME_CAMPAIGN), default=0.0)
    return (f"Nowy wzorzec {pid}: {len(items)} nowe materiały z tej partii ({', '.join(items)}) łączą dowody na"
            f" poziomie kampanii (najsłabsze ogniwo {num_pl(weakest)} bit); wspólne cechy: {genes}. Żadna znana"
            " kampania nie ma wystarczających dowodów.")


def cross_compare(rows: list[dict[str, Any]], settings: Settings, writer: Writer, base_run: str, th: Thresholds,
                  *, job_id: str | None = None) -> dict[str, Any]:
    """Post-pass over finished batch rows (mutated in place): duplicates, then grouping of the `new_campaign`
    items among themselves. Writes one `assign_batch` run and annotates the members' assignment rows."""
    from hydra.entropy import apply_operator_seed_units
    from hydra.genome.vid_seq import annotate_footage
    from hydra.nexus import cluster
    from hydra.pipeline import (
        active_cannot_links,
        load_clustering,
        load_genomes,
        new_run_id,
        rarity_for,
        scoring_context,
    )

    with closing(connect(settings.db_path, readonly=True)) as conn:
        campaigns, _groups = load_clustering(conn, base_run)
        mrow = conn.execute("SELECT params FROM runs WHERE id = ?", (base_run,)).fetchone()
        base_params = json.loads(mrow["params"]) if mrow else {}
        base_ids = sorted({m for c in campaigns for m in c.members})
        dups = find_duplicates(rows, conn, base_ids)
    for i, r in enumerate(rows):  # re-upload of bytes analysed in an earlier run (analyse_input), same field
        if i not in dups and r.get("duplicate_of"):
            dups[i] = {"duplicate_of": r["duplicate_of"], "duplicate_of_item": None,
                       "reason_pl": r.get("duplicate_reason_pl")
                       or f"Te same bajty były już analizowane jako incydent {r['duplicate_of']}."}
    with closing(connect(settings.db_path, readonly=True)) as conn:
        batch_ids = sorted({r["incident_id"] for r in rows if r.get("incident_id")} - set(base_ids))
        genomes = load_genomes(conn, base_ids + batch_ids)
    for i, d in dups.items():
        rows[i].update({"duplicate_of": d["duplicate_of"], "duplicate_of_item": d["duplicate_of_item"],
                        "duplicate_reason_pl": d["reason_pl"]})
    # the first row of each incident and decision new_campaign takes part; duplicates of a batch row are the
    # same incident (or the same files) and follow their original
    first_row: dict[str, int] = {}
    for i, r in enumerate(rows):
        if r.get("incident_id"):
            first_row.setdefault(r["incident_id"], i)
    new_ids = sorted(iid for iid, i in first_row.items()
                     if rows[i].get("decision") == "new_campaign" and iid in genomes and iid not in base_ids
                     and not (i in dups and dups[i].get("duplicate_of_item")))
    known_ids = sorted(iid for iid, i in first_row.items()
                       if rows[i].get("decision") in ("assign", "abstain") and iid in genomes and iid not in base_ids)
    annotate_footage(genomes)
    _root, prior, fusion = scoring_context(settings, base_run, base_params, set(genomes))
    rarity = rarity_for(genomes, prior=prior)
    # operator values counted per shared-operator unit, the `analyse` / assignment regime (finding C1)
    apply_operator_seed_units(rarity, {k: g.genes for k, g in genomes.items()})
    rarity.kit_unit_of = {m: (c.group_id or c.id) for c in campaigns for m in c.members}
    pairs: dict[tuple[str, str], PairEval] = {}
    # candidates as in `analyse`: every pair below 60 batch items, index candidates above (retrieval.candidates)
    from hydra.retrieval import candidates as retrieve

    cand = retrieve({i: genomes[i] for i in new_ids + known_ids}, rarity) if len(new_ids) + len(known_ids) > 1 \
        else {}
    known_set = set(known_ids)
    for (a, b), src in sorted(cand.items()):
        if a in known_set and b in known_set:
            continue
        pe = score_pair(genomes[a], genomes[b], rarity, th, sorted(src))
        if fusion is not None and getattr(fusion, "_lr", None) is not None:
            fusion.apply(pe, th)
        if is_linked(pe, th):
            pe.robustness = robustness(genomes[a], genomes[b], rarity, pe, th)
        pairs[(a, b)] = pe
    new_set = set(new_ids)
    inner = {k: pe for k, pe in pairs.items() if k[0] in new_set and k[1] in new_set}
    cannot = active_cannot_links(settings, base_params, new_set) if new_ids else []
    clustering = cluster({i: genomes[i] for i in new_ids}, inner, rarity, th, cannot_link=cannot) if new_ids \
        else None
    item_of: dict[str, list[str]] = {}
    for r in rows:
        if r.get("incident_id"):
            item_of.setdefault(r["incident_id"], []).append(r["item"])
    run_id = new_run_id(RUN_KIND)
    patterns: list[dict[str, Any]] = []
    pattern_of: dict[str, dict[str, Any]] = {}
    renamed = []
    for c in clustering.campaigns if clustering else []:
        pid = pattern_id(c.members)
        links = [_link(pe) for (a, b), pe in sorted(inner.items())
                 if a in c.members and b in c.members and pe.score.hypothesis == Hypothesis.SAME_CAMPAIGN]
        core = [m.model_dump(mode="json") for m in c.core_genome]
        items = [it for m in c.members for it in item_of.get(m, [])[:1]]
        known_groups = sorted({rows[first_row[m]].get("group_id") for m in c.members} - {None})
        p = {"id": pid, "members": list(c.members), "items": items, "size": len(c.members), "label": c.label,
             "batch_group_id": c.group_id, "known_group_ids": known_groups, "core_genome": core, "links": links,
             "reason_pl": _pattern_reason(pid, items, core, links)}
        patterns.append(p)
        for m in c.members:
            pattern_of[m] = p
        renamed.append(c.model_copy(update={"id": pid, "meta": {**c.meta, "batch_run": run_id, "kind": "new_pattern",
                                                                  "base_run": base_run}}))
    if clustering:
        by_old = {c.id: n.id for c, n in zip(clustering.campaigns, renamed, strict=True)}
        for g in clustering.groups:
            g.campaign_ids = [by_old.get(x, x) for x in g.campaign_ids]
        clustering.campaigns = renamed
        clustering.membership = {m: pattern_of[m]["id"] for m in pattern_of}
    # links of new items to batch items that went to a known campaign or abstained: reported, never merged
    side: dict[str, list[dict[str, Any]]] = {}
    for (a, b), pe in pairs.items():
        if pe.score.hypothesis != Hypothesis.SAME_CAMPAIGN or (a in new_set) == (b in new_set):
            continue
        n_, k_ = (a, b) if a in new_set else (b, a)
        kr = rows[first_row[k_]]
        side.setdefault(n_, []).append({"incident_id": k_, "item": kr["item"], "decision": kr["decision"],
                                        "campaign_id": kr.get("campaign_id"), "support": pe.support})
    for r in rows:
        iid = r.get("incident_id")
        if not iid:
            continue
        p = pattern_of.get(iid)
        if r.get("duplicate_of_item") and r["duplicate_of"] in pattern_of:
            p = pattern_of[r["duplicate_of"]]
        if p is not None and r.get("decision") == "new_campaign":
            r.update({"new_pattern_id": p["id"], "new_pattern_size": p["size"],
                      "new_pattern_members": p["items"]})
        if iid in side:
            r["batch_links"] = side[iid]
        r["batch_run"] = run_id
    decisions = Counter(r["decision"] for r in rows)
    multi = [p for p in patterns if p["size"] > 1]
    report = {"base_run": base_run, "batch_run": run_id, "job_id": job_id, "items": rows,
              "decisions": dict(decisions), "new_patterns": patterns,
              "summary": {"items": len(rows), "new_campaign_items": len(new_ids), "new_patterns": len(patterns),
                          "multi_member_new_patterns": len(multi),
                          "items_in_multi_member_patterns": sum(p["size"] for p in multi),
                          "duplicates": len(dups), "pairs_scored": len(pairs)},
              "duplicates": [{"item": rows[i]["item"], **d} for i, d in sorted(dups.items())],
              "note_pl": ("Każdy materiał oceniono najpierw osobno względem znanych kampanii (bez zmian). Następnie"
                          " materiały z decyzją „nowy wzorzec” porównano między sobą tą samą regułą co w analizie"
                          " zbioru; materiały jednej nieznanej kampanii dostają wspólny identyfikator nowego wzorca."
                          " Do znanej kampanii nic nie jest dołączane bez dowodów wobec niej samej.")}
    _persist(writer, run_id, base_run, rows, report, pairs, clustering, pattern_of, dups, job_id)
    return report


def _persist(writer: Writer, run_id: str, base_run: str, rows: list[dict[str, Any]], report: dict[str, Any],
             pairs: dict[tuple[str, str], PairEval], clustering: Any, pattern_of: dict[str, dict[str, Any]],
             dups: dict[int, dict[str, Any]], job_id: str | None) -> None:
    from hydra.pipeline import _write_run

    params = {"base_run": base_run, "job_id": job_id, "items": [redact_text(r["item"]) for r in rows]}
    metrics = {k: report[k] for k in ("decisions", "summary", "note_pl")}
    metrics["new_patterns"] = [{k: v for k, v in p.items() if k != "core_genome"} for p in report["new_patterns"]]
    metrics["duplicates"] = report["duplicates"]
    metrics["items"] = [{k: r.get(k) for k in ("item", "incident_id", "decision", "campaign_id", "group_id",
                                               "new_pattern_id", "duplicate_of", "duplicate_of_item", "assign_run")}
                        for r in rows]
    dup_by_incident = {rows[i]["incident_id"]: d for i, d in dups.items()}

    def _tx(conn: sqlite3.Connection) -> None:
        _write_run(conn, run_id, RUN_KIND, params, redact_obj(metrics), pairs, clustering)
        for r in rows:
            iid, arun = r.get("incident_id"), r.get("assign_run")
            if not iid or not arun:
                continue
            row = conn.execute("SELECT detail FROM assignments WHERE run_id=? AND incident_id=?",
                               (arun, iid)).fetchone()
            if row is None:
                continue
            detail = json.loads(row["detail"])
            meta = detail.setdefault("meta", {})
            meta["batch_run"] = run_id
            if r.get("new_pattern_id"):
                p = pattern_of.get(iid) or {}
                meta["new_pattern_id"] = r["new_pattern_id"]
                meta["new_pattern"] = {"id": r["new_pattern_id"], "size": r.get("new_pattern_size"),
                                       "members": p.get("members", []), "batch_run": run_id,
                                       "reason_pl": p.get("reason_pl")}
                if (r.get("new_pattern_size") or 0) > 1 and "Nowy wzorzec N-" not in detail.get("reason_pl", ""):
                    detail["reason_pl"] = (detail.get("reason_pl", "") + " W tej partii: wspólny nowy wzorzec"
                                           f" {r['new_pattern_id']} ({r['new_pattern_size']} materiały).")
            d = dup_by_incident.get(iid)
            if d:
                meta["duplicate_of"] = d["duplicate_of"]
                meta["duplicate_of_item"] = d["duplicate_of_item"]
            if r.get("batch_links"):
                meta["batch_links"] = r["batch_links"]
            conn.execute("UPDATE assignments SET detail=? WHERE run_id=? AND incident_id=?",
                         (events.canonical_json(redact_obj(detail)), arun, iid))

    writer.run(_tx, timeout=600)


def load_batch(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    """The stored report of a batch run by run id or job id."""
    row = conn.execute("SELECT id, params, metrics, created_at FROM runs WHERE kind=? AND (id=? OR"
                       " json_extract(params, '$.job_id')=?) ORDER BY created_at DESC LIMIT 1",
                       (RUN_KIND, key, key)).fetchone()
    if row is None:
        return None
    params, metrics = json.loads(row["params"]), json.loads(row["metrics"])
    return {"batch_run": row["id"], "created_at": row["created_at"], "base_run": params.get("base_run"),
            "job_id": params.get("job_id"), **metrics}


CSV_COLUMNS = ["item", "decision", "campaign_id", "group_id", "new_pattern_id", "new_pattern_size",
               "duplicate_of", "duplicate_of_item", "top_candidate", "top_bits", "top_support", "incident_id",
               "reason_pl", "seconds", "base_run", "assign_run", "batch_run"]


def write_outputs(report: dict[str, Any], out: Path) -> None:
    """assignments.json (rows), assignments.csv, new_patterns.json (patterns with members and reasons)."""
    import csv

    out.mkdir(parents=True, exist_ok=True)
    (out / "assignments.json").write_text(json.dumps(report["items"], indent=2, ensure_ascii=False, default=str)
                                          + "\n", encoding="utf-8")
    from hydra.export_safe import csv_cell

    with (out / "assignments.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        # Spreadsheet-safe cells (CR-ASTRA-SEC-10); item names stay exact apart from a formula prefix, the
        # untouched values are in assignments.json.
        w.writerows({k: csv_cell(v, redact=k == "reason_pl") for k, v in r.items()} for r in report["items"])
    (out / "new_patterns.json").write_text(json.dumps(
        {k: report[k] for k in ("base_run", "batch_run", "summary", "new_patterns", "duplicates", "note_pl")},
        indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
