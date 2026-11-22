"""HUNT core: no time cheating, hash-chained ledger, signatures, verification rules, next evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hydra.hunt import planner
from hydra.hunt.hypotheses import Ledger, issue
from hydra.hunt.records import DatasetKind, Observation, Tier, Trait
from hydra.hunt.replay import KnowledgeView, ReplayClock, TemporalLeak, load_certpl, name_traits
from hydra.hunt.scenarios import run_certpl, run_synthetic, synthetic_world
from hydra.hunt.signatures import build_signature
from hydra.hunt.verify import check, resolve

T0 = "2026-11-20T12:00:00Z"


def world():
    w = synthetic_world(T0)
    return w, w["known"] + w["later"]


def k_signature(view: KnowledgeView):
    vis = {o.ref: o.traits for o in view.visible()}
    members = ["syn-K1", "syn-K2", "syn-K3"]
    return build_signature("K-SYN", "K-SYN:r1", {m: vis[m] for m in members},
                           {i: t for i, t in vis.items() if i not in members}, approval_basis="analyst",
                           dataset_kind=DatasetKind.SYNTHETIC)


def k_hyp(tmp_path: Path | None = None):
    _w, allobs = world()
    view = KnowledgeView(allobs, T0)
    sig = k_signature(view)
    led = Ledger(tmp_path / "l.jsonl") if tmp_path else None
    return issue(sig, view, issued_at=T0, horizon_hours=24, sources=["synthetic_stream"], query_budget=3, ledger=led,
                 evidence_ids=["syn-K1", "syn-K2", "syn-K3"]), sig, allobs, led


# ------------------------------------------------------------------------------------ no time cheating


def test_view_refuses_future_observations():
    _w, allobs = world()
    view = KnowledgeView(allobs, T0)
    assert all(o.available_at <= T0 for o in view.visible())
    with pytest.raises(TemporalLeak):
        view.get("syn-v1-new-domain")
    with pytest.raises(KeyError):
        view.get("never-seen")


def test_issue_refuses_future_evidence_and_late_cutoff():
    _w, allobs = world()
    view = KnowledgeView(allobs, T0)
    sig = k_signature(view)
    with pytest.raises(TemporalLeak):
        issue(sig, view, issued_at=T0, horizon_hours=24, sources=["s"], query_budget=1,
              evidence_ids=["syn-K1", "syn-v1-new-domain"])
    with pytest.raises(ValueError):
        issue(sig, view, issued_at="2026-11-20T11:00:00Z", horizon_hours=24, sources=["s"], query_budget=1)


def test_hypothesis_is_independent_of_future_data():
    """Changing or removing everything after the cutoff cannot change what was issued."""
    w, allobs = world()
    h1 = issue(k_signature(KnowledgeView(allobs, T0)), KnowledgeView(allobs, T0), issued_at=T0, horizon_hours=24,
               sources=["s"], query_budget=3)
    only_past = w["known"]
    h2 = issue(k_signature(KnowledgeView(only_past, T0)), KnowledgeView(only_past, T0), issued_at=T0,
               horizon_hours=24, sources=["s"], query_budget=3)
    assert h1.hypothesis_id == h2.hypothesis_id
    assert h1.corpus_manifest_sha256 == h2.corpus_manifest_sha256


def test_replay_clock_orders_by_available_at():
    _w, allobs = world()
    seen = [o.available_at for o in ReplayClock(allobs, T0).advance("2026-11-22T00:00:00Z")]
    assert seen == sorted(seen) and all(s > T0 for s in seen)


# ------------------------------------------------------------------------------------ ledger


def test_ledger_hash_chain_and_tamper(tmp_path: Path):
    h, _sig, _all, led = k_hyp(tmp_path)
    led.append("VERIFIED", {"hypothesis_id": h.hypothesis_id, "status": "confirmed"})
    assert led.verify() == (True, 2, None)
    assert led.hypotheses()[h.hypothesis_id]["hypothesis"]["status"] == "confirmed"
    lines = led.path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["body"]["horizon_hours"] = 999  # rewrite history
    led.path.write_text(json.dumps(rec) + "\n" + lines[1] + "\n")
    ok, _n, bad = led.verify()
    assert not ok and bad == 1


# ------------------------------------------------------------------------------------ signature


def test_signature_separates_operator_from_decoration_and_kit():
    _w, allobs = world()
    sig = k_signature(KnowledgeView(allobs, T0))
    role = {f"{g.kind}={g.value}": g for g in sig.genes}
    assert role["tracker_id=PX-551"].role == "invariant"
    assert role["brand_label=brandx"].role == "decoration"  # a brand is never an invariant
    assert "domain" in sig.allowed_mutations and sig.mutations_basis == "observed"
    sdk = role["sdk=aio-sdk-1.0.28"]
    assert sdk.background_basis == "kit_users" and sdk.background_df == 2 and sdk.role != "invariant"
    g = role["tracker_id=PX-551"]
    assert g.utility == pytest.approx(g.stability * g.rarity * g.reliability, abs=1e-3)
    assert sig.score_kind == "heuristic_priority"


def test_hypothesis_traits_never_enter_a_signature():
    t = Trait(kind="tracker_id", value="PX-HYP", tier=Tier.OPERATOR, group="operator", source="har",
              provenance="HYP")
    real = Trait(kind="tracker_id", value="PX-1", tier=Tier.OPERATOR, group="operator", source="har")
    sig = build_signature("X", "X:r1", {"a": [t, real], "b": [t, real]}, {}, approval_basis="analyst")
    assert "tracker_id=PX-HYP" not in sig.invariants and "tracker_id=PX-1" in sig.invariants


# ------------------------------------------------------------------------------------ verification


def _later(allobs, ref):
    return next(o for o in allobs if o.ref == ref)


def test_counterexample_same_brand_other_operator_is_not_linked():
    h, _sig, allobs, _ = k_hyp()
    c = check(h, _later(allobs, "syn-v4-clone"))
    assert c["verdict"] == "conflict" and any(x.startswith("different_operator_id") for x in c["conflicts"])
    assert check(h, _later(allobs, "syn-v1-new-domain"))["verdict"] == "supports"


def test_self_derived_and_known_observations_never_confirm():
    h, _sig, allobs, _ = k_hyp()
    v1 = _later(allobs, "syn-v1-new-domain")
    assert check(h, v1.model_copy(update={"derived_from_hypothesis": h.hypothesis_id}))["verdict"] == "self"
    assert check(h, _later(allobs, "syn-K1"))["verdict"] == "known"
    hyp_only = v1.model_copy(update={"traits": [t.model_copy(update={"provenance": "HYP"}) for t in v1.traits]})
    assert check(h, hyp_only)["verdict"] != "supports"


def test_resolve_statuses():
    h, _sig, allobs, _ = k_hyp()
    later = [o for o in allobs if o.available_at > T0]
    checks = [check(h, o) for o in later]
    assert resolve(h, checks, "2026-11-22T00:00:00Z", coverage_complete=True)["status"] == "confirmed"
    none = [c for c in checks if c["verdict"] != "supports" and c["verdict"] != "unknown"]
    assert resolve(h, none, "2026-11-22T00:00:00Z", coverage_complete=True)["status"] == "refuted"
    assert resolve(h, none, "2026-11-22T00:00:00Z", coverage_complete=False)["status"] == "unverifiable"
    assert resolve(h, none, "2026-11-20T13:00:00Z", coverage_complete=True)["status"] == "pending"
    late = [c for c in checks if c["ref"] == "syn-v7-late"]  # supports, but after the horizon
    assert late and late[0]["verdict"] == "supports"
    assert resolve(h, late, "2026-11-22T00:00:00Z", coverage_complete=True)["status"] == "refuted"


# ------------------------------------------------------------------------------------ next evidence


def test_next_action_reads_dom_and_changes_the_decision():
    h, _sig, allobs, _ = k_hyp()
    step = planner.next_action(h, _later(allobs, "syn-v3-drop-har"))
    assert step["before"]["verdict"] == "decoration_only"
    top = step["ranking"][0]
    assert top["action"] == "read_dom_fragment" and top["resolving_power"] == "high" and top["priority"] == 3
    har = next(r for r in step["ranking"] if r["action"] == "reread_har")
    assert har["availability"] == "unavailable" and har["priority"] == 0  # the HAR was dropped
    assert step["after"]["verdict"] == "supports" and step["decision_changed"]


def test_next_action_exposes_a_template_clone():
    h, _sig, allobs, _ = k_hyp()
    step = planner.next_action(h, _later(allobs, "syn-v5-clone-har"))
    assert step["executed"]["action"] == "reread_har"
    assert step["after"]["verdict"] == "conflict" and step["decision_changed"]


def test_decisive_verdict_is_not_spent_on_actions():
    h, _sig, allobs, _ = k_hyp()
    step = planner.next_action(h, _later(allobs, "syn-v1-new-domain"))
    assert step["executed"] is None


# ------------------------------------------------------------------------------------ runs


def test_synthetic_run_is_labelled_and_beats_brand_baseline_on_its_own_construction(tmp_path: Path):
    r = run_synthetic(tmp_path)
    assert r["dataset_kind"] == "SYNTHETIC" and "not a performance claim" in r["note"]
    assert r["hypotheses"]["confirmed"] == 1 and r["hypotheses"]["refuted"] == 1
    assert r["candidates"]["hunt"]["k"] == r["candidates"]["brand_baseline"]["k"] == 3
    assert r["ledger"]["ok"]


def _rec(path: Path, rows: list[tuple[int, str, str, str]]) -> None:
    path.write_text("".join(json.dumps({"rec_ts": rec, "source": "certpl", "raw": {
        "RegisterPositionId": pid, "DomainAddress": host, "ActionTime": at, "ActionType": "block"}}) + "\n"
        for pid, host, at, rec in rows))


def test_certpl_replay_on_a_tiny_recording(tmp_path: Path):
    rec = tmp_path / "rec"
    rec.mkdir()
    r = "2026-11-25T00:00:00Z"
    _rec(rec / "certpl-backfill-x.jsonl", [
        (1, "olx.zakup-oferta1111.sbs", "2026-11-20T10:00:00+00:00", r),
        (2, "vinted.zakup-oferta2222.sbs", "2026-11-20T11:00:00+00:00", r),
        (3, "olx.paczka-dostawa77.top", "2026-11-20T11:30:00+00:00", r),
        (4, "olx.paczka-dostawa88.top", "2026-11-20T11:40:00+00:00", r),
        (5, "allegro.zakup-oferta3333.sbs", "2026-11-21T03:00:00+00:00", r),  # predicted variant
        (6, "olx.inny-sklep.shop", "2026-11-21T04:00:00+00:00", r),  # same brand, other generator
        (7, "x.filler.com", "2026-11-22T06:00:00+00:00", r)])
    obs, info = load_certpl(rec)
    assert obs[0].available_at_basis.startswith("assumed")
    out = run_certpl(rec, tmp_path, horizon_h=24, k=5, cutoffs=["2026-11-21T00:00:00Z"])
    by = {c["hypothesis"]["campaign_id"]: c["result"]["status"] for c in out["cases"]}
    assert by == {"F:zakup-oferta#": "confirmed", "F:paczka-dostawa#": "refuted"}
    assert out["counterexamples"] and out["counterexamples"][0]["observation"]["ref"] == "olx.inny-sklep.shop"
    m = out["metrics"]["hunt"]
    assert m["confirmed"] == 1 and m["refuted"] == 1 and m["precision_at_k"]["lower"] == 0.2
    assert out["ledger"]["ok"]


def test_generic_name_templates_are_not_operator_markers():
    t = {x.kind: x for x in name_traits("olx.o10202038.shop")}
    assert t["name_template"].tier == "kit" and t["brand_label"].tier == "decoration"


def test_observation_carries_three_times():
    o = Observation(observation_id="o", source="s", ref="r", event_time="2026-11-20T10:00:00Z",
                    available_at="2026-11-20T10:05:00Z", available_at_basis="x", collected_at="2026-11-21T00:00:00Z",
                    payload_sha256="0" * 64, evidence_pointer="p")
    assert o.event_time < o.available_at < o.collected_at
