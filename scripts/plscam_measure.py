#!/usr/bin/env python3
"""Aggregate-only measurement of the PL-SCAM public lexicons on a corpus folder (PL-SCAM stream, 2026-11-21).

    HYDRA_PL_LEXICON=1 uv run python scripts/plscam_measure.py SAMPLE_DIR OUT.json
    HYDRA_PL_LEXICON=0 uv run python scripts/plscam_measure.py SAMPLE_DIR OUT_off.json
    uv run python scripts/plscam_measure.py --diff OUT_off.json OUT.json

Runs the normal pipeline (`hydra.pipeline.analyse`) in a throw-away state dir and writes only aggregates: number of
incidents, campaigns and groups, per-pair hypothesis counts, number of incidents carrying each PL-SCAM gene kind and
the public ids that fired (scheme ids, brand ids, shape ids, TLDs, list names: all public vocabulary). It never
writes incident text, hosts, names or gene values that come from the material itself (task PDF §8).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

PUBLIC_VALUE_KINDS = {"scheme", "pl_domain_brand_lookalike", "pl_domain_shape", "pl_domain_service_word",
                      "pl_domain_tld_risk"}
COUNT_ONLY_KINDS = {"pl_domain_template", "knf_warning_listed", "mf_hazard_listed", "cert_pl_listed_host"}


def measure(sample: Path) -> dict:
    from hydra.config import Settings, get_settings
    from hydra.genome.lexicons import SCHEME_INFO
    from hydra.pipeline import analyse

    base = get_settings()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        s = Settings(root=root, data_dir=base.data_dir, models_dir=base.models_dir, cache_dir=base.cache_dir,
                     state_dir=root / "state")
        s.ensure_dirs()
        res = analyse([sample], s)
    out: dict = {"pl_lexicon": os.environ.get("HYDRA_PL_LEXICON", "1"), "incidents": len(res.incidents),
                 "campaigns": len(res.clustering.campaigns), "groups": len(res.clustering.groups),
                 "campaign_sizes": sorted((len(c.members) for c in res.clustering.campaigns), reverse=True),
                 "pair_hypotheses": dict(Counter(str(p.score.hypothesis) for p in res.pairs.values()))}
    kinds: dict[str, set] = {}
    values: dict[str, Counter] = {}
    public_motifs = Counter()
    new_schemes = set(SCHEME_INFO)
    for iid, g in res.genomes.items():
        for gene in g.genes:
            k = gene.kind
            if k in PUBLIC_VALUE_KINDS or k in COUNT_ONLY_KINDS:
                kinds.setdefault(k, set()).add(iid)
            if k in PUBLIC_VALUE_KINDS and gene.value:
                if k == "scheme" and gene.value not in new_schemes and gene.value not in (
                        "investment", "health_supplement", "gambling", "fake_news_bait", "giveaway", "other"):
                    continue
                values.setdefault(k, Counter())[gene.value] += 1
            note = gene.evidence.note or "" if gene.evidence else ""
            if gene.family == "NAR" and ", public:" in note:
                public_motifs[f"{gene.kind}:{gene.value}"] += 1
    out["incidents_with_kind"] = {k: len(v) for k, v in sorted(kinds.items())}
    out["value_counts"] = {k: dict(v.most_common()) for k, v in sorted(values.items())}
    out["nar_public_term_hits"] = dict(public_motifs.most_common())
    out["membership"] = sorted(sorted(c.members) for c in res.clustering.campaigns)
    return out


def diff(a: dict, b: dict) -> dict:
    return {"campaigns": (a["campaigns"], b["campaigns"]), "groups": (a["groups"], b["groups"]),
            "same_membership": a["membership"] == b["membership"],
            "pair_hypotheses": (a["pair_hypotheses"], b["pair_hypotheses"]),
            "scheme_counts": (a["value_counts"].get("scheme"), b["value_counts"].get("scheme"))}


if __name__ == "__main__":
    if sys.argv[1] == "--diff":
        x, y = (json.loads(Path(p).read_text()) for p in sys.argv[2:4])
        print(json.dumps(diff(x, y), indent=1))
    else:
        r = measure(Path(sys.argv[1]))
        Path(sys.argv[2]).write_text(json.dumps(r, indent=1))
        print(json.dumps({k: v for k, v in r.items() if k != "membership"}, indent=1))
