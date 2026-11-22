"""Explicit training step for the LR + isotonic fusion (`hydra train --corpus ext:...`), never inside analyse.

Labels (DATA-HUNT `meta.ext.label_kind`): brand / target / site / domain labels group independent scams that target
the same victim, so they are never campaign labels (refused; they serve as group / hard-negative structure).
Advertiser-page labels (purino-scam-ads) are WEAK campaign labels: allowed by default, recorded as
`weak_labels: true` in the provenance, refused with `allow_weak_labels=False`. Isotonic calibration is fitted on
out-of-fold predictions.

Data: incidents of the chosen corpora that carry a grouping label (`incidents.label`, or `group_label` with
`--label group_label`): kit / advertiser page in external datasets (DATA-HUNT adapters set them). By default
only external corpora (`ext:*`, `public:*`) are allowed; organiser incidents need `allow_nask=True` and are then
reported as such (the NASK sample is the dev truth for EVAL, so training on it leaks).

Split: by label (group-level), `test_frac` of the labels are held out, so no group is on both sides. Pairs are the
retrieval candidates within each side (hard negatives, as at inference). Metrics on the held-out pairs: Brier, log
loss, ECE (10 equal-width bins), ROC AUC, for the fused probability and for the uncalibrated rarity-bits logistic.

Output: `<state>/models/fusion/fusion-<ts>.pkl` + `.json` provenance (corpus, label field, split seed, counts,
metrics, extractor tags, sha256 of the pickle, git commit). `load_fusion` refuses a pickle whose sha256 differs from
its provenance file (only files written by this function are loaded).
"""

from __future__ import annotations

import hashlib
import json
import pickle
import random
import re
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import numpy as np

from hydra.config import Settings
from hydra.db import connect
from hydra.synapse import FusionModel, Thresholds

TARGET_LABEL_RE = re.compile(r"brand|target|site|domain", re.I)  # victim / impersonated target: never campaigns
WEAK_LABEL_RE = re.compile(r"page", re.I)  # advertiser page id (purino-scam-ads): weak campaign label


def _git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                              cwd=Path(__file__).resolve().parent).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def calibration_metrics(p: np.ndarray, y: np.ndarray, bins: int = 10) -> dict[str, Any]:
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score

    p = np.clip(p.astype(float), 1e-6, 1 - 1e-6)
    out: dict[str, Any] = {"n": int(len(y)), "positives": int(y.sum()),
                           "brier": round(float(brier_score_loss(y, p)), 4),
                           "log_loss": round(float(log_loss(y, p, labels=[0, 1])), 4)}
    out["roc_auc"] = round(float(roc_auc_score(y, p)), 4) if 0 < y.sum() < len(y) else None
    edges = np.linspace(0, 1, bins + 1)
    ece, table = 0.0, []
    for lo, hi in zip(edges[:-1], edges[1:], strict=True):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if m.any():
            ece += m.mean() * abs(p[m].mean() - y[m].mean())
            table.append({"bin": [round(lo, 2), round(hi, 2)], "n": int(m.sum()),
                          "mean_p": round(float(p[m].mean()), 4), "frac_pos": round(float(y[m].mean()), 4)})
    out["ece"] = round(float(ece), 4)
    out["reliability"] = table
    return out


