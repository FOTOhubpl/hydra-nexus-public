"""PhishDecloaker phishing-kit collection (Zenodo 10.5281/zenodo.11228974, `phishing-kits-dataset.zip`, CC BY 4.0).

100 defanged phishing kits as `src/kit1` .. `src/kit100` inside one ZIP (server-side PHP plus the HTML/CSS/JS/
images of the imitated brand page). Kits are hostile code and are NEVER executed or served: the archive is read
with `zipfile` only, member paths are sanitised (no absolute path, no `..`), and large members are skipped.

Brand per kit: the Zenodo record "Phishing Kit Dataset" lists a brand for each kit, in order; that list is copied
verbatim into `BRANDS`, so `kit<N>` maps to `BRANDS[N-1]` (checked 2026-11-21: 92/100 kits mention their own
listed brand in their text; distinctive brands such as Chronopost / Intesa Sanpaolo / WalletConnect occur in no
other kit, so the positional mapping holds). This is the grouping the task asks for (`label_kind="kit"`): the
label groups the few kits that imitate the same brand (6 brands have 2-3 kits).

One kit = one incident, with INERT derived artifacts only (never raw PHP):
- `landing.html`: the kit's main page. An `index` HTML file (shallowest), else the largest `.html/.htm/.xhtml`;
  if the kit ships only PHP, the largest `index*.php` (else largest PHP) with every `<?php ... ?>` block removed,
  so nothing executable is left (`meta["landing_from"]="php-static"`).
- `assets/<path>`: the kit's other HTML pages and its static files (images, CSS, JS), capped. Role landing_asset
  (kit-level asset-reuse ground truth). Vendored libraries (phpmailer/jquery/bootstrap) are skipped.
- `file_tree.txt` (role text): member paths and sizes - the kit's template/asset file set, a sharing signal.
- `kit_indicators.txt` (role text): inert exfiltration indicators found by regex over the kit's server-side code
  (`.php/.inc/.ini/.js/.htaccess`): Telegram bot ids and chat ids, `mail()` / hard-coded recipient addresses,
  outbound `curl` / `file_get_contents` / `fopen` targets and their hosts, local drop files, author tags. These
  are threat-intel genes (where stolen data would be sent). The brand is NOT written into any file (it is the
  label - kept out of content so the grouping never leaks into the genes).

Labels: `label = ext:phishdecloaker-kits:<brand>`, `group_label = ext:phishdecloaker-kits:phish_kit`.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from hydra.adapters.datasets._common import (
    HTML_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
)

INFO = DatasetInfo(
    name="phishdecloaker-kits",
    title="PhishDecloaker phishing kits (inert page, assets, file tree, exfil indicators; brand per kit)",
    homepage="https://zenodo.org/records/11228974",
    licence="CC BY 4.0 (Zenodo record)", verdict="SHIP-SAFE", modality="kit html+assets+indicators",
    label_kind="kit", dir="phishdecloaker-kits",
    citation="PhishDecloaker phishing-kits-dataset, Zenodo 10.5281/zenodo.11228974",
)

# Zenodo record 11228974 "Phishing Kit Dataset": entry N (1-based) is the brand of src/kit<N> (verbatim list).
BRANDS = (
    "Microsoft", "Banco de Oro", "Microsoft OneDrive", "Deutsche Kreditbank", "Adobe Acrobat", "N26", "Absa Group",
    "DHL", "Microsoft", "Correos", "Kempinski Summerland Hotel & Resort Beirut", "Vantage West Credit Union",
    "NetFlix", "Agencia Tributaria", "Square", "Chronopost", "PayPal", "American Express", "Allegro", "LinkedIn",
    "virtru", "Citibank", "AOL", "Credit Agricole", "Mercado Pago",
    "Universite de Pau et des Pays de l Adour (UPPA)", "Fifth Third Bank", "Columbia Bank", "Alibaba Mail",
    "Microsoft OneDrive", "Intesa Sanpaolo", "Santander", "America First Credit Union", "Barclays", "Interac",
    "USPS", "Wells Fargo", "Yahoo", "XFINITY", "Berliner Sparkasse", "OneDrive", "Standard Bank", "Wells Fargo",
    "aruba.it", "Bancolombia", "Caisse d Epargne", "DubaiPay", "Chase Bank", "M&T Bank", "Postmaster",
    "Volksbanken Raiffeisenbanken", "Facebook", "Huntington Bank", "Commonwealth Bank of Australia", "Orange",
    "shopify", "Google Drive", "WalletConnect", "Meritrust Credit Union", "Credit Agricole", "Desjardins",
    "Postbank", "Dropbox", "DocuSign", "dpdgroup", "L Assurance Maladie", "Adobe Acrobat", "Global Sources",
    "Microsoft Excel", "SFR", "FedEx", "Citibank", "Royal Credit Union", "GoDaddy", "ADP",
    "International Card Services", "Israeli Post", "UNI Financial Cooperation", "TD Bank", "ATB Mobile", "HSBC",
    "Bank of Montreal", "RBC Royal Bank", "IONOS", "AlaskaUSA Federal Credit Union", "French Government", "UOL SAC",
    "Banco Itau Paraguay", "Amazon", "Apple", "AT&T", "Australian Government", "Bank of America", "BNP Paribas",
    "eBay", "ING Group", "Instagram", "MetaMask", "SingTel", "Societe Generale",
)
_CANON = {"onedrive": "microsoft onedrive"}  # record spells this brand two ways

PAGE_EXT = (".html", ".htm", ".xhtml")
PHP_EXT = (".php", ".phtml", ".php5", ".php7", ".inc")
STATIC_EXT = (".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp")
CODE_EXT = (*PHP_EXT, ".ini", ".js")
VENDOR_RE = re.compile(r"(?:^|/)(?:vendor|phpmailer|node_modules|bootstrap[^/]*|jquery[^/]*)(?:/|$)", re.I)
MAX_PAGE = 2 * 1024 * 1024
MAX_ASSET_BYTES = 5 * 1024 * 1024
MAX_READ_BYTES = 20 * 1024 * 1024
MAX_ASSETS = 120
MAX_PER_KIND = 40

PHP_BLOCK_RE = re.compile(rb"<\?(?:php|=)?.*?(?:\?>|\Z)", re.S | re.I)
HTMLISH_RE = re.compile(rb"<(?:!doctype|html|head|body|form|div|input|title)\b", re.I)
LANG_RE = re.compile(rb"<html[^>]*\blang\s*=\s*[\"\x27]?([A-Za-z]{2})\b", re.I)

EMAIL_RE = re.compile(rb"[A-Za-z0-9][A-Za-z0-9._%+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
TG_TOKEN_RE = re.compile(rb"(?<![\d])(\d{8,10}):(AA[A-Za-z0-9_-]{30,40})")
TG_CHAT_RE = re.compile(rb"chat_?id[\"\x27]?\s*(?:=>|=|:|,)\s*[\"\x27]?(-?\d{5,16})", re.I)
RECIPIENT_RE = re.compile(
    rb"\$(?:to|send|sendto|recipient|receiver|yourmail|youremail|mailto|email_?to|admin_?mail|result_?mail|"
    rb"my_?mail|mail_?to)\w*\s*=\s*[\"\x27]([^\"\x27\r\n]{3,300})[\"\x27]", re.I)
URL_RE = re.compile(rb"https?://[A-Za-z0-9._~%-]+(?:/[A-Za-z0-9._~%/?#=&+-]*)?")
DROP_RE = re.compile(
    rb"(?:file_put_contents|fopen|fwrite)\s*\(\s*[\"\x27]([A-Za-z0-9_./-]+\.(?:txt|log|html?|dat|json))[\"\x27]", re.I)
AUTHOR_RE = re.compile(
    rb"(?:created\s+by|coded\s+by|made\s+by|result\s+by|dev\s+by)\s*[-:]*\s*([^\r\n<>\"\x27]{2,40})", re.I)
IGNORE_EMAIL = (b"@example.", b"@sentry.", b"@2x", b"@media", b"@font", b"@domain.", b"@email.")


def _zip(root: Path) -> Path | None:
    return next(iter(sorted(root.glob("*.zip"))), None)


def _canon_brand(name: str | None) -> str | None:
    b = norm_label(name)
    return None if b is None else _CANON.get(b, b)


def _kits(zf: zipfile.ZipFile) -> dict[str, list[zipfile.ZipInfo]]:
    out: dict[str, list[zipfile.ZipInfo]] = {}
    for i in zf.infolist():
        parts = i.filename.split("/")
        if len(parts) >= 2 and parts[0] == "src" and parts[1] and not i.is_dir():
            out.setdefault(parts[1], []).append(i)
    return out


def index(root: Path) -> list[Row]:
    z = _zip(root)
    if z is None:
        return []
    with zipfile.ZipFile(z) as zf:
        kits = sorted(_kits(zf), key=lambda k: (int(k[3:]) if k[3:].isdigit() else 0, k))
    out: list[Row] = []
    for kit in kits:
        n = int(kit[3:]) if kit.lower().startswith("kit") and kit[3:].isdigit() else 0
        raw = BRANDS[n - 1] if 1 <= n <= len(BRANDS) else None
        out.append(Row(kit, _canon_brand(raw), "phish_kit", {"kit": kit, "brand_raw": raw}))
    return out


def _rel(info: zipfile.ZipInfo, kit: str) -> str:
    rel = info.filename.split(f"src/{kit}/", 1)[-1]
    p = PurePosixPath(rel)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        return ""
    return p.as_posix()


def _strip_php(data: bytes) -> bytes:
    return PHP_BLOCK_RE.sub(b"", data)


def _pick_landing(members: dict[str, zipfile.ZipInfo],
                  zf: zipfile.ZipFile) -> tuple[str | None, bytes | None, str | None]:
    htmls = [r for r in members if r.lower().endswith(PAGE_EXT) and not VENDOR_RE.search(r)
             and members[r].file_size <= MAX_PAGE]
    if htmls:
        idx = [r for r in htmls if PurePosixPath(r).name.lower().startswith("index")]
        rel = min(idx or htmls, key=lambda r: (r.count("/"), -members[r].file_size, r))
        return rel, zf.read(members[rel]), None
    phps = [r for r in members if r.lower().endswith(".php") and not VENDOR_RE.search(r)
            and members[r].file_size <= MAX_PAGE]
    if phps:
        idx = [r for r in phps if PurePosixPath(r).name.lower().startswith("index")]
        for rel in sorted(idx or phps, key=lambda r: -members[r].file_size):
            static = _strip_php(zf.read(members[rel]))
            if HTMLISH_RE.search(static):
                return rel, static, "php-static"
    return None, None, None


def _dedup(values: Iterator[bytes], *, skip: tuple[bytes, ...] = ()) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        if skip and any(s in v.lower() for s in skip):
            continue
        t = v.decode("utf-8", "replace").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= MAX_PER_KIND:
            break
    return out


def _indicators(blob: bytes) -> dict[str, list[str]]:
    urls = [m.group(0) for m in URL_RE.finditer(blob)]
    hosts = (u.split(b"//", 1)[1].split(b"/", 1)[0] for u in urls)
    emails = list(EMAIL_RE.finditer(blob))
    return {
        "telegram_bot": _dedup(m.group(0) for m in TG_TOKEN_RE.finditer(blob)),
        "telegram_chat_id": _dedup(m.group(1) for m in TG_CHAT_RE.finditer(blob)),
        "exfil_email": _dedup((m.group(0) for m in emails), skip=IGNORE_EMAIL)
        + [r for r in _dedup(m.group(1) for m in RECIPIENT_RE.finditer(blob)) if "@" in r],
        "outbound_host": _dedup(iter(hosts)),
        "outbound_url": _dedup(iter(urls)),
        "local_drop_file": _dedup(m.group(1) for m in DROP_RE.finditer(blob)),
        "author_tag": _dedup(m.group(1) for m in AUTHOR_RE.finditer(blob)),
    }


def _indicators_text(ind: dict[str, list[str]]) -> bytes:
    lines = ["# phishing-kit exfiltration indicators (inert; regex-extracted, never executed)"]
    for kind in ("telegram_bot", "telegram_chat_id", "exfil_email", "outbound_host", "outbound_url",
                 "local_drop_file", "author_tag"):
        vals = ind.get(kind) or []
        if vals:
            lines.append(f"[{kind}] ({len(vals)})")
            lines.extend(f"  {v}" for v in vals)
    return ("\n".join(lines) + "\n").encode("utf-8", "replace")


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    z = _zip(root)
    if z is None:
        return
    by_kit = {r.key: r for r in rows}
    with zipfile.ZipFile(z) as zf:
        kits = _kits(zf)
        for kit in sorted((k for k in kits if k in by_kit),
                          key=lambda k: (int(k[3:]) if k[3:].isdigit() else 0, k)):
            r = by_kit[kit]
            members: dict[str, zipfile.ZipInfo] = {}
            for info in kits[kit]:
                if info.file_size > MAX_READ_BYTES:
                    continue
                rel = _rel(info, kit)
                if rel:
                    members[rel] = info
            files: dict[str, bytes | Path] = {}
            roles: dict[str, str] = {"file_tree.txt": "text", "kit_indicators.txt": "text"}
            meta: dict[str, object] = {"kit": kit, "brand_raw": r.extra.get("brand_raw"),
                                       "n_members": len(members), "label_kind": INFO.label_kind}

            landing_rel, landing_bytes, how = _pick_landing(members, zf)
            if landing_bytes is not None:
                files[HTML_FILE] = landing_bytes
                meta["main_page"] = landing_rel
                if how:
                    meta["landing_from"] = how
                lm = LANG_RE.search(landing_bytes[:4000])
                if lm:
                    meta["lang"] = lm.group(1).decode().lower()

            n_assets = 0
            for rel in sorted(members):
                if rel == landing_rel or n_assets >= MAX_ASSETS or VENDOR_RE.search(rel):
                    continue
                info = members[rel]
                low = rel.lower()
                if low.endswith(PAGE_EXT):
                    files[f"assets/{rel}"] = zf.read(info)
                    n_assets += 1
                elif low.endswith(STATIC_EXT) and info.file_size <= MAX_ASSET_BYTES:
                    files[f"assets/{rel}"] = zf.read(info)
                    n_assets += 1

            tree = "\n".join(f"{rel}\t{members[rel].file_size}" for rel in sorted(members))
            files["file_tree.txt"] = (tree + "\n").encode()

            code = [zf.read(members[rel]) for rel in sorted(members)
                    if (rel.lower().endswith(CODE_EXT) or PurePosixPath(rel).name.lower() == ".htaccess")
                    and members[rel].file_size <= MAX_ASSET_BYTES and not VENDOR_RE.search(rel)]
            ind = _indicators(b"\n".join(code))
            n_ind = sum(len(v) for v in ind.values())
            if n_ind:
                files["kit_indicators.txt"] = _indicators_text(ind)
                meta["indicator_counts"] = {k: len(v) for k, v in ind.items() if v}
            meta["has_landing"] = HTML_FILE in files
            meta["n_indicators"] = n_ind

            yield ExtIncident(
                dataset=INFO.name, key=kit, files=files, roles=roles,
                label=prefixed(INFO.name, r.label), group_label=prefixed(INFO.name, r.group), meta=meta,
            )
