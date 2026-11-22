"""G1 held-out validation (2026-11-22). Measurement only: no scoring code, default or threshold is changed.

Held-out material = public external corpora already extracted on the server (state-ext-all) that were not used to
set any threshold (thresholds were chosen on NASK-derived synthetic sets) and that carry an origin-like label:
- ext:phishing-alive-7days   label = same phishing URL re-crawled over time (re-capture / duplicate origin);
- ext:purino-scam-ads        label = advertiser page (weak "recognised operator" label);
- ext:phishing-websites-putra-legit label = source site (legitimate pages, hard negatives against fraud);
- ext:phishdecloaker-kits    unlabelled for campaigns (brand only): foreign material, counted only in cross-source
                             false merges.
Two steps, run in this order; the split file is written (and its hash committed) before any metric is computed:

    python -m eval.holdout_g1 freeze --state <state> --out <dir>      # rows, components, campaign_split, hashes
    python -m eval.holdout_g1 run --state <state> --out <dir> [--assign-n 150]

Configs compared on the same split sides: `rule` (shared registered domain or identifier value links, no
weighting), `text` (product scoring with every family but TXT dropped), `image` (only VIS kept), `full` (product).
Only aggregates are printed and written to <dir>/results.json; ids stay in <dir>/split.json on the server.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import random
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from eval import metrics as M

SOURCES = {
    "ext:phishing-alive-7days": 5000,
    "ext:purino-scam-ads": 10_000,
    "ext:phishing-websites-putra-legit": 1000,
    "ext:phishdecloaker-kits": 10_000,
}
UNLABELLED = {"ext:phishdecloaker-kits"}
FAMILIES = ("VIS", "TXT", "INF", "ID", "NAR", "META")
DROP = {"full": frozenset(), "text": frozenset(set(FAMILIES) - {"TXT"}), "image": frozenset(set(FAMILIES) - {"VIS"})}
RULE_KINDS = {"domain", "tracker_id", "platform_ad_id"}  # plus every entity_* identifier kind
# rule v1 (frozen 2026-11-22 07:41 CET) named a gene kind `domain` that the extractors never emit (the stored kinds
# are `registrable_domain` / `landing_host`), so v1 linked only on identifiers. rule2 (added after the first rule
# run, reported separately): registrable domain or landing host or an identifier value (prices excluded).
RULE2_KINDS = {"registrable_domain", "landing_host", "tracker_id", "platform_ad_id"}
RULE2_ENTITY_EXCLUDE = {"entity_price"}
SEED = 20261122


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def rule_keys(genome, version: int = 1) -> set[tuple[str, str]]:
    if version == 2:
        return {
            (g.kind, g.value)
            for g in genome.genes
            if g.value
            and (g.kind in RULE2_KINDS or (g.kind.startswith("entity_") and g.kind not in RULE2_ENTITY_EXCLUDE))
        }
    return {
        (g.kind, g.value) for g in genome.genes if g.value and (g.kind in RULE_KINDS or g.kind.startswith("entity_"))
    }


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> list[float] | None:
    if not n:
        return None
    from scipy.stats import beta

    lo = 0.0 if k == 0 else float(beta.ppf(alpha / 2, k, n - k + 1))
    hi = 1.0 if k == n else float(beta.ppf(1 - alpha / 2, k + 1, n - k))
    return [round(lo, 4), round(hi, 4)]


def zero_error_bound(n: int) -> float | None:
    return round(1 - 0.05 ** (1 / n), 4) if n else None


# --------------------------------------------------------------------------------------- loading


def load(state: Path):
    from hydra.config import Settings
    from hydra.db import connect
    from hydra.pipeline import latest_run, load_genomes

    s = Settings(state_dir=state)
    with closing(connect(s.db_path, readonly=True)) as conn:
        run_id = latest_run(conn)
        # genes are stored per incident whatever run extracted them; every incident with stored genes is usable
        analysed = {r[0] for r in conn.execute("SELECT DISTINCT incident_id FROM genes")}
        rows = [dict(r) for r in conn.execute("SELECT id, source, label FROM incidents")]
        rng = random.Random(SEED)
        chosen = []
        for src, cap in SOURCES.items():
            ids = sorted(r["id"] for r in rows if r["source"] == src and r["id"] in analysed)
            if len(ids) > cap:
                ids = sorted(rng.sample(ids, cap))
            chosen += ids
        meta = {r["id"]: r for r in rows}
        genomes = load_genomes(conn, chosen)
        params = json.loads(conn.execute("SELECT params FROM runs WHERE id = ?", (run_id,)).fetchone()["params"])
    return run_id, params, genomes, {i: meta[i] for i in genomes}


# --------------------------------------------------------------------------------------- freeze


def freeze(a) -> None:
    from eval.run import campaign_split
    from hydra.entropy import duplicate_units

    run_id, params, genomes, meta = load(a.state)
    dup = duplicate_units({i: g.genes for i, g in genomes.items()}) or {}
    truth = {i: m["label"] for i, m in meta.items() if m["source"] not in UNLABELLED and m["label"]}
    size = Counter(truth.values())
    rows = []
    for i, m in meta.items():
        lab = truth.get(i)
        rows.append(
            {
                "id": i,
                "source": m["source"],
                # campaign_split alternates campaign-bearing components (C-* labels) separately
                "label": ("C-" + _h(lab)[:16]) if lab and size[lab] > 1 else (("S-" + _h(lab)[:16]) if lab else None),
                "duplicate_of": dup.get(i) if dup.get(i) != i else None,
                "parent": None,
            }
        )
    calib, test, info = campaign_split(rows)
    side = {r["id"]: "A" for r in calib} | {r["id"]: "B" for r in test}
    a.out.mkdir(parents=True, exist_ok=True)
    label_hash = _h(json.dumps(sorted((i, truth.get(i)) for i in genomes)))
    split_hash = _h(json.dumps(sorted(side.items())))
    (a.out / "split.json").write_text(
        json.dumps({"side": side, "truth": truth, "source": {i: m["source"] for i, m in meta.items()}})
    )
    per = defaultdict(lambda: Counter())
    for i, m in meta.items():
        lab = truth.get(i)
        c = per[f"{m['source']}|{side[i]}"]
        c["n"] += 1
        c["labelled"] += bool(lab)
    comps = defaultdict(lambda: Counter())
    for lab, n in size.items():
        if n > 1:
            src = next(meta[i]["source"] for i in truth if truth[i] == lab)
            s = {side[i] for i in truth if truth[i] == lab}
            comps[src]["multi_member_labels"] += 1
            comps[src]["members_in_multi"] += n
            comps[src]["sides_" + "".join(sorted(s))] += 1
    manifest = {
        "state_run": run_id,
        "run_params_keys": sorted(params),
        "n": len(genomes),
        "duplicate_units_members": len(dup),
        "split_info": info,
        "per_source_side": {k: dict(v) for k, v in sorted(per.items())},
        "components": {k: dict(v) for k, v in comps.items()},
        "label_hash_sha256": label_hash,
        "split_hash_sha256": split_hash,
        "seed": SEED,
        "caps": SOURCES,
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(json.dumps(manifest, indent=1))


# --------------------------------------------------------------------------------------- configs


def cluster_rule(genomes: dict, version: int = 1) -> dict[str, str]:
    parent = {i: i for i in genomes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first: dict = {}
    for i, g in genomes.items():
        for k in rule_keys(g, version):
            if k in first:
                ra, rb = find(i), find(first[k])
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)
            else:
                first[k] = i
    return {i: "R-" + find(i) for i in genomes}


def cluster_hydra(genomes: dict, cfg: str, prior):
    import hydra.pipeline as P
    from hydra.synapse import Thresholds, score_pair

    P.score_pair = score_pair if cfg == "full" else functools.partial(score_pair, drop=DROP[cfg])
    _r, _pairs, clustering, _info = P.score_and_cluster(genomes, Thresholds(), prior=prior)
    P.score_pair = score_pair
    return {i: clustering.membership.get(i, f"solo:{i}") for i in genomes}, clustering


def group_metrics(pred: dict, truth: dict, source: dict) -> dict:
    lab = {i: truth[i] for i in pred if i in truth}
    g = M.grouping({i: pred[i] for i in lab}, lab)
    pw = g["pairwise"]
    by = defaultdict(list)
    for i, c in pred.items():
        by[c].append(i)
    sizes = sorted((len(v) for v in by.values()), reverse=True)
    cross = [v for v in by.values() if len({source[i] for i in v}) > 1]
    cross_pairs = 0
    for v in cross:
        cnt = Counter(source[i] for i in v)
        cross_pairs += (len(v) * (len(v) - 1) - sum(x * (x - 1) for x in cnt.values())) // 2
    return {
        "n_labelled": pw["n"],
        "pair_precision": pw["precision"],
        "pair_recall": pw["recall"],
        "pair_f1": pw["f1"],
        "tp_pairs": pw["tp"],
        "false_merge_pairs": pw["fp"],
        "false_split_pairs": pw["fn"],
        "bcubed_precision": g["bcubed"]["precision"],
        "bcubed_recall": g["bcubed"]["recall"],
        "bcubed_f1": g["bcubed"]["f1"],
        "pred_clusters_labelled": g["pred_clusters"],
        "true_clusters": g["true_clusters"],
        "impure_clusters": g["impure_clusters"],
        "multi_member_clusters": sum(1 for s in sizes if s > 1),
        "largest_cluster": sizes[0] if sizes else 0,
        "cross_source_clusters": len(cross),
        "cross_source_pairs": cross_pairs,
    }


# --------------------------------------------------------------------------------------- assignment


def assign_eval(
    cfg: str, items: list[str], base_ids: list[str], genomes: dict, pred: dict, clustering, truth: dict, prior
) -> dict:
    """Each item is decided against campaigns of `base_ids` (the item itself removed). Known = another member of
    its truth label is in the base; foreign otherwise."""
    from hydra.contracts import Campaign

    by_c = defaultdict(list)
    for i in base_ids:
        by_c[pred[i]].append(i)
    base_set = set(base_ids)
    lab_count = Counter(truth.get(j) for j in base_ids)
    all_camps = [Campaign(id=c, label=c, members=ms) for c, ms in by_c.items()]
    base_genomes = {m: genomes[m] for m in base_ids}
    out = Counter()
    for it in items:
        lab = truth.get(it)
        known = bool(lab) and lab_count[lab] - (it in base_set) > 0
        own = pred.get(it) if it in base_set else None
        camps = {c: (ms if c != own else [m for m in ms if m != it]) for c, ms in by_c.items()}
        camps = {c: ms for c, ms in camps.items() if ms}
        if cfg in ("rule", "rule2"):
            v = 2 if cfg == "rule2" else 1
            keys = rule_keys(genomes[it], v)
            hit = {c for c, ms in camps.items() if any(keys & rule_keys(genomes[m], v) for m in ms)}
            dec, cid = (
                ("assign", next(iter(hit))) if len(hit) == 1 else (("abstain", None) if hit else ("new_campaign", None))
            )
        else:
            import hydra.assign as HA
            from hydra.synapse import score_pair

            HA.score_pair = score_pair if cfg == "full" else functools.partial(score_pair, drop=DROP[cfg])
            cl = [
                c if c.id != own else Campaign(id=c.id, label=c.id, members=camps[c.id])
                for c in all_camps
                if c.id in camps
            ]
            sub = base_genomes if it not in base_set else {k: v for k, v in base_genomes.items() if k != it}
            res = HA.assign(genomes[it], sub, cl, [])
            HA.score_pair = score_pair
            dec, cid = str(res.assignment.decision), res.assignment.campaign_id
        maj = None
        if dec == "assign" and cid in camps:
            maj = Counter(truth.get(m) for m in camps[cid]).most_common(1)[0][0]
        kind = "known" if known else "foreign"
        out[f"{kind}_n"] += 1
        if dec == "assign":
            out[f"{kind}_assign"] += 1
            out[f"{kind}_assign_correct"] += bool(known and maj == lab)
        elif dec == "abstain":
            out[f"{kind}_abstain"] += 1
        else:
            out[f"{kind}_new"] += 1
    o = dict(out)
    n = o.get("known_n", 0) + o.get("foreign_n", 0)
    assigned = o.get("known_assign", 0) + o.get("foreign_assign", 0)
    correct = o.get("known_assign_correct", 0)
    abst = o.get("known_abstain", 0) + o.get("foreign_abstain", 0)
    o.update(
        {
            "n": n,
            "auto_assign": assigned,
            "auto_assign_precision": round(correct / assigned, 4) if assigned else None,
            "auto_assign_precision_wilson95": M.wilson(correct, assigned),
            "auto_assign_precision_cp95": clopper_pearson(correct, assigned),
            "coverage_known": round(o.get("known_assign", 0) / o["known_n"], 4) if o.get("known_n") else None,
            "abstention_share": round(abst / n, 4) if n else None,
            "foreign_false_assign": o.get("foreign_assign", 0),
            "foreign_false_assign_cp95": clopper_pearson(o.get("foreign_assign", 0), o.get("foreign_n", 0)),
            "foreign_zero_error_upper95": zero_error_bound(o.get("foreign_n", 0))
            if not o.get("foreign_assign")
            else None,
        }
    )
    return o


def run(a) -> None:
    from hydra.pipeline import with_shipped_prior

    split = json.loads((a.out / "split.json").read_text())
    side, truth, source = split["side"], split["truth"], split["source"]
    _run_id, _params, genomes, _meta = load(a.state)
    assert set(genomes) == set(side), "loaded set differs from the frozen split"
    prior = with_shipped_prior(None)
    rng = random.Random(SEED)
    res: dict = {"configs": {}, "assign": {}}
    skip = set(a.exclude_source)
    sides = {s: sorted(i for i in side if side[i] == s and source[i] not in skip) for s in ("A", "B")}
    tag = ("-x" + "-".join(x.split(":")[-1] for x in sorted(skip))) if skip else ""
    outfile = a.out / f"results-{'-'.join(a.configs)}-{''.join(a.sides)}{tag}.json"
    for cfg in a.configs:
        preds = {}
        for s, ids in sides.items():
            if s not in a.sides:
                continue
            g = {i: genomes[i] for i in ids}
            t0 = time.time()
            if cfg in ("rule", "rule2"):
                pred, cl = cluster_rule(g, 2 if cfg == "rule2" else 1), None
            else:
                pred, cl = cluster_hydra(g, cfg, prior)
            secs = round(time.time() - t0, 1)
            out = {"all": group_metrics(pred, truth, source), "seconds": secs}
            for src in sorted({source[i] for i in ids}):
                sub = [i for i in ids if source[i] == src]
                out[src] = group_metrics({i: pred[i] for i in sub}, truth, source)
            res["configs"][f"{cfg}|{s}{tag}"] = out
            preds[s] = pred
            outfile.write_text(json.dumps(res, indent=1))
            print(cfg, s, secs, json.dumps(out["all"]), flush=True)
            # LOO assignment within side B on a fixed seeded sample; open set: side-B items against side-A base
            if s == "B" and a.assign_n:
                lab_ids = [i for i in ids if i in truth]
                multi = [i for i in lab_ids if sum(1 for j in lab_ids if truth[j] == truth[i]) > 1]
                single = [i for i in ids if i not in set(multi)]
                r2 = random.Random(SEED)
                items = sorted(r2.sample(multi, min(a.assign_n, len(multi)))) + sorted(
                    r2.sample(single, min(a.assign_n, len(single)))
                )
                t0 = time.time()
                res["assign"][f"{cfg}|loo-B{tag}"] = assign_eval(cfg, items, ids, genomes, pred, cl, truth, prior)
                res["assign"][f"{cfg}|loo-B{tag}"]["seconds"] = round(time.time() - t0, 1)
                outfile.write_text(json.dumps(res, indent=1))
                print(cfg, "assign", json.dumps(res["assign"][f"{cfg}|loo-B{tag}"]), flush=True)
        if a.assign_n and "A" in preds:
            # open set across the split: side-B items against the side-A campaigns (disjoint by construction)
            ida = sides["A"]
            preda = preds["A"]
            items = sorted(rng.sample(sides["B"], min(2 * a.assign_n, len(sides["B"]))))
            t0 = time.time()
            r = assign_eval(cfg, items, ida + [], genomes, {**preda, **{i: "x" for i in items}}, None, truth, prior)
            r["seconds"] = round(time.time() - t0, 1)
            res["assign"][f"{cfg}|openset-B-vs-A"] = r
            print(cfg, "openset", json.dumps(r), flush=True)
        outfile.write_text(json.dumps(res, indent=1))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("step", choices=["freeze", "run"])
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--configs", nargs="+", default=["rule", "full", "text", "image"])
    ap.add_argument("--assign-n", type=int, default=150)
    ap.add_argument("--sides", nargs="+", default=["A", "B"], choices=["A", "B"])
    ap.add_argument("--exclude-source", nargs="*", default=[])
    a = ap.parse_args()
    freeze(a) if a.step == "freeze" else run(a)


if __name__ == "__main__":
    main()
