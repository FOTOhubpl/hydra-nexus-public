"""Link-threshold sweep at scale (EVAL-EXTRA, 2026-11-22). Measurement only: the frozen default (10 bits) is not
changed; each point re-scores and re-clusters the stored genes with `Thresholds(link=L)` through the product path
`pipeline.score_and_cluster` (no re-extraction, nothing written to the state).

Two corpora:
- `sim`   : the NASK sample + every hidden_sim item stored by a sharded `hydra assign-batch` run, clustered
            together. Generated truth: a positive variant carries its source incident's sample label, a sample
            incident its organiser label (uncertain/missing labels are left out of the metrics), every negative
            is its own campaign. Metrics: B-cubed P/R/F1, pairwise precision, false-merge pairs, impure campaigns.
- `state` : an analysed state (e.g. 837 incidents: corpus-demo + NASK sample). Its dataset labels (brand target,
            advertiser page) are NOT campaign ground truth, so no F1 is computed there. Reported instead: campaign
            counts, largest campaign, and merges across independent sources (NASK sample / PhreshPhish /
            Purino scam ads), which are certain false merges whatever the true campaigns are.

    python -m eval.threshold_sweep sim --run <assignq run dir> --truth <sim>/truth.csv --labels <labels.json> \
        --json out.json [--dump-pairs pairs.jsonl]
    python -m eval.threshold_sweep state --state <state dir> --run-id <analyse run> --json out.json
Prints aggregates only (incident ids are hashed in the pair dump).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from eval import metrics as M

LINKS = [6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
DEFAULT_LINK = 10.0


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:12]


# --------------------------------------------------------------------------------------- pure helpers


def sim_truth(truth_rows: list[dict], item_incident: dict[str, str], sample_labels: dict[str, dict]
              ) -> tuple[dict[str, str], dict[str, str]]:
    """incident id -> generated truth label, incident id -> variant type ('sample' for the organiser incidents).
    Positives inherit the label of their source sample incident; negatives are singletons."""
    truth: dict[str, str] = {}
    variant: dict[str, str] = {}
    for iid, info in sample_labels.items():
        if info.get("label") and not info.get("uncertain"):
            truth[iid] = str(info["label"])
            variant[iid] = "sample"
    for r in truth_rows:
        iid = item_incident.get(r["item"])
        if not iid or iid in variant:  # not stored, or deduplicated into a sample incident
            continue
        variant[iid] = r["variant"]
        if r["expect"] == "new":
            truth[iid] = f"neg:{r['item']}"
        else:
            src = sample_labels.get(r["source"], {})
            if src.get("label") and not src.get("uncertain"):
                truth[iid] = str(src["label"])
    return truth, variant


def source_group(source: str) -> str:
    """Independent sources: the two PhreshPhish sizes are one upstream dataset (may overlap), so one group."""
    if source.startswith("ext:phreshphish"):
        return "phreshphish"
    if source.startswith("ext:"):
        return source.split(":", 1)[1]
    return "nask" if source == "nask-sample" else "other"


def cross_source(pred: dict[str, str], group: dict[str, str]) -> dict:
    by: dict[str, list[str]] = defaultdict(list)
    for i, c in pred.items():
        by[c].append(i)
    # 'other' (ad-hoc uploads of unknown origin) never counts as an independent source
    mixed = [m for m in ([i for i in v if group[i] != "other"] for v in by.values()) if len({group[i] for i in m}) > 1]
    pairs = 0
    for m in mixed:
        cnt = Counter(group[i] for i in m)
        n = len(m)
        pairs += (n * (n - 1) - sum(v * (v - 1) for v in cnt.values())) // 2
    sizes = sorted((len(m) for m in by.values()), reverse=True)
    return {"campaigns": len(by), "multi_member_campaigns": sum(1 for s in sizes if s > 1),
            "largest_campaign": sizes[0] if sizes else 0, "incidents_in_multi": sum(s for s in sizes if s > 1),
            "cross_source_campaigns": len(mixed), "cross_source_pairs": pairs,
            "cross_source_incidents": sum(len(m) for m in mixed),
            "cross_source_composition": sorted((dict(sorted(Counter(group[i] for i in m).items())) for m in mixed),
                                               key=lambda d: -sum(d.values()))}


def truth_metrics(pred: dict[str, str], truth: dict[str, str]) -> dict:
    g = M.grouping(pred, truth)
    return {"bcubed_f1": g["bcubed"]["f1"], "bcubed_precision": g["bcubed"]["precision"],
            "bcubed_recall": g["bcubed"]["recall"], "pairwise_precision": g["pairwise"]["precision"],
            "pairwise_recall": g["pairwise"]["recall"], "pairwise_f1": g["pairwise"]["f1"],
            "false_merge_pairs": g["pairwise"]["false_merge_pairs"], "impure_campaigns": g["impure_clusters"],
            "pred_campaigns": g["pred_clusters"], "true_campaigns": g["true_clusters"], "n": g["pairwise"]["n"]}


def variant_diagnostics(pred: dict[str, str], truth: dict[str, str], variant: dict[str, str]) -> dict:
    """Per variant type: how many labelled positives share a predicted campaign with their organiser sample
    incident(s); and the variant mix of every impure predicted campaign (types only, no ids)."""
    sample_camps: dict[str, set[str]] = defaultdict(set)
    for i, v in variant.items():
        if v == "sample" and i in truth:
            sample_camps[truth[i]].add(pred[i])
    per: dict[str, Counter] = defaultdict(Counter)
    for i, v in variant.items():
        if v == "sample" or i not in truth or truth[i].startswith("neg:"):
            continue
        per[v]["n"] += 1
        per[v]["with_source"] += pred[i] in sample_camps.get(truth[i], set())
    by: dict[str, list[str]] = defaultdict(list)
    for i, c in pred.items():
        if i in truth:
            by[c].append(i)
    impure = [dict(sorted(Counter(variant.get(i, "?") for i in m).items())) for m in by.values()
              if len({truth[i] for i in m}) > 1]
    return {"positives_with_source": {v: dict(c) for v, c in sorted(per.items())}, "impure_mix": impure}


def pair_rows(pairs: dict, truth: dict[str, str]) -> list[dict]:
    """Calibration rows (`eval.calibration`): scored candidate pairs with both ends labelled; ids hashed."""
    out = []
    for (a, b), pe in pairs.items():
        if a in truth and b in truth:
            out.append({"a": _h(a), "b": _h(b), "label_a": _h(truth[a]), "label_b": _h(truth[b]),
                        "support": pe.support, "total_bits": pe.score.total_bits,
                        "probability": pe.score.probability, "hypothesis": str(pe.score.hypothesis)})
    return out


# --------------------------------------------------------------------------------------- loading (server)


def _prior(settings, params: dict):
    from hydra.pipeline import load_prior

    return load_prior(settings, params["prior"]) if params.get("prior") else None


def load_state(state: Path, run_id: str | None):
    from hydra.config import Settings
    from hydra.db import connect
    from hydra.pipeline import latest_run, load_clustering, load_genomes

    s = Settings(state_dir=state)
    with closing(connect(s.db_path, readonly=True)) as conn:
        run_id = run_id or latest_run(conn)
        camps, _ = load_clustering(conn, run_id)
        ids = sorted({m for c in camps for m in c.members})
        params = json.loads(conn.execute("SELECT params FROM runs WHERE id = ?", (run_id,)).fetchone()["params"])
        src = {r["id"]: r["source"] for r in conn.execute("SELECT id, source FROM incidents")}
        genomes = load_genomes(conn, ids)
    return s, run_id, params, genomes, {i: src.get(i, "other") for i in genomes}


def load_sim(run_dir: Path, shards: int = 4):
    """Sample genomes from state-0 plus each shard's stored items (ids from out-k/assignments.json)."""
    from hydra.config import Settings
    from hydra.db import connect
    from hydra.pipeline import latest_run, load_clustering, load_genomes

    s0 = Settings(state_dir=run_dir / "state-0")
    with closing(connect(s0.db_path, readonly=True)) as conn:
        base = latest_run(conn, "analyse")
        camps, _ = load_clustering(conn, base)
        params = json.loads(conn.execute("SELECT params FROM runs WHERE id = ?", (base,)).fetchone()["params"])
        genomes = load_genomes(conn, sorted({m for c in camps for m in c.members}))
    item_incident: dict[str, str] = {}
    for k in range(shards):
        rows = json.loads((run_dir / f"out-{k}" / "assignments.json").read_text(encoding="utf-8"))
        ids = [r["incident_id"] for r in rows if r.get("incident_id") and r["incident_id"] not in genomes]
        with closing(connect(Settings(state_dir=run_dir / f"state-{k}").db_path, readonly=True)) as conn:
            genomes.update(load_genomes(conn, sorted(set(ids))))
        item_incident.update({r["item"]: r["incident_id"] for r in rows if r.get("incident_id")})
    return s0, base, params, genomes, item_incident


