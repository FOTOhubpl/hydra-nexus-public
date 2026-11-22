"""HUNT endpoints (INNOWACJE §11): hunt plan, hypothesis ledger, next evidence, verification, metrics.

Nothing here scans the internet: candidates are incidents already in the local store, and the replay reads local
recordings only. LIVE hypotheses use evidence availability as `available_at`: an incident's import time for its
first genes, the extraction / enrichment completion for genes added later (CR-ASTRA-RES-4, `store_observations`).
Writes go to `<state>/hunt/ledger.jsonl` (hash-chained) and the main audit log (`HUNT_*` events) via the Writer.
"""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from hydra import events
from hydra.db import connect
from hydra.hunt import planner
from hydra.hunt.hypotheses import Ledger, issue, parse_ts
from hydra.hunt.records import DatasetKind, Hypothesis, Mode, Observation, sha256_of
from hydra.hunt.replay import KnowledgeView
from hydra.hunt.signatures import from_revision, trait_timeline
from hydra.hunt.verify import check, resolve

router = APIRouter(prefix="/v1", tags=["hunt"])
FIRST_LOOK = {"url", "text", "image", "name"}  # progressive triage: what an analyst sees before opening artefacts
# Sources whose collection the API actually performs at verification time (only the local store: nothing here scans
# the internet). A hypothesis naming any other source cannot be refuted by absence (CR-ASTRA-PIPE-308).
LIVE_SOURCES = frozenset({"local_store"})
ORDER = {"supports": 0, "unknown": 1, "partial": 2, "decoration_only": 3, "conflict": 4, "unrelated": 5,
         "known": 6, "self": 7}


def _st(request: Request) -> Any:
    return request.app.state.hydra


def _ledger(st: Any) -> Ledger:
    return Ledger(Path(st.settings.state_dir) / "hunt" / "ledger.jsonl", writer=st.writer)


def store_observations(db_path: Path, *, progressive: bool = False, exclude: set[str] | None = None
                       ) -> list[Observation]:
    """One Observation per (incident, evidence version) (CR-ASTRA-RES-4). A version becomes available when HYDRA
    held that gene set: `max(import time, latest gene availability in it)`, never the source's event date. Genes
    added by a later re-extraction / enrichment open a new version at their own time; genes retired by a correction
    stay in the versions before the correction. A gene whose availability is unknown is left out of every version
    (never back-dated) and counted in `meta.unknown_time_traits`."""
    with closing(connect(db_path, readonly=True)) as conn:
        rows = conn.execute("SELECT id, source, observed_at, created_at FROM incidents ORDER BY created_at, id"
                            ).fetchall()
        rows = [r for r in rows if not exclude or r["id"] not in exclude]
        timeline = trait_timeline(conn, [r["id"] for r in rows])
    out = []
    for r in rows:
        acq = parse_ts(r["created_at"])
        label = {acq: r["created_at"]}  # original strings: re-formatting could truncate (back-date) sub-seconds
        known = []
        for t, s, u in timeline.get(r["id"], []):
            if s is None:
                continue
            ps = parse_ts(s)
            if ps > acq:
                label.setdefault(ps, s)
            pu = parse_ts(u) if u else None
            if pu is not None and pu > acq:
                label.setdefault(pu, u)
            known.append((t, max(ps, acq), pu))
        unknown = len(timeline.get(r["id"], [])) - len(known)
        times = sorted({s for _t, s, _u in known} | {u for _t, _s, u in known if u is not None and u > acq})
        series: list[Observation] = []
        for at in times or [acq]:
            ts = [t for t, s, u in known if s <= at and (u is None or u > at)]
            keys = sorted(t.key for t in ts)
            if series and series[-1].payload_sha256 == sha256_of(keys):
                continue
            vis = [t for t in ts if not progressive or t.source in FIRST_LOOK]
            hid = [t for t in ts if progressive and t.source not in FIRST_LOOK]
            version = "ev-" + sha256_of([r["id"], keys])[:16]
            stamp = label[at]
            series.append(Observation(
                observation_id=f"incident:{r['id']}#{version}", source="local_store", ref=r["id"],
                event_time=r["observed_at"], available_at=stamp,
                available_at_basis="max(import time, first availability of every gene in this version)",
                collected_at=r["created_at"], completed_at=stamp if times else None, evidence_version=version,
                payload_sha256=sha256_of(keys), evidence_pointer=f"/v1/incidents/{r['id']}", traits=vis,
                hidden=hid, dataset_kind=DatasetKind.REAL,
                meta={"unread_sources": sorted({t.source for t in hid}), "evidence_series": f"incident:{r['id']}",
                      "unknown_time_traits": unknown}))
        out.extend(series)
    return out


