"""CERT Polska public analysis of a fake-ad toll-fraud operation (2026-09-23,
https://cert.pl/posts/2026/09/analiza-tollfraud/): Meta ads under fake profiles promoting Google Play apps that
load toll-fraud payloads. The 898 ad files mentioned in the post are NOT published; the post publishes tables and
indicators of compromise only (Meta profile ids and Ad Library ids, app names, package names, APK/DEX/certificate
SHA-256, rule/C2 hosts and IPs (defanged), TLS certificate SHA-256, Polish billing domains).

This adapter turns the saved post HTML (`post-pl-*.html`) into a small reference corpus, parsed with regular
expressions only (no rendering, no scripts). One incident per:
- advertised app (`app:<package or name>`): `indicators.txt` (package, rule endpoint, C2, sha256 values, ad
  count, promoting profile ids, Ad Library ids) and `url.txt` = `http://<rule host>/` (synthesised) when known;
- advertiser profile (`profile:<Meta profile id>`): `indicators.txt` (profile id, promoted apps and packages);
- API / infrastructure host (`infra:<host>`): `indicators.txt` (host, IPs, TLS certificate sha256).
Every incident carries the same campaign label, `ext:certpl-tollfraud-2026:operation:tollfraud-2026`
(label kind `operation`: CERT Polska attributes all of it to one operation); `group_label` = app|profile|infra.

LEAKAGE-RISK: the publisher (CERT Polska, NASK) is the task organiser, so this material may overlap the NASK
sample or the hidden test set. EVAL/reference only: never fold it into shipped priors or thresholds.
Licence: no open licence ("(c) 2026 NASK" footer) → EVAL-ONLY. Never redistributed.
"""

from __future__ import annotations

import html as htmllib
import re
from collections.abc import Iterator
from pathlib import Path

from hydra.adapters.datasets._common import (
    URL_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
    slug,
)

INFO = DatasetInfo(
    name="certpl-tollfraud-2026", title="CERT Polska toll-fraud fake-ad operation (2026-09): IOC reference corpus",
    homepage="https://cert.pl/posts/2026/09/analiza-tollfraud/",
    licence="no open licence ((c) 2026 NASK); public analysis post", verdict="EVAL-ONLY",
    modality="indicators+url", label_kind="operation", dir="certpl-tollfraud-2026",
    citation="CERT Polska, 'Fałszywe reklamy i złośliwe aplikacje - analiza operacji toll fraud', 2026-09-23",
)
LEAKAGE_RISK = True
OPERATION = "operation:tollfraud-2026"
IND_FILE = "indicators.txt"

SHA_RE = re.compile(r"\b[0-9a-f]{64}\b")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOST_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+[a-z]{2,}\b")
PKG_RE = re.compile(r"\b[a-z][a-z0-9_]*(?:\.[a-z0-9_]+){2,}\b")
ADLIB_RE = re.compile(r"\b\d{15,17}\b")


def refang(s: str) -> str:
    return s.replace("[.]", ".").replace("hxxp", "http")


def _text(fragment: str) -> str:
    t = re.sub(r"<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmllib.unescape(t)).strip()


def _tables(page: str) -> list[list[list[str]]]:
    out = []
    for tb in re.findall(r"<table.*?</table>", page, re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tb, re.S | re.I):
            rows.append([_text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)])
        if rows:
            out.append(rows)
    return out


def _ioc_blocks(page: str) -> dict[str, list[str]]:
    """`<h3>App</h3><ul><li>...</li></ul>` blocks after the 'Wskaźniki kompromitacji' heading."""
    i = page.find("Wskaźniki kompromitacji")
    if i < 0:
        return {}
    seg = page[i:]
    blocks: dict[str, list[str]] = {}
    cur = None
    for m in re.finditer(r"<(h3|li|p)[^>]*>(.*?)</\1>", seg, re.S | re.I):
        tag, body = m.group(1).lower(), _text(m.group(2))
        if tag == "h3":
            cur = body
            blocks.setdefault(cur, [])
        elif cur is not None and body:
            blocks[cur].append(body)
    return blocks


def _find(tables: list[list[list[str]]], first: str) -> list[list[str]]:
    for t in tables:
        if t[0] and t[0][0].lower().startswith(first.lower()):
            return t[1:]
    return []


