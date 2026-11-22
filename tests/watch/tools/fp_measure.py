"""Fingerprint-alert measurement (run on the server; prints JSON).

1. Recall by injection: hold out one member of each campaign with >= 2 members that have a landing domain; build
   fingerprints without the held-out incidents; inject the held-out landing hosts as watch events, (a) name only,
   (b) name + infrastructure simulated from the held-out incident's own stored RDAP/DNS genes.
2. Feed alerts: name-only alerts of the full fingerprint set on recorded CERT events (per hour, by level), and the
   top-N alerts by campaign bits for manual precision review.

Usage: python tests/watch/tools/fp_measure.py <recordings dir> <feed file> [max_holdout]
"""

from __future__ import annotations

import json
import random
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from hydra.config import get_settings
from hydra.watch import fingerprints as F
from hydra.watch.engine import read_recording
from hydra.watch.families import FamilyIndex
from hydra.watch.link import Linker
from hydra.watch.score import score_host


def simulated_enrichment(genome) -> dict:
    ns, reg = [], {}
    for g in genome.genes:
        if g.value is None:
            continue
        if g.kind == "ns_host":
            ns.append(str(g.value))
        elif g.kind == "registrar_day":
            r, _, d = str(g.value).partition("|")
            reg = {"registrar": r, "registration": d}
    return {"ns": sorted(set(ns)), "rdap": reg}


def landing(genome) -> str | None:
    for kind in ("landing_host", "registrable_domain"):
        for g in genome.genes:
            if g.kind == kind and g.value:
                return str(g.value)
    return None


def main(rec_dir: str, feed: str, max_holdout: int = 400) -> None:
    s = get_settings()
    idx = Linker(s, __import__("os").environ.get("HYDRA_WATCH_CORPUS", "nask")).reload()
    fam = FamilyIndex()
    feed_events = []
    for p in sorted(Path(rec_dir).glob("certpl*.jsonl")):
        for e in read_recording(p):
            sc = score_host(e.host)
            from hydra.enrich.domains import url_label_skeleton

            fam.add(e.first_seen, sc.family, sc.registrable, url_label_skeleton(sc.host))
            if p.name == feed:
                feed_events.append(e)
    rng = random.Random(7)
    multi = [c for c in idx.campaigns if sum(1 for m in c.members if m in idx.genomes and landing(idx.genomes[m])) >= 2]
    rng.shuffle(multi)
    held = {}
    for c in multi[:max_holdout]:
        ms = sorted(m for m in c.members if m in idx.genomes and landing(idx.genomes[m]))
        held[ms[-1]] = c.id
    fs_ho = F.build(idx, set(held))
    # pattern-only variant: drop exact-domain clauses (a held-out host on a known registrable domain is trivial)
    fs_pat = F.build(idx, set(held))
    fs_pat.index = {k: v for k, v in fs_pat.index.items() if k[0] != "domain"}
    res = {"corpus": idx.corpus, "campaigns": len(idx.campaigns), "multi_member_campaigns": len(multi),
           "held_out": len(held), "fingerprints_holdout": fs_ho.summary()}
    for mode, fsx in (("name_only", fs_ho), ("name_plus_infra", fs_ho), ("pattern_name_only", fs_pat),
                      ("pattern_plus_infra", fs_pat)):
        hit_top1 = hit_any = alerted = wrong_top1 = 0
        level = Counter()
        clauses = Counter()
        for iid, cid in held.items():
            g = idx.genomes[iid]
            enr = simulated_enrichment(g) if mode.endswith("infra") else None
            al = F.match(fsx, landing(g), enr, fam)
            if not al:
                continue
            alerted += 1
            level[al[0]["level"]] += 1
            if al[0]["campaign_id"] == cid:
                hit_top1 += 1
                for m in al[0]["matched"]:
                    clauses[m["kind"]] += 1
            else:
                wrong_top1 += 1
            hit_any += any(a["campaign_id"] == cid for a in al)
        n = len(held) or 1
        res[mode] = {"alerted": alerted, "recall_top1": round(hit_top1 / n, 3), "recall_top3": round(hit_any / n, 3),
                     "top1_precision_on_alerted": round(hit_top1 / alerted, 3) if alerted else None,
                     "wrong_top1": wrong_top1, "levels": dict(level), "clauses_in_correct_top1": dict(clauses)}
    fs = F.build(idx)
    rows = []
    for e in feed_events:
        al = F.match(fs, e.host, None, fam)
        if al:
            rows.append((e, al[0]))
    hours = 1.0
    if feed_events:
        ts = sorted(e.first_seen for e in feed_events)
        hours = max(1e-6, (datetime.fromisoformat(ts[-1]) - datetime.fromisoformat(ts[0])).total_seconds() / 3600)
    only_domain = sum(1 for _e, a in rows if {m["kind"] for m in a["matched"]} <= {"domain"})
    res["feed"] = {"file": feed, "events": len(feed_events), "hours": round(hours, 2),
                   "alerts": len(rows), "alerts_per_h": round(len(rows) / hours, 2),
                   "levels": dict(Counter(a["level"] for _e, a in rows)),
                   "confidence": dict(Counter(a["confidence"] for _e, a in rows)),
                   "domain_reuse_only": only_domain,
                   "top": [{"host": e.host.replace(".", "[.]"), "campaign": a["campaign_id"],
                            "label": (a["campaign_label"] or "")[:60], "bits": a["bits"], "level": a["level"],
                            "matched": [f"{m['kind']}={m['value']}" for m in a["matched"]]}
                           for e, a in sorted(rows, key=lambda t: -t[1]["campaign_bits"])[:30]]}
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 400)