def train_fusion(settings: Settings, corpus: str, *, label_field: str = "label", test_frac: float = 0.3,
                 seed: int = 0, min_pairs: int = 30, allow_nask: bool = False, out_dir: Path | None = None,
                 allow_weak_labels: bool = True,
                 thresholds: Thresholds | None = None) -> dict[str, Any]:
    from hydra.corpus import corpus_of, incidents_in, is_external
    from hydra.pipeline import load_genomes, rarity_for, score_corpus

    if label_field not in ("label", "group_label"):
        raise ValueError("label_field must be 'label' or 'group_label'")
    th = thresholds or Thresholds()
    with closing(connect(settings.db_path, readonly=True)) as conn:
        ids = incidents_in(conn, corpus)
        rows = {}
        for r in conn.execute("SELECT id, source, label, meta FROM incidents"):
            meta = json.loads(r["meta"] or "{}")
            # no group_label column in the schema: adapters put it in incidents.meta (contracts Incident field)
            rows[r["id"]] = {"source": r["source"], "label": r["label"],
                             "label_kind": meta.get("label_kind") or (meta.get("ext") or {}).get("label_kind"),
                             "group_label": meta.get("group_label") or (meta.get("labels") or {}).get("group")}
        sources = {i: corpus_of(rows[i]["source"]) for i in ids}
        nask = sorted(i for i in ids if not is_external(sources[i]))
        if nask and not allow_nask:
            raise ValueError(f"corpus {corpus!r} includes {len(nask)} organiser/upload incidents; training uses"
                             " external corpora only (pass allow_nask=True / --allow-nask to override)")
        labels = {i: rows[i][label_field] for i in ids if rows[i][label_field]}
        kinds = sorted({str(rows[i].get("label_kind") or "") for i in labels})
        target_kinds = [k for k in kinds if TARGET_LABEL_RE.search(k)]
        if target_kinds:
            raise ValueError(f"label kinds {target_kinds}: brand / target / site / domain labels group independent"
                             " scams aimed at the same victim. They are group-level / hard-negative structure, never"
                             " campaign labels (audit §7); train on advertiser-page or kit labels instead.")
        weak = [k for k in kinds if WEAK_LABEL_RE.search(k)]
        if weak and not allow_weak_labels:
            raise ValueError(f"label kinds {weak} are weak campaign labels (advertiser page): pass"
                             " allow_weak_labels=True to use them; the provenance records weak_labels=true")
        genomes = load_genomes(conn, sorted(labels))
    groups = sorted(set(labels.values()))
    if len(groups) < 4:
        raise ValueError(f"need >= 4 labelled groups for a group-level split, found {len(groups)}")
    rng = random.Random(seed)
    rng.shuffle(groups)
    n_test = max(1, round(test_frac * len(groups)))
    test_groups = set(groups[:n_test])
    train = {i: g for i, g in genomes.items() if labels[i] not in test_groups}
    test = {i: g for i, g in genomes.items() if labels[i] in test_groups}

    from hydra.pipeline import fit_fusion

    t0 = time.perf_counter()
    fm = fit_fusion(train, labels, th, min_pairs=min_pairs)
    if fm is None:
        raise ValueError("not enough labelled training pairs (need >= min_pairs with >= 5 of each class)")
    fit_s = time.perf_counter() - t0
    # held-out evaluation
    pairs, _ = score_corpus(test, rarity_for(test), th)
    lab = [(pe, int(labels[a] == labels[b])) for (a, b), pe in pairs.items()]
    y = np.array([v for _, v in lab])
    raw = np.array([pe.score.probability for pe, _ in lab])
    fused = np.array([fm.predict(pe) for pe, _ in lab])
    held = {"fused": calibration_metrics(fused, y), "rarity_bits_logistic": calibration_metrics(raw, y)} \
        if len(y) and 0 < y.sum() < len(y) else {"note": "held-out pairs lack both classes; no metrics"}

    out_dir = out_dir or fusion_store(settings)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"fusion-{time.strftime('%Y%m%dT%H%M%S')}"
    pkl = out_dir / f"{stem}.pkl"
    pkl.write_bytes(pickle.dumps(fm))
    from hydra.genome import base

    base.load_family_modules()  # the registry is empty in `hydra train` unless the extractors were imported
    prov = {
        "model": fm.name, "file": pkl.name, "sha256": hashlib.sha256(pkl.read_bytes()).hexdigest(),
        "corpus": corpus, "corpora": sorted(set(sources.values())), "label_field": label_field,
        "includes_organiser_data": bool(nask), "label_kinds": kinds, "weak_labels": bool(weak),
        "split": {"by": "label", "test_frac": test_frac, "seed": seed},
        "groups": {"train": len(groups) - n_test, "test": n_test},
        "incidents": {"train": len(train), "test": len(test)},
        "train_pairs": fm.n_pairs, "held_out_pairs": int(len(y)), "fit_seconds": round(fit_s, 3),
        "held_out": held, "extractors": sorted(base.REGISTRY[n].tag for n in base.REGISTRY),
        "thresholds": th.__dict__, "git_commit": _git_commit(), "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "note": "Calibration measured on held-out label groups of the training corpora only; it does not transfer"
                " to the NASK corpus without verification by EVAL (calibrated=True only for runs using this model).",
    }
    (out_dir / f"{stem}.json").write_text(json.dumps(prov, indent=2, ensure_ascii=False), encoding="utf-8")
    prov["path"] = str(pkl)
    return prov


def fusion_store(settings: Settings) -> Path:
    return (settings.state_dir or Path(".")) / "models" / "fusion"


def load_fusion(path: Path, settings: Settings | None = None) -> FusionModel:
    """Load a model written by `train_fusion`. Pickles are executable, so only files inside OUR store directory
    (`<state>/models/fusion`) are accepted (audit §7.4), and the sha256 must match the provenance file."""
    from hydra.config import get_settings

    path = Path(path).expanduser().resolve()
    store = fusion_store(settings or get_settings()).resolve()
    if store not in path.parents:
        raise ValueError(f"fusion models load only from {store} (got {path})")
    prov_p = path.with_suffix(".json")
    if not prov_p.is_file():
        raise ValueError(f"no provenance file next to {path}")
    prov = json.loads(prov_p.read_text(encoding="utf-8"))
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != prov.get("sha256"):
        raise ValueError(f"{path} does not match its provenance sha256; refusing to load")
    fm = pickle.loads(data)  # noqa: S301 — only our own files, verified by sha256 above
    if not isinstance(fm, FusionModel):
        raise ValueError("not a FusionModel")
    fm.name = f"{fm.name}:{path.stem}"
    return fm