def _find_all(tables: list[list[list[str]]], header: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for t in tables:
        if [h.lower() for h in t[0][: len(header)]] == [h.lower() for h in header]:
            rows.extend(t[1:])
    return rows


def _int(s: str) -> int | None:
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def parse(page: str) -> dict:
    """Structured records from the post: apps, profiles, infra hosts, and the flat IOC sets."""
    tables = _tables(page)
    blocks = _ioc_blocks(page)
    inne = re.compile(r"([A-Z][A-Za-z0-9 ]+?) - ([a-z][a-z0-9_.]+\.[a-z0-9_]+)"
                      r"(?: - (\d+) reklam)?\s+bazowy APK: ([0-9a-f]{64})")
    # pass 1: (display name -> packages) from every place that pairs a name with a package
    named: dict[str, set[str]] = {}
    for r in _find_all(tables, ["Aplikacja", "Pakiet"]):
        if len(r) >= 2:
            named.setdefault(r[0], set()).add(r[1])
    for name, items in blocks.items():
        if name.lower().startswith("inne reklamowane"):
            for m in inne.finditer(refang(" ".join(items))):
                named.setdefault(m.group(1).strip(), set()).add(m.group(2))
        for x in items:
            if x.lower().startswith("pakiet:"):
                named.setdefault(name, set()).add(x.split(":", 1)[1].strip())
    apps: dict[str, dict] = {}

    def app(name: str, package: str | None = None) -> dict:
        """One record per package; a name-only mention resolves to its package when the name is unique."""
        name = name.strip()
        if package is None and PKG_RE.fullmatch(name):
            package = name
        if package is None and len(named.get(name, ())) == 1:
            package = next(iter(named[name]))
        key = package or f"name:{norm_label(name) or name}"
        a = apps.setdefault(key, {"name": name, "packages": set(), "sha256": set(), "ips": set(),
                                  "hosts": set(), "adlib": set(), "profiles": set()})
        if package:
            a["packages"].add(package)
            if PKG_RE.fullmatch(a["name"]) and not PKG_RE.fullmatch(name):
                a["name"] = name
        return a

    for r in _find(tables, "Aplikacja lub pakiet"):
        if len(r) >= 2:
            a = app(r[0])
            a["ads"] = _int(r[1])
            a["status"] = r[2] if len(r) > 2 else None
    for r in _find_all(tables, ["Aplikacja", "Pakiet", "Domena reguł"]):
        app(r[0], r[1])["hosts"].update(HOST_RE.findall(refang(r[2]).lower()))
    for r in _find_all(tables, ["Aplikacja", "Pakiet", "Reklamy"]):
        a = app(r[0], r[1])
        a.setdefault("ads", _int(r[2]))
    profiles: dict[str, dict] = {}
    for r in _find(tables, "Wyświetlana nazwa profilu"):
        if len(r) >= 4 and r[1].isdigit():
            promoted = [x.strip() for x in r[2].split(",") if x.strip()]
            profiles[r[1]] = {"name": r[0], "apps": promoted, "ads": _int(r[3])}
            for p in promoted:
                app(p)["profiles"].add(r[1])
    for name, items in blocks.items():
        if name.lower().startswith("inne reklamowane"):
            for m in inne.finditer(refang(" ".join(items))):
                a = app(m.group(1), m.group(2))
                a["sha256"].add(m.group(4))
                if m.group(3):
                    a.setdefault("ads", int(m.group(3)))
            continue
        pkg = next((x.split(":", 1)[1].strip() for x in items if x.lower().startswith("pakiet:")), None)
        if pkg is None:
            continue
        a = app(name, pkg)
        for x in items:
            rx = refang(x)
            low = rx.lower()
            if "biblioteki reklam" in low:
                a["adlib"].update(ADLIB_RE.findall(rx))
            elif not low.startswith("pakiet:"):
                a["sha256"].update(SHA_RE.findall(low))
                a["ips"].update(IP_RE.findall(low))
                a["hosts"].update(h for h in HOST_RE.findall(low) if not IP_RE.fullmatch(h))
    infra: dict[str, dict] = {}
    for t in tables:
        if t[0] and t[0][0].lower() == "host api":
            for r in t[1:]:
                host = refang(r[0]).lower()
                rec = infra.setdefault(host, {"ips": set(), "sha256": set()})
                for c in r[1:]:
                    rec["ips"].update(IP_RE.findall(refang(c)))
                    rec["sha256"].update(SHA_RE.findall(c.lower()))
    flat = refang(_text(page)).lower()
    iocs = {
        "sha256": set(SHA_RE.findall(flat)),
        "package": {p for a in apps.values() for p in a["packages"]},
        "host": {h for a in apps.values() for h in a["hosts"]} | set(infra),
        "ip": {ip for a in apps.values() for ip in a["ips"]} | {ip for r in infra.values() for ip in r["ips"]},
        "meta_profile_id": set(profiles),
        "ad_library_id": {x for a in apps.values() for x in a["adlib"]},
    }
    return {"apps": apps, "profiles": profiles, "infra": infra, "iocs": iocs}


def _page(root: Path) -> Path | None:
    return next(iter(sorted(root.glob("post-pl-*.html"), reverse=True)), None)


def _parsed(root: Path) -> dict:
    p = _page(root)
    return parse(p.read_text(encoding="utf-8", errors="replace")) if p else {"apps": {}, "profiles": {},
                                                                              "infra": {}, "iocs": {}}


def iocs(root: Path) -> dict[str, set[str]]:
    """Flat indicator sets (for aggregate overlap checks; values are never printed)."""
    return _parsed(root)["iocs"]


def index(root: Path) -> list[Row]:
    d = _parsed(root)
    rows = []
    for key in sorted(d["apps"]):
        rows.append(Row(f"app-{slug(key.removeprefix('name:'), 80)}", OPERATION, "app", {"kind": "app", "key": key}))
    for pid in sorted(d["profiles"]):
        rows.append(Row(f"profile-{pid}", OPERATION, "profile", {"kind": "profile", "key": pid}))
    for host in sorted(d["infra"]):
        rows.append(Row(f"infra-{slug(host, 80)}", OPERATION, "infra", {"kind": "infra", "key": host}))
    return rows


def _lines(pairs: list[tuple[str, object]]) -> bytes:
    out = []
    for k, v in pairs:
        vals = sorted(v) if isinstance(v, set | list) else [v]
        out.extend(f"{k}: {x}" for x in vals if x not in (None, ""))
    return ("\n".join(out) + "\n").encode()


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    d = _parsed(root)
    pkg_of: dict[str, set[str]] = {}
    for a in d["apps"].values():
        pkg_of.setdefault(a["name"], set()).update(a["packages"])
    for r in rows:
        kind, key = r.extra["kind"], r.extra["key"]
        files: dict[str, bytes | Path] = {}
        meta: dict[str, object] = {"kind": kind, "lang": "pl", "leakage_risk": True, "label_kind": INFO.label_kind}
        if kind == "app":
            a = d["apps"][key]
            files[IND_FILE] = _lines([("app", a["name"]), ("package", a["packages"]), ("host", a["hosts"]),
                                      ("ip", a["ips"]), ("sha256", a["sha256"]), ("ad_library_id", a["adlib"]),
                                      ("advertiser_profile_id", a["profiles"]), ("ads", a.get("ads")),
                                      ("analysis_status", a.get("status"))])
            rule = next((h for h in sorted(a["hosts"]) if h.startswith("api.")), None)
            if rule:
                files[URL_FILE] = f"http://{rule}/\n".encode()
                meta["url_synthesised"] = True
            meta |= {"app": a["name"], "ads": a.get("ads")}
        elif kind == "profile":
            p = d["profiles"][key]
            pk = {x for name in p["apps"] for x in pkg_of.get(name, set())}
            files[IND_FILE] = _lines([("advertiser_profile_id", key), ("promoted_app", p["apps"]),
                                      ("package", pk), ("ads", p.get("ads"))])
            meta |= {"ads": p.get("ads")}
        else:
            rec = d["infra"][key]
            files[IND_FILE] = _lines([("host", key), ("ip", rec["ips"]), ("tls_sha256", rec["sha256"])])
            files[URL_FILE] = f"http://{key}/\n".encode()
            meta["url_synthesised"] = True
        yield ExtIncident(dataset=INFO.name, key=r.key, files=files, roles={IND_FILE: "text"},
                          label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group), meta=meta)
