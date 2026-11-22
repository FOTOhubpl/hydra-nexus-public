"""Generate docs/LICENSES.md: Python packages, UI packages, models/data, external services.

Sources (no guessing; anything not found is printed as UNKNOWN and flagged):
- Python: `uv export --locked` for the runtime/dev split, licence from the installed package metadata
  (License-Expression, then a short License field, then trove classifiers). Run inside the uv env.
- UI: `pnpm licenses list --json` (all deps) and `--prod` (shipped deps) in ui/.
- Models and reference data: models.lock.json.
- External services: the static table SERVICES below (terms read on the date given, URL per row).

Usage:  uv run python scripts/gen_licenses.py [--out docs/LICENSES.md]
"""

from __future__ import annotations

import argparse
import importlib.metadata as md
import json
import platform
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Licence families. Anything outside PERMISSIVE is flagged in the output.
PERMISSIVE = {
    "MIT", "MIT-CMU", "BSD-2-Clause", "BSD-3-Clause", "BSD", "Apache-2.0", "ISC", "PSF-2.0", "Python-2.0",
    "0BSD", "Unlicense", "CC0-1.0", "BlueOak-1.0.0", "Zlib", "HPND", "MIT-0", "BSD-3-Clause-Clear",
    "Matplotlib", "OFL-1.1", "CNRI-Python", "BSL-1.0", "LLVM-exception",
}
WEAK_COPYLEFT = {"MPL-2.0", "LGPL-2.1", "LGPL-3.0", "LGPL-2.1-or-later", "LGPL-3.0-or-later", "EPL-2.0", "CDDL-1.0"}

CLASSIFIER_MAP = {
    "MIT License": "MIT",
    "BSD License": "BSD",
    "Apache Software License": "Apache-2.0",
    "ISC License (ISCL)": "ISC",
    "Python Software Foundation License": "PSF-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0",
    "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0",
    "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.1-or-later",
    "GNU General Public License v3 (GPLv3)": "GPL-3.0",
    "The Unlicense (Unlicense)": "Unlicense",
    "Historical Permission Notice and Disclaimer (HPND)": "HPND",
}

# Free-text License fields seen in the lock that need a canonical name (verified against the package's
# own LICENSE file / PyPI page). Keep this list short and explicit.
TEXT_MAP = {
    "Apache 2.0": "Apache-2.0", "Apache License 2.0": "Apache-2.0", "Apache Software License": "Apache-2.0",
    "Apache License, Version 2.0": "Apache-2.0", "Apache-2.0 license": "Apache-2.0", "Apache 2": "Apache-2.0",
    "MIT License": "MIT", "MIT license": "MIT", "BSD License": "BSD", "BSD 3-Clause": "BSD-3-Clause",
    "3-Clause BSD License": "BSD-3-Clause", "BSD-3-Clause License": "BSD-3-Clause", "new BSD": "BSD-3-Clause",
    "Modified BSD License": "BSD-3-Clause", "BSD 2-Clause License": "BSD-2-Clause",
    "2-clause BSD License": "BSD-2-Clause",
    "ISC License": "ISC",
    "PSF": "PSF-2.0", "Python Software Foundation License": "PSF-2.0", "MPL 2.0": "MPL-2.0",
    "Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0", "PSF License": "PSF-2.0",
}

# System packages inside the Docker image (Debian bookworm apt). Not pip/npm, so listed by hand.
SYSTEM = [
    ("ffmpeg", "Debian bookworm apt", "LGPL-2.1-or-later / GPL-2.0-or-later (Debian build enables GPL parts)",
     "invoked as a separate process (frames/audio extraction); not linked into HYDRA code"),
    ("tesseract-ocr + pol/eng traineddata", "Debian bookworm apt", "Apache-2.0", "OCR fallback, separate process"),
    ("dnsutils (BIND 9 `dig`)", "Debian bookworm apt", "MPL-2.0", "live DNS lookups, separate process, unmodified"),
    ("libgl1, libglib2.0-0", "Debian bookworm apt", "MIT-style (Mesa) / LGPL-2.1-or-later (GLib)",
     "runtime libraries required by opencv-python; dynamically linked"),
    ("python:3.11-slim-bookworm base image", "Docker Hub official image", "PSF-2.0 (CPython) + Debian packages",
     "container base"),
    ("uv 0.12.19 binary", "ghcr.io/astral-sh/uv", "MIT OR Apache-2.0", "build-time installer"),
    ("node 24 + pnpm 12.15.0 (UI build stage)", "Docker Hub node:24-bookworm-slim", "MIT",
     "build-time only; not in the final image"),
    ("Neo4j 5.26 Community (optional compose profile `neo4j`)", "Docker Hub neo4j:5.26-community",
     "GPL-3.0 (server)", "optional external service in its own container; HYDRA only sends Cypher through "
     "cypher-shell, does not link to or ship Neo4j code (docs/NEO4J.md)"),
]