def current_observations(db_path: Path, cutoff: str, *, progressive: bool = False) -> list[Observation]:
    """The evidence version of every incident current at `cutoff` (one per incident)."""
    return KnowledgeView(store_observations(db_path, progressive=progressive), cutoff).visible()


def _signature(st: Any, campaign_id: str, run_id: str | None):
    try:
        return from_revision(st.settings.db_path, campaign_id, run_id)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc


def _cannot(meta: dict[str, Any], members: list[str]) -> set[str]:
    ms = set(members)
    return {b if a in ms else a for a, b in meta.get("cannot_link", []) if a in ms or b in ms}


def _hyp(st: Any, hid: str) -> tuple[Hypothesis, dict[str, Any]]:
    entry = _ledger(st).hypotheses().get(hid)
    if entry is None:
        raise HTTPException(404, "hypothesis not found")
    body = {k: v for k, v in entry["hypothesis"].items() if k != "signature"}
    return Hypothesis.model_validate(body), entry


class PlanBody(BaseModel):
    run_id: str | None = None
    query_budget: int = Field(default=20, ge=1, le=200)
    horizon_hours: float = Field(default=72, gt=0, le=24 * 30)


class HypothesisBody(PlanBody):
    campaign_id: str
    sources: list[str] = Field(default_factory=lambda: ["local_store"])


class NextActionBody(BaseModel):
    ref: str | None = None
    execute: bool = True


@router.post("/campaigns/{campaign_id}/hunt-plan")
async def hunt_plan(campaign_id: str, request: Request, body: PlanBody | None = None) -> dict[str, Any]:
    body = body or PlanBody()
    st = _st(request)

    def work() -> dict[str, Any]:
        sig, meta = _signature(st, campaign_id, body.run_id)
        cann = _cannot(meta, sig.members)
        view = KnowledgeView(store_observations(st.settings.db_path), events.utcnow())
        draft = _draft(sig, view, body)
        if draft is None:  # CR-ASTRA-PIPE-307: no operator-tier invariant → nothing to hunt, not a 500
            return {"campaign_id": campaign_id, "run_id": meta["run_id"], "label": meta.get("label"),
                    "signature": sig.model_dump(mode="json"), "draft_hypothesis": None, "candidates": [],
                    "next_evidence": [], "status": "unavailable", "reason": "no_operator_invariant",
                    "reason_pl": "Brak niezmiennika poziomu operatora w sygnaturze kampanii (np. tylko wspólna "
                                 "infrastruktura lub szablon): nie ma czego weryfikować, plan polowania niedostępny.",
                    "note_pl": "Plan nie uruchamia skanowania Internetu: kandydaci to materiały w lokalnym magazynie.",
                    "score_kind": "heuristic_priority"}
        cands = []
        for o in view.visible():
            if o.ref in set(sig.members):
                continue
            c = check(draft, o, cannot_link=cann)
            if c["verdict"] in ("unrelated", "known", "self"):
                continue
            cands.append(c)
        cands.sort(key=lambda c: (ORDER[c["verdict"]], -len(c.get("matched_invariants") or []), c["ref"]))
        cands = cands[: body.query_budget]
        prog = {o.ref: o for o in current_observations(st.settings.db_path, view.cutoff, progressive=True)}
        plans = [{"ref": c["ref"], "verdict": c["verdict"],
                  "ranking": planner.rank(draft, prog[c["ref"]], check(draft, prog[c["ref"]], cannot_link=cann))[:3]}
                 for c in cands if c["verdict"] not in planner.DECISIVE and c["ref"] in prog][:5]
        return {"campaign_id": campaign_id, "run_id": meta["run_id"], "label": meta.get("label"),
                "signature": sig.model_dump(mode="json"), "draft_hypothesis": draft.model_dump(mode="json"),
                "status": "ok", "candidates": cands, "next_evidence": plans,
                "note_pl": "Plan nie uruchamia skanowania Internetu: kandydaci to materiały w lokalnym magazynie.",
                "score_kind": "heuristic_priority"}

    return await run_in_threadpool(work)


