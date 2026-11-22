"""Analyst reviews that change the engine state (audit A04/A05): constraints + published campaign revisions.

Automatic assignments are candidates only; they never change campaigns (no self-reinforcing contamination).
An analyst review turns into constraints and publishes a new campaign revision:

| action | target | constraint(s) |
|---|---|---|
| accept_link | {a, b} | must_link(a, b); a new incident becomes a member |
| reject_link | {a, b} | cannot_link(a, b) |
| move_incident | {incident_id, campaign_id} | cannot_link to its current campaign mates; must_link to one member of the
  target campaign; `campaign_id = "new"` (or empty) publishes the incident as a new pattern (its own campaign) |
| merge_campaigns | {campaign_ids: [...]} | must_link between one member of each campaign |
| split_campaign | {campaign_id, incident_ids: [...]} | cannot_link between the listed incidents and the other members,
  must_link inside the listed part |
| ignore_value | {kind, value} | the gene value moves to the boilerplate tier: 0 bits in every pair (ADOPT #2); pairs
  holding it are rescored in the revision. Needs `ReviewAction.IGNORE_VALUE` (CR-HARDEN-1); until the contract has
  it, `resolve_constraints` accepts the raw action string |
| undo | review.undoes = review id | the constraints of that review are dropped |

Campaign ids name member sets, so targets are resolved against the CURRENT revision when the review is applied
and stored as concrete incident pairs. A revision is a run of kind `analyse` (so every reader shows it) with
`params.revision_of`, `params.root_run`, `params.constraints` (all active constraints, review id per item) and
`params.added` (incidents published by analysts). It is recomputed from the root batch run: root pairs + pairs
from assignment runs of added incidents, `nexus.cluster(..., must_link, cannot_link)`. Undo therefore restores the
previous membership exactly, and the history stays in `reviews` + the hash-chained audit log.

The UI calls `apply_review(review_id | Review, settings, writer) -> new_run_id` right after POST /v1/reviews.

ADOPT #14 additions (2026-11-21):
- "niepewne" (unsure): `mark_unsure` / `undo_unsure` append REVIEW_UNSURE / REVIEW_UNSURE_UNDONE events to the
  hash-chained audit log. A mark takes a pair or an assignment out of the analyst queue and never changes
  campaigns. `ReviewAction` has no unsure value (contracts are owned by the architect, CR-REVIEW-1), so the marks
  live in the audit log, not in `reviews`.
- Dependent assignments: when a revision separates campaigns by cannot-link, every open-set assignment of the
  same root whose link-strength pairs reach two separated campaigns becomes ABSTAIN in the new revision
  (`assignments` rows of the revision run, `meta.bridge_cannot_link`); an undo that removes the separation
  restores the assignment to the campaign of its strongest link.

CR-E2E-5 (2026-11-21): material assigned after the analysis (assign runs with `params.base_run` = the root or an
earlier revision) stays attached to every later revision: readers resolve assign runs over the `revision_of`
lineage (`hydra.dataflow.assign_runs`), and an assignment whose campaign changed its member set (so its id) in the
new revision is re-pointed there (`meta.carried_over`) to the campaign of its strongest link, else to the one
holding most of the old members.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from typing import Any

from hydra import events
from hydra.config import Settings
from hydra.contracts import Assignment, AssignmentDecision, Review, ReviewAction
from hydra.db import Writer, connect
from hydra.labels_pl import num_pl
from hydra.synapse import PairEval, Thresholds

NEW = {"new", "", None}
IGNORE_VALUE = "ignore_value"  # pending ReviewAction.IGNORE_VALUE (CR-HARDEN-1)


def _run_params(conn: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT params FROM runs WHERE id = ?", (run_id,)).fetchone()
    return json.loads(row["params"]) if row else {}


def _membership(conn: sqlite3.Connection, run_id: str) -> dict[str, str]:
    return {r["incident_id"]: r["campaign_id"]
            for r in conn.execute("SELECT campaign_id, incident_id FROM campaign_members WHERE run_id = ?", (run_id,))}


def _load_review(conn: sqlite3.Connection, review: str | Review) -> Review | None:
    if isinstance(review, Review):
        return review
    r = conn.execute("SELECT * FROM reviews WHERE id = ?", (review,)).fetchone()
    if r is None:
        return None
    fields = dict(id=r["id"], action=r["action"], target=json.loads(r["target"]), analyst=r["analyst"],
                  note=r["note"], created_at=r["created_at"], undoes=r["undoes"])
    if r["action"] == IGNORE_VALUE and IGNORE_VALUE not in {a.value for a in ReviewAction}:
        return Review.model_construct(**fields)  # until CR-HARDEN-1 adds the enum member
    return Review(**fields)


def resolve_constraints(review: Review, members: dict[str, str]) -> tuple[list[dict], list[str]]:
    """Review → concrete constraints against the current revision membership (incident → campaign)."""
    t, rid = dict(review.target or {}), review.id
    by_campaign: dict[str, list[str]] = {}
    for inc, c in members.items():
        by_campaign.setdefault(c, []).append(inc)
    out: list[dict] = []
    added: list[str] = []

    def c(kind: str, a: str, b: str) -> None:
        if a != b:
            out.append({"review_id": rid, "kind": kind, "a": a, "b": b})

    if str(getattr(review.action, "value", review.action)) == IGNORE_VALUE:
        kind, value = str(t.get("kind") or ""), t.get("value")
        if not kind or value is None:
            raise ValueError("ignore_value needs target.kind and target.value")
        out.append({"review_id": rid, "kind": "ignore", "a": kind, "b": str(value)})
        return out, added
    act = ReviewAction(review.action)
    if act in (ReviewAction.ACCEPT_LINK, ReviewAction.REJECT_LINK):
        a, b = str(t["a"]), str(t["b"])
        c("must" if act == ReviewAction.ACCEPT_LINK else "cannot", a, b)
        if act == ReviewAction.ACCEPT_LINK:
            added += [x for x in (a, b) if x not in members]
    elif act == ReviewAction.MOVE_INCIDENT:
        x, target = str(t["incident_id"]), t.get("campaign_id")
        for mate in by_campaign.get(members.get(x, ""), []):
            c("cannot", x, mate)
        if target not in NEW:
            anchors = sorted(by_campaign.get(str(target), []))
            if not anchors:
                raise ValueError(f"unknown campaign {target!r} in the current revision")
            c("must", x, anchors[0])
        if x not in members:
            added.append(x)
        out.append({"review_id": rid, "kind": "pin", "a": x, "b": x})  # keep as its own pattern if alone
    elif act == ReviewAction.MERGE_CAMPAIGNS:
        heads = [sorted(by_campaign.get(str(cid), []))[0] for cid in t.get("campaign_ids", [])
                 if by_campaign.get(str(cid))]
        for h in heads[1:]:
            c("must", heads[0], h)
    elif act == ReviewAction.SPLIT_CAMPAIGN:
        part = [str(x) for x in t.get("incident_ids", [])]
        rest = [m for m in by_campaign.get(str(t["campaign_id"]), []) if m not in part]
        if not part or not rest:
            raise ValueError("split_campaign needs target.incident_ids: a proper subset of the campaign")
        for x in part:
            for y in rest:
                c("cannot", x, y)
        for x in part[1:]:
            c("must", part[0], x)
    return out, added


def _pairs_for(conn: sqlite3.Connection, root: str, added: list[str]) -> dict[tuple[str, str], PairEval]:
    """Root run pairs + the newest stored pair for every pair touching an added incident (assignment runs)."""
    from hydra.pipeline import pair_score_from_detail

    def pe_of(detail: dict) -> PairEval:
        pe = PairEval(pair_score_from_detail(detail), float(detail.get("campaign_bits", 0.0)),
                      float(detail.get("kit_bits", 0.0)), float(detail.get("asset_bits", 0.0)),
                      dict(detail.get("campaign_by_family") or {}), float(detail.get("operator_bits", 0.0)),
                      float(detail.get("support", 0.0)), kit_groups=int(detail.get("kit_groups", 2)),
                      kit_min_df=detail.get("kit_min_df"))
        pe.robustness = detail.get("robustness")
        return pe

    out = {(r["a"], r["b"]): pe_of(json.loads(r["detail"]))
           for r in conn.execute("SELECT a, b, detail FROM pairs WHERE run_id = ?", (root,))}
    if added:
        q = ",".join("?" * len(added))
        for r in conn.execute(
            f"SELECT p.a, p.b, p.detail FROM pairs p JOIN runs r ON r.id = p.run_id WHERE (p.a IN ({q}) OR p.b IN"
            f" ({q})) ORDER BY r.created_at", [*added, *added]):
            out[(r["a"], r["b"])] = pe_of(json.loads(r["detail"]))
    return out


def rebuild(settings: Settings, root: str, constraints: list[dict], added: list[str],
            th: Thresholds | None = None):
    """Campaigns of the root run's incidents + analyst-added incidents under all active constraints."""
    from hydra.nexus import cluster
    from hydra.pipeline import load_genomes, rarity_for

    th = th or Thresholds()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        ids = sorted(set(_membership(conn, root)) | set(added))
        genomes = load_genomes(conn, ids)
        pairs = {k: v for k, v in _pairs_for(conn, root, added).items() if k[0] in genomes and k[1] in genomes}
    must = [(x["a"], x["b"]) for x in constraints if x["kind"] == "must"]
    cannot = [(x["a"], x["b"]) for x in constraints if x["kind"] == "cannot"]
    rarity = rarity_for(genomes)
    rarity.ignored = {(x["a"], x["b"]) for x in constraints if x["kind"] == "ignore"}
    if rarity.ignored:
        pairs = rescore_ignored(genomes, pairs, rarity, th)
    return genomes, pairs, cluster(genomes, pairs, rarity, th, must_link=must, cannot_link=cannot)


