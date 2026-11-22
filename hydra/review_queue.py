"""Analyst queue ordered by the expected value of a decision (ADOPT_PLAN #14).

Items (one per pair or per abstained assignment; a pair may carry several kinds):
- `bridge`: a link whose removal splits its campaign (`Campaign.meta.support.bridges`);
- `near_threshold`: a pair whose campaign-level support is within `NEAR_BITS` of the link threshold;
- `abstain`: a scorer abstain pair, or an open-set assignment that abstained (incl. "bridges cannot-link");
- `conflict`: a pair with a contradiction and some support, or two members of one campaign with a contradiction.

Expected value (heuristic, documented, not a probability): `ev = uncertainty x impact`.
- uncertainty = 2^(-|support - link| / 2): 1 at the threshold, 0.5 two bits away; a link that rests on one
  family (leave-one-family-out `robust_link = false`) or carries a contradiction is raised to at least 0.5;
  an assignment abstain uses its margin #1 - #2: 2^(-margin / 2).
- impact = incidents whose membership flips with the decision: the smaller side of a bridge, the smaller of two
  campaigns a cross-campaign pair would merge, 1 for any other pair or for an abstained incident (+1 when the
  abstain bridges two analyst-separated campaigns).
Skipped: pairs with an active accept/reject review, items marked "niepewne", and pairs between two campaigns
that already have an analyst decision between them (nothing new is learned from a second pair).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from typing import Any

from hydra import queue_gain
from hydra.labels_pl import num_pl
from hydra.synapse import FAMILY_PL, Thresholds

METHOD = "review_queue@1"
NEAR_BITS = 3.0
METHOD_PL = ("Kolejność = wartość decyzji (heurystyka): niepewność × wpływ. Niepewność = 2^(−|bity − próg| / 2),"
             " co najmniej 0,5 dla powiązania opartego na jednej rodzinie cech lub ze sprzecznością; wpływ = liczba"
             " incydentów, których przynależność zmieni decyzja. To nie jest prawdopodobieństwo.")
KIND_ORDER = ("bridge", "abstain", "conflict", "near_threshold")


def _u(distance: float) -> float:
    return 2.0 ** (-abs(distance) / 2.0)


def _reasons(detail: dict, n: int = 4) -> list[dict[str, Any]]:
    return [{"family": r.get("family"), "kind": r.get("kind"), "value": r.get("value_a"), "bits": r.get("bits"),
             "explanation_pl": r.get("explanation_pl")}
            for r in (detail.get("reasons") or []) if (r.get("bits") or 0) > 0][:n]


def build_queue(conn: sqlite3.Connection, run: str, assignment_rows: list[Any], decided: dict[tuple[str, str], str],
                unsure_keys: set[str], th: Thresholds | None = None, limit: int = 200, order: str | None = None
                ) -> dict[str, Any]:
    """`order`: "gain" (expected changed decisions per unit of analyst effort, `hydra.queue_gain`) or "ev" (the
    uncertainty x impact heuristic below); default `HYDRA_QUEUE_ORDER`, else "gain"."""
    th = th or Thresholds()
    link = th.link
    member_of: dict[str, str] = {}
    size: dict[str, int] = defaultdict(int)
    for r in conn.execute("SELECT campaign_id, incident_id FROM campaign_members WHERE run_id=?", (run,)):
        member_of[r["incident_id"]] = r["campaign_id"]
        size[r["campaign_id"]] += 1
    bridges: dict[tuple[str, str], dict] = {}
    for r in conn.execute("SELECT id, detail FROM campaigns WHERE run_id=?", (run,)):
        sup = ((json.loads(r["detail"]) or {}).get("meta") or {}).get("support") or {}
        for b in sup.get("bridges") or []:
            bridges[tuple(sorted((b["a"], b["b"])))] = {**b, "campaign_id": r["id"]}  # type: ignore[index]
    decided_between = {tuple(sorted((member_of.get(a, a), member_of.get(b, b)))) for a, b in decided
                       if member_of.get(a) != member_of.get(b)}
    skipped = {"decided": 0, "unsure": 0, "decided_between_campaigns": 0}
    items: list[dict[str, Any]] = []
    for r in conn.execute("SELECT a, b, hypothesis, total_bits, detail FROM pairs WHERE run_id=?", (run,)):
        a, b = r["a"], r["b"]
        detail = json.loads(r["detail"]) or {}
        support = float(detail.get("support", detail.get("campaign_bits", 0.0)) or 0.0)
        contra = bool(detail.get("contradictions"))
        ca, cb = member_of.get(a), member_of.get(b)
        same = ca is not None and ca == cb
        kinds = []
        br = bridges.get((a, b))
        if br:
            kinds.append("bridge")
        if r["hypothesis"] == "abstain":
            kinds.append("abstain")
        if contra and (support >= th.low or same):
            kinds.append("conflict")
        if abs(support - link) <= NEAR_BITS and r["hypothesis"] in ("same_campaign", "abstain"):
            kinds.append("near_threshold")
        if not kinds:
            continue
        if (a, b) in decided:
            skipped["decided"] += 1
            continue
        if f"{a}|{b}" in unsure_keys:
            skipped["unsure"] += 1
            continue
        if not same and ca and cb and tuple(sorted((ca, cb))) in decided_between:
            skipped["decided_between_campaigns"] += 1
            continue
        u = _u(support - link)
        if (detail.get("robust_link") is False) or contra:
            u = max(u, 0.5)
        if br:
            impact = int(br["split_sizes"][0])
        elif same or not (ca and cb):
            impact = 1
        else:
            impact = min(size[ca], size[cb])
        why = []
        if br:
            why.append(f"most w kampanii {br['campaign_id']}: odrzucenie oddzieli {br['split_sizes'][0]} z"
                       f" {sum(br['split_sizes'])} incydentów")
        if "near_threshold" in kinds:
            why.append(f"{num_pl(support)} bit wobec progu {num_pl(link, '.0f')}"
                       f" (różnica {num_pl(support - link, '+.1f')})")
        if "abstain" in kinds:
            why.append("model wstrzymał decyzję (dowody słabe lub sprzeczne)")
        if "conflict" in kinds:
            why.append("sprzeczność: " + str((detail.get("contradictions") or [{}])[0].get("explanation_pl") or
                                              "dowód przeciw")
                       + (" (w tej samej kampanii)" if same else ""))
        if detail.get("robust_link") is False:
            crit = ", ".join(FAMILY_PL.get(f, f)
                             for f in (detail.get("robustness") or {}).get("critical_families") or [])
            why.append(f"powiązanie zależy od jednej rodziny cech ({crit})" if crit else
                       "powiązanie zależy od jednej rodziny cech")
        items.append({"key": f"{a}|{b}", "kinds": [k for k in KIND_ORDER if k in kinds], "a": a, "b": b,
                      "incident_id": None, "campaigns": sorted({c for c in (ca, cb) if c}), "bits": round(support, 3),
                      "distance_bits": round(support - link, 3), "impact": impact, "uncertainty": round(u, 4),
                      "ev": round(u * impact, 4), "reason_pl": "; ".join(why) + ".", "hypothesis": r["hypothesis"],
                      "same_campaign": same, "robust_link": detail.get("robust_link")})
    for row in assignment_rows:
        if row["decision"] != "abstain":
            continue
        inc = row["incident_id"]
        if inc in member_of:
            continue
        if f"inc:{inc}" in unsure_keys:
            skipped["unsure"] += 1
            continue
        d = json.loads(row["detail"]) or {}
        meta = d.get("meta") or {}
        bridge = meta.get("bridge_cannot_link")
        margin = meta.get("margin_bits")
        u = _u(float(margin)) if isinstance(margin, int | float) else 1.0
        if bridge:
            u = 1.0
        cands = []
        by_cid = {c.get("campaign_id"): c for c in d.get("candidates") or []}
        for t in (meta.get("top_candidates") or [])[:2]:
            c = by_cid.get(t.get("campaign_id")) or {}
            cands.append({"campaign_id": t.get("campaign_id"), "bits": t.get("support"),
                          "supporting_members": t.get("supporting_members"),
                          "top_reasons": _reasons(c.get("best_pair") or {})})
        if bridge and not cands:
            for cid, bits, pair in zip(bridge.get("campaigns") or [], bridge.get("bits") or [],
                                       bridge.get("pairs") or [], strict=False):
                pr = conn.execute("SELECT detail FROM pairs WHERE run_id=? AND a=? AND b=?",
                                  (bridge.get("source_run"), *sorted(pair))).fetchone()
                cands.append({"campaign_id": cid, "bits": bits, "supporting_members": None,
                              "top_reasons": _reasons(json.loads(pr["detail"]) if pr else {})})
        impact = 2 if bridge else 1
        top_bits = cands[0]["bits"] if cands else None
        items.append({"key": f"inc:{inc}", "kinds": ["abstain", *(["bridge"] if bridge else [])], "a": None, "b": None,
                      "incident_id": inc, "campaigns": [c["campaign_id"] for c in cands if c["campaign_id"]],
                      "bits": top_bits,
                      "distance_bits": round(float(top_bits) - link, 3) if top_bits is not None else None,
                      "impact": impact, "uncertainty": round(u, 4), "ev": round(u * impact, 4),
                      "reason_pl": d.get("reason_pl") or "Przypisanie wstrzymane.", "candidates": cands,
                      "assign_run_id": row["run_id"]})
    order = queue_order(order)
    queue_gain.annotate(conn, run, items, assignment_rows, member_of, bridges)
    items.sort(key=queue_gain.sort_key(order))
    queue_gain.why_first(items, order)
    gain = order == "gain"
    return {"run_id": run, "method": queue_gain.METHOD if gain else METHOD,
            "method_pl": queue_gain.METHOD_PL if gain else METHOD_PL, "order": order, "orders": list(queue_gain.ORDERS),
            "threshold_bits": link, "near_bits": NEAR_BITS, "calibrated": False, "total": len(items),
            "items": items[:limit], "skipped": skipped}


def queue_order(order: str | None = None) -> str:
    order = order or os.environ.get("HYDRA_QUEUE_ORDER") or "gain"
    return order if order in queue_gain.ORDERS else "gain"


__all__ = ["METHOD", "NEAR_BITS", "build_queue", "queue_order"]