# External services used by the enrichment layer (ADR-007) and by the offline infra analysis.
# terms_checked = date the operator's agent read the terms page. "unverified" = no terms page found.
# Offline datasets used outside the product image (evaluation, calibration, public demonstration corpus).
# Verdicts follow docs/datasets/EXTERNAL.md. None of these files is shipped; page/ad bytes are third-party content.
DATASETS = [
    ("CERT Polska warning list", "hole.cert.pl/domains/v2/domains.json (snapshot 2026-11-20)",
     "free for any use; no endorsement implied", "lookup + public corpus; local snapshot"),
    ("PhishTank online-valid", "data.phishtank.com (2026-11-20)", "free use incl. commercial (FAQ); attribution",
     "eval distractors, public corpus"),
    ("PhiUSIIL Phishing URL Dataset", "UCI 967 (Prasad & Chandra 2024)", "CC BY 4.0",
     "eval distractors, public corpus"),
    ("phreshphish", "huggingface.co/datasets/phreshphish/phreshphish @eca603e; arXiv 2507.10854",
     "CC BY 4.0; card: 'anti-phishing research'", "SHIP-SAFE: calibration/priors/eval (ext corpus)"),
    ("phreshphish-2k", "huggingface.co/datasets/gt2026workshop/phreshphish-2k @7ceeea2", "CC BY 4.0",
     "SHIP-SAFE: calibration/eval (ext corpus)"),
    ("purino-scam-ads", "huggingface.co/datasets/Purino/scam-ads-detection-dataset @484306e",
     "card: MIT, but contains third-party ad creatives", "calibration/eval only; creatives never redistributed"),
    ("phishing-websites-putra", "zenodo.org/records/8041387", "CC BY 4.0", "SHIP-SAFE: calibration/eval"),
    ("phish-screens", "huggingface.co/datasets/shresthsamyak/phishing-website-screenshots @757b719",
     "'other': defensive research and education", "EVAL-ONLY"),
    ("phishdecloaker-kits", "zenodo.org/records/11228974", "CC BY 4.0", "SHIP-SAFE; stored, no adapter yet"),
    ("Phishpedia, PhishIntention, DynaPD", "Google Drive links in the authors' repos",
     "code CC0 / none; no data licence", "not downloaded; EVAL-ONLY if ever used"),
    ("VisualPhish, PILWD-134K, deepfake sets (Deepfake-Eval-2024, Celeb-DF, FF++)", "-",
     "e-mail request / form / gated", "EXCLUDED"),
]

