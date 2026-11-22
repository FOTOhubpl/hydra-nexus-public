"""Serialisation safety for every export boundary (CR-ASTRA-SEC-7, -10, -11, -13).

Exports carry hostile text (scam titles, page fragments, gene values). Before it leaves the process:

- `csv_cell` / `csv_row`: redaction (`hydra.redact`), control characters removed and spreadsheet formulas
  neutralised. A text cell starting with `=`, `+`, `-`, `@`, TAB or CR gets a leading apostrophe (OWASP CSV
  injection guidance), so Excel/LibreOffice/Sheets show it as text. Numbers stay numbers. This applies to every
  CSV we write, including the Neo4j `LOAD CSV` files: the exact value of a field is in the JSON/STIX exports.
- `cypher_string` / `cypher_comment`: string literals with every quote, backslash and control character
  escaped; comments that can never contain a line terminator (a stored id cannot start a new statement).
- `safe_filename` / `content_disposition`: ASCII download names without quotes, separators or CR/LF, plus an
  RFC 6266 `filename*` for the readable UTF-8 name.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote

from hydra.redact import redact_text

FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\uff1d", "\uff0b", "\uff0d", "\uff20")
# C0/C1 controls except TAB/LF, DEL, and bidi/zero-width format characters that spoof how a value reads.
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_LINE_BREAK_RE = re.compile("[\r\n\x0b\x0c\x85\u2028\u2029]+")


def strip_controls(text: str) -> str:
    """Remove control and bidi-override characters (TAB and LF are kept)."""
    return _CONTROL_RE.sub("", text)


def csv_cell(value: Any, *, redact: bool = True) -> Any:
    """One spreadsheet-safe CSV cell: strings redacted, controls stripped, formulas neutralised. `redact=False`
    keeps identifiers exact (item names of a batch run) while still neutralising formulas."""
    if value is None:
        return ""
    if isinstance(value, bool | int | float):
        return value
    text = strip_controls((redact_text(str(value)) or "") if redact else str(value))
    if text.lstrip(" ").startswith(FORMULA_PREFIXES) or text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def csv_row(values: Iterable[Any]) -> list[Any]:
    return [csv_cell(v) for v in values]


def cypher_string(value: Any) -> str:
    """Cypher literal for an untrusted value: double-quoted, `\\`, `"` and all controls escaped (`\\uXXXX`)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    text = json.dumps(str(value), ensure_ascii=False)  # escapes \\ " and every char < 0x20
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").replace("\x85", "\\u0085")


def cypher_comment(value: Any, limit: int = 200) -> str:
    """Text safe inside a `//` comment: no line terminators, no block-comment delimiters."""
    text = _LINE_BREAK_RE.sub(" ", strip_controls(str(value))).replace("*/", "* /").replace("/*", "/ *")
    return text[:limit]


def safe_filename(name: Any, default: str = "export", limit: int = 120) -> str:
    """ASCII file name: letters, digits, `.`, `-`, `_`; never empty, never a dot file, never `..`."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")
    text = re.sub(r"\.{2,}", ".", text)[:limit].strip("._")
    return text or default


def content_disposition(filename: Any, default: str = "export") -> dict[str, str]:
    """`Content-Disposition: attachment` header with an ASCII fallback and an encoded UTF-8 name."""
    raw = strip_controls(_LINE_BREAK_RE.sub(" ", str(filename or "")))[:200]
    ascii_name = safe_filename(raw, default)
    value = f'attachment; filename="{ascii_name}"'
    if raw and raw != ascii_name:
        value += f"; filename*=UTF-8''{quote(raw, safe='')}"
    return {"Content-Disposition": value}