def _draft(sig, view, body) -> Hypothesis | None:
    try:
        return issue(sig, view, issued_at=view.cutoff, horizon_hours=body.horizon_hours, sources=["local_store"],
                     query_budget=body.query_budget, ledger=None, mode=Mode.LIVE, protocol="live_local_store_v1")
    except ValueError:
        return None


@router.post("/hypotheses", status_code=201)
async def create_hypothesis(body: HypothesisBody, request: Request) -> dict[str, Any]:
    st = _st(request)

    unsupported = sorted(set(body.sources) - LIVE_SOURCES)
    if unsupported or not body.sources:
        raise HTTPException(422, f"Nieobsługiwane źródło weryfikacji: {', '.join(unsupported) or '(brak)'}. "
                                 f"HYDRA sprawdza tylko: {', '.join(sorted(LIVE_SOURCES))} — dla innych źródeł nie "
                                 "zbiera danych, więc nie mogłaby ani potwierdzić, ani obalić hipotezy.")

    def work() -> dict[str, Any]:
        sig, _meta = _signature(st, body.campaign_id, body.run_id)
        now = events.utcnow()
        view = KnowledgeView(store_observations(st.settings.db_path), now)
        try:
            h = issue(sig, view, issued_at=now, horizon_hours=body.horizon_hours, sources=body.sources,
                      query_budget=body.query_budget, ledger=_ledger(st), mode=Mode.LIVE,
                      protocol="live_local_store_v1")
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"hypothesis": h.model_dump(mode="json"), "signature": sig.model_dump(mode="json")}

    return await run_in_threadpool(work)


@router.get("/hypotheses")
def list_hypotheses(request: Request) -> dict[str, Any]:
    hs = _ledger(_st(request)).hypotheses()
    items = [{k: e["hypothesis"].get(k) for k in ("hypothesis_id", "campaign_id", "issued_at", "horizon_end",
                                                    "status", "mode", "dataset_kind", "statement_pl")}
             for e in hs.values()]
    return {"items": items, "total": len(items)}


@router.get("/hypotheses/{hid}")
def get_hypothesis(hid: str, request: Request) -> dict[str, Any]:
    st = _st(request)
    _h, entry = _hyp(st, hid)
    ok, n, bad = _ledger(st).verify()
    return {**entry, "ledger": {"ok": ok, "records": n, "first_bad": bad}}


@router.post("/hypotheses/{hid}/next-action")
async def hypothesis_next_action(hid: str, request: Request, body: NextActionBody | None = None) -> dict[str, Any]:
    body = body or NextActionBody()
    st = _st(request)

    def work() -> dict[str, Any]:
        h, _e = _hyp(st, hid)
        obs = current_observations(st.settings.db_path, events.utcnow(), progressive=True)
        if body.ref:
            pick = next((o for o in obs if o.ref == body.ref), None)
            if pick is None:
                raise HTTPException(404, "candidate not found")
        else:
            cands = [o for o in obs if o.ref not in set(h.known_refs)
                     and check(h, o)["verdict"] in ("unknown", "partial", "decoration_only")]
            if not cands:
                return {"hypothesis_id": hid, "executed": None, "note_pl": "Brak nierozstrzygniętego kandydata."}
            pick = cands[-1]
        step = planner.next_action(h, pick, execute=body.execute)
        out = {k: v for k, v in step.items() if k != "observation"}
        if body.execute:
            _ledger(st).append("ACTION", {"hypothesis_id": hid, "action_id": (step["executed"] or {}).get("action"),
                                          "ref": pick.ref, "before": step["before"]["verdict"],
                                          "after": (step["after"] or {}).get("verdict"),
                                          "decision_changed": step["decision_changed"]})
        return out

    return await run_in_threadpool(work)


