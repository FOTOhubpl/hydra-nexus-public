"""TRAIN: background prior off vs mixed on the same genes of an external labelled corpus (e.g. corpus-demo).

    HYDRA_BACKGROUND_PRIOR_FILE=<prior.json.gz> uv run python scripts/prior_demo_compare.py <state dir>

The state holds the corpus already extracted (`scripts/train_prior.py extract`). Pairs linked into one campaign
by `score_and_cluster` (default thresholds) are compared with the dataset labels: same label, cross label (brand
or advertiser page differs: a false-merge proxy), touching a legitimate page; recall of same-label fraud pairs.
External data only; never point it at a state holding organiser incidents.
"""
import json
import os
import sys
from collections import Counter
from contextlib import closing
from itertools import combinations
from pathlib import Path

from hydra import pipeline as P
from hydra import priors
from hydra.db import connect
from hydra.synapse import Thresholds

state = Path(sys.argv[1])
with closing(connect(state / "hydra.sqlite", readonly=True)) as c:
    ids = sorted(r[0] for r in c.execute("SELECT DISTINCT incident_id FROM genes"))
    meta = {}
    for r in c.execute("SELECT id, label, meta FROM incidents"):
        m = json.loads(r["meta"] or "{}")
        ext = m.get("ext") or {}
        meta[r["id"]] = {"label": r["label"], "group": m.get("group_label") or "",
                         "kind": ext.get("label_kind") or m.get("label_kind"),
                         "ds": ext.get("dataset") or r["id"].split("-")[1]}
    genomes = P.load_genomes(c, ids)
print("incidents", len(genomes), Counter(v["ds"] for k, v in meta.items() if k in genomes))


def cls(i):
    g = meta[i]["group"].rsplit(":", 1)[-1]
    return "legit" if g in {"benign", "non_scam", "legit", "legitimate"} else "fraud"


def run(mode):
    os.environ["HYDRA_BACKGROUND_PRIOR"] = mode
    priors._load.cache_clear()
    _r, pairs, cl, _info = P.score_and_cluster(genomes, Thresholds())
    member = dict(cl.membership)
    camp_pairs = set()
    by = {}
    for i, c_ in member.items():
        by.setdefault(c_, []).append(i)
    for ms in by.values():
        camp_pairs |= {tuple(sorted(p)) for p in combinations(ms, 2)}
    out = Counter()
    for a, b in camp_pairs:
        la, lb = meta[a]["label"], meta[b]["label"]
        if cls(a) == "legit" or cls(b) == "legit":
            out["links_touching_legit"] += 1
        if la and lb:
            out["same_label" if la == lb else "cross_label"] += 1
        else:
            out["unlabelled_pair"] += 1
    # recall of same-label (advertiser page / brand) pairs among labelled fraud
    lab = [i for i in genomes if meta[i]["label"] and cls(i) == "fraud"]
    same = {tuple(sorted(p)) for p in combinations(lab, 2) if meta[p[0]]["label"] == meta[p[1]]["label"]}
    out["same_label_pairs_total"] = len(same)
    out["same_label_recall_pct"] = round(100 * len(same & camp_pairs) / max(1, len(same)), 2)
    out["campaigns_multi"] = sum(1 for ms in by.values() if len(ms) > 1)
    out["linked_pairs"] = len(camp_pairs)
    out["scored_pairs_ge_link"] = sum(1 for pe in pairs.values() if pe.score.total_bits >= Thresholds().link)
    bits = {k: pe.score.total_bits for k, pe in pairs.items()}
    return out, camp_pairs, bits


res = {}
for mode in ("0", "mixed"):
    res[mode] = run(mode)
    print(mode, dict(res[mode][0]))
a, b = res["0"][1], res["mixed"][1]
b0, b1 = res["0"][2], res["mixed"][2]
common = b0.keys() & b1.keys()
d = [b1[k] - b0[k] for k in common]
print("scored pairs common:", len(common), "lower with mixed:", sum(x < -1e-9 for x in d),
      "higher:", sum(x > 1e-9 for x in d), "mean delta bits:", round(sum(d) / max(1, len(d)), 3))
lp = [k for k in common if b0[k] >= Thresholds().link]
dl = [b1[k] - b0[k] for k in lp]
print("pairs >= link with off:", len(lp), "mean delta:", round(sum(dl) / max(1, len(dl)), 3), "dropped below link:",
      sum(1 for k in lp if b1[k] < Thresholds().link))
print("pairs only with off:", len(a - b), "only with mixed:", len(b - a))
for p in sorted(b - a)[:10]:
    print("  new with mixed:", p, meta[p[0]]["label"], meta[p[1]]["label"], cls(p[0]), cls(p[1]))
for p in sorted(a - b)[:10]:
    print("  lost with mixed:", p, meta[p[0]]["label"], meta[p[1]]["label"], cls(p[0]), cls(p[1]))
