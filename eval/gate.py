"""Merge gate: evaluate the branch and the main baseline with the SAME harness and split, then compare.

    uv run python -m eval.gate                      # baseline = `hydra/` of local main (git archive), same harness
    uv run python -m eval.gate --baseline x.json    # compare with a stored results.json

Fails (exit 1) when, on any corpus, leave-one-out assignment accuracy drops, open-set rejection drops,
false-merge pairs rise, or the branch run has more crashes than zero (any crash fails). Results that are not
comparable fail too (CR-ASTRA-PIPE-202): missing corpora/metrics, NaN/inf, another scorer family, another harness
identity (`headline.harness`: quick/full mode and labels hash). A stored baseline without `harness` therefore fails
explicitly; re-save it with `--save-baseline` from a run of the same harness.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EPS = 1e-9


GATED = (("loo_accuracy", "drop"), ("open_set_rejection", "drop"), ("false_merge_pairs", "rise"))


def _finite(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def backend_family(name) -> str | None:
    """`hydra-core(link=10.0,kit=6.0)` → `hydra-core`: thresholds may change on a branch, the scorer may not."""
    return None if name is None else str(name).split("(")[0].strip()


def compare(base: dict, branch: dict) -> list[str]:
    """Gate failures of `branch` against `base` (both `eval.run` headlines). Not comparable = failure
    (CR-ASTRA-PIPE-202): a corpus or gated metric of the baseline missing on the branch, a non-finite value on
    either side, a different scorer family, or a different harness identity (quick/full, labels)."""
    fails = []
    crashes = branch.get("crashes")
    if crashes is None and "crashes" in base:
        fails.append("crashes: missing on branch (not comparable)")
    elif crashes is not None and not _finite(crashes):
        fails.append(f"crashes: {crashes!r} is not a finite count")
    elif (crashes or 0) > 0:
        fails.append(f"crashes: {crashes} (must be 0)")
    fb, fn = backend_family(base.get("backend")), backend_family(branch.get("backend"))
    if fb != fn:
        fails.append(f"backend: {base.get('backend')!r} -> {branch.get('backend')!r} (other scorer, not comparable)")
    for side, h in (("baseline", base.get("harness")), ("branch", branch.get("harness"))):
        if isinstance(h, dict) and "corpus_sha256" in h and not h.get("corpus_sha256"):
            fails.append(f"harness: {side} corpus fingerprint unavailable (failed or not computed; not comparable)")
    if base.get("harness") != branch.get("harness"):
        fails.append(f"harness: {base.get('harness')!r} -> {branch.get('harness')!r} (different split/labels/mode,"
                     " not comparable; re-run both with the same harness or re-save the baseline)")
    for corpus, b in base.items():
        if not isinstance(b, dict) or corpus == "harness":
            continue
        n = branch.get(corpus)
        if not isinstance(n, dict):
            fails.append(f"{corpus}: not evaluated on branch (not comparable)")
            continue
        for key, worse in GATED:
            if key not in b:
                continue
            x, y = b.get(key), n.get(key)
            if not _finite(x):
                fails.append(f"{corpus}.{key}: baseline value {x!r} is not finite (not comparable)")
            elif y is None:
                fails.append(f"{corpus}.{key}: missing on branch (not comparable)")
            elif not _finite(y):
                fails.append(f"{corpus}.{key}: branch value {y!r} is not finite")
            elif (worse == "drop" and y < x - EPS) or (worse == "rise" and y > x + EPS):
                fails.append(f"{corpus}.{key}: {x} -> {y} ({worse})")
    return fails


def _run(out_json: Path, hydra_root: Path | None, backend: str, extra: list[str] | None = None) -> dict:
    """One harness run in its own eval state and shard root (never shared between base and branch: CORE's gene
    cache is keyed by extractor tags, which may not change when code does)."""
    work = out_json.parent
    cmd = [sys.executable, "-W", "ignore", "-m", "eval.run", "--quick", "--no-db", "--no-report", "--no-sweep",
           "--backend", backend, "--json-out", str(out_json), "--out", str(work), "--state-dir", str(work / "state"),
           "--tmp-root", str(work / "shards")] + (extra or [])
    if hydra_root:
        cmd += ["--hydra-root", str(hydra_root)]
    subprocess.run(cmd, cwd=REPO, check=True)
    return json.loads(out_json.read_text())["headline"]


def _export(ref: str, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(["git", "archive", ref, "hydra"], cwd=REPO, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", type=Path, help="stored results.json of the baseline run")
    ap.add_argument("--base-ref", default="main")
    ap.add_argument("--backend", default="auto")
    ap.add_argument("--save-baseline", action="store_true", help="store the branch headline as eval/baseline_main.json")
    ap.add_argument("--branch-ref", default=None, help="evaluate `hydra/` of this ref instead of the checkout")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--extract-workers", type=int, default=1)
    a = ap.parse_args()
    extra = ["--workers", str(a.workers), "--extract-workers", str(a.extract_workers)]
    tmp = Path(tempfile.mkdtemp(prefix="hydra-eval-gate-"))
    if a.baseline:
        base = json.loads(a.baseline.read_text())["headline"]
    else:
        broot = tmp / "base"
        (tmp / "base-out").mkdir()
        base = None
        try:
            _export(a.base_ref, broot)
            base = _run(tmp / "base-out" / "results.json", broot, a.backend, extra)
        except subprocess.CalledProcessError as exc:
            print(f"baseline run on {a.base_ref} unavailable ({' '.join(map(str, exc.cmd[:3]))} failed)")
        if base is None:
            stored = REPO / "eval" / "baseline_main.json"
            if stored.exists():
                print(f"using stored baseline {stored}")
                base = json.loads(stored.read_text())["headline"]
            elif not a.save_baseline:
                print("GATE FAIL: no baseline (run once with --save-baseline to bootstrap)")
                return 2
    (tmp / "branch-out").mkdir()
    branch_root = _export(a.branch_ref, tmp / "branch") if a.branch_ref else None
    branch = _run(tmp / "branch-out" / "results.json", branch_root, a.backend, extra)
    if base is None:  # bootstrap: the first stored baseline is this run
        print("bootstrapping baseline from this run")
        base = branch
    print(f"{'metric':45} {'baseline':>10} {'branch':>10}")
    for corpus, b in base.items():
        if isinstance(b, dict):
            for k, v in b.items():
                print(f"{corpus + '.' + k:45} {v!s:>10} {branch.get(corpus, {}).get(k)!s:>10}")
    print(f"{'backend':45} {base.get('backend')!s:>10} {branch.get('backend')!s:>10}")
    fails = compare(base, branch)
    if a.save_baseline:
        (REPO / "eval" / "baseline_main.json").write_text(json.dumps({"headline": branch}, indent=1))
    for f in fails:
        print("GATE FAIL:", f)
    print("GATE", "FAIL" if fails else "PASS", f"(outputs in {tmp})")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