def sweep(genomes: dict, prior, links: list[float], on_point=None) -> dict[float, tuple[dict[str, str], dict]]:
    from hydra.pipeline import score_and_cluster
    from hydra.synapse import Thresholds

    out = {}
    for link in links:
        t0 = time.time()
        _rarity, pairs, clustering, _info = score_and_cluster(genomes, Thresholds(link=link), prior=prior)
        pred = {i: clustering.membership.get(i, f"solo:{i}") for i in genomes}
        out[link] = (pred, pairs)
        if on_point:
            on_point(link, pred, pairs, time.time() - t0)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a1 = sub.add_parser("sim")
    a1.add_argument("--run", type=Path, required=True)
    a1.add_argument("--truth", type=Path, required=True)
    a1.add_argument("--labels", type=Path, required=True)
    a1.add_argument("--dump-pairs", type=Path, default=None, help="calibration rows at the default link")
    a2 = sub.add_parser("state")
    a2.add_argument("--state", type=Path, required=True)
    a2.add_argument("--run-id", default=None)
    for p in (a1, a2):
        p.add_argument("--json", type=Path, default=None)
        p.add_argument("--links", type=float, nargs="+", default=LINKS)
    a = ap.parse_args()
    rows: list[dict] = []
    if a.cmd == "sim":
        s, base, params, genomes, item_incident = load_sim(a.run)
        lab = json.loads(a.labels.read_text(encoding="utf-8"))["incidents"]
        truth_rows = list(csv.DictReader(a.truth.open(encoding="utf-8")))
        truth, variant = sim_truth(truth_rows, item_incident, lab)
        meta = {"corpus": "sample+hidden_sim", "base_run": base, "incidents": len(genomes),
                "labelled": len(truth), "by_variant": dict(sorted(Counter(variant.values()).items()))}

        def point(link, pred, pairs, dt):
            r = {"link": link, "default": link == DEFAULT_LINK, "seconds": round(dt, 1),
                 "candidate_pairs": len(pairs), **truth_metrics(pred, truth)}
            if link == DEFAULT_LINK:
                r["diagnostics"] = variant_diagnostics(pred, truth, variant)
            rows.append(r)
            print(json.dumps(r))
            if a.dump_pairs and link == DEFAULT_LINK:
                with a.dump_pairs.open("w", encoding="utf-8") as fh:
                    for x in pair_rows(pairs, truth):
                        fh.write(json.dumps(x) + "\n")
    else:
        s, run_id, params, genomes, source = load_state(a.state, a.run_id)
        group = {i: source_group(source[i]) for i in genomes}
        meta = {"corpus": str(a.state.name), "run_id": run_id, "incidents": len(genomes),
                "by_source_group": dict(sorted(Counter(group.values()).items()))}

        def point(link, pred, pairs, dt):
            r = {"link": link, "default": link == DEFAULT_LINK, "seconds": round(dt, 1),
                 "candidate_pairs": len(pairs), **cross_source(pred, group)}
            rows.append(r)
            print(json.dumps(r))
    print(json.dumps(meta))
    sweep(genomes, _prior(s, params), a.links, point)
    if a.json:
        a.json.write_text(json.dumps({"meta": meta, "points": rows}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