@router.post("/hypotheses/{hid}/verify")
async def verify_hypothesis(hid: str, request: Request) -> dict[str, Any]:
    st = _st(request)

    def work() -> dict[str, Any]:
        h, _e = _hyp(st, hid)
        try:
            _sig, meta = from_revision(st.settings.db_path, h.campaign_id)
            cann = _cannot(meta, h.known_refs)
        except LookupError:
            cann = set()
        now = events.utcnow()
        obs = store_observations(st.settings.db_path)
        # every evidence version that became available after the issue (an incident imported earlier but enriched
        # later counts from its enrichment time, CR-ASTRA-RES-4)
        later = [o for o in obs if parse_ts(o.available_at) > parse_ts(h.issued_at)]
        checks = [check(h, o, cannot_link=cann) for o in later]
        # Only sources HYDRA itself collects are covered; any other named source (older ledger entries) leaves the
        # window uncovered → unverifiable, never refuted by absence (CR-ASTRA-PIPE-308). Evidence whose availability
        # time is unknown cannot be placed inside or outside the window either (CR-ASTRA-RES-4).
        uncovered = sorted(set(h.sources) - LIVE_SOURCES) if h.sources else ["(brak)"]
        if any(o.meta.get("unknown_time_traits") for o in obs):
            uncovered.append("evidence_time_unknown")
        res = resolve(h, checks, now, coverage_complete=not uncovered)
        res["source_note_pl"] = "Źródło: lokalny magazyn (materiały zaimportowane po wydaniu hipotezy)." + (
            f" Niepokryte źródła: {', '.join(uncovered)}." if uncovered else "")
        res["uncovered_sources"] = uncovered
        _ledger(st).append("VERIFIED", res)
        return {**res, "checks": checks}

    return await run_in_threadpool(work)


def _report_path(st: Any) -> Path:
    return Path(st.settings.state_dir) / "hunt" / "replay-report.json"


def _report(st: Any) -> dict[str, Any] | None:
    p = _report_path(st)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


@router.get("/hunt/metrics")
def hunt_metrics(request: Request) -> dict[str, Any]:
    st = _st(request)
    rep = _report(st)
    hs = _ledger(st).hypotheses()
    live: dict[str, int] = {}
    for e in hs.values():
        s = e["hypothesis"].get("status", "pending")
        live[s] = live.get(s, 0) + 1
    ok, n, bad = _ledger(st).verify()
    return {
        "TEMPORAL_REPLAY": None if rep is None else {
            "generated_at": rep.get("generated_at"),
            "REAL": {k: rep["REAL"].get(k) for k in ("source", "metrics", "cutoffs", "horizon_hours",
                                                     "budget_k_per_cutoff", "time_basis", "campaign_notion",
                                                     "status", "reason", "ledger")},
            "SYNTHETIC": {k: rep["SYNTHETIC"].get(k) for k in ("note", "hypotheses", "candidates", "budget_k",
                                                               "ledger")}},
        "LIVE": {"hypotheses": len(hs), "by_status": live, "ledger": {"ok": ok, "records": n, "first_bad": bad}},
        "rules": (rep or {}).get("rules", []),
        "score_kind": "counts; rankings are heuristic priorities, not probabilities",
    }


@router.get("/hunt/replay")
def hunt_replay(request: Request) -> dict[str, Any]:
    rep = _report(_st(request))
    if rep is None:
        return {"available": False, "note_pl": "Brak raportu replay - uruchom POST /v1/hunt/replay."}
    return {"available": True, **rep}


@router.post("/hunt/replay")
async def run_replay(request: Request) -> dict[str, Any]:
    from hydra.hunt.scenarios import build_report

    st = _st(request)
    rep = await run_in_threadpool(build_report, st.settings)
    return {"available": True, "generated_at": rep["generated_at"]}
