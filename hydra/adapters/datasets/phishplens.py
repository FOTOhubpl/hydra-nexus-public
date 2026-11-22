"""PhishPLENS (Zenodo 10.5281/zenodo.23579138, CC BY 4.0): Polish-English university phishing e-mail benchmark,
755 pseudonymised `.eml` messages in three subsets (`university` = institutional mail and incident reports,
`private` = student mailboxes, `crafted` = authored legitimate mail), each with a three-class label
(phishing / spam / ham) and seven explanation attributes (style, sender / link / attachment suspicion,
impersonation likelihood, interaction required). Profile (2026-11-21): 461 Polish messages (249 phishing,
45 spam, 167 ham).

Layout (zip extracted to `extracted/`): `<subset>/labels_<subset>.csv` (`file, classification, language, style,
...`) and `<subset>/<PL|EN|other languages>/<NNN>.eml`. One message = one incident with `ad_text.txt` (role
text): `Subject:` and `From:` lines, a blank line, then the text/plain body (an HTML-only body is reduced to text
by dropping tags; the HTML is never rendered). Malicious URLs and attachments were removed by the authors, so no
`url.txt`. The set has no campaign / brand field: `label = None`, `group_label = ext:phishplens:<class>`.
`meta.lang` = the dataset's language code (`pl` for Polish). Mail is third-party content (pseudonymised):
server-side only, aggregates only in public material.
"""

from __future__ import annotations

import csv
import email
import html
import re
from collections.abc import Iterator
from email import policy
from email.message import EmailMessage
from pathlib import Path

from hydra.adapters.datasets._common import (
    AD_TEXT_FILE,
    DatasetInfo,
    ExtIncident,
    Row,
    norm_label,
    prefixed,
)

INFO = DatasetInfo(
    name="phishplens", title="PhishPLENS Polish-English university phishing e-mails (3-class + explanations)",
    homepage="https://zenodo.org/records/23579138",
    licence="CC BY 4.0 (Zenodo record)", verdict="SHIP-SAFE", modality="email text", label_kind="none",
    dir="phishplens", citation="PhishPLENS, Zenodo 10.5281/zenodo.23579138 (2026)",
)
ATTRS = ("style", "sender_suspicion_level", "link_suspicion_level", "attachments_suspicion_level",
         "impersonation_likelihood", "interaction_required", "num_attachments")
MAX_BODY = 200_000
TAG_RE = re.compile(r"<(script|style)\b.*?</\1\s*>|<[^>]+>", re.S | re.I)
WS_RE = re.compile(r"[ \t\r\f\v\xa0]+")
NL_RE = re.compile(r"\n\s*\n\s*\n+")


def _labels(root: Path) -> list[Path]:
    return sorted(root.rglob("labels_*.csv"))


def index(root: Path) -> list[Row]:
    out = []
    for lab in _labels(root):
        subset = lab.stem.removeprefix("labels_")
        emls = {p.name: p for p in sorted(lab.parent.rglob("*.eml"))}
        with lab.open(newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                name = (r.get("file") or "").strip()
                p = emls.get(name)
                if p is None:
                    continue
                cls = norm_label(r.get("classification")) or "unknown"
                lang = norm_label(r.get("language"))
                extra = {"lang": lang, "subset": subset, "path": p.relative_to(root).as_posix(),
                         **{k: _attr(r.get(k)) for k in ATTRS}}
                out.append(Row(f"{subset}/{name.removesuffix('.eml')}", None, cls, extra))
    return out


def _attr(v: str | None) -> str | None:
    t = (v or "").strip().lower()
    return t or None  # "none" / "none_present" are real attribute values here, kept as is


def where(lang: str | None = None, subset: str | None = None, cls: str | None = None):
    if not (lang or subset or cls):
        return None
    return lambda r: ((lang is None or r.extra.get("lang") == lang) and (subset is None or r.extra.get("subset") ==
                      subset) and (cls is None or r.group == cls))


def html_to_text(s: str) -> str:
    s = re.sub(r"<br\s*/?>|</p\s*>|</div\s*>|</tr\s*>|</h\d\s*>|</li\s*>", "\n", s, flags=re.I)
    s = html.unescape(TAG_RE.sub(" ", s))
    lines = [WS_RE.sub(" ", ln).strip() for ln in s.split("\n")]
    return NL_RE.sub("\n\n", "\n".join(lines)).strip()


def message_text(raw: bytes) -> str:
    """Subject + From + plain body of one e-mail (stdlib parser; attachments are skipped, nothing is decoded
    beyond text parts)."""
    msg = email.message_from_bytes(raw, policy=policy.default)
    plain, htm = [], []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            body = part.get_content() if isinstance(part, EmailMessage) else part.get_payload(decode=True) or ""
        except (LookupError, ValueError, AssertionError):
            payload = part.get_payload(decode=True) or b""
            body = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else str(payload)
        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="replace")
        (plain if ctype == "text/plain" else htm).append(str(body)[:MAX_BODY])
    text = "\n\n".join(t.strip() for t in plain if t.strip()) or "\n\n".join(html_to_text(t) for t in htm)
    head = [f"Subject: {_hdr(msg, 'subject')}", f"From: {_hdr(msg, 'from')}"]
    return "\n".join(head) + "\n\n" + text.strip() + "\n"


def _hdr(msg, name: str) -> str:
    try:
        v = msg.get(name)
    except (ValueError, IndexError):  # malformed header in a hostile message
        v = None
    return re.sub(r"\s+", " ", str(v or "")).strip()


def load(root: Path, rows: list[Row]) -> Iterator[ExtIncident]:
    for r in rows:
        p = root / r.extra["path"]
        if not p.is_file():
            continue
        text = message_text(p.read_bytes())
        yield ExtIncident(
            dataset=INFO.name, key=r.key, files={AD_TEXT_FILE: text.encode("utf-8", errors="replace")},
            label=None, group_label=prefixed(INFO.name, r.group),
            meta={"class": r.group, "lang": r.extra.get("lang"), "subset": r.extra.get("subset"),
                  **{k: r.extra.get(k) for k in ATTRS}, "label_kind": INFO.label_kind},
        )
