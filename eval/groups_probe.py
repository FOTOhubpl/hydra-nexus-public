"""G2 campaign-group diagnosis on the eval harness's 231-incident retrieval corpus (SYNTHETIC-heavy).

Rebuilds the retrieval corpus of `eval.run.main` (syn-dev + sample+syn + robustness variants) from the shard
states of a finished eval run (read-only preload, no extraction), then runs CORE's build on it (all pairs, and
optionally CORE's candidate filtering) and on `sample` / `sample+syn`, and writes:

- `<out>/metrics.json`: campaign / group pairwise metrics per corpus, campaign-partition sha256;
- `<out>/bridges.json` (with --diagnose): every group link that joins campaigns of different labelled groups,
  with the genes behind it (kind, family, df, bits) and the incidents' corpus kind.

Usage (server): uv run python -m eval.groups_probe --shards /tmp/hydra-eval-sub-final-f177f0d --out DIR
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zlib
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

from eval import labels as L
from eval import metrics as M
from eval import run as R
from eval import variants as V
from eval.backend import HydraBackend


def corpora(a) -> tuple[HydraBackend, dict[str, R.Corpus]]:
    sample_rows = V.load_sample(a.sample, a.labels)
    s1 = R.relabel(R.uniq_ids(R.relocate(json.loads((a.variants / "set-s1" / "manifest.json").read_text())
                                         ["incidents"], a.variants), "s1"), sample_rows)
    s2 = R.relabel(R.uniq_ids(R.relocate(json.loads((a.variants / "set-s2" / "manifest.json").read_text())
                                         ["incidents"], a.variants), "s2"), sample_rows)
    rob = R.robustness_set(sample_rows, a.out / "robustness")
    rob_rows = [r for rows in rob.values() for r in rows]
    noenrich_rows = [{"id": f"rob-noenrich-{r['id']}", "path": r["path"], "label": r["label"],
                      "group_label": r["group_label"], "parent": r["id"], "kind": "enrich_unreachable",
                      "synthetic": True} for r in sample_rows]
    primary = HydraBackend(state_dir=a.out / "state")
    seen, uniq = set(), []
    for r in sample_rows + s2 + rob_rows + s1:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    for k in range(a.extract_workers):
        part = [r["id"] for r in uniq if zlib.crc32(r["id"].encode()) % a.extract_workers == k]
        if part:
            miss = primary.preload(a.shards / f"shard-{k:02d}" / "hydra.sqlite", part)
            assert not miss, f"missing genes: {miss[:5]}"
    primary.preload(a.shards / "noenrich-00" / "hydra.sqlite", [r["id"] for r in noenrich_rows])
    refs = R.refs_from_rows
    sample = R.Corpus("sample", refs(sample_rows), primary)
    mixed = R.Corpus("sample+syn", refs(sample_rows + s2), primary)
    rob_c = R.Corpus("robust-variants", refs(rob_rows + noenrich_rows), primary)
    robust = R.robust_corpus(mixed, rob_c)
    dev = R.Corpus("syn-dev", refs(s1), primary)
    big = R.Corpus.__new__(R.Corpus)
    big.name, big.disputed = "retrieval", set()
    big.refs = {**dev.refs, **robust.refs}
    big.genes = {**dev.genes, **robust.genes}
    big.labels = {**dev.labels, **robust.labels}
    big.groups = {**dev.groups, **robust.groups}
    return primary, {"sample": sample, "sample+syn": mixed, "retrieval": big}


def partition_sha(member_of: dict[str, str]) -> str:
    comps: dict[str, list[str]] = defaultdict(list)
    for i, c in member_of.items():
        comps[c].append(i)
    canon = sorted(sorted(v) for v in comps.values())
    return hashlib.sha256(json.dumps(canon).encode()).hexdigest()


def group_sha(b) -> str:
    return partition_sha(b.group_of)


def kind_of(corpus, iid: str) -> str:
    return (corpus.refs[iid].meta or {}).get("kind", "sample")


def diagnose(b, corpus) -> dict:
    cl = b.ctx["clustering"]
    pairs = b.ctx["pair_evals"]
    memb = cl.membership
    camp_grp: dict[str, str] = {}
    for c in cl.campaigns:
        labs = Counter(corpus.groups.get(m) for m in c.members)
        camp_grp[c.id] = labs.most_common(1)[0][0]
    from hydra.nexus import group_link_supported
    from hydra.synapse import Hypothesis

    kit = b.ctx["kit"]
    bridges, gene_ct, gene_bits = [], Counter(), defaultdict(float)
    for (x, y), pe in pairs.items():
        cx, cy = memb[x], memb[y]
        if cx == cy:
            continue
        if not (pe.score.hypothesis == Hypothesis.SHARED_TEMPLATE or pe.kit_bits >= kit):
            continue
        if not group_link_supported(pe):
            continue
        wrong = camp_grp[cx] != camp_grp[cy]
        genes = [{"kind": r.kind, "family": str(r.family), "df": r.doc_freq, "bits": r.bits,
                  "value": (r.value_a or "")[:60]} for r in pe.score.reasons if r.bits > 0]
        row = {"a": x, "b": y, "ca": cx, "cb": cy, "kind_a": kind_of(corpus, x), "kind_b": kind_of(corpus, y),
               "grp_a": corpus.groups.get(x), "grp_b": corpus.groups.get(y), "wrong": wrong,
               "hyp": str(pe.score.hypothesis), "kit_bits": pe.kit_bits, "kit_groups": pe.kit_groups,
               "kit_min_df": pe.kit_min_df, "campaign_bits": pe.campaign_bits, "genes": genes}
        bridges.append(row)
        if wrong:
            for gn in genes:
                gene_ct[(gn["kind"], gn["family"])] += 1
                gene_bits[(gn["kind"], gn["family"])] += gn["bits"]
    wrong = [r for r in bridges if r["wrong"]]
    pair_sides: dict = defaultdict(lambda: {"links": 0, "strong_links": 0, "a": set(), "b": set(), "sa": set(),
                                            "sb": set()})
    for r in wrong:
        k = tuple(sorted((r["ca"], r["cb"])))
        ps = pair_sides[k]
        ps["links"] += 1
        sa, sb = (r["a"], r["b"]) if r["ca"] == k[0] else (r["b"], r["a"])
        ps["a"].add(sa)
        ps["b"].add(sb)
        if r["kit_groups"] >= 2:
            ps["strong_links"] += 1
            ps["sa"].add(sa)
            ps["sb"].add(sb)
    size = {c.id: len(c.members) for c in cl.campaigns}
    wrong_pairs = [{"campaigns": list(k), "sizes": [size[k[0]], size[k[1]]],
                    "groups": [camp_grp[k[0]], camp_grp[k[1]]], "links": v["links"],
                    "strong_links": v["strong_links"], "members": [len(v["a"]), len(v["b"])],
                    "strong_members": [len(v["sa"]), len(v["sb"])],
                    "strong_member_ids": [sorted(v["sa"])[:6], sorted(v["sb"])[:6]]}
                   for k, v in sorted(pair_sides.items())]
    groups = []
    for g in cl.groups:
        mem = [m for cid in g.campaign_ids for m in next(c.members for c in cl.campaigns if c.id == cid)]
        groups.append({"id": g.id, "campaigns": len(g.campaign_ids), "members": len(mem),
                       "true_groups": dict(Counter(corpus.groups.get(m) for m in mem).most_common(8)),
                       "kinds": dict(Counter(kind_of(corpus, m) for m in mem).most_common(8)),
                       "meta": cl.group_meta.get(g.id)})
    return {"n_links": len(bridges), "n_wrong_links": len(wrong),
            "wrong_by_kindpair": dict(Counter(f"{r['kind_a']}|{r['kind_b']}" for r in wrong).most_common(20)),
            "wrong_kit_groups": dict(Counter(r["kit_groups"] for r in wrong)),
            "wrong_kit_min_df": dict(Counter(r["kit_min_df"] for r in wrong).most_common(15)),
            "wrong_gene_kinds": {f"{k[0]}|{k[1]}": [n, round(gene_bits[k], 1)] for k, n in gene_ct.most_common(30)},
            "groups": groups, "wrong_campaign_pairs": wrong_pairs, "wrong_links": wrong,
            "right_sample": [r for r in bridges if not r["wrong"]][:20]}


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=Path, default=R.NASK / "sample")
    ap.add_argument("--labels", type=Path, default=R.NASK / "labels" / "sample_labels.json")
    ap.add_argument("--variants", type=Path, default=R.NASK / "variants")
    ap.add_argument("--shards", type=Path, required=True)
    ap.add_argument("--extract-workers", type=int, default=6)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--diagnose", action="store_true")
    ap.add_argument("--filtered", action="store_true", help="also CORE's candidate filtering at 231")
    ap.add_argument("--only", default=None, help="comma-separated run names (sample, sample+syn, retrieval, ...)")
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    L.load(a.labels)
    primary, cs = corpora(a)
    out: dict = {}
    runs = [("sample", None), ("sample+syn", None), ("retrieval", 10**9)] + (
        [("retrieval-filtered", 60)] if a.filtered else [])
    if a.only:
        runs = [r for r in runs if r[0] in a.only.split(",")]
    for name, apb in runs:
        corpus = cs[name.split("-")[0]]
        ov = {"all_pairs_below": apb} if apb else {}
        with R._Overrides(primary, ov):
            b = primary.build(corpus.subset(sorted(corpus.genes)), corpus.labels)
        b.ctx["kit"] = primary.th.kit
        g = R.grouping_metrics(b, corpus)
        out[name] = {"n": len(corpus.genes), "campaign": g["campaign"]["pairwise"], "group": g["group"]["pairwise"],
                     "campaign_partition_sha256": partition_sha(b.member_of), "group_partition_sha256": group_sha(b),
                     "n_campaigns": len(set(b.member_of.values())), "n_groups": len(b.ctx["clustering"].groups),
                     "labelled_groups": len(set(corpus.groups.values())),
                     "labelled_campaigns": len(set(corpus.labels.values())),
                     "true_group_pairs": sum(1 for x, y in combinations(sorted(corpus.groups), 2)
                                             if corpus.groups[x] == corpus.groups[y])}
        print(name, json.dumps({k: out[name][k] for k in ("campaign", "group", "n_groups")}), flush=True)
        if a.diagnose:
            (a.out / f"bridges-{name}.json").write_text(json.dumps(diagnose(b, corpus), indent=1, default=str))
    (a.out / "metrics.json").write_text(json.dumps(out, indent=1))
    _ = M


if __name__ == "__main__":
    main()
