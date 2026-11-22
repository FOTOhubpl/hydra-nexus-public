#!/usr/bin/env python3
"""F01: extraction accuracy on SYNTHETIC artefacts with known embedded facts -> docs/release/EXTRACTION_ACCURACY.md.

    uv run python scripts/measure_extraction.py                      # defaults below, real OCR (rapidocr) if present
    uv run python scripts/measure_extraction.py --docs 20 --ocr-docs 0 --jobs 2   # quick, no OCR
    uv run python scripts/measure_extraction.py --out-md /tmp/x.md --out-json /tmp/x.json

Generator, product run and scoring live in tests/extract_accuracy/harness.py (also exercised by `just check`).
State, cache and the corpus go to a temporary directory; nothing touches ~/HydraNexus/state. Offline; the
organiser-derived lexicon overlay is off (HYDRA_LEXICON_DIR=none), so only shipped public lexicons are used.
Models: OCR uses $HYDRA_MODELS_DIR (rapidocr-v6) as the product does; without it the OCR carriers report
`unavailable` and the report says so.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
os.environ["HYDRA_LEXICON_DIR"] = "none"
os.environ.setdefault("HYDRA_OFFLINE", "1")
os.environ.setdefault("HYDRA_BACKGROUND_PRIOR", "0")
os.environ.setdefault("HYDRA_ENRICH", "0")

from extract_accuracy import harness as H  # noqa: E402

# ------------------------------------------------------------------------------------------- known defects
# Each CR carries a minimal repro run live at report time, so the report states whether it still reproduces.
CRS: list[dict[str, str]] = [
    {
        "id": "CR-F01-1",
        "title": "A foreign phone number also yields a spurious Polish number from its last nine digits",
        "where": "hydra/genome/txt_entities.py find_phones (txt_entities); hydra/genome/dom.py PHONE_RX (inf_dom)",
        "repro": "from hydra.genome.txt_entities import find_phones\n"
                 "print([e.value for e in find_phones('Konsultant zagraniczny +420 601 126 480')])",
        "expect": "[] (the number is Czech, +420601126480, already found by find_intl_phones)",
        "bad": "+48601126480",
        "impact": "A false operator-tier phone gene for numbers whose national part is written 3-3-3 (seen for "
                  "+420 and +380 '+380 50 123 4567' -> +48501234567; not for +44/+49/+31/+370 in this run); two "
                  "incidents quoting different foreign numbers with the same tail would share a phone that neither "
                  "material contains. inf_dom (PHONE_RX, hashed) has the same defect.",
    },
    {
        "id": "CR-F01-2",
        "title": "A meta-refresh hop in a standard HAR body does not extend redirect_chain",
        "where": "hydra/genome/webparse.py _parse_har (only `_redirect_kind` annotated responses become hops); "
                 "hydra/genome/har.py inf_har",
        "repro": "see tests/extract_accuracy/harness.py har_case (variant meta_refresh): entry 2 is a 200 HTML page "
                 "with <meta http-equiv=refresh content='0;url=https://land/'>; inf_har reports "
                 "redirect_chain 'click>mid' and a separate js_redirect gene, not 'click>mid>land'",
        "expect": "redirect_chain click>mid>land (the module docstring: 'redirect chain (3xx + JS/meta found in "
                  "first-party bodies)')",
        "bad": "",
        "impact": "Chains through an HTML/JS TDS hop are truncated before the landing host in Chrome/Firefox HARs; "
                  "the landing host is still seen as host_contacted.",
    },
    {
        "id": "CR-F01-3",
        "title": "Genitive (and dative) forms of -ski/-cki surnames are not matched by the person gazetteer",
        "where": "hydra/genome/txt_entities.py find_persons (a token may add at most 3 characters to the stem; "
                 "'stanowsk' + 'iego' adds 4)",
        "repro": "from hydra.genome.txt_entities import find_persons\n"
                 "print([e.value for e in find_persons('Wywiad z udziałem: Krzysztofa Stanowskiego')] or 'MISSED')",
        "expect": "['Krzysztof Stanowski'] (the nominative 'Krzysztof Stanowski' is found)",
        "bad": "MISSED",
        "impact": "Scam copy such as 'wywiad z Roberta Lewandowskiego' / 'według Rafała Trzaskowskiego' loses the "
                  "impersonated-person gene; recall for person in section 1 is the nominative/genitive mix.",
    },
    {
        "id": "CR-F01-4",
        "title": "IBAN finder glues the capital letter of a following Polish word onto the account number",
        "where": "hydra/genome/operator_ids.py _IBAN_RX (ASCII-only negative lookahead after the optional 1-4 "
                 "character group); hits inf_dom, which scans the page text joined with spaces",
        "repro": "from hydra.genome.operator_ids import find_accounts\n"
                 "print([(e.value, e.quality) for e in find_accounts('IBAN ES91 2100 0418 4502 0005 1332 "
                 "Złośliwy adres')])",
        "expect": "[('ES9121000418450200051332', 1.0), ...] (valid IBAN, bank code)",
        "bad": "1332Z",
        "impact": "A valid account becomes an invalid one (quality 0.4, different hash) when the next word starts "
                  "with Z/Ł/Ś/Ż + diacritic (Złoty, Łódź, Źródło ...); the TXT and INF genes of one account then "
                  "no longer share a value.",
    },
    {
        "id": "CR-F01-5",
        "title": "Four-letter brands written in title case (Onet, Lidl) are never matched",
        "where": "hydra/genome/txt_entities.py find_brands (short-token guard: len <= 4 and not upper-case is "
                 "skipped; its own comment names 'Onet' as a brand that must match)",
        "repro": "from hydra.genome.txt_entities import find_brands\n"
                 "print([e.value for e in find_brands('Jak informuje Onet, program')] or 'MISSED', "
                 "[e.value for e in find_brands('Jak informuje LIDL')])",
        "expect": "['onet'] (upper-case 'LIDL' is matched: ['lidl'])",
        "bad": "MISSED",
        "impact": "brand_mention (txt_lines, boilerplate tier: no linking bits) is lost for the usual spelling of "
                  "Onet and Lidl; affects the evidence display, not the score.",
    },
    {
        "id": "CR-F01-6",
        "title": "A bare 't.me/<handle>' link (no scheme) gives no domain_in_text / url_in_text",
        "where": "hydra/genome/txt_entities.py find_hosts (a one-letter second-level label needs a scheme)",
        "repro": "from hydra.genome.txt_entities import find_hosts\n"
                 "print([e.value for e in find_hosts('Napisz: t.me/abc_defgh')] or 'MISSED')",
        "expect": "['t.me', 'https://t.me/abc_defgh']",
        "bad": "MISSED",
        "impact": "Low: the Telegram handle itself is still found (messenger_handle); only the URL gene is missing.",
    },
]


def run_repro(cr: dict[str, str]) -> str:
    if not cr["repro"].startswith("from "):
        return "see the harness (measured in the tables below)"
    try:
        out = subprocess.run([sys.executable, "-c", cr["repro"]], capture_output=True, text=True, timeout=60,
                             cwd=REPO, env={**os.environ}).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"repro failed to run: {exc}"
    return f"{out}  -> {'REPRODUCES' if cr['bad'] and cr['bad'] in out else 'does not reproduce'}"


# ------------------------------------------------------------------------------------------- worker


def _settings(root: Path, models_dir: Path):
    from hydra.config import Settings

    s = Settings(root=root, data_dir=root / "data", models_dir=models_dir, cache_dir=root / "cache",
                 state_dir=root / "state", max_file_mb=50)
    s.ensure_dirs()
    return s


def _work(args: tuple[list, str, str]) -> list[dict]:
    cases, root, models_dir = args
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    from hydra.genome.ocr_pool import OcrPool

    s = _settings(Path(root) / f"w{os.getpid()}", Path(models_dir))
    pool = OcrPool(None, "rapidocr", workers=0)
    out = []
    for c in cases:
        r = H.run_case(c, s, ocr_pool=pool)
        out.append({"case_id": c.case_id, "carrier": c.carrier, "scores": H.score_case(r), "failed": r["failed"],
                    "seconds": r["seconds"], "roles": r["roles"], "import_errors": r["import_errors"],
                    "font": c.meta.get("font")})
    return out


# ------------------------------------------------------------------------------------------- report

def _f(x: float) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.3f}"


def _ci(k: int, n: int) -> str:
    lo, hi = H.wilson(k, n)
    return "–" if n == 0 else f"[{lo:.2f}, {hi:.2f}]"


def counts_row(label: list[str], c: H.Counts) -> str:
    n_true = c.tp + c.fn
    return "| " + " | ".join(label + [str(n_true), str(c.tp), str(c.fp), str(c.fp_hq),
                                      f"{_f(c.precision)} {_ci(c.tp, c.tp + c.fp)}", _f(c.precision_hq),
                                      f"{_f(c.recall)} {_ci(c.tp, n_true)}", _f(c.f1)]) + " |"


HDR = "| n facts | TP | FP | FP q≥0.5 | precision [95% CI] | precision q≥0.5 | recall [95% CI] | F1 |"


def build_report(results: list[dict], meta: dict) -> tuple[str, dict]:
    scores = [s for r in results for s in r["scores"]]
    for s in scores:
        s.carrier = H.carrier_group(s.carrier) if not s.carrier.startswith("ocr_") else s.carrier
    L: list[str] = []
    L += ["# Extraction accuracy (F01) — SYNTHETIC ground truth", "",
          "> **SYNTHETIC.** Every number below is measured on generated artefacts with known embedded facts "
          "(tests/extract_accuracy/harness.py). It measures whether the product's extractors observe facts that "
          "are present, and nothing else; it is not a measurement on organiser data and says nothing about the "
          "frequency of these formats in real material.", "",
          f"- Command: `{meta['command']}`",
          f"- Commit: `{meta['commit']}`{' (working tree dirty)' if meta['dirty'] else ''}; seed {meta['seed']}; "
          f"generated {meta['date']}; host `{meta['host']}` ({meta['platform']}), {meta['jobs']} worker processes; "
          f"wall time {meta['wall_s']:.0f} s.",
          f"- Corpus: {meta['n_docs']} text documents x {len(meta['carriers'])} carriers "
          f"({', '.join(meta['carriers'])}); "
          f"{meta['n_ocr_docs']} documents rendered at {meta['ocr_sizes']} px x {meta['ocr_formats']} "
          f"(font {meta['font']}); {meta['n_har']} HAR redirect chains; {meta['n_url']} URL-only inputs; "
          f"{meta['n_cases']} incidents in total.",
          f"- OCR engine: rapidocr (PP-OCRv6, product default) from `{meta['models_dir']}`; "
          f"available: {meta['ocr_available']}.",
          "- Path: each incident folder is planned by `hydra.adapters.nask_folder.plan_incident` (MIME by bytes, "
          "roles) and run through `hydra.genome.base.run_all` with the scored extractors only "
          f"({', '.join(H.SCORED)}). Observed genes are compared with the ground truth by (kind, product value).",
          "- Counting unit: one fact per (incident, kind, value). FP = an observed gene of a scored kind that is "
          "not in the ground truth (decoys: IP addresses, dates, versions, NIP, ids, invalid IBAN/BTC checksums, "
          "click ids, foreign numbers). `precision q≥0.5` ignores genes the product itself marks as low quality "
          "(accounts/wallets failing the checksum are kept at q ≤ 0.4 by design).",
          "- Confidence intervals: Wilson 95% per cell, assuming independent facts. Facts come from a fixed set of "
          "sentence templates, so the intervals understate the uncertainty for unseen phrasings.", ""]
    # 1. per extractor x kind
    L += ["## 1. Per extractor and fact kind (all carriers)", "", "| extractor | kind " + HDR,
          "|---|---" + "|---:" * 8 + "|"]
    agg = H.aggregate(scores, ("extractor", "kind"))
    summary: dict = {"by_extractor_kind": {}, "by_carrier_kind": {}, "ocr": {}}
    for (ex, kind), c in sorted(agg.items()):
        L.append(counts_row([ex, kind], c))
        summary["by_extractor_kind"][f"{ex}/{kind}"] = vars(c) | {"precision": c.precision, "recall": c.recall,
                                                                  "f1": c.f1}
    # 2. txt_entities by carrier group
    L += ["", "## 2. txt_entities by carrier (text, HTML, chat exports, OCR aggregated)", "",
          "| carrier | kind " + HDR, "|---|---" + "|---:" * 8 + "|"]
    by_c: dict[tuple, H.Counts] = defaultdict(H.Counts)
    for s in scores:
        if s.extractor in ("txt_entities", "txt_lines"):
            g = "ocr (all sizes/formats)" if s.carrier.startswith("ocr_") else s.carrier
            by_c[(g, s.kind)].add(s.counts)
    for (g, kind), c in sorted(by_c.items()):
        L.append(counts_row([g, kind], c))
        summary["by_carrier_kind"][f"{g}/{kind}"] = vars(c) | {"precision": c.precision, "recall": c.recall}
    # 3. chat parsing per export format
    L += ["", "## 3. Chat exports: txt_chat parse (platform, persona, first date) and sender phone", "",
          "| export | kind " + HDR, "|---|---" + "|---:" * 8 + "|"]
    chat = H.aggregate([s for s in scores if s.extractor == "txt_chat"], ("carrier", "kind"))
    raw_chat: dict[tuple, H.Counts] = defaultdict(H.Counts)
    for r in results:
        if r["carrier"].startswith("chat_"):
            for s in r["scores"]:
                if s.extractor == "txt_chat":
                    raw_chat[(r["carrier"], s.kind)].add(s.counts)
    for (car, kind), c in sorted(raw_chat.items()):
        L.append(counts_row([car, kind], c))
    del chat
    L += ["", "## 3b. HAR and URL inputs by variant", "", "| variant | extractor | kind " + HDR,
          "|---|---|---" + "|---:" * 8 + "|"]
    net: dict[tuple, H.Counts] = defaultdict(H.Counts)
    for r in results:
        if r["carrier"].startswith("har_") or r["carrier"] == "url":
            for s in r["scores"]:
                net[(r["carrier"], s.extractor, s.kind)].add(s.counts)
    for (car, ex, kind), c in sorted(net.items()):
        L.append(counts_row([car, ex, kind], c))
    # 4. OCR matrix
    L += ["", "## 4. Screenshot OCR: txt_entities recall by font size x encoding", "",
          "Rows: rendered text height and encoding (PNG lossless, JPEG quality 90/50/20). Cells: recall "
          "(TP / facts) over all txt_entities kinds; then per kind.", ""]
    ocr_scores = [s for s in scores if s.carrier.startswith("ocr_") and s.extractor == "txt_entities"]
    kinds = sorted({s.kind for s in ocr_scores})
    if ocr_scores:
        L += ["| size x format | all: recall [CI] | all: precision | " + " | ".join(kinds) + " |",
              "|---|---:|---:|" + "---:|" * len(kinds)]
        cells = H.aggregate(ocr_scores, ("carrier", "kind"))
        for car in sorted({s.carrier for s in ocr_scores}, key=lambda x: (int(x.split("_")[1][:-2]), x)):
            tot = H.Counts()
            for k in kinds:
                tot.add(cells.get((car, k), H.Counts()))
            row = [car.removeprefix("ocr_"), f"{_f(tot.recall)} {_ci(tot.tp, tot.tp + tot.fn)}", _f(tot.precision)]
            row += [_f(cells.get((car, k), H.Counts()).recall) for k in kinds]
            L.append("| " + " | ".join(row) + " |")
            summary["ocr"][car] = {"recall": tot.recall, "precision": tot.precision, "n": tot.tp + tot.fn}
    else:
        L.append("No OCR carriers in this run.")
    # 5. errors
    failed = [(r["case_id"], f) for r in results for f in r["failed"]]
    imp = [(r["case_id"], e) for r in results for e in r["import_errors"]]
    secs = sorted(r["seconds"] for r in results)
    L += ["", "## 5. Failures, import errors, cost", "",
          f"- Extractor `failed` genes: {len(failed)}" + (": " + "; ".join(f"{a}: {b}" for a, b in failed[:10])
                                                      if failed else "."),
          f"- Import (plan_incident) errors: {len(imp)}" + (": " + "; ".join(f"{a}: {b}" for a, b in imp[:10])
                                                           if imp else "."),
          f"- Extractor time per incident (scored extractors, s): median {secs[len(secs) // 2]:.3f}, "
          f"p95 {secs[int(len(secs) * 0.95)]:.3f}, max {secs[-1]:.3f}." if secs else "- no cases"]
    ocr_secs = sorted(r["seconds"] for r in results if r["carrier"].startswith("ocr_"))
    if ocr_secs:
        L.append(f"- OCR incidents (one image each, includes OCR): median {ocr_secs[len(ocr_secs) // 2]:.2f} s, "
                 f"max {ocr_secs[-1]:.2f} s.")
    # 6. examples
    L += ["", "## 6. Error examples (first three per extractor/kind; synthetic values)", ""]
    ex_miss: dict[tuple, list[str]] = defaultdict(list)
    ex_spur: dict[tuple, list[str]] = defaultdict(list)
    for s in scores:
        for v in s.missed:
            ex_miss[(s.extractor, s.kind)].append(f"{s.case_id}: {v}")
        for v in s.spurious:
            ex_spur[(s.extractor, s.kind)].append(f"{s.case_id}: {v}")
    for key in sorted(set(ex_miss) | set(ex_spur)):
        L.append(f"- **{key[0]}/{key[1]}** — missed {len(ex_miss[key])}: {'; '.join(ex_miss[key][:3]) or '–'}; "
                 f"spurious {len(ex_spur[key])}: {'; '.join(ex_spur[key][:3]) or '–'}")
    # 7. CRs
    L += ["", "## 7. Defects found (CR-F01-n, owners: extractor owners; not fixed here)", ""]
    for cr in CRS:
        L += [f"### {cr['id']}: {cr['title']}", "", f"- Where: {cr['where']}", f"- Expected: {cr['expect']}",
              f"- Impact: {cr['impact']}", "- Minimal repro:", "", "```python", cr["repro"], "```", "",
              f"- Live result at report time: `{run_repro(cr)}`", ""]
    L += ["## 8. Reading the tables", "",
          "- Text, HTML and chat carriers contain the same documents, so their differences isolate the parser; "
          "the OCR rows add the recogniser. OCR misses and OCR false positives in section 6 are mostly character "
          "confusions (1/l, I/l) in hosts, handles and e-mails: an OCR error, not a "
          "normaliser error (a changed host is a different host, so it is deliberately not corrected).",
          "- Precision `q≥0.5` vs all: the difference is the invalid-checksum decoys the product keeps at "
          "quality <= 0.4 by design (accounts, wallets); in the committed run every other false positive has "
          "quality >= 0.5.",
          "- redirect_chain: section 3b separates the 3xx variant from the meta-refresh variant (CR-F01-2).",
          "- person: the corpus mixes nominative and genitive forms; the genitive misses are CR-F01-3.", ""]
    L += ["## 9. What this does not measure", "",
          "- Real-world phrasing, layouts, fonts, photos of screens, rotated or partially covered text, emoji "
          "and mixed scripts; the OCR carrier is clean rendered text on a flat background.",
          "- Video, PDF, MHTML and ZIP carriers, the enrichment (online) path and the downstream decision; facts "
          "are scored at gene level only.",
          "- Persons outside the shipped lexicon (the heuristic 'Given Surname' path) and the local "
          "organiser-derived overlay.", ""]
    return "\n".join(L) + "\n", summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=20261122)
    ap.add_argument("--docs", type=int, default=60)
    ap.add_argument("--ocr-docs", type=int, default=20)
    ap.add_argument("--ocr-sizes", default=",".join(map(str, H.OCR_SIZES)))
    ap.add_argument("--ocr-formats", default=",".join(H.OCR_FORMATS))
    ap.add_argument("--har", type=int, default=40)
    ap.add_argument("--url", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    ap.add_argument("--out-md", default=str(REPO / "docs/release/EXTRACTION_ACCURACY.md"))
    ap.add_argument("--out-json", default="")
    a = ap.parse_args()
    t0 = time.time()
    from hydra.config import Settings

    models_dir = Path(os.environ.get("HYDRA_MODELS_DIR") or Settings().models_dir)
    sizes = tuple(int(x) for x in a.ocr_sizes.split(",") if x)
    fmts = tuple(x for x in a.ocr_formats.split(",") if x)
    tmp = Path(tempfile.mkdtemp(prefix="hydra-f01-"))
    cases, _ = H.make_corpus(tmp / "corpus", a.seed, a.docs, a.ocr_docs, a.har, a.url, sizes, fmts)
    chunks = [cases[i::a.jobs] for i in range(a.jobs)]
    results: list[dict] = []
    if a.jobs <= 1:
        results = _work((cases, str(tmp), str(models_dir)))
    else:
        import multiprocessing as mp

        with ProcessPoolExecutor(a.jobs, mp_context=mp.get_context("spawn")) as ex:
            for part in ex.map(_work, [(c, str(tmp), str(models_dir)) for c in chunks]):
                results.extend(part)
    results.sort(key=lambda r: r["case_id"])
    rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "hydra"], capture_output=True, text=True,
                                cwd=REPO).stdout.strip())
    font = next((r["font"] for r in results if r.get("font")), "-")
    meta = {"command": "uv run python scripts/measure_extraction.py " + " ".join(sys.argv[1:]), "commit": rev,
            "dirty": dirty, "seed": a.seed,
            "date": datetime.now(ZoneInfo("Europe/Warsaw")).isoformat(timespec="minutes"),
            "host": platform.node().split(".")[0], "platform": f"{platform.system()} {platform.machine()}",
            "jobs": a.jobs, "wall_s": time.time() - t0, "n_docs": a.docs, "carriers": list(H.TEXT_CARRIERS),
            "n_ocr_docs": a.ocr_docs, "ocr_sizes": list(sizes), "ocr_formats": list(fmts), "font": font,
            "n_har": a.har, "n_url": a.url, "n_cases": len(cases), "models_dir": "$HYDRA_MODELS_DIR"
            if os.environ.get("HYDRA_MODELS_DIR") else str(models_dir),
            "ocr_available": (models_dir / "rapidocr-v6").is_dir()}
    md, summary = build_report(results, meta)
    Path(a.out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out_md).write_text(md, "utf-8")
    if a.out_json:
        Path(a.out_json).write_text(json.dumps({"meta": meta, "summary": summary}, indent=1, default=str), "utf-8")
    print(md)
    print(f"wrote {a.out_md} ({len(cases)} incidents, {time.time() - t0:.0f} s; corpus in {tmp})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