SERVICES = [
    {
        "name": "rdap.org (RDAP bootstrap redirector) + authoritative registry RDAP servers (e.g. rdap.dns.pl)",
        "use": "domain and IP registration data: registrar, creation/expiry dates, status, nameservers",
        "auth": "none",
        "terms": "operated by Gavin Brown privately; no formal ToS; registry RDAP servers have their own terms "
                 "(registration data, no bulk harvesting / marketing use)",
        "limits": "10 requests / 10 s per client (Cloudflare), 429 above; 403 on abuse",
        "url": "https://about.rdap.org/",
        "checked": "2026-11-21",
        "flag": "",
    },
    {
        "name": "Public DNS resolvers (Cloudflare 1.1.1.1, Google 8.8.8.8) via the system `dig` binary",
        "use": "A/AAAA/NS/MX/TXT/CNAME records of hosts found in the material",
        "auth": "none",
        "terms": "free public resolvers under the operators' resolver policies",
        "limits": "no published hard limit for normal use (unverified); HYDRA caches every answer",
        "url": "https://developers.cloudflare.com/1.1.1.1/privacy/public-dns-resolver/ ; "
               "https://developers.google.com/speed/public-dns/terms",
        "checked": "unverified",
        "flag": "",
    },
    {
        "name": "Team Cymru IP-to-ASN mapping (DNS interface, origin.asn.cymru.com)",
        "use": "IP -> ASN, BGP prefix, country, AS name (hosting provider)",
        "auth": "none",
        "terms": "\"Free, forever\"; use bulk mode or DNS at volume",
        "limits": "keep bulk runs to a few thousand addresses; hosts hammering WHOIS get null-routed",
        "url": "https://www.team-cymru.com/ip-asn-mapping",
        "checked": "2026-11-21",
        "flag": "",
    },
    {
        "name": "crt.sh (Certificate Transparency log search, Sectigo)",
        "use": "certificates issued for a domain: issuers, SAN names, first/last issuance",
        "auth": "none",
        "terms": "no published terms of use found (unverified)",
        "limits": "no published limit; slow / 429 / 502 under load; HYDRA uses >= 3 s between queries and caches",
        "url": "https://crt.sh/",
        "checked": "unverified",
        "flag": "",
    },
    {
        "name": "CERT Polska warning list (hole.cert.pl domains v2)",
        "use": "host-level lookup: is the host on the Polish warning list (local snapshot)",
        "auth": "none",
        "terms": "free for any use, attribution not required; must not imply endorsement by CERT Polska",
        "limits": "local snapshot (models.lock.json `cert-domains`), no runtime calls",
        "url": "https://cert.pl/en/warning-list/",
        "checked": "2026-11-20",
        "flag": "",
    },
    {
        "name": "Shodan InternetDB",
        "use": "open ports / tags / hostnames of an IP (analysis enrichment, optional)",
        "auth": "none",
        "terms": "\"free for non-commercial use\"; commercial use needs a Shodan enterprise licence",
        "limits": "none published; weekly data refresh",
        "url": "https://internetdb.shodan.io/",
        "checked": "2026-11-21",
        "flag": "NON-COMMERCIAL ONLY: disabled by default; a deployment by NASK must check its own licence",
    },
    {
        "name": "urlscan.io Search API",
        "use": "earlier public scans of a domain (first seen, related pages) (analysis enrichment, optional)",
        "auth": "optional API key; unauthenticated users get \"minor quotas\"",
        "terms": "urlscan.io terms of service; commercial plans exist",
        "limits": "small unauthenticated quota (exact number not published on the API intro page)",
        "url": "https://docs.urlscan.io/pages/api-intro",
        "checked": "2026-11-21",
        "flag": "QUOTA + TERMS: optional, off by default; no key is shipped or required",
    },
    {
        "name": "Internet Archive Wayback CDX API",
        "use": "first/last archived capture of a domain (age of the landing page)",
        "auth": "none",
        "terms": "Internet Archive terms of use (unverified for API rate)",
        "limits": "no published limit found; HYDRA uses >= 1.5 s between queries and caches",
        "url": "https://archive.org/about/terms",
        "checked": "unverified",
        "flag": "DROPPED from the default set (16 of 23 calls timed out in the analysts' run)",
    },
    {
        "name": "certstream-server-go (self-hosted Certificate Transparency stream, hydra/watch/ct_recorder.py)",
        "use": "optional live feed of newly issued certificate names for the Watch module; replays by default",
        "auth": "none (runs on the operator's own host, websocket on loopback, e.g. ws://127.0.0.1:8098)",
        "terms": "MIT-licensed server (https://github.com/d-Rickyy-b/certstream-server-go); it reads the public CT "
                 "logs, whose operators publish their own usage policies",
        "limits": "bounded by the CT logs polled by the self-hosted server; HYDRA only listens",
        "url": "https://github.com/d-Rickyy-b/certstream-server-go",
        "checked": "2026-11-21 (licence as recorded in hydra/watch/ct_recorder.py; not re-read online)",
        "flag": "optional, off by default",
    },
    {
        "name": "Amazon Bedrock: Claude models (optional LLM analyst layer, ADR-008, hydra/llm/)",
        "use": "Polish campaign briefs, pair explanations, narrative genes that never create or support a link",
        "auth": "the operator's AWS account through the standard AWS credential chain; not in the repo",
        "terms": "AWS Service Terms and the model provider's acceptable-use policy; text redacted before sending",
        "limits": "account quotas per model; every call cached and replayable (HYDRA_LLM=replay, no network)",
        "url": "https://aws.amazon.com/service-terms/",
        "checked": "unverified",
        "flag": "OPTIONAL, OFF BY DEFAULT (HYDRA_LLM unset): never needed for the demo or the hidden-set run",
    },
    {
        "name": "Meta Ad Library API (bonus: hydra/enrich/adlibrary.py, scheduler)",
        "use": "search public ads by keyword, country and date (NASK §3 bonus)",
        "auth": "Meta developer account + identity confirmation + user token in HYDRA_META_TOKEN (not in the repo)",
        "terms": "Meta Ad Library API terms; [U] coverage of non-political ads in PL (EU DSA scope)",
        "limits": "Graph API rate limits; HYDRA waits 2 s between calls, backs off on 429 / codes 4,17,32,613,80004",
        "url": "https://www.facebook.com/ads/library/api/",
        "checked": "unverified",
        "flag": "NEEDS TOKEN: never required for evaluation; manual import works without a token",
    },
]


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout


