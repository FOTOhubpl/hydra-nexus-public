"""One server-side input-path policy for every endpoint (audit A11).

A path given to the API must exist and resolve (symlinks followed) inside Settings.data_dir or the import tmp dir.
Trust model: the API is loopback-only with Host/Origin checks (api._LocalOnlyGuard); this policy keeps even a
local caller from importing arbitrary files such as ~/.ssh into the CAS.
"""

from __future__ import annotations

import re
from pathlib import Path

from hydra.config import Settings


class PathNotAllowed(ValueError):
    pass


def allowed_roots(settings: Settings) -> list[Path]:
    assert settings.data_dir is not None
    return [Path(settings.data_dir).resolve(), Path(settings.tmp_dir).resolve()]


def check_input_path(raw: str | Path, settings: Settings) -> Path:
    """Resolve `raw` and require it inside the allowed roots. Only top-level: folder children are checked one by
    one during the walk (hydra.ingest). Malformed input (NUL byte, ~unknownuser, overlong) is PathNotAllowed too."""
    try:
        p = Path(raw).expanduser().resolve()
        exists = p.exists()
    except (OSError, RuntimeError, ValueError) as exc:
        shown = safe_display_name(str(raw), max_len=300)
        raise PathNotAllowed(f"invalid path ({type(exc).__name__}): {shown}") from exc
    if not exists:
        raise PathNotAllowed(f"path does not exist: {raw}")
    if not is_within(p, allowed_roots(settings)):
        raise PathNotAllowed(f"path outside the data directory: {raw}")
    return p


def is_within(path: Path, roots: list[Path]) -> bool:
    """True when the already-resolved `path` equals or lies under one of the resolved `roots`."""
    return any(path == r or r in path.parents for r in roots)


# C0/C1 controls, DEL, ALM/LRM/RLM, line/paragraph separators, bidi embeddings/overrides/isolates, and lone
# surrogates (undecodable bytes from os.fsdecode). They spoof or break names shown in the UI, logs and exports.
_UNSAFE_DISPLAY = re.compile("[\\x00-\\x1f\\x7f-\\x9f\\u061c\\u200e\\u200f\\u2028\\u2029\\u202a-\\u202e"
                             "\\u2066-\\u2069\\ud800-\\udfff]")


def _escape_char(m: re.Match[str]) -> str:
    code = ord(m.group())
    return f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}"


def safe_display_name(name: str, *, max_len: int | None = None) -> str:
    """A file name / source reference that is safe to display (CR-ASTRA-SEC-2): every control, bidi or invalid
    character becomes a visible escape (`\\x0a`, `\\u202e`), so the shown text cannot be reordered or split.
    Optional `max_len` truncates with an ellipsis. Not a filesystem sanitiser: `/`, `\\` and `..` are kept."""
    out = _UNSAFE_DISPLAY.sub(_escape_char, name)
    if max_len is not None and len(out) > max_len:
        out = out[: max(0, max_len - 1)] + "…"
    return out
