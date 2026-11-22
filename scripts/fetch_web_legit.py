#!/usr/bin/env python3
"""One-off fetch of legit homepages (Tranco-listed domains) for background document-frequency statistics.

TRAIN stream, 2026-11-21 (operator instruction: "Tranco-listed sites' public landing HTML fetched once on the server
for statistics only"). Output stays on the server, never in git (docs/datasets/EXTERNAL.md, `web-legit-tranco`):

    $HYDRA_DATA_DIR/external-big/web-legit-tranco/
        tranco-<id>.csv            the Tranco list used (rank,domain)
        selection.json             selected domains (tld group pl|global, rank) and skip counts by reason
        pages/pages-NNN.parquet    one row per attempted domain (see COLUMNS); `html_gz` = gzip of the raw body
        stats.json, SOURCE.md

    uv run python scripts/fetch_web_legit.py --pl 3000 --global 2000 --jobs 16
    uv run python scripts/fetch_web_legit.py --summary-only      # recount + SOURCE.md from existing parts

Each homepage is fetched once (https://<domain>/, redirects followed, 15 s total deadline, 2 MB cap, generic UA, no
JS, no cookies kept). robots.txt is checked first; a "/" disallow for our UA or `*` skips the domain. Resumable: domains
already present in a part file are not fetched again. Response bytes are untrusted data: stored, never parsed or run.
pyarrow is used for the parquet parts (project dependency).
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
import zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

UA = "Mozilla/5.0 (compatible; research-crawler)"
UA_TOKEN = "research-crawler"
TRANCO_LATEST = "https://tranco-list.eu/api/lists/date/latest"
MAX_BYTES = 2 * 1024 * 1024
DEADLINE = 15.0
PART_ROWS = 500
COLUMNS = ["domain", "rank", "tld_group", "url", "final_url", "status", "content_type", "fetched_at", "ok",
           "error", "bytes", "truncated", "sha256", "html_gz"]

# hosts without an HTML homepage (CDN, API, telemetry, DNS, ads) and adult/gambling/malware-ish names:
# a coarse keyword filter on the registrable domain; misses are harmless (they fail or return non-HTML)
SKIP_PATTERNS = {
    "adult": r"porn|xxx|sex|xvideo|xhamster|redtube|youporn|onlyfans|chaturbate|stripchat|livejasmin|hentai|nude"
             r"|camsoda|bongacams|erotic|fetish|escort|brazzers|spankbang|pornhub|xnxx|milf|tube8|rule34",
    "gambling": r"casino|bet365|betting|gambl|poker|slots?\b|lotto|bukmach|zaklad|fortuna\.|sts\.pl|totalbet"
                r"|betfan|superbet|betclic|lvbet|1xbet|bwin|unibet|betway|stake\.",
    "infra": r"cdn|akamai|cloudfront|amazonaws|googleapis|gstatic|googleusercontent|doubleclick|googlesyndication"
             r"|googletagmanager|google-analytics|googleadservices|app-measurement|ggpht|ytimg|fbcdn|fbsbx|akadns"
             r"|edgekey|edgesuite|edgecast|fastly|azureedge|azurefd|trafficmanager|msedge|msftncsi|windowsupdate"
             r"|digicert|ocsp|pki\.|root-servers|ntp\.|dns\.|apple-dns|icloud-content|aaplimg|mzstatic|cloudflare-dns"
             r"|tiktokcdn|tiktokv|byteoversea|ibyteimg|sc-cdn|scdn|nflxso|nflxext|nflxvideo|2mdn|adnxs|adsrvr"
             r"|criteo|rubiconproject|pubmatic|openx|casalemedia|taboola|outbrain|scorecardresearch|demdex|omtrdc"
             r"|rlcdn|bidswitch|smartadserver|teads|moatads|doubleverify|adsafeprotected|crashlytics|appsflyer"
             r"|branch\.io|onesignal|sentry|newrelic|nr-data|hotjar|clarity\.ms|segment\.io|mixpanel|amplitude"
             r"|braze|kochava|adjust\.com|unity3d|applovin|ironsrc|vungle|chartboost|inmobi|mopub|gvt1|gvt2|1e100"
             r"|xboxlive|live\.net|office\.net|windows\.net|microsoftonline|msauth|s-msft|wns\.windows|static\."
             r"|^api\.|-api\.|\bapi\b|tracking|telemetry|metrics|analytics|pixel|beacon|ads\.|adserver|adservice",
    "malware": r"malware|phish|exploit|botnet|warez|crack|keygen|torrent|pirate",
}
SKIP_RE = {k: re.compile(v) for k, v in SKIP_PATTERNS.items()}


def data_root() -> Path:
    base = os.environ.get("HYDRA_DATA_DIR")
    return (Path(base) if base else Path.home() / "HydraNexus" / "data") / "external-big"


def now() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ------------------------------------------------------------------------------------------ Tranco list

def get_tranco(out: Path) -> tuple[Path, dict]:
    existing = sorted(out.glob("tranco-*.csv"))
    meta_p = out / "tranco-meta.json"
    if existing and meta_p.exists():
        return existing[-1], json.loads(meta_p.read_text())
    req = urllib.request.Request(TRANCO_LATEST, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        meta = json.loads(r.read())
    dst = out / f"tranco-{meta['list_id']}.csv"
    req = urllib.request.Request(meta["download"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=300) as r, open(dst, "wb") as f:
        f.write(r.read())
    meta["retrieved_at"] = now()
    meta_p.write_text(json.dumps(meta, indent=1) + "\n")
    return dst, meta


def skip_reason(domain: str) -> str | None:
    for k, rx in SKIP_RE.items():
        if rx.search(domain):
            return k
    return None


def select_domains(csv_path: Path, n_pl: int, n_global: int) -> tuple[list[dict], dict]:
    pl: list[dict] = []
    glob: list[dict] = []
    skipped: Counter = Counter()
    with open(csv_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            rank, dom = int(parts[0]), parts[1].strip().lower()
            is_pl = dom.endswith(".pl")
            if (is_pl and len(pl) >= n_pl) or (not is_pl and len(glob) >= n_global):
                if len(pl) >= n_pl and len(glob) >= n_global:
                    break
                continue
            why = skip_reason(dom)
            if why:
                skipped[f"{'pl' if is_pl else 'global'}:{why}"] += 1
                continue
            (pl if is_pl else glob).append({"domain": dom, "rank": rank, "tld_group": "pl" if is_pl else "global"})
    return pl + glob, {"selected_pl": len(pl), "selected_global": len(glob), "skipped_by_filter": dict(skipped),
                       "max_rank_pl": pl[-1]["rank"] if pl else None,
                       "max_rank_global": glob[-1]["rank"] if glob else None}


# ------------------------------------------------------------------------------------------ fetching

def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(urllib.request.HTTPRedirectHandler())


def _read_capped(resp, cap: int, deadline: float) -> tuple[bytes, bool]:
    buf = io.BytesIO()
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("total deadline")
        chunk = resp.read(65536)
        if not chunk:
            return buf.getvalue(), False
        buf.write(chunk)
        if buf.tell() >= cap:
            return buf.getvalue()[:cap], True


def robots_allows(opener, domain: str) -> tuple[bool, str]:
    url = f"https://{domain}/robots.txt"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with opener.open(req, timeout=8) as r:
            body, _ = _read_capped(r, 512 * 1024, time.monotonic() + 10)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"robots_http_{e.code}"
        return True, f"robots_http_{e.code}"
    except Exception as e:  # noqa: BLE001 - unreachable robots.txt = no rules
        return True, f"robots_unavailable:{type(e).__name__}"
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.decode("utf-8", errors="replace").splitlines())
    ok = rp.can_fetch(UA_TOKEN, f"https://{domain}/")
    return ok, "robots_ok" if ok else "robots_disallow"


def _err_name(e: BaseException) -> str:
    if isinstance(e, urllib.error.HTTPError):
        return f"http_{e.code}"
    if isinstance(e, urllib.error.URLError):
        r = e.reason
        if isinstance(r, BaseException):
            n = type(r).__name__
            if "SSL" in n or "Certificate" in n:
                return "tls"
            if isinstance(r, OSError) and getattr(r, "errno", None) in (-2, -3, -5):
                return "dns"
            if "gaierror" in n:
                return "dns"
            if isinstance(r, TimeoutError):
                return "timeout"
            return f"url:{n}"
        return "url:" + str(r)[:40]
    if isinstance(e, TimeoutError):
        return "timeout"
    if isinstance(e, ConnectionError):
        return "connection"
    return type(e).__name__


def fetch(item: dict, robots: bool) -> dict:
    opener = _opener()
    dom = item["domain"]
    url = f"https://{dom}/"
    row = {**item, "url": url, "final_url": None, "status": None, "content_type": None, "fetched_at": now(),
           "ok": False, "error": None, "bytes": 0, "truncated": False, "sha256": None, "html_gz": None}
    if robots:
        allowed, why = robots_allows(opener, dom)
        if not allowed:
            row["error"] = why
            return row
    deadline = time.monotonic() + DEADLINE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                                                   "Accept-Language": "pl,en;q=0.8"})
        with opener.open(req, timeout=DEADLINE) as r:
            row["status"] = r.status
            row["final_url"] = r.geturl()
            row["content_type"] = (r.headers.get("Content-Type") or "")[:120]
            ct = row["content_type"].lower()
            if "html" not in ct:
                row["error"] = "not_html"
                return row
            body, trunc = _read_capped(r, MAX_BYTES, deadline)
            if r.headers.get("Content-Encoding", "").lower() == "gzip":  # not requested; capped (gzip bombs)
                body = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(body, MAX_BYTES)
    except urllib.error.HTTPError as e:
        row["status"] = e.code
        row["final_url"] = e.geturl() if hasattr(e, "geturl") else None
        row["error"] = _err_name(e)
        return row
    except Exception as e:  # noqa: BLE001 - every failure is recorded by reason
        row["error"] = _err_name(e)
        return row
    row.update(bytes=len(body), truncated=trunc, sha256=hashlib.sha256(body).hexdigest(),
               html_gz=gzip.compress(body, 6), ok=row["status"] == 200 and len(body) > 0)
    if not row["ok"]:
        row["error"] = "empty_body" if not body else f"status_{row['status']}"
    return row


# ------------------------------------------------------------------------------------------ parts

def write_part(pages: Path, rows: list[dict]) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pages.mkdir(parents=True, exist_ok=True)
    n = len(list(pages.glob("pages-*.parquet")))
    dst = pages / f"pages-{n:03d}.parquet"
    schema = pa.schema([("domain", pa.string()), ("rank", pa.int64()), ("tld_group", pa.string()),
                        ("url", pa.string()), ("final_url", pa.string()), ("status", pa.int64()),
                        ("content_type", pa.string()), ("fetched_at", pa.string()), ("ok", pa.bool_()),
                        ("error", pa.string()), ("bytes", pa.int64()), ("truncated", pa.bool_()),
                        ("sha256", pa.string()), ("html_gz", pa.binary())])
    tmp = dst.with_suffix(".part")
    pq.write_table(pa.Table.from_pylist([{c: r.get(c) for c in COLUMNS} for r in rows], schema=schema), tmp,
                   row_group_size=100, compression="zstd")
    tmp.replace(dst)
    return dst


def scan_parts(pages: Path, columns: list[str]) -> list[dict]:
    import pyarrow.parquet as pq

    out: list[dict] = []
    for p in sorted(pages.glob("pages-*.parquet")):
        out.extend(pq.read_table(p, columns=columns).to_pylist())
    return out


def summarise(out: Path, tranco_meta: dict, sel_info: dict) -> dict:
    rows = scan_parts(out / "pages", ["domain", "tld_group", "ok", "error", "bytes", "status"])
    st: dict = {"attempted": len(rows), "by_group": {}}
    for g in ("pl", "global"):
        rs = [r for r in rows if r["tld_group"] == g]
        fails = Counter((r["error"] or "unknown").split(":")[0] for r in rs if not r["ok"])
        st["by_group"][g] = {"attempted": len(rs), "ok": sum(r["ok"] for r in rs), "failed": dict(fails.most_common()),
                             "html_bytes_ok": sum(r["bytes"] or 0 for r in rs if r["ok"])}
    st["ok"] = sum(v["ok"] for v in st["by_group"].values())
    st["failed_total"] = st["attempted"] - st["ok"]
    st["selection"] = sel_info
    st["tranco"] = {k: tranco_meta.get(k) for k in ("list_id", "download", "created_on", "retrieved_at")}
    st["tranco"]["configuration"] = tranco_meta.get("configuration")
    files = [p for p in sorted(out.rglob("*")) if p.is_file() and p.name not in ("SOURCE.md", "stats.json")
             and not p.name.startswith(".hydra-index")]
    st["files"] = [{"path": str(p.relative_to(out)), "bytes": p.stat().st_size, "sha256": sha256_file(p)}
                   for p in files]
    st["bytes_total"] = sum(f["bytes"] for f in st["files"])
    st["summarised_at"] = now()
    (out / "stats.json").write_text(json.dumps(st, indent=1) + "\n")
    (out / "SOURCE.md").write_text(source_md(st))
    return st


def source_md(st: dict) -> str:
    t = st["tranco"]
    lines = [
        "# web-legit-tranco (NOT organiser data)", "",
        "- what: homepages (https://<domain>/) of Tranco-listed domains, fetched once for background document-"
        "frequency statistics of gene values (TRAIN stream)",
        f"- seed list: Tranco list {t.get('list_id')} ({t.get('download')}), created {t.get('created_on')}, "
        f"retrieved {t.get('retrieved_at')}; citation: Le Pochat et al., Tranco, NDSS 2019, "
        f"https://tranco-list.eu/list/{t.get('list_id')}",
        "- Tranco terms: no licence of its own stated on tranco-list.eu; asks for citation of the list ID. Upstream "
        "providers: Cisco Umbrella (free of charge), Majestic (CC BY 3.0), Farsight, CrUX (CC BY-SA 4.0), "
        "Cloudflare Radar (CC BY-NC 4.0). The list is used only to choose which public homepages to fetch; it is not "
        "redistributed.",
        "- pages: third-party content, fetched once (UA `Mozilla/5.0 (compatible; research-crawler)`, robots.txt "
        "checked for `/`, 15 s deadline, 2 MB cap, concurrency 16, no JS, no cookies). Never redistributed.",
        "- verdict: SHIP-SAFE: derived hashed document-frequency statistics only; page content never redistributed; "
        "operator instruction 2026-11-21",
        "- method: `scripts/fetch_web_legit.py` (branch feat/train-data); adapter `hydra/adapters/datasets/"
        "web_legit.py` (`ext:web-legit-tranco`)",
        f"- summarised: {st['summarised_at']}",
        "- DO NOT commit or redistribute. Aggregates only in public material.", "",
        "## Counts", "",
        f"attempted {st['attempted']}, ok (200 + text/html) {st['ok']}, failed {st['failed_total']}", "",
        "| group | attempted | ok | HTML MB (ok) | failed by reason |", "|---|---|---|---|---|"]
    for g, v in st["by_group"].items():
        lines.append(f"| {g} | {v['attempted']} | {v['ok']} | {v['html_bytes_ok'] / 1e6:.1f} | "
                     f"{', '.join(f'{k} {n}' for k, n in v['failed'].items())} |")
    lines += ["", f"Selection: {json.dumps(st['selection'])}", "", "## Files", "",
              "| file | bytes | sha256 |", "|---|---|---|"]
    lines += [f"| {f['path']} | {f['bytes']} | {f['sha256']} |" for f in st["files"]]
    return "\n".join(lines) + "\n"


def lock_entry(st: dict) -> dict:
    t = st["tranco"]
    base = f"crawl:{t.get('list_id')}"
    return {
        "name": "web-legit-tranco", "dir": "web-legit-tranco", "homepage": "https://tranco-list.eu",
        "licence": "Tranco list: no licence of its own stated on tranco-list.eu, citation of the list ID requested; "
                   "upstream rankings Cisco Umbrella (free of charge), Majestic (CC BY 3.0), Farsight, CrUX "
                   "(CC BY-SA 4.0), Cloudflare Radar (CC BY-NC 4.0). Pages: third-party content of each site",
        "verdict": "SHIP-SAFE",
        "verdict_note": "derived hashed document-frequency statistics only; page content never redistributed; "
                        "operator instruction 2026-11-21",
        "citation": f"Le Pochat et al., Tranco: A Research-Oriented Top Sites Ranking Hardened Against Manipulation, "
                    f"NDSS 2019; list https://tranco-list.eu/list/{t.get('list_id')}",
        "use": f"background (legit) corpus for the TRAIN document-frequency prior: {st['ok']} homepages fetched once "
               f"(pl {st['by_group']['pl']['ok']}, global {st['by_group']['global']['ok']}) of {st['attempted']} "
               "attempted; no labels",
        "source": {"kind": "crawl", "script": "scripts/fetch_web_legit.py", "list_id": t.get("list_id"),
                   "revision": f"tranco:{t.get('list_id')} {str(t.get('created_on'))[:10]}"},
        "fetch": False,
        "files": [{"path": f["path"], "url": t.get("download") if f["path"].startswith("tranco-") and
                   f["path"].endswith(".csv") else base, "bytes": f["bytes"], "sha256": f["sha256"]}
                  for f in st["files"]],
        "bytes_total": st["bytes_total"], "retrieved": "2026-11-21",
    }


def write_lock(entry: dict, paths: list[Path]) -> None:
    for p in paths:
        lock = json.loads(p.read_text())
        lock["datasets"] = [d for d in lock["datasets"] if d["name"] != entry["name"]] + [entry]
        p.write_text(json.dumps(lock, indent=1, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=None, help="default $HYDRA_DATA_DIR/external-big")
    ap.add_argument("--pl", type=int, default=3000)
    ap.add_argument("--global", dest="n_global", type=int, default=2000)
    ap.add_argument("--jobs", type=int, default=16)
    ap.add_argument("--limit", type=int, default=None, help="fetch at most N new domains (smoke test)")
    ap.add_argument("--no-robots", action="store_true")
    ap.add_argument("--summary-only", action="store_true")
    ap.add_argument("--write-lock", action="store_true",
                    help="add/replace the entry in the repo lock and <root>/datasets.lock.json")
    ap.add_argument("--max-gb", type=float, default=8.0, help="address-space limit of this process (RLIMIT_AS)")
    a = ap.parse_args()
    import resource

    import pyarrow as pa

    pa.set_cpu_count(2)  # pyarrow sizes its pools by core count (128 on the server); keep it small
    pa.set_io_thread_count(2)
    lim = int(a.max_gb * 1024**3)
    resource.setrlimit(resource.RLIMIT_AS, (lim, lim))
    out = (Path(a.root) if a.root else data_root()) / "web-legit-tranco"
    out.mkdir(parents=True, exist_ok=True)
    csv_path, meta = get_tranco(out)
    todo, sel_info = select_domains(csv_path, a.pl, a.n_global)
    (out / "selection.json").write_text(json.dumps({"info": sel_info, "domains": todo}, indent=0) + "\n")
    print(f"tranco {meta['list_id']}: selected {sel_info}", flush=True)
    if not a.summary_only:
        done = {r["domain"] for r in scan_parts(out / "pages", ["domain"])}
        todo = [x for x in todo if x["domain"] not in done][: a.limit]
        print(f"already fetched {len(done)}, to fetch {len(todo)}", flush=True)
        buf: list[dict] = []
        n = ok = 0
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(16, a.jobs)) as ex:
            for i in range(0, len(todo), PART_ROWS):  # one window per part file: bounded memory
                for f in as_completed([ex.submit(fetch, x, not a.no_robots) for x in todo[i:i + PART_ROWS]]):
                    r = f.result()
                    buf.append(r)
                    n += 1
                    ok += r["ok"]
                    if n % 100 == 0:
                        print(f"{n}/{len(todo)} ok {ok} ({time.monotonic() - t0:.0f} s)", flush=True)
                write_part(out / "pages", buf)
                buf = []
        if buf:
            write_part(out / "pages", buf)
    st = summarise(out, meta, sel_info)
    if a.write_lock:
        repo_lock = Path(__file__).resolve().parents[1] / "hydra" / "adapters" / "datasets" / "datasets.lock.json"
        write_lock(lock_entry(st), [repo_lock, out.parent / "datasets.lock.json"])
    print(json.dumps({k: st[k] for k in ("attempted", "ok", "failed_total", "by_group", "bytes_total")}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
