"""Replay runs reported separately: TEMPORAL REPLAY on REAL CERT PL recordings, and a SYNTHETIC mechanism test.

    uv run python -m hydra.hunt.scenarios            # writes <state>/hunt/replay-report.json (+ ledgers)

Strategies share the cutoff, the hypothesis horizon and the budget K (hypotheses watched per cutoff):
- `hunt`: signature priority x recent activity of the name generator (categorical: distinct registrable domains
  in the 48 h before the cutoff: >= 3 high = 3, 2 medium = 2, 1 low = 1, 0 none = 0);
- `volume` baseline: most listed hosts in the 7-day window;
- `brand` baseline: most brand-labelled hosts in the window (the brand-centric approach INNOWACJE §12 warns about).
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from hydra import events
from hydra.hunt import HUNT_VERSION
from hydra.hunt.hypotheses import Ledger, iso, issue, parse_ts
from hydra.hunt.metrics import candidate_precision, summarise
from hydra.hunt.planner import next_action
from hydra.hunt.records import DatasetKind, Observation, Tier, Trait, sha256_of
from hydra.hunt.replay import (
    PUBLICATION_LAG,
    KnowledgeView,
    ReplayClock,
    families,
    load_certpl,
    registrable_units,
    split_host,
)
from hydra.hunt.signatures import build_signature, has_operator_invariant
from hydra.hunt.verify import check, resolve

STRATEGIES = ("hunt", "volume", "brand")
REFERENCE = "CERT Polska warning list (recorded feed)"


def _activity(regs: list[str], first_seen: dict[str, str], t0: str) -> int:
    lo = parse_ts(t0) - timedelta(hours=48)
    n = sum(1 for r in regs if parse_ts(first_seen[r]) > lo)
    return 3 if n >= 3 else n


def run_certpl(recordings: Path, out_dir: Path, *, horizon_h: float = 24.0, k: int = 10, window_days: int = 7,
               cutoffs: list[str] | None = None, time_basis: str = "assumed_publication",
               lag: timedelta = PUBLICATION_LAG, tag: str = "certpl") -> dict[str, Any]:
    obs, info = load_certpl(recordings, time_basis=time_basis, lag=lag)
    if not obs:
        return {"dataset_kind": "REAL", "mode": "TEMPORAL_REPLAY", "status": "unavailable",
                "reason": f"no CERT PL recordings under {recordings}"}
    start, end = parse_ts(info["first_available"]), parse_ts(info["last_available"])
    if cutoffs is None:
        d = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        cutoffs = []
        while d < end:
            cutoffs.append(iso(d))
            d += timedelta(days=1)
    ledger = Ledger(out_dir / f"ledger-{tag}.jsonl")
    per: dict[str, list[dict[str, Any]]] = {s: [] for s in STRATEGIES}
    cases: list[dict[str, Any]] = []
    counterexamples: list[dict[str, Any]] = []
    for t0 in cutoffs:
        view = KnowledgeView(obs, t0)
        lo = parse_ts(t0) - timedelta(days=window_days)
        win = [o for o in view.visible() if parse_ts(o.available_at) > lo]
        units = registrable_units(win)
        fams = families(units)
        first_seen: dict[str, str] = {}
        hosts_of: dict[str, list[Observation]] = {}
        for o in win:
            reg = split_host(o.ref)[0]
            first_seen.setdefault(reg, o.available_at)
            hosts_of.setdefault(reg, []).append(o)
        sigs = {}
        for tpl, regs in fams.items():
            members = {r: units[r] for r in regs}
            bg = {r: ts for r, ts in units.items() if r not in members}
            sigs[tpl] = build_signature(f"F:{tpl}", f"F:{tpl}@{t0}", members, bg, approval_basis="replay_seed",
                                        dataset_kind=DatasetKind.REAL)
        last = {tpl: max(first_seen[r] for r in regs) for tpl, regs in fams.items()}
        score = {
            "hunt": {t: sigs[t].priority * _activity(fams[t], first_seen, t0) for t in fams},
            "volume": {t: sum(len(hosts_of[r]) for r in fams[t]) for t in fams},
            "brand": {t: sum(1 for r in fams[t] for o in hosts_of[r] if any(x.kind == "brand_label" for x in o.traits))
                      for t in fams},
        }
        sigs = {t: s for t, s in sigs.items() if has_operator_invariant(s)}  # nothing verifiable to predict otherwise
        score = {s: {t: v for t, v in sc.items() if t in sigs} for s, sc in score.items()}
        picks = {s: [t for t in sorted(sigs, key=lambda t: (-score[s][t], -parse_ts(last[t]).timestamp(), t))
                     if score[s][t] > 0][:k] for s in STRATEGIES}
        hyps = {}
        n_cases = 0
        for tpl in sorted({t for p in picks.values() for t in p}):
            ev = sorted(o.observation_id for r in fams[tpl] for o in hosts_of[r])
            hyps[tpl] = issue(sigs[tpl], view, issued_at=t0, horizon_hours=horizon_h, sources=["certpl_recording"],
                              query_budget=k, ledger=ledger, evidence_ids=ev)
        clock = ReplayClock(obs, t0)
        later = list(clock.advance(iso(min(parse_ts(t0) + timedelta(hours=horizon_h), end))))
        complete = parse_ts(t0) + timedelta(hours=horizon_h) <= end
        for tpl, h in hyps.items():
            checks = [check(h, o) for o in later]
            res = resolve(h, checks, iso(end), coverage_complete=complete)
            ledger.append("VERIFIED", res)
            row = {"hypothesis": h.model_dump(mode="json"), "result": res, "template": tpl,
                   "strategies": [s for s in STRATEGIES if tpl in picks[s]]}
            for s in row["strategies"]:
                per[s].append(row)
            if "hunt" in row["strategies"] and n_cases < 5:
                n_cases += 1
                cases.append(_case(h, sigs[tpl], res, hosts_of, fams[tpl], later))
            if len(counterexamples) < 5:
                ce = next((c for c in checks if c["verdict"] == "decoration_only"
                           and any(d.startswith("brand_label=") for d in c.get("decoration", []))
                           and c["ref"] not in {x["observation"]["ref"] for x in counterexamples}), None)
                if ce:
                    counterexamples.append({"hypothesis_id": h.hypothesis_id, "template": tpl,
                                            "observation": ce, "linked": False})
    ok, n, bad = ledger.verify()
    return {
        "dataset_kind": "REAL", "mode": "TEMPORAL_REPLAY", "source": REFERENCE, "recordings": info,
        "horizon_hours": horizon_h, "budget_k_per_cutoff": k, "window_days": window_days, "cutoffs": cutoffs,
        "time_basis": ("available_at = rec_ts for every record (when HYDRA acquired it; nothing back-dated)"
                       if time_basis == "acquisition" else
                       f"available_at = rec_ts (live tail) or ActionTime + {lag.total_seconds() / 60:g} min "
                       "(backfill, ASSUMED)"),
        "confirming_time_basis": _basis_counts(per, obs),
        "campaign_notion": "name-generator family (digit template over >= 2 registrable domains); a name-pattern "
                           "hypothesis, not proof of one operator",
        "metrics": {s: summarise(per[s], k=k * len(cutoffs), reference_source=REFERENCE) for s in STRATEGIES},
        "cases": cases, "counterexamples": counterexamples,
        "ledger": {"path": str(ledger.path.name), "ok": ok, "records": n, "first_bad": bad},
    }


def _basis_counts(per: dict[str, list[dict[str, Any]]], obs: list[Observation]) -> dict[str, dict[str, int]]:
    """Per strategy: how many first confirmations rest on an assumed vs a recorded availability time."""
    basis = {o.observation_id: o.available_at_basis.split(":")[0] for o in obs}
    out: dict[str, dict[str, int]] = {}
    for s, rows in per.items():
        c: dict[str, int] = {}
        for r in rows:
            f = r["result"].get("first_confirming")
            if f:
                b = basis.get(f.get("observation_id", ""), "unknown")
                c[b] = c.get(b, 0) + 1
        out[s] = c
    return out


def _case(h, sig, res, hosts_of, regs, later) -> dict[str, Any]:
    before = [{"ref": o.ref, "available_at": o.available_at, "basis": o.available_at_basis}
              for r in regs for o in hosts_of[r]][:8]
    first = res.get("first_confirming")
    later_obs = next((o for o in later if first and o.observation_id == first["observation_id"]), None)
    return {"hypothesis": h.model_dump(mode="json"), "signature": sig.model_dump(mode="json"),
            "evidence_before": before, "missing_evidence": [] if first else h.required_evidence_groups,
            "later_observation": later_obs.model_dump(mode="json") if later_obs else None, "result": res}


# ------------------------------------------------------------------------------------------- SYNTHETIC


def _t(kind: str, value: str, tier: Tier, source: str, group: str | None = None, dep: str | None = None) -> Trait:
    g = group or {Tier.OPERATOR: "operator", Tier.KIT: "page_structure", Tier.DECORATION: "decoration",
                  Tier.BOILERPLATE: "boilerplate"}[tier]
    return Trait(kind=kind, value=value, tier=tier, group=g, dep=dep or f"{source}:{kind}", source=source)


def _syn_obs(oid: str, at: str, traits: list[Trait], hidden: list[Trait] | None = None, *, truth: str,
             mutation: str, unread: list[str] | None = None) -> Observation:
    hidden = hidden or []
    return Observation(observation_id=oid, source="synthetic_stream", ref=oid, event_time=at, available_at=at,
                       available_at_basis="SYNTHETIC timeline", collected_at=at,
                       payload_sha256=sha256_of([t.key for t in traits + hidden]), evidence_pointer=f"synthetic:{oid}",
                       traits=traits, hidden=hidden, dataset_kind=DatasetKind.SYNTHETIC,
                       meta={"truth": truth, "mutation": mutation,
                             "unread_sources": sorted(set(unread if unread is not None else
                                                          [t.source for t in hidden]))})


# mutation names follow eval/variants.py (m_new_domain, m_headline, m_person_photo, m_drop_har) + template_clone
SYN_MUTATIONS = ("new_domain", "headline", "person_photo", "drop_har", "template_clone")


def synthetic_world(t0: str = "2026-11-20T12:00:00Z") -> dict[str, Any]:
    T0 = parse_ts(t0)

    def at(h: float) -> str:
        return iso(T0 + timedelta(hours=h))

    OP, K, D = Tier.OPERATOR, Tier.KIT, Tier.DECORATION
    kit = [_t("sdk", "aio-sdk-1.0.28", K, "dom"), _t("form_action", "/lead.php", K, "dom")]

    def k_member(i: int) -> list[Trait]:
        return [_t("tracker_id", "PX-551", OP, "har", dep="INF:operator:tracker"),
                _t("operator_id", "acct-77", OP, "dom", dep="INF:operator:acct"),
                _t("entity_phone", "+48700100100", OP, "text"), *kit,
                _t("brand_label", "brandx", D, "image"), _t("domain", f"brandx-news{i}.example", OP, "url"),
                _t("headline", f"Nagłówek wariant {i}", D, "text"), _t("person_named", f"Osoba {i}", D, "image")]

    def l_member(i: int) -> list[Trait]:
        return [_t("tracker_id", "PX-300", OP, "har"), _t("operator_id", "acct-300", OP, "dom"),
                _t("brand_label", "brandy", D, "image"), _t("domain", f"brandy-promo{i}.example", OP, "url")]

    def clone(i: int) -> list[Trait]:
        return [*kit, _t("tracker_id", f"PX-9{i:02d}", OP, "har"), _t("operator_id", f"acct-9{i:02d}", OP, "dom"),
                _t("brand_label", "brandx", D, "image"), _t("domain", f"clone{i}.example", OP, "url")]

    known = [_syn_obs(f"syn-K{i}", at(-72 + 10 * i), k_member(i), truth="K", mutation="approved_member")
             for i in range(1, 4)]
    known += [_syn_obs(f"syn-L{i}", at(-60 + 10 * i), l_member(i), truth="L", mutation="approved_member")
              for i in range(1, 3)]
    known += [_syn_obs(f"syn-C{i}", at(-50 + 5 * i), clone(i), truth=f"C{i}", mutation="template_clone")
              for i in range(1, 3)]
    known += [_syn_obs(f"syn-F{i}", at(-40 + 3 * i), [_t("tracker_id", f"PX-F{i}", OP, "har"),
                                                      _t("domain", f"filler{i}.example", OP, "url")],
                       truth=f"F{i}", mutation="filler") for i in range(1, 5)]
    v_new_domain = k_member(9)
    v_head = k_member(1)
    v_head = [t for t in v_head if t.kind not in ("headline", "person_named")] + [
        _t("headline", "Zupełnie nowy nagłówek", D, "text"), _t("person_named", "Nowa twarz", D, "image")]
    later = [
        _syn_obs("syn-v4-clone", at(2), clone(12), truth="C12", mutation="template_clone"),
        _syn_obs("syn-v1-new-domain", at(5), v_new_domain, truth="K", mutation="new_domain"),
        _syn_obs("syn-v5-clone-har", at(8), [_t("brand_label", "brandx", D, "image"),
                                             _t("domain", "clone13.example", OP, "url")],
                 [_t("tracker_id", "PX-913", OP, "har"), *kit], truth="C13", mutation="template_clone"),
        _syn_obs("syn-v2-headline", at(12), v_head, truth="K", mutation="headline+person_photo"),
        _syn_obs("syn-v3-drop-har", at(20), [_t("brand_label", "brandx", D, "image"),
                                             _t("domain", "brandx-fresh.example", OP, "url"),
                                             _t("headline", "Inny nagłówek", D, "text")],
                 [_t("operator_id", "acct-77", OP, "dom"), *kit], truth="K", mutation="drop_har"),
        _syn_obs("syn-v6-filler", at(22), [_t("tracker_id", "PX-F9", OP, "har"),
                                           _t("domain", "filler9.example", OP, "url")], truth="F9", mutation="filler"),
        _syn_obs("syn-v7-late", at(30), k_member(7), truth="K", mutation="new_domain (after horizon)"),
    ]
    return {"t0": t0, "known": known, "later": later}


def run_synthetic(out_dir: Path, *, horizon_h: float = 24.0, k: int = 3) -> dict[str, Any]:
    w = synthetic_world()
    t0 = w["t0"]
    allobs = w["known"] + w["later"]
    view = KnowledgeView(allobs, t0)
    ledger = Ledger(out_dir / "ledger-synthetic.jsonl")
    vis = {o.ref: o.traits for o in view.visible()}
    results, cards = [], []
    for camp, members in (("K-SYN", ["syn-K1", "syn-K2", "syn-K3"]), ("L-SYN", ["syn-L1", "syn-L2"])):
        sig = build_signature(camp, f"{camp}:r1", {m: vis[m] for m in members},
                              {i: ts for i, ts in vis.items() if i not in members}, approval_basis="analyst",
                              dataset_kind=DatasetKind.SYNTHETIC)
        h = issue(sig, view, issued_at=t0, horizon_hours=horizon_h, sources=["synthetic_stream"], query_budget=k,
                  ledger=ledger, evidence_ids=members)
        clock = ReplayClock(allobs, t0)
        later = list(clock.advance(iso(parse_ts(t0) + timedelta(hours=40))))
        checks, steps = [], []
        for o in later:
            c = check(h, o)
            if c["verdict"] not in ("supports", "conflict", "unrelated") and o.hidden:
                step = next_action(h, o)
                ledger.append("ACTION", {"hypothesis_id": h.hypothesis_id, "action_id": step["executed"]["action"]
                                         if step["executed"] else None, "ref": o.ref,
                                         "before": step["before"]["verdict"],
                                         "after": step["after"]["verdict"] if step["after"] else None,
                                         "decision_changed": step["decision_changed"]})
                steps.append({k2: v for k2, v in step.items() if k2 != "observation"})
                c = step["after"] or c
            checks.append({**c, "truth": o.meta["truth"], "mutation": o.meta["mutation"]})
        res = resolve(h, checks, iso(parse_ts(t0) + timedelta(hours=40)), coverage_complete=True)
        ledger.append("VERIFIED", res)
        results.append({"hypothesis": h.model_dump(mode="json"), "result": res})
        cards.append({"hypothesis": h.model_dump(mode="json"), "signature": sig.model_dump(mode="json"),
                      "evidence_before": [{"ref": m, "available_at": view.get(m)[0].available_at} for m in members],
                      "checks": checks, "next_actions": steps, "result": res})
    # candidate ranking vs a brand baseline, same budget K, same cutoff, K-SYN only
    kcard = cards[0]
    truth = {c["ref"] for c in kcard["checks"] if c["truth"] == "K" and c["verdict"] != "known"
             and parse_ts(c["available_at"]) <= parse_ts(kcard["hypothesis"]["horizon_end"])}
    in_h = [c for c in kcard["checks"] if parse_ts(c["available_at"]) <= parse_ts(kcard["hypothesis"]["horizon_end"])]
    hunt_flags = [c["ref"] for c in sorted(in_h, key=lambda c: -len(c.get("matched_invariants") or []))
                  if c["verdict"] == "supports"][:k]
    brand_flags = [o.ref for o in w["later"] if any(t.key == "brand_label=brandx" for t in o.traits)
                   and parse_ts(o.available_at) <= parse_ts(kcard["hypothesis"]["horizon_end"])][:k]
    ok, n, bad = ledger.verify()
    return {
        "dataset_kind": "SYNTHETIC", "mode": "TEMPORAL_REPLAY",
        "note": "SYNTHETIC mechanism test: constructed by hydra.hunt.synthetic_world; the same author wrote the "
                "generator and the rules, so these numbers are not a performance claim",
        "mutations": list(SYN_MUTATIONS), "budget_k": k,
        "hypotheses": summarise(results, k=len(results), reference_source="synthetic_stream"),
        "candidates": {"hunt": {**candidate_precision(hunt_flags, truth), "flagged": hunt_flags},
                       "brand_baseline": {**candidate_precision(brand_flags, truth), "flagged": brand_flags}},
        "cards": cards, "ledger": {"path": ledger.path.name, "ok": ok, "records": n, "first_bad": bad},
    }


def build_report(settings: Any, *, horizon_h: float = 24.0, k: int = 10) -> dict[str, Any]:
    out_dir = Path(settings.state_dir) / "hunt" / "replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("ledger-*.jsonl"):
        f.unlink()  # a replay is recomputed from the recordings; the product ledger lives elsewhere
    rec = Path(settings.data_dir) / "watch" / "recordings"
    report = {"version": HUNT_VERSION, "generated_at": events.utcnow(),
              "REAL": run_certpl(rec, out_dir, horizon_h=horizon_h, k=k),
              # CR-ASTRA-RES-4: the same replay with availability = HYDRA's acquisition time (nothing back-dated),
              # and a sensitivity run with the publication lag measured on the live tail instead of the assumed 5 min
              "REAL_ACQUISITION": run_certpl(rec, out_dir, horizon_h=horizon_h, k=k, time_basis="acquisition",
                                             tag="certpl-acquisition"),
              "REAL_LAG_SENSITIVITY": _lag_sensitivity(rec, out_dir, horizon_h=horizon_h, k=k),
              "SYNTHETIC": run_synthetic(out_dir, horizon_h=horizon_h),
              "rules": ["brand is never ground truth", "rankings are heuristic priorities, not probabilities",
                        "unresolved hypotheses stay in the denominator (lower/upper bounds)",
                        "REAL, SYNTHETIC and TEMPORAL REPLAY are reported separately"]}
    path = Path(settings.state_dir) / "hunt" / "replay-report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return report


def _lag_sensitivity(rec: Path, out_dir: Path, *, horizon_h: float, k: int) -> dict[str, Any]:
    _obs, info = load_certpl(rec)
    p90 = (info.get("live_tail_lag_min") or {}).get("p90")
    if not p90:
        return {"status": "unavailable", "reason": "no live-tail records to measure a publication lag"}
    r = run_certpl(rec, out_dir, horizon_h=horizon_h, k=k, lag=timedelta(minutes=p90), tag="certpl-lag-p90")
    return {"lag_minutes": p90, "lag_basis": "p90 of the measured live-tail rec_ts - ActionTime", **r}


def main(argv: list[str] | None = None) -> int:
    from hydra.config import get_settings

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--horizon", type=float, default=24.0)
    ap.add_argument("-k", type=int, default=10)
    a = ap.parse_args(argv)
    r = build_report(get_settings(), horizon_h=a.horizon, k=a.k)
    print(json.dumps({"REAL": r["REAL"].get("metrics", r["REAL"].get("status")),
                      "REAL_ACQUISITION": r["REAL_ACQUISITION"].get("metrics", r["REAL_ACQUISITION"].get("status")),
                      "REAL_LAG_SENSITIVITY": r["REAL_LAG_SENSITIVITY"].get(
                          "metrics", r["REAL_LAG_SENSITIVITY"].get("status")),
                      "recordings": r["REAL"].get("recordings"),
                      "SYNTHETIC": {"hypotheses": r["SYNTHETIC"]["hypotheses"],
                                    "candidates": r["SYNTHETIC"]["candidates"]}}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
