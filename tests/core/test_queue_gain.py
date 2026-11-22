"""ADV-QUEUE: analyst queue by expected information gain, simulated learning curve, Polish decision-card text.
SYNTHETIC fixtures only."""

from __future__ import annotations

import json
import sqlite3

from eval.queue_curve import make_world, simulate
from hydra.assign import BRIDGE_REASON, _pointer, best_pair_caveats
from hydra.contracts import EvidencePointer, Family, Gene, GeneMatch, Hypothesis, PairScore
from hydra.queue_gain import LinkGraph, p_confirm
from hydra.review_queue import build_queue, queue_order
from hydra.synapse import PairEval, build_genome

SCHEMA = """
CREATE TABLE runs (id TEXT PRIMARY KEY, params TEXT);
CREATE TABLE pairs (a TEXT, b TEXT, run_id TEXT, total_bits REAL, hypothesis TEXT, detail TEXT);
CREATE TABLE campaigns (id TEXT, run_id TEXT, detail TEXT);
CREATE TABLE campaign_members (campaign_id TEXT, incident_id TEXT, run_id TEXT);
"""


def _db(campaigns: dict[str, list[str]], pairs: list[tuple[str, str, float, str]], must=(), bridges=None):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    cons = [{"kind": "must", "a": a, "b": b} for a, b in must]
    conn.execute("INSERT INTO runs VALUES ('r', ?)", (json.dumps({"constraints": cons}),))
    for cid, ms in campaigns.items():
        sup = {"bridges": (bridges or {}).get(cid, [])}
        conn.execute("INSERT INTO campaigns VALUES (?, 'r', ?)", (cid, json.dumps({"meta": {"support": sup}})))
        conn.executemany("INSERT INTO campaign_members VALUES (?, ?, 'r')", [(cid, m) for m in ms])
    for a, b, bits, hyp in pairs:
        conn.execute("INSERT INTO pairs VALUES (?, ?, 'r', ?, ?, ?)",
                     (a, b, bits, hyp, json.dumps({"support": bits, "contradictions": []})))
    return conn


def test_reject_inside_a_campaign_is_the_minimum_cut_and_must_link_blocks():
    chain = {"C1": ["A", "B", "C", "D"]}
    pairs = [("A", "B", 20.0, "same_campaign"), ("B", "C", 11.0, "same_campaign"), ("C", "D", 20.0, "same_campaign")]
    lg = LinkGraph(_db(chain, pairs), "r", {m: "C1" for m in chain["C1"]})
    assert lg.split("B", "C", None) == (2, 2, False, 0.0)
    # a triangle with a weak side: rejecting the weak side cuts the cheaper edge, the rest flows over A
    tri = [("A", "B", 20.0, "same_campaign"), ("A", "C", 12.0, "same_campaign"), ("B", "C", 11.0, "same_campaign")]
    lg = LinkGraph(_db({"C1": ["A", "B", "C"]}, tri), "r", {m: "C1" for m in "ABC"})
    sa, sb, blocked, other = lg.split("B", "C", None)
    assert (sa, sb, blocked) == (2, 1, False) and other == 12.0
    lg = LinkGraph(_db(chain, pairs, must=[("B", "C")]), "r", {m: "C1" for m in chain["C1"]})
    assert lg.split("B", "C", None)[2] is True


def test_gain_order_prefers_the_decision_that_changes_most_and_explains_it_in_polish():
    camps = {"C1": ["A", "B", "C", "D"], "C2": ["E", "F", "G"], "C3": ["X"], "C4": ["Y"]}
    pairs = [("A", "B", 20.0, "same_campaign"), ("B", "C", 11.0, "same_campaign"), ("C", "D", 20.0, "same_campaign"),
             ("E", "F", 20.0, "same_campaign"), ("F", "G", 20.0, "same_campaign"),
             ("D", "E", 9.0, "abstain"),   # would merge 4 + 3 incidents
             ("X", "Y", 9.0, "abstain")]   # would merge 1 + 1
    br = {"C1": [{"a": "B", "b": "C", "split_sizes": [2, 2]}]}
    q = build_queue(_db(camps, pairs, bridges=br), "r", [], {}, set(), order="gain")
    assert q["order"] == "gain" and q["method"] == "queue_info_gain@1"
    keys = [it["key"] for it in q["items"]]
    assert keys[0] == "D|E" and keys.index("D|E") < keys.index("X|Y")
    top = q["items"][0]
    assert top["counterfactual"]["confirm"] == {"decisions": 12, "incidents": 3, "campaigns": ["C1", "C2"],
                                                "sizes": [4, 3], "effect": "merge", "assignments": 0}
    assert top["counterfactual"]["reject"]["effect"] == "none"
    assert top["why_first_pl"].startswith("Potwierdzenie połączy C1 i C2")
    assert "najwyższa w kolejce" in top["why_first_pl"]
    bridge = next(it for it in q["items"] if it["key"] == "B|C")
    assert bridge["counterfactual"]["reject"]["sides"] == [2, 2]
    assert bridge["counterfactual"]["reject"]["decisions"] == 4
    # the previous heuristic stays available and keeps its own order and method
    ev = build_queue(_db(camps, pairs, bridges=br), "r", [], {}, set(), order="ev")
    assert ev["method"] == "review_queue@1" and [it["key"] for it in ev["items"]] == sorted(
        keys, key=lambda k: next((-it["ev"], k) for it in ev["items"] if it["key"] == k))