def lock_names(dev: bool) -> dict[str, str]:
    cmd = ["uv", "export", "--locked", "--no-hashes", "--no-emit-project", "--no-annotate", "--no-header"]
    cmd += ["--only-dev"] if dev else ["--no-dev"]
    out: dict[str, str] = {}
    for line in run(cmd, ROOT).splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)", line)
        if m:
            out[canon(m.group(1))] = m.group(2)
    return out


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# Packages whose metadata names no SPDX licence; value read from the package's own LICENSE file.
PKG_OVERRIDE = {
    "python-dateutil": "Apache-2.0 OR BSD-3-Clause",  # LICENSE: dual licensed (Apache 2.0 for changes since 2017)
}


def licence_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    if canon(meta["Name"]) in PKG_OVERRIDE:
        return PKG_OVERRIDE[canon(meta["Name"])]
    expr = meta.get("License-Expression")
    if expr:
        return expr.strip()
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) <= 60 and text.upper() != "UNKNOWN":
        return TEXT_MAP.get(text, text)
    cls = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    cls = [CLASSIFIER_MAP.get(c, c) for c in cls if c != "OSI Approved"]
    if cls:
        return " OR ".join(sorted(set(cls)))
    if text:
        first = text.splitlines()[0].strip()
        return TEXT_MAP.get(first, first[:60]) + " (from License text)"
    return "UNKNOWN"


def classify(lic: str) -> str:
    """'' = permissive, otherwise a short flag."""
    if lic.startswith("UNKNOWN"):
        return "UNKNOWN: check manually"
    parts = re.split(r"\s+(?:OR|AND|WITH)\s+|[()/,;]", lic.replace(" (from License text)", ""))
    parts = [p.strip() for p in parts if p.strip()]
    if " OR " in lic and any(p in PERMISSIVE for p in parts):
        return ""  # dual licence with a permissive option
    flags = []
    for p in parts:
        if p in PERMISSIVE or p.startswith("BSD") or p.startswith("Apache") or p.startswith("MIT"):
            continue
        if p in WEAK_COPYLEFT or p.startswith("LGPL") or p.startswith("MPL"):
            flags.append(f"weak copyleft ({p}): use unmodified")
        elif "GPL" in p:
            flags.append(f"copyleft ({p})")
        elif p.lower().startswith("cc-by"):
            flags.append(f"attribution required ({p})")
        else:
            flags.append(f"non-standard ({p}): reviewed below")
    return "; ".join(dict.fromkeys(flags))


def python_rows() -> tuple[list[tuple], list[tuple]]:
    installed = {canon(d.metadata["Name"]): d for d in md.distributions()}
    result = []
    for dev in (False, True):
        rows = []
        for name, ver in sorted(lock_names(dev).items()):
            dist = installed.get(name)
            if dist is None:
                rows.append((name, ver, "not installed on this platform", "check on the other platform"))
                continue
            lic = licence_of(dist)
            rows.append((name, ver, lic, classify(lic)))
        result.append(rows)
    return result[0], result[1]


