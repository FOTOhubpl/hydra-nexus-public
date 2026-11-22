"""Evaluation manifest: everything needed to say which code, models, configuration and inputs produced a run.

    python -m eval.manifest --out manifest.json --product-sha <sha> --hydra-root <dir> --results results.json \
        --steps steps.json [--hidden-sim rescore.json]

Contents (no organiser content: incident ids appear only as sha256 of sorted lists):
- product / harness commits, sha256 of models.lock.json (product tree) and uv.lock (harness);
- extractor names and version tags from the product registry;
- reference-data snapshot: sha256 of the label file, synthetic manifests, CERT warning list, and an index hash
  (names + sizes) of the enrichment replay cache;
- split identity: per corpus the number of incidents and sha256 of the sorted id list and of the label map;
- offline / replay mode (HYDRA_OFFLINE, HYDRA_ENRICH, HF_HUB_OFFLINE, ...);
- every command of the run with its real exit code, and the crash count.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home() / "HydraNexus"
SCORING_PATHS = ["hydra/synapse.py", "hydra/entropy.py", "hydra/assign.py", "hydra/conformal.py", "hydra/nexus.py",
                 "hydra/retrieval.py", "hydra/priors", "hydra/genome", "hydra/pipeline.py", "hydra/batch.py"]


def sha256_file(p: Path) -> str | None:
    if not p.exists():
        return None
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def dir_index(root: Path, limit: int = 2_000_000) -> dict:
    """Hash of sorted (relative name, size) of a directory (enrichment cache replay snapshot)."""
    if not root.exists():
        return {"path": str(root).replace(str(Path.home()), "~"), "exists": False}
    rows = []
    for p in root.rglob("*"):
        if p.is_file():
            rows.append(f"{p.relative_to(root)}\t{p.stat().st_size}")
            if len(rows) >= limit:
                break
    rows.sort()
    return {"path": str(root).replace(str(Path.home()), "~"), "files": len(rows),
            "index_sha256": sha256_text("\n".join(rows))}


def git(*args: str, cwd: Path) -> str | None:
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout
    except Exception:  # noqa: BLE001
        return None


def extractor_tags(hydra_root: Path | None) -> dict:
    if hydra_root:
        sys.path.insert(0, str(hydra_root))
    try:
        from hydra.genome import base

        base.load_family_modules()
        import hydra.genome.artifact  # noqa: F401
        return {n: base.REGISTRY[n].tag for n in sorted(base.REGISTRY)}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


REQUIRED_STAGES = ("eval", "hidden_sim", "extra_batch", "extra_summary", "error_report")


def read_steps(path: Path | None) -> list[dict]:
    """steps.jsonl (one JSON object per line, CR-RELEASE-1); a legacy JSON list is accepted too."""
    if not path or not path.exists():
        return []
    text = path.read_text().strip()
    if text.startswith("["):
        return json.loads(text)
    return [json.loads(x) for x in text.splitlines() if x.strip()]


def stage_status(steps: list[dict], crashes) -> dict:
    """PASS only if every required stage ran with exit 0 and the eval recorded no crash. A required stage that
    never ran is `missing`; a hidden_sim result taken from the fast rescore (no full score) is `fallback`."""
    by = {x["name"]: x for x in steps}
    stages = {}
    for name in REQUIRED_STAGES:
        x = by.get(name)
        stages[name] = "missing" if x is None else ("ok" if x.get("exit_code") == 0 else f"FAIL(exit {x['exit_code']})")
    for x in steps:
        if x["name"] not in stages:
            stages[x["name"]] = "ok" if x.get("exit_code") == 0 else f"FAIL(exit {x['exit_code']})"
    bad = [k for k, v in stages.items() if v != "ok"]
    if crashes is None:  # missing evidence is not a pass (ASTRA-DELTA-11)
        bad.append("eval_crash_count_missing")
    elif crashes:
        bad.append(f"eval_crashes={crashes}")
    status = "PASS" if not bad else ("FAIL" if stages.get("eval") != "ok" else "PARTIAL")
    return {"status": status, "stages": stages, "failed_or_missing": bad, "eval_crashes": crashes}


def split_identity(res: dict) -> dict:
    out = {}
    ls = res.get("meta", {}).get("label_status") or {}
    out["labels"] = {"incidents": len(ls), "status_sha256": sha256_text(json.dumps(ls, sort_keys=True))}
    for name in ("sample", "sample+syn"):
        v = res.get("system", {}).get(name, {})
        rows = v.get("loo", {}).get("rows", [])
        ids = sorted(r["incident"] for r in rows)
        labs = sorted(f"{r['incident']}={r.get('true_label')}" for r in rows)
        out[name] = {"incidents_in_loo": len(ids), "ids_sha256": sha256_text("\n".join(ids)),
                     "label_map_sha256": sha256_text("\n".join(labs))}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--product-sha", required=True)
    ap.add_argument("--product-repo", type=Path, default=HOME / "hydra-nexus")
    ap.add_argument("--hydra-root", type=Path, default=None)
    ap.add_argument("--results", type=Path, default=None)
    ap.add_argument("--steps", type=Path, default=None, help="JSON list of {name, command, exit_code, seconds}")
    ap.add_argument("--hidden-sim", type=Path, default=None)
    ap.add_argument("--scoring-frozen", default=None, help="scoring-frozen SHA to check against (default product)")
    a = ap.parse_args()
    repo = Path(__file__).resolve().parents[1]
    res = json.loads(a.results.read_text()) if a.results and a.results.exists() else {}
    models_lock = git("show", f"{a.product_sha}:models.lock.json", cwd=a.product_repo)
    nask = HOME / "data" / "nask"
    man = {
        "product_sha": a.product_sha,
        "harness_sha": (git("rev-parse", "HEAD", cwd=repo) or "").strip() or None,
        "harness_dirty": bool((git("status", "--porcelain", "eval", cwd=repo) or "").strip()),
        "models_lock_sha256": sha256_text(models_lock) if models_lock else None,
        "uv_lock_sha256": sha256_file(repo / "uv.lock"),
        "extractors": extractor_tags(a.hydra_root),
        "reference_data": {
            "labels_sha256": sha256_file(nask / "labels" / "sample_labels.json"),
            "variants_s1_manifest_sha256": sha256_file(nask / "variants" / "set-s1" / "manifest.json"),
            "variants_s2_manifest_sha256": sha256_file(nask / "variants" / "set-s2" / "manifest.json"),
            "robustness_variants": "regenerated by eval/run.py (seed 7) on every run",
            "cert_warning_list_sha256": sha256_file(HOME / "data" / "external" / "cert" / "domains.json"),
            "enrich_cache": dir_index(Path(os.environ.get("HYDRA_ENRICH_CACHE")
                                           or HOME / "cache" / "linux-x86_64" / "enrich")),
        },
        "split": split_identity(res),
        "mode": {k: os.environ.get(k) for k in ("HYDRA_OFFLINE", "HYDRA_ENRICH", "HF_HUB_OFFLINE",
                                                 "TRANSFORMERS_OFFLINE", "HYDRA_LLM", "HYDRA_EXTRACT_WORKERS",
                                                 "HYDRA_TORCH_THREADS", "HYDRA_OCR_WORKERS", "OMP_NUM_THREADS")},
        "run_id": res.get("meta", {}).get("run_id"),
        "crashes": res.get("crashes", {}).get("count"),
        "harness_identity": (res.get("headline") or {}).get("harness"),
        "corpus_fingerprint": res.get("meta", {}).get("corpus_fingerprint"),
        "steps": read_steps(a.steps),
        "hidden_sim_sha256": sha256_file(a.hidden_sim) if a.hidden_sim else None,
    }
    # release identity of what the jury gets (CR-RELEASE-1)
    ui_tree = (git("rev-parse", f"{a.product_sha}:ui", cwd=a.product_repo) or "").strip() or None
    man["ui_tree_sha"] = ui_tree
    try:
        img = subprocess.run(["docker", "image", "ls", "--no-trunc", "--format", "{{.Repository}}:{{.Tag}} {{.ID}}"],
                             capture_output=True, text=True, timeout=20).stdout.split("\n")
        man["docker_images"] = [x for x in img if "hydra" in x.lower()][:5] or "no hydra image built on this host"
    except Exception:  # noqa: BLE001
        man["docker_images"] = "docker not available on this host"
    frozen = a.scoring_frozen or a.product_sha
    man["scoring_paths_unchanged_since"] = {
        "frozen_sha": frozen, "paths": SCORING_PATHS,
        "check": f"git diff --quiet {frozen} <final-sha> -- " + " ".join(SCORING_PATHS),
        "product_vs_frozen_unchanged": subprocess.run(["git", "diff", "--quiet", frozen, a.product_sha, "--",
                                                       *SCORING_PATHS], cwd=a.product_repo).returncode == 0}
    adjudicated = sorted(str(p.name) for p in (HOME / "data" / "nask" / "labels").glob("*adjudicated*"))
    man["labels_source"] = (f"adjudicated: {adjudicated}" if adjudicated else
                            "team hypothesis: no independently adjudicated labels existed at run time "
                            "(kit: docs/LABELING_GUIDE.md)")
    st = stage_status(man["steps"], man["crashes"])
    if a.hidden_sim and a.hidden_sim.name != "full_score.json":
        st["stages"]["hidden_sim_source"] = "fallback: fast rescore (full score missing)"
        st["failed_or_missing"].append("hidden_sim_full")
        if st["status"] == "PASS":
            st["status"] = "PARTIAL"
    sp = man["scoring_paths_unchanged_since"]
    if not sp["product_vs_frozen_unchanged"]:  # a scoring change since the frozen SHA lowers the release status
        st["failed_or_missing"].append(f"scoring_paths_changed_since_{sp['frozen_sha'][:8]}")
        if st["status"] == "PASS":
            st["status"] = "PARTIAL"
    man.update(st)
    text = json.dumps(man, indent=1, ensure_ascii=False).replace(str(Path.home()) + "/", "~/")
    a.out.write_text(text)
    print(json.dumps({k: man[k] for k in ("product_sha", "harness_sha", "run_id", "crashes", "status",
                                           "failed_or_missing")}))
    return 0 if man["status"] == "PASS" else (2 if man["status"] == "PARTIAL" else 1)


if __name__ == "__main__":
    raise SystemExit(main())