def test_queue_order_default_and_env(monkeypatch):
    monkeypatch.delenv("HYDRA_QUEUE_ORDER", raising=False)
    assert queue_order() == "gain"
    monkeypatch.setenv("HYDRA_QUEUE_ORDER", "ev")
    assert queue_order() == "ev" and queue_order("gain") == "gain"
    monkeypatch.setenv("HYDRA_QUEUE_ORDER", "nonsense")
    assert queue_order() == "gain"


def test_p_confirm_is_half_at_threshold_and_shrinks_for_shaky_links():
    assert p_confirm(0.0, False) == 0.5 and p_confirm(None, False) == 0.5
    assert p_confirm(4.0, False) > p_confirm(4.0, True) > 0.5 > p_confirm(-4.0, False)


def test_oracle_simulation_runs_on_a_synthetic_world():
    world = make_world(7, n_campaigns=4, singletons=2, background=20)
    for strategy in ("gain", "ev", "random"):
        rows = simulate(world, strategy, 3)
        assert [r["k"] for r in rows] == [0, 1, 2, 3] and all(0.0 <= r["f1"] <= 1.0 for r in rows)


# --------------------------------------------------------------------------------------- decision card text


def _g(iid, kind, value, ev, family=Family.VIS):
    return Gene(incident_id=iid, family=family, kind=kind, value=value, extractor="t@1", evidence=ev)


def test_more_precise_pointer_of_the_same_value_replaces_a_whole_file_pointer():
    whole = EvidencePointer(artifact_id="v", timecode_s=0.0)
    frame = EvidencePointer(artifact_id="v", timecode_s=3.5, bbox=(10.0, 20.0, 30.0, 40.0))
    g = build_genome("n", [_g("n", "pdq", "ff00", whole), _g("n", "pdq", "ff00", frame)], {})
    best, replaced = _pointer(g, "pdq", "ff00", whole)
    assert replaced and best.timecode_s == 3.5
    assert _pointer(g, "pdq", "other", whole) == (whole, False)


def test_best_pair_caveats_copy_contradictions_missing_families_and_pointers():
    ev = EvidencePointer(artifact_id="x", timecode_s=0.0)
    contra = GeneMatch(family=Family.NAR, kind="scheme", value_a="investment", value_b="giveaway", bits=-3.0,
                       explanation_pl="Inny typ oszustwa")
    reason = GeneMatch(family=Family.VIS, kind="pdq", value_a="ff00", value_b="ff00", bits=12.0, evidence_a=ev,
                       evidence_b=ev, explanation_pl="Ten sam obraz")
    s = PairScore(a="k", b="n", total_bits=9.0, probability=0.5, hypothesis=Hypothesis.ABSTAIN, model="t@1",
                  reasons=[reason], contradictions=[contra], missing_families=["INF"])
    frame = EvidencePointer(artifact_id="x", timecode_s=2.0)
    genomes = {"n": build_genome("n", [_g("n", "pdq", "ff00", frame)], {})}
    out = best_pair_caveats(PairEval(score=s, campaign_bits=9.0, kit_bits=0.0, asset_bits=0.0), genomes)
    assert out["contradiction_list"] == [{"kind": "scheme", "family": "NAR", "bits": -3.0,
                                          "explanation_pl": "Inny typ oszustwa"}]
    assert out["missing_families"] == ["INF"]
    e = out["evidence"][0]
    assert e["refined"] is True and e["evidence_b"]["timecode_s"] == 2.0 and e["evidence_a"]["timecode_s"] == 0.0


def test_bridge_reason_code_stays_english_only_in_meta():
    assert BRIDGE_REASON == "bridges cannot-link"
    import inspect

    import hydra.assign as A
    import hydra.review as R

    for mod in (A, R):  # the reason_pl f-strings use the Polish text (CR-UI-FINAL-1)
        assert "({BRIDGE_REASON})" not in inspect.getsource(mod)