def pnpm_rows() -> tuple[list[tuple], list[tuple]]:
    ui = ROOT / "ui"
    if not (ui / "node_modules").exists():
        return [], []

    def load(prod: bool) -> dict[tuple, str]:
        cmd = ["pnpm", "licenses", "list", "--json"] + (["--prod"] if prod else [])
        data = json.loads(run(cmd, ui))
        out = {}
        for lic, pkgs in data.items():
            for p in pkgs:
                for v in p["versions"]:
                    out[(p["name"], v)] = lic
        return out

    prod = load(True)
    every = load(False)
    p_rows = [(n, v, lic, classify(lic)) for (n, v), lic in sorted(prod.items())]
    d_rows = [(n, v, lic, classify(lic)) for (n, v), lic in sorted(every.items()) if (n, v) not in prod]
    return p_rows, d_rows


def model_rows() -> list[tuple]:
    lock = json.loads((ROOT / "models.lock.json").read_text())
    rows = []
    for key, e in lock["entries"].items():
        rev = e.get("revision", "")
        rows.append((key, e.get("kind", ""), e.get("repo") or e.get("source", ""), rev[:12] if len(rev) == 40 else rev,
                     e.get("license", ""), f"{e.get('total_size', 0) / 1e6:.1f} MB"))
    return rows


