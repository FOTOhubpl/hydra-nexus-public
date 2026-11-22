"""Score the pairs only the ADV-RETR sources propose, on a read-only DB copy (aggregates only).

usage: python scripts/adv_retr_newpairs.py <copy of hydra.sqlite>
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bench_adv_retr import load  # noqa: E402

from hydra import pipeline as PL  # noqa: E402
from hydra import retrieval as R  # noqa: E402
from hydra import retrieval_adv as A  # noqa: E402
from hydra.entropy import duplicate_units, operator_seed_units  # noqa: E402
from hydra.synapse import Thresholds, score_pair  # noqa: E402

t0 = time.perf_counter()
g = load(sys.argv[1], None)
r = PL.rarity_for(g, duplicate_units({i: x.genes for i, x in g.items()}))
r.ignored = set()
r.seed_operator_unit_of = operator_seed_units({k: x.genes for k, x in g.items()}, r.ignored)
print("loaded", len(g), round(time.perf_counter() - t0), file=sys.stderr, flush=True)
adv: dict = {}


def add(a, b, src, _d=False):
    if a != b:
        adv.setdefault((a, b) if a < b else (b, a), set()).add(src)


n = len(g)
max_df = min(R.MAX_DF_ABS, max(5, int(0.2 * n)))
A._lsh(g, add, max_df=max_df, qset=None)
A._tfidf(g, add, k=10, qset=None)
print("adv", len(adv), round(time.perf_counter() - t0), file=sys.stderr, flush=True)
base = R.candidates(g, r)
new = [k for k in adv if k not in base]
th = Thresholds()
hyp: dict[str, int] = {}
linked = 0
for a, b in new:
    pe = score_pair(g[a], g[b], r, th, sorted(adv[(a, b)]))
    hyp[str(pe.score.hypothesis)] = hyp.get(str(pe.score.hypothesis), 0) + 1
    linked += bool(PL.is_linked(pe, th))
lab = sum(1 for a, b in new if g[a].label and g[a].label == g[b].label)
print(json.dumps({"n": n, "adv_pairs": len(adv), "new_pairs": len(new), "new_linked": linked,
                  "new_hypotheses": hyp, "new_same_label": lab, "s": round(time.perf_counter() - t0)}))
