"""Simulated analyst learning curve for the review queue order (ADV-QUEUE, 2026-11-22). SYNTHETIC data only.

A world = synthetic incidents with ground-truth campaigns, scored by the real `synapse.score_pair` and clustered by
the real `nexus.cluster`. Generator (seeded): campaigns of 2-8 members; each campaign owns three rare operator
values (tracker ids, ~14 bit, link alone) and one phrase (TXT, 8 bit: abstain alone); every member carries each
operator value with probability `q_op` and the phrase with `q_txt`; noise: a member also carries another
campaign's operator value (`p_false_op`, false links -> false merges and bridges) or phrase (`p_false_txt`).
Plus unrelated singletons and a background of one-host incidents for rarity.

Oracle analyst: answers the first queue item (or a random one) with the truth: accept_link when both incidents
share a true campaign, else reject_link; the answer becomes a must/cannot-link constraint, the world is re-clustered
with `nexus.cluster(..., must_link, cannot_link)` exactly as `hydra.review.rebuild` does, the queue is rebuilt from
the new revision (answered pairs skipped as decided). Metric: pairwise F1 against the ground truth after k answers.

usage: uv run python -m eval.queue_curve --worlds 12 --steps 30 --regime sparse --out docs/eval/queue_curve_sparse
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import time
from itertools import combinations
from pathlib import Path
from typing import Any

from eval.metrics import pairwise
from hydra.contracts import EvidencePointer, Family, Gene
from hydra.entropy import build_rarity
from hydra.events import canonical_json
from hydra.nexus import cluster
from hydra.review_queue import build_queue
from hydra.synapse import build_genome, score_pair

STRATEGIES = ("gain", "ev", "random")
# sparse: links are missing (recall-limited, the NASK sample regime); noisy: more cross-campaign false links
REGIMES = {"sparse": {}, "noisy": {"q_op": 0.55, "p_false_op": 0.15, "p_false_txt": 0.2}}


def _gene(iid: str, kind: str, value: str, group: str, family: Family = Family.INF) -> Gene:
    return Gene(incident_id=iid, family=family, kind=kind, value=value, extractor="queue_curve_synthetic@1",
                dependency_group=group,
                evidence=EvidencePointer(artifact_id=hashlib.sha256(f"{iid}:{kind}:{value}".encode()).hexdigest(),
                                         note="SYNTHETIC queue-curve fixture"))


def make_world(seed: int, n_campaigns: int = 10, n_op: int = 6, q_op: float = 0.4, q_txt: float = 0.7,
               p_false_op: float = 0.08, p_false_txt: float = 0.12, singletons: int = 8, background: int = 150
               ) -> dict[str, Any]:
    rng = random.Random(seed)
    rows: dict[str, list[Gene]] = {}
    truth: dict[str, str] = {}
    camps = [f"T{seed}-{c}" for c in range(n_campaigns)]
    for c in camps:
        for m in range(rng.randint(2, 8)):
            iid = f"{c}-m{m}"
            truth[iid] = c
            rows[iid] = [_gene(iid, "host", f"{iid}.invalid", "INF:operator:host")]
    ids = sorted(rows)
    for iid in ids:
        c = truth[iid]
        for j in range(n_op):
            if rng.random() < q_op:
                rows[iid].append(_gene(iid, "tracker_id", f"{c}-op{j}", "INF:operator:tracker"))
        if rng.random() < q_txt:
            rows[iid].append(_gene(iid, "phrase", f"fraza kampanii {c}", "TXT:phrase", Family.TXT))
        if rng.random() < p_false_op:
            o = rng.choice([x for x in camps if x != c])
            rows[iid].append(_gene(iid, "tracker_id", f"{o}-op{rng.randrange(n_op)}", "INF:operator:tracker"))
        if rng.random() < p_false_txt:
            o = rng.choice([x for x in camps if x != c])
            rows[iid].append(_gene(iid, "phrase", f"fraza kampanii {o}", "TXT:phrase", Family.TXT))
    for k in range(singletons):
        iid = f"S{seed}-{k}"
        truth[iid] = iid
        rows[iid] = [_gene(iid, "host", f"{iid}.invalid", "INF:operator:host")]
        if rng.random() < 0.5:  # an unrelated incident that reuses one campaign phrase (hard negative)
            rows[iid].append(_gene(iid, "phrase", f"fraza kampanii {rng.choice(camps)}", "TXT:phrase", Family.TXT))
    for k in range(background):
        iid = f"background-{k:03}"
        truth[iid] = iid
        rows[iid] = [_gene(iid, "host", f"{iid}.invalid", "INF:operator:host")]
    genomes = {iid: build_genome(iid, gs, {}) for iid, gs in rows.items()}
    rarity = build_rarity(rows, {})
    pairs = {(a, b): score_pair(genomes[a], genomes[b], rarity) for a, b in combinations(sorted(genomes), 2)}
    details = {k: canonical_json({**pe.score.model_dump(mode="json"), "campaign_bits": pe.campaign_bits,
                                  "support": pe.support, "campaign_by_family": pe.campaign_by_family})
               for k, pe in pairs.items() if pe.score.total_bits > 0 or pe.support > 0}
    return {"seed": seed, "genomes": genomes, "rarity": rarity, "pairs": pairs, "details": details, "truth": truth}


SCHEMA = """
CREATE TABLE runs (id TEXT PRIMARY KEY, params TEXT);
CREATE TABLE pairs (a TEXT, b TEXT, run_id TEXT, total_bits REAL, hypothesis TEXT, detail TEXT);
CREATE TABLE campaigns (id TEXT, run_id TEXT, detail TEXT);
CREATE TABLE campaign_members (campaign_id TEXT, incident_id TEXT, run_id TEXT);
"""


def _revision_db(world: dict, clustering: Any, constraints: list[dict]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO runs VALUES ('r', ?)", (json.dumps({"constraints": constraints}),))
    pairs = world["pairs"]
    conn.executemany("INSERT INTO pairs VALUES (?, ?, 'r', ?, ?, ?)",
                     [(a, b, pairs[(a, b)].score.total_bits, str(pairs[(a, b)].score.hypothesis), d)
                      for (a, b), d in world["details"].items()])
    for c in clustering.campaigns:
        conn.execute("INSERT INTO campaigns VALUES (?, 'r', ?)", (c.id, json.dumps({"meta": c.meta}, default=str)))
        conn.executemany("INSERT INTO campaign_members VALUES (?, ?, 'r')", [(c.id, m) for m in c.members])
    return conn


def simulate(world: dict, strategy: str, steps: int, seed: int = 0) -> list[dict[str, Any]]:
    """F1 after 0..steps oracle answers (sequential: the queue is rebuilt after every answer)."""
    rng = random.Random(seed)
    truth = world["truth"]
    must: list[tuple[str, str]] = []
    cannot: list[tuple[str, str]] = []
    decided: dict[tuple[str, str], str] = {}
    out = []
    for k in range(steps + 1):
        cl = cluster(world["genomes"], world["pairs"], world["rarity"], must_link=must, cannot_link=cannot)
        m = pairwise(cl.membership, truth)
        row = {"k": k, "f1": m["f1"], "precision": m["precision"], "recall": m["recall"],
               "campaigns": sum(1 for c in cl.campaigns if len(c.members) > 1)}
        out.append(row)
        if k == steps:
            break
        cons = [{"kind": "must", "a": a, "b": b} for a, b in must] + [{"kind": "cannot", "a": a, "b": b}
                                                                    for a, b in cannot]
        conn = _revision_db(world, cl, cons)
        q = build_queue(conn, "r", [], decided, set(), limit=100000, order="ev" if strategy == "ev" else "gain")
        conn.close()
        items = [it for it in q["items"] if it["a"] and it["b"]]
        row["queue"] = len(items)
        if not items:
            for j in range(k + 1, steps + 1):
                out.append({**{x: y for x, y in row.items() if x != "queue"}, "k": j, "queue": 0})
            break
        it = rng.choice(items) if strategy == "random" else items[0]
        a, b = it["a"], it["b"]
        if truth[a] == truth[b]:
            must.append((a, b))
            decided[(a, b)] = "accept_link"
        else:
            cannot.append((a, b))
            decided[(a, b)] = "reject_link"
    return out


def run(worlds: int, steps: int, random_seeds: int = 5, first_seed: int = 100, regime: str = "sparse"
        ) -> dict[str, Any]:
    t0 = time.time()
    curves: dict[str, list[list[float]]] = {s: [] for s in STRATEGIES}
    per_world = []
    for w in range(worlds):
        world = make_world(first_seed + w, **REGIMES[regime])
        rec = {"seed": first_seed + w, "incidents": sum(1 for x in world["truth"] if not x.startswith("background"))}
        for s in STRATEGIES:
            reps = random_seeds if s == "random" else 1
            runs = [[r["f1"] for r in simulate(world, s, steps, seed=j)] for j in range(reps)]
            avg = [sum(col) / len(col) for col in zip(*runs, strict=True)]
            curves[s].append(avg)
            rec[s] = {"f1_0": avg[0], "f1_5": avg[min(5, steps)], "f1_10": avg[min(10, steps)], "f1_end": avg[-1]}
        per_world.append(rec)
    mean = {s: [round(sum(col) / len(col), 4) for col in zip(*curves[s], strict=True)] for s in STRATEGIES}
    auc = {s: round(sum(mean[s][1:]) / steps, 4) for s in STRATEGIES}
    not_worse = all(g >= e - 1e-9 for g, e in zip(mean["gain"], mean["ev"], strict=True))
    worlds_worse = sum(1 for c_g, c_e in zip(curves["gain"], curves["ev"], strict=True)
                       if sum(c_g[1:]) < sum(c_e[1:]) - 1e-9)
    return {"regime": regime, "generator": REGIMES[regime], "worlds": worlds, "steps": steps,
            "random_seeds": random_seeds, "mean_f1": mean, "mean_f1_auc": auc,
            "gain_not_worse_than_ev_at_every_k": not_worse, "worlds_gain_auc_below_ev": worlds_worse,
            "per_world": per_world, "seconds": round(time.time() - t0, 1)}


def plot(res: dict[str, Any], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = {"gain": "#1f6feb", "ev": "#d97706", "random": "#8b8b8b"}
    names = {"gain": "info-gain / effort (new)", "ev": "uncertainty x impact (previous)", "random": "random item"}
    fig, ax = plt.subplots(figsize=(7.2, 4.2), dpi=150)
    ks = list(range(res["steps"] + 1))
    for s in STRATEGIES:
        ax.plot(ks, res["mean_f1"][s], color=colors[s], lw=2 if s != "random" else 1.5,
                ls="-" if s != "random" else "--", label=f"{names[s]} (mean F1 over k=1..{res['steps']}:"
                                                        f" {res['mean_f1_auc'][s]:.3f})")
    ax.set_xlabel("oracle analyst answers (k)")
    ax.set_ylabel("pairwise campaign F1 (mean over worlds)")
    ax.set_title(f"SYNTHETIC queue learning curve ({res['regime']}): {res['worlds']} worlds,"
                 f" random = {res['random_seeds']} seeds",
                 fontsize=10)
    ax.grid(alpha=0.25)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    fig.tight_layout()
    fig.savefig(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worlds", type=int, default=12)
    ap.add_argument("--steps", type=int, default=30)
    ap.add_argument("--random-seeds", type=int, default=5)
    ap.add_argument("--regime", choices=sorted(REGIMES), default="sparse")
    ap.add_argument("--out", default="docs/eval/queue_curve_sparse")
    a = ap.parse_args()
    res = run(a.worlds, a.steps, a.random_seeds, regime=a.regime)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".json").write_text(json.dumps(res, indent=1) + "\n")
    plot(res, out.with_suffix(".png"))
    print(json.dumps({k: v for k, v in res.items() if k != "per_world"}, indent=1))


if __name__ == "__main__":
    main()