def table(header: tuple, rows: list[tuple]) -> str:
    if not rows:
        return "_none found (dependency not installed in this environment)_\n"
    esc = [tuple(str(c).replace("|", "\\|") for c in r) for r in rows]
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in esc]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs" / "LICENSES.md"))
    args = ap.parse_args()

    py_run, py_dev = python_rows()
    ui_prod, ui_dev = pnpm_rows()
    models = model_rows()
    flagged = [("Python runtime", r) for r in py_run if r[3]] + [("UI shipped", r) for r in ui_prod if r[3]]
    flagged += [("Python dev/test", r) for r in py_dev if r[3]] + [("UI build/test", r) for r in ui_dev if r[3]]

    out = [
        "# Licences: libraries, models, data, services",
        "",
        f"Generated by `scripts/gen_licenses.py` on {date.today().isoformat()} "
        f"({platform.system()} {platform.machine()}, Python {sys.version.split()[0]}). Do not edit by hand: "
        "re-run `uv run python scripts/gen_licenses.py`.",
        "",
        "Sources: installed package metadata in the uv environment (`uv.lock`), `pnpm licenses list` for "
        "`ui/pnpm-lock.yaml`, `models.lock.json`, and the external-service table in the script (terms read "
        "on the date in the row; `unverified` means no terms page was found or read). This answers NASK "
        "task §4: libraries, models and external services with licences and usage limits.",
        "",
        "## Scope: four different kinds of rights",
        "",
        "This file separates four things that are often mixed up:",
        "",
        "1. **Library licences** (Python and UI packages, system packages): what may be done with the code we "
        "run and ship in the image.",
        "2. **Model weight licences** (`models.lock.json`): set by the weight publisher, independent of the "
        "licence of the library that loads them.",
        "3. **Dataset and service terms** (CERT Polska list, PhishTank, PhiUSIIL, RDAP registries, crt.sh, "
        "Team Cymru, Meta Ad Library, Amazon Bedrock): conditions on queries, attribution and reuse of the "
        "answers.",
        "4. **Rights to ad content**: the creatives, pages and texts inside the organiser material and inside "
        "public feeds belong to third parties. A permissive licence of a dataset card (CC BY, MIT) does not by "
        "itself grant the right to redistribute every image or page in it. Organiser material is used only for "
        "this challenge and never published (task §8); public-corpus results are shown as aggregates and "
        "public indicators.",
        "",
        "Copyleft is not automatically a problem: file-level copyleft (MPL-2.0) used unmodified, and GPL programs "
        "run as separate processes or servers (ffmpeg, Neo4j Community in an optional container) do not place "
        "HYDRA's own code under their licence. They are listed below so the jury and NASK can check that "
        "reading.",
        "",
        "## Summary",
        "",
        f"- Python runtime packages: {len(py_run)}; dev/test only: {len(py_dev)}.",
        f"- UI packages shipped in the bundle: {len(ui_prod)}; build/test only: {len(ui_dev)}.",
        f"- Models and reference data: {len(models)} (all downloaded, none trained by the team).",
        f"- External services: {len(SERVICES)}; none needs a private key for evaluation. Default mode is "
        "offline replay of the enrichment cache (`HYDRA_OFFLINE=1`); live lookups need `HYDRA_ENRICH=1` and "
        "`HYDRA_OFFLINE=0`.",
        f"- Entries outside the permissive set (MIT, BSD, Apache-2.0, ISC, PSF, OFL fonts...): {len(flagged)}, "
        "listed in the next section.",
        "- The analysis core is offline: local models with pinned revisions, no inference API (ADR-003). "
        "Enrichment returns public facts, not model outputs. The optional LLM analyst layer (ADR-008: Claude on "
        "Amazon Bedrock) is off by default, cached and replayable without our account, and never a linking "
        "rule. A local open-weights model (for example Bielik) could serve the same layer; that is not "
        "implemented.",
        "",
        "## Flagged (not plainly permissive)",
        "",
        table(("scope", "package", "version", "licence", "flag"), [(s, *r) for s, r in flagged]),
        "Notes on the flags:",
        "",
        "- MPL-2.0 (e.g. tqdm, certifi, hypothesis) is file-level copyleft: HYDRA uses these packages "
        "unmodified, so no HYDRA source is affected. Modified MPL files would have to be published.",
        "- LGPL components are used as separate, dynamically loaded libraries or processes.",
        "- dnsutils (BIND 9 `dig`, MPL-2.0) is called as a separate process, unmodified.",
        "- Neo4j Community (GPL-3.0) is an optional server in its own container (compose profile `neo4j`), not "
        "part of the HYDRA image; HYDRA does not link to it.",
        "- ffmpeg (system package in the image) is a Debian build with GPL parts. HYDRA calls it as an external "
        "process and does not link to it; redistribution of the image must ship Debian's licence files "
        "(present under `/usr/share/doc/*/copyright` in the image).",
        "- Services flagged NON-COMMERCIAL or QUOTA are optional and off by default; see the services table.",
        "",
        "## Python runtime (shipped in the Docker image)",
        "",
        table(("package", "version", "licence", "flag"), py_run),
        "## Python dev/test only (not shipped)",
        "",
        table(("package", "version", "licence", "flag"), py_dev),
        "## UI packages bundled into `ui/dist`",
        "",
        table(("package", "version", "licence", "flag"), ui_prod),
        "## UI build/test tooling (not shipped)",
        "",
        table(("package", "version", "licence", "flag"), ui_dev),
        "## Models and reference data (`models.lock.json`)",
        "",
        "Weights live outside the repository and the image; they are mounted read-only. Full sha256 per file "
        "in `models.lock.json`, smoke tests in `docs/MODEL_REGISTRY.md`.",
        "",
        table(("key", "kind", "source", "revision", "licence / terms", "size"), models),
        "## System packages in the Docker image",
        "",
        table(("component", "source", "licence", "how used"), SYSTEM),
        "## External datasets (offline, not shipped; details in docs/datasets/EXTERNAL.md)",
        "",
        "Brand labels in these datasets are group-level, not campaign truth. External data is separated from "
        "organiser data by the corpus flag (`ext:<name>`, excluded from the default NASK-only run). Nothing "
        "external ships inside the product except, if enabled, derived priors fitted on SHIP-SAFE sources; no "
        "page, screenshot or ad creative is redistributed.",
        "",
        table(("dataset", "source", "licence / terms", "use"), DATASETS),
        "## External services (enrichment layer, ADR-007; sources detail in docs/DATA_SOURCES.md)",
        "",
        "Only hostnames and IP addresses extracted from the material are sent. No organiser file, image or text "
        "leaves the machine. Every response is cached with source, query, timestamp and sha256, so the "
        "evaluation can be replayed offline.",
        "",
        table(("service", "used for", "auth", "terms", "usage limits", "terms URL", "checked", "flag"),
              [(s["name"], s["use"], s["auth"], s["terms"], s["limits"], s["url"], s["checked"], s["flag"])
               for s in SERVICES]),
    ]
    Path(args.out).write_text("\n".join(out))
    print(f"wrote {args.out}: {len(py_run)} py runtime, {len(py_dev)} py dev, {len(ui_prod)} ui prod, "
          f"{len(ui_dev)} ui dev, {len(models)} models, {len(flagged)} flagged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
