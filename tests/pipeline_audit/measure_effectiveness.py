"""Aggregate-only, model-free method diagnostics on generated truth (not NASK accuracy)."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import subprocess
from collections import Counter
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

from hydra.assign import assign
from hydra.pipeline import rarity_for, score_and_cluster
from hydra.retrieval import candidates
from hydra.synapse import build_genome, score_pair
from tests.pipeline_audit.test_effectiveness import (
    TH,
    background,
    common_template_population,
    gene,
    genomes,
    growth_identity,
    growth_population,
    identity,
    labelled_population,
    pair_confusion,
    population_fingerprint,
)

ROOT = Path(__file__).resolve().parents[2]


def query_summary(population, clustering, *, known, extra_identity=False):
    counts = Counter({"assign": 0, "abstain": 0, "new_campaign": 0})
    supports = []
    for campaign in range(6):
        iid = f"query-{campaign}"
        label = campaign if known else f"unknown-{campaign}"
        genes = identity(iid, label) + background(iid)
        if extra_identity:
            genes.append(gene(iid, "landing_host", f"truth-{label}.example.test", "INF:operator:landing"))
        query = build_genome(iid, genes, {})
        result = assign(query, population, clustering.campaigns, clustering.groups)
        counts[str(result.assignment.decision)] += 1
        supports.append(result.assignment.meta["top_candidates"][0]["support"])
    return {"n": 6, "decisions": dict(counts), "support_range_bits": [min(supports), max(supports)]}


def measure():
    population, truth = labelled_population()
    _r, _p, clustering, _i = score_and_cluster(population, TH)
    small = {"n_incidents": len(population), "input_sha256": population_fingerprint(population),
             "pair_confusion": pair_confusion(clustering.membership, truth),
             "known_variant_tracker_phone": query_summary(population, clustering, known=True),
             "unknown_operator_shared_kit": query_summary(population, clustering, known=False)}
    rows = {iid: list(g.genes) for iid, g in population.items()}
    for iid, label in truth.items():
        if label.startswith("truth-"):
            rows[iid].append(gene(iid, "landing_host", f"{label}.example.test", "INF:operator:landing"))
    strengthened = genomes(rows)
    _r, _p, sc, _i = score_and_cluster(strengthened, TH)
    small["known_variant_second_infrastructure_identity"] = query_summary(strengthened, sc, known=True,
                                                                         extra_identity=True)
    growth = []
    for n in (60, 61):
        population = growth_population(n)
        rarity = rarity_for(population)
        candidate = candidates(population, rarity)
        relevant = list(combinations([f"i{i:02d}" for i in range(13)], 2))
        direct = score_pair(population["i00"], population["i01"], rarity)
        _r, _p, c, _i = score_and_cluster(population, TH)
        truth = {iid: "shared" if int(iid[1:]) < 13 else iid for iid in population}
        growth.append({"n": n, "input_sha256": population_fingerprint(population),
                       "true_pairs": len(relevant), "retrieved_true_pairs": sum(pair in candidate for pair in relevant),
                       "all_candidates": len(candidate), "direct_support_bits": direct.support,
                       "direct_hypothesis": direct.score.hypothesis,
                       "predicted_components_of_true_campaign": len({c.membership[f"i{i:02d}"] for i in range(13)}),
                       "pair_confusion": pair_confusion(c.membership, truth)})
    population = common_template_population()
    rarity, pairs, c, _i = score_and_cluster(population, TH)
    constant = {"n": len(population), "input_sha256": population_fingerprint(population),
                "predicted_campaigns": len(c.campaigns),
                "pair_confusion": pair_confusion(c.membership, {iid: iid for iid in population}),
                "support_bits": pairs["template-0", "template-1"].support,
                "operator_bits": pairs["template-0", "template-1"].operator_bits}
    identity_definition = [
        {"kind": g.kind, "value": g.value, "dependency_group": g.dependency_group, "quality": g.quality}
        for g in growth_identity("definition")
    ]
    return {"truth_fixed_population": small, "retrieval_boundary": growth,
            "retrieval_shared_identity_definition": identity_definition,
            "common_body_distinct_operators": constant}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = measure()
    source_paths = ["hydra/synapse.py", "hydra/entropy.py", "hydra/retrieval.py", "hydra/nexus.py", "hydra/assign.py",
                    "hydra/conformal.py", "hydra/pipeline.py", "hydra/genome/webparse.py", "hydra/genome/har.py",
                    "tests/pipeline_audit/test_effectiveness.py",
                    "tests/pipeline_audit/measure_effectiveness.py"]
    result.update({"synthetic": True, "level": "public gene/scorer contracts; model-free",
                   "measured_at": datetime.now(UTC).isoformat(), "python": platform.python_version(),
                   "git_commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                                text=True, check=True).stdout.strip(),
                   "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                                     for name in source_paths},
                   "command": ". /private/tmp/astra-pipeline-env.sh && .venv/bin/python "
                              "tests/pipeline_audit/measure_effectiveness.py --out " + str(args.out),
                   "limitations": ["No raw NASK material was read", "No ML encoder or OCR accuracy was measured",
                                   "Generator truth is synthetic, not independent real-world adjudication",
                                   "CR-201 uses distinct production groups; statistical independence is not proven",
                                   "CR-201 uses public genes, not raw HTML/HAR extraction or warninglists",
                                   "CR-203/204 request policy hardening, not specification-compliance fixes",
                                   "Populations are targeted challenges, not prevalence-representative samples",
                                   "No confidence interval or hidden-set generalization estimate is implied"]})
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key not in {"source_sha256"}}, indent=2))


if __name__ == "__main__":
    main()