def rescore_ignored(genomes: dict, pairs: dict[tuple[str, str], PairEval], rarity, th: Thresholds
                    ) -> dict[tuple[str, str], PairEval]:
    """Rescore the stored pairs whose two incidents both hold an ignored value (`rarity.ignored`, 0 bits); the other
    pairs keep their stored score. Undo drops the constraint, so the next revision uses the stored scores again."""
    from hydra.synapse import score_pair

    holders = set()
    for key in rarity.ignored:
        holders |= rarity.postings.get(key, set())
    out = dict(pairs)
    for (a, b), pe in pairs.items():
        if a in holders and b in holders and a in genomes and b in genomes:
            new = score_pair(genomes[a], genomes[b], rarity, th, pe.score.candidate_sources)
            new.robustness = pe.robustness
            out[(a, b)] = new
    return out


def ignored_values(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Active analyst `ignore_value` decisions (not undone), for a fresh batch run to re-apply."""
    undone = {r["undoes"] for r in conn.execute("SELECT undoes FROM reviews WHERE action='undo' AND undoes IS NOT"
                                                " NULL")}
    out: set[tuple[str, str]] = set()
    for r in conn.execute("SELECT id, target FROM reviews WHERE action = ?", (IGNORE_VALUE,)):
        if r["id"] in undone:
            continue
        t = json.loads(r["target"] or "{}")
        if t.get("kind") and t.get("value") is not None:
            out.add((str(t["kind"]), str(t["value"])))
    return out


def active_constraints(conn: sqlite3.Connection, run_id: str) -> tuple[str, list[dict], list[str], list[str]]:
    p = _run_params(conn, run_id)
    root = p.get("root_run") or run_id
    return root, list(p.get("constraints", [])), list(p.get("added", [])), list(p.get("reviews_applied", []))


def apply_review(review: str | Review, settings: Settings, writer: Writer, *, base_run: str | None = None,
                 thresholds: Thresholds | None = None) -> str:
    """Apply one analyst review and publish a new campaign revision; returns the new run id."""
    from hydra.pipeline import _write_run, latest_run, new_run_id, rarity_for, self_assign

    with closing(connect(settings.db_path, readonly=True)) as conn:
        rv = _load_review(conn, review)
        if rv is None:
            raise ValueError("review not found")
        stored = conn.execute("SELECT 1 FROM reviews WHERE id = ?", (rv.id,)).fetchone() is not None
        current = base_run or latest_run(conn)
        if current is None:
            root, constraints, added, applied, members = None, [], [], [], {}
        else:
            root, constraints, added, applied = active_constraints(conn, current)
            members = _membership(conn, current)
        root_metrics = json.loads(conn.execute("SELECT metrics FROM runs WHERE id = ?", (root,)).fetchone()["metrics"]
                                  ) if root else {}
    if rv.id in applied:
        raise ValueError(f"review {rv.id} is already applied in {current}")
    if str(getattr(rv.action, "value", rv.action)) == ReviewAction.UNDO.value:
        if not rv.undoes:
            raise ValueError("undo needs `undoes`")
        if rv.undoes not in applied:
            raise ValueError(f"review {rv.undoes} is not applied in the current revision")
        constraints = [x for x in constraints if x["review_id"] != rv.undoes]
        dropped = _added_by(current, settings, rv.undoes)
        added = [a for a in added if a not in dropped]
    else:
        new_c, new_added = resolve_constraints(rv, members)
        constraints += new_c
        added = sorted(set(added) | set(new_added))
    if root is None:  # no batch run yet: the first published pattern is built from analyst-added incidents only
        root = new_run_id("analyse")
        _write_empty_root(settings, writer, root)
        root_metrics = {}
    genomes, pairs, clustering = rebuild(settings, root, constraints, added, thresholds)
    with closing(connect(settings.db_path, readonly=True)) as conn:
        dependent = dependent_assignments(conn, root, current, clustering, constraints, thresholds or Thresholds(),
                                          rv.id)
    run_id = new_run_id("analyse")
    params = {"revision_of": current, "root_run": root, "review_id": rv.id, "action": rv.action,
              "constraints": constraints, "added": added, "reviews_applied": [*applied, rv.id],
              "added_by": {a: _added_map(current, settings).get(a, rv.id) for a in added}}
    metrics = {k: root_metrics[k] for k in ("conformal", "corpora", "prior", "scorer") if k in root_metrics}
    metrics.update({"revision": True, "campaigns": len(clustering.campaigns), "incidents": len(genomes),
                    "multi_member_campaigns": sum(1 for c in clustering.campaigns if len(c.members) > 1),
                    "alerts": clustering.alerts, "pairs_scored": len(pairs),
                    "bridge_abstains": sum(1 for a in dependent if a.decision == AssignmentDecision.ABSTAIN),
                    "bridge_restored": sum(1 for a in dependent if "bridge_restored" in a.meta),
                    "carried_over": sum(1 for a in dependent if "carried_over" in a.meta),
                    "campaign_groups": [g.model_dump() for g in clustering.groups]})
    # every revision is assigned like a batch run (leave-one-out against the revised campaigns), so the open-set
    # stage of "Jak to działa" never goes empty after a review
    th = thresholds or Thresholds()
    assignments, conf, summary = (self_assign(genomes, clustering, rarity_for(genomes), th,
                                              root_metrics.get("conformal"), None, run_id)
                                  if genomes else ([], {}, None))
    metrics["assignment"] = summary

    carried = [a for a in dependent if "carried_over" in a.meta]
    carry_run = new_run_id("assign")

    def _tx(conn: sqlite3.Connection) -> None:
        if not stored:
            conn.execute(
                "INSERT INTO reviews (id, action, target, analyst, note, undoes, created_at) VALUES (?,?,?,?,?,?,?)",
                (rv.id, rv.action, events.canonical_json(rv.target), rv.analyst, rv.note, rv.undoes, rv.created_at))
            events.append(conn, "REVIEW", rv.model_dump(mode="json"))
        # leave-one-out self-assignment of the revision, with bridge abstains/restores of dependent assignments
        # taking precedence for the same incident
        merged = {a.incident_id: a for a in assignments}
        merged.update({a.incident_id: a for a in dependent if "carried_over" not in a.meta})
        _write_run(conn, run_id, "analyse", params, metrics, pairs, clustering, list(merged.values()), conformal=conf)
        if carried:  # CR-E2E-5: re-pointed post-analysis material stays a delta of the revision (an assign run)
            _write_run(conn, carry_run, "assign", {"base_run": run_id, "review_id": rv.id, "carried_over": True},
                       {"carried_over": len(carried)}, {}, None, carried)
        for a in dependent:
            if "carried_over" in a.meta:  # re-pointed to the revised campaign; REVISION_PUBLISHED records it
                continue
            events.append(conn, "ASSIGNMENT_BRIDGE_ABSTAIN" if a.decision == AssignmentDecision.ABSTAIN
                          else "ASSIGNMENT_BRIDGE_RESTORED",
                          {"run_id": run_id, "incident_id": a.incident_id, "review_id": rv.id,
                           "campaigns": a.meta.get("bridge_cannot_link", {}).get("campaigns")
                           or a.meta.get("bridge_restored", {}).get("campaigns")})
        events.append(conn, "REVISION_PUBLISHED", {"run_id": run_id, "revision_of": current, "review_id": rv.id,
                                                   "constraints": len(constraints), "added": len(added)})

    writer.run(_tx, timeout=600)
    from hydra.dataflow import build_safely

    build_safely(settings.db_path, writer, run_id)  # DATAFLOW: the revision is a new base
    return run_id


def _added_map(run_id: str | None, settings: Settings) -> dict[str, str]:
    if run_id is None:
        return {}
    with closing(connect(settings.db_path, readonly=True)) as conn:
        return dict(_run_params(conn, run_id).get("added_by", {}))


def _added_by(run_id: str | None, settings: Settings, review_id: str) -> set[str]:
    return {inc for inc, rid in _added_map(run_id, settings).items() if rid == review_id}


def _write_empty_root(settings: Settings, writer: Writer, root: str) -> None:
    def _tx(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES (?,?,?,?,?,?)",
                     (root, "analyse", events.canonical_json({"empty_root": True}), "{}", events.utcnow(),
                      events.utcnow()))

    writer.run(_tx)


def new_review(action: str, target: dict, *, undoes: str | None = None, analyst: str = "operator",
               note: str | None = None) -> Review:
    """Convenience for CLI/tests; the API builds the same object."""
    t = dict(target)
    if action in ("accept_link", "reject_link"):
        t["a"], t["b"] = sorted((str(t["a"]), str(t["b"])))
    if action == IGNORE_VALUE and IGNORE_VALUE not in {a.value for a in ReviewAction}:
        return Review.model_construct(id=uuid.uuid4().hex[:16], action=action, target=t, analyst=analyst, note=note,
                                      created_at=events.utcnow(), undoes=undoes)  # until CR-HARDEN-1
    return Review(id=uuid.uuid4().hex[:16], action=action, target=t, analyst=analyst, note=note,
                  created_at=events.utcnow(), undoes=undoes)


def pair_constraints(conn: sqlite3.Connection, ids: set[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Pair-level analyst decisions (accept_link / reject_link, not undone; the latest decision per pair wins)
    that a fresh batch `analyse` re-applies to the incidents of its run."""
    undone = {r["undoes"] for r in conn.execute("SELECT undoes FROM reviews WHERE action='undo' AND undoes IS NOT"
                                                " NULL")}
    state: dict[tuple[str, str], str] = {}
    for r in conn.execute("SELECT id, action, target FROM reviews WHERE action IN ('accept_link','reject_link')"
                          " ORDER BY created_at, id"):
        if r["id"] in undone:
            continue
        t = json.loads(r["target"] or "{}")
        a, b = t.get("a"), t.get("b")
        if a in ids and b in ids:
            state[tuple(sorted((a, b)))] = r["action"]
    return ([k for k, v in state.items() if v == "accept_link"], [k for k, v in state.items() if v == "reject_link"])


# --------------------------------------------------------------------------------------- dependent assignments


def _revision_chain(conn: sqlite3.Connection, root: str) -> list[str]:
    return [root, *[r["id"] for r in conn.execute(
        "SELECT id FROM runs WHERE kind='analyse' AND json_extract(params, '$.root_run')=? ORDER BY created_at",
        (root,))]]


def dependent_assignments(conn: sqlite3.Connection, root: str | None, current: str | None, clustering: Any,
                          constraints: list[dict], th: Thresholds, review_id: str) -> list[Assignment]:
    """Open-set assignments made against any revision of `root` (newest per incident) and bridge abstains carried
    by `current`, re-checked against the new clustering: link-strength pairs (same_campaign, support >= link)
    reaching two campaigns separated by an active cannot-link → ABSTAIN; a carried abstain whose separation is
    gone → ASSIGN to the campaign of its strongest link. Incidents published as members are skipped."""
    from hydra.assign import BRIDGE_REASON, BRIDGE_REASON_PL, separated_campaigns
    from hydra.pipeline import assignment_from_detail

    if root is None:
        return []
    chain = _revision_chain(conn, root)
    q = ",".join("?" * len(chain))
    assign_runs = [r["id"] for r in conn.execute(
        f"SELECT id FROM runs WHERE kind='assign' AND json_extract(params, '$.base_run') IN ({q})", chain)]
    latest: dict[str, tuple[str, dict, str]] = {}  # incident -> (created_at, detail, run holding its pairs)
    if assign_runs:
        qa = ",".join("?" * len(assign_runs))
        for r in conn.execute(f"SELECT incident_id, run_id, detail, created_at FROM assignments WHERE run_id IN ({qa})"
                              " ORDER BY created_at, run_id", assign_runs):
            d = json.loads(r["detail"])
            src = ((d.get("meta") or {}).get("carried_over") or {}).get("source_run") or r["run_id"]
            latest[r["incident_id"]] = (r["created_at"], d, src)
    if current:
        for r in conn.execute("SELECT incident_id, detail, created_at FROM assignments WHERE run_id=?", (current,)):
            d = json.loads(r["detail"])
            src = ((d.get("meta") or {}).get("bridge_cannot_link") or {}).get("source_run")
            if src and (r["incident_id"] not in latest or latest[r["incident_id"]][0] <= r["created_at"]):
                latest[r["incident_id"]] = (r["created_at"], d, src)
    member_of = clustering.membership
    live = {c.id for c in clustering.campaigns}
    cannot = [(x["a"], x["b"]) for x in constraints if x["kind"] == "cannot"]
    sep = separated_campaigns(clustering.campaigns, cannot)
    out: list[Assignment] = []
    for inc, (_ts, detail, src) in sorted(latest.items()):
        if inc in member_of:
            continue
        carried = (detail.get("meta") or {}).get("bridge_cannot_link")
        if not carried and detail.get("decision") != AssignmentDecision.ASSIGN:
            continue
        best: dict[str, tuple[float, list[str]]] = {}
        for r in conn.execute("SELECT a, b, hypothesis, detail FROM pairs WHERE run_id=? AND (a=? OR b=?)",
                              (src, inc, inc)):
            sup = float(json.loads(r["detail"]).get("support", 0.0))
            other = r["b"] if r["a"] == inc else r["a"]
            cid = member_of.get(other)
            if cid is None or r["hypothesis"] != "same_campaign" or sup < th.link:
                continue
            if cid not in best or sup > best[cid][0]:
                best[cid] = (sup, [r["a"], r["b"]])
        ranked = sorted(best.items(), key=lambda t: (-t[1][0], t[0]))
        hit = next(((c1, c2) for i, (c1, _v1) in enumerate(ranked) for c2, _v2 in ranked[i + 1:]
                    if ((c1, c2) if c1 < c2 else (c2, c1)) in sep), None)
        base = assignment_from_detail(detail)
        if hit:
            c1, c2 = hit
            key = (c1, c2) if c1 < c2 else (c2, c1)
            prev = carried or {"previous_decision": base.decision, "previous_campaign": base.campaign_id}
            meta = {**base.meta, "bridge_cannot_link": {
                "reason": BRIDGE_REASON, "campaigns": [c1, c2], "bits": [round(best[c1][0], 3), round(best[c2][0], 3)],
                "cannot_link": [list(p) for p in sep[key]], "pairs": [best[c1][1], best[c2][1]],
                "previous_decision": prev.get("previous_decision"), "previous_campaign": prev.get("previous_campaign"),
                "source_run": src, "review_id": review_id}}
            out.append(base.model_copy(update={
                "decision": AssignmentDecision.ABSTAIN, "campaign_id": None, "group_id": None, "meta": meta,
                "reason_pl": (f"Wstrzymano po decyzji analityka: materiał łączy kampanie {c1} i {c2}"
                              f" ({num_pl(best[c1][0])} i {num_pl(best[c2][0])} bit dowodów), rozdzielone decyzją"
                              f" „nie może być połączone” ({BRIDGE_REASON_PL}). Wymaga decyzji analityka.")}))
        elif not carried and base.campaign_id and base.campaign_id not in live:
            # CR-E2E-5: the review changed the member set (so the id) of the campaign this item was assigned to;
            # carry the assignment over to the revised campaign of its strongest link, else of most old members
            old = {r[0] for r in conn.execute("SELECT DISTINCT incident_id FROM campaign_members WHERE campaign_id=?",
                                              (base.campaign_id,))}
            overlap = Counter(member_of[m] for m in old if m in member_of)
            cid = ranked[0][0] if ranked else (min(overlap, key=lambda c: (-overlap[c], c)) if overlap else None)
            if cid is None:
                continue
            meta = {**base.meta, "carried_over": {"from_campaign": base.campaign_id, "review_id": review_id,
                                                  "source_run": src}}
            gid = next((c.group_id for c in clustering.campaigns if c.id == cid), None)
            out.append(base.model_copy(update={
                "campaign_id": cid, "group_id": gid, "meta": meta,
                "reason_pl": (f"{base.reason_pl} Po decyzji analityka kampania {base.campaign_id} ma nowy skład:"
                              f" przypisanie przeniesiono do kampanii {cid}.")}))
        elif carried and ranked:
            cid = ranked[0][0]
            meta = {k: v for k, v in base.meta.items() if k != "bridge_cannot_link"}
            meta["bridge_restored"] = {"campaigns": carried.get("campaigns"), "review_id": review_id,
                                       "source_run": src}
            gid = next((c.group_id for c in clustering.campaigns if c.id == cid), None)
            out.append(base.model_copy(update={
                "decision": AssignmentDecision.ASSIGN, "campaign_id": cid, "group_id": gid, "meta": meta,
                "reason_pl": (f"Przywrócono przypisanie do kampanii {cid} ({num_pl(ranked[0][1][0])} bit): rozdzielenie"
                              " kampanii przez analityka zostało cofnięte.")}))
    return out


# --------------------------------------------------------------------------------------- unsure ("niepewne")

UNSURE_EVENT = "REVIEW_UNSURE"
UNSURE_UNDO_EVENT = "REVIEW_UNSURE_UNDONE"


def unsure_key(target: dict) -> str:
    """`a|b` (sorted) for a pair, `inc:<id>` for an assignment / incident."""
    if target.get("a") and target.get("b"):
        a, b = sorted((str(target["a"]), str(target["b"])))
        return f"{a}|{b}"
    if target.get("incident_id"):
        return f"inc:{target['incident_id']}"
    raise ValueError("unsure target needs {a, b} or {incident_id}")


def unsure_marks(conn: sqlite3.Connection) -> list[dict]:
    """Active unsure marks from the audit log (marks minus undone marks), oldest first."""
    marks: dict[str, dict] = {}
    for r in conn.execute("SELECT type, payload FROM events WHERE type IN (?, ?) ORDER BY seq",
                          (UNSURE_EVENT, UNSURE_UNDO_EVENT)):
        p = json.loads(r["payload"])
        if r["type"] == UNSURE_EVENT:
            marks[p["id"]] = p
        else:
            marks.pop(p.get("undoes"), None)
    return list(marks.values())


def mark_unsure(writer: Writer, target: dict, *, note: str | None = None, analyst: str = "operator"
                ) -> tuple[dict, str]:
    """Record "niepewne" for a pair or an assignment; returns (mark, event hash). Campaigns are unchanged."""
    t = {k: str(v) for k, v in target.items() if k in ("a", "b", "incident_id") and v}
    if t.get("a") and t.get("b"):
        if t["a"] == t["b"]:
            raise ValueError("a pair needs two different incidents")
        t["a"], t["b"] = sorted((t["a"], t["b"]))
    key = unsure_key(t)
    mark = {"id": uuid.uuid4().hex[:16], "key": key, "target": t, "note": note, "analyst": analyst,
            "created_at": events.utcnow()}

    def _tx(conn: sqlite3.Connection) -> str:
        if any(m["key"] == key for m in unsure_marks(conn)):
            raise ValueError(f"already marked unsure: {key}")
        return events.append(conn, UNSURE_EVENT, mark)

    return mark, writer.run(_tx)


def undo_unsure(writer: Writer, mark_id: str, *, analyst: str = "operator") -> str:
    def _tx(conn: sqlite3.Connection) -> str:
        if not any(m["id"] == mark_id for m in unsure_marks(conn)):
            raise LookupError(f"no active unsure mark {mark_id}")
        return events.append(conn, UNSURE_UNDO_EVENT, {"undoes": mark_id, "analyst": analyst,
                                                        "created_at": events.utcnow()})

    return writer.run(_tx)
