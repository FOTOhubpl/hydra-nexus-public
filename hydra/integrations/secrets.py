"""Connector secrets: `~/.config/hydra/<connector_id>.env` (dir 0700, file 0600) or environment variables.

A value is never logged, cached, audited, returned by the API or written to the state DB or the repo. The API only
reports `configured`, the source (`env` / `file`) and the last 4 characters of long values. The environment wins
over the file. Meta Ad Library keeps its existing file and variable names (`hydra.enrich.adlibrary.token_file`,
`META_AD_LIBRARY_TOKEN`). `HYDRA_SECRETS_DIR` overrides the directory (tests, Docker); otherwise `$HOME` is read
at call time, so tests with a temporary HOME never touch the operator's files.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_VALUE = 8192
META_CONNECTOR = "meta-adlibrary"


class SecretError(ValueError):
    """Invalid connector id, secret name or value (message is safe to show; never contains the value)."""


def secret_dir() -> Path:
    override = os.environ.get("HYDRA_SECRETS_DIR")
    return Path(override) if override else Path.home() / ".config" / "hydra"


def secret_file(connector_id: str) -> Path:
    if not _ID_RE.match(connector_id or ""):
        raise SecretError("Nieprawidłowy identyfikator konektora.")
    if connector_id == META_CONNECTOR and (os.environ.get("HYDRA_META_ENV_FILE") or
                                           not os.environ.get("HYDRA_SECRETS_DIR")):
        # explicit HYDRA_META_ENV_FILE wins, then HYDRA_SECRETS_DIR, then ~/.config/hydra/meta.env (one provider:
        # meta_token() reads exactly the file the Integrations form writes)
        from hydra.enrich.adlibrary import token_file

        return token_file()
    return secret_dir() / f"{connector_id}.env"


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise SecretError("Nieprawidłowa nazwa sekretu (dozwolone: A-Z, 0-9, _).")
    return name


def _read_file(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    vals: dict[str, str] = {}
    for line in lines:
        k, sep, v = line.strip().removeprefix("export ").partition("=")
        if sep and k.strip() and not k.strip().startswith("#"):
            vals[k.strip()] = v.strip().strip("'\"")
    return vals


def get_secret(connector_id: str, name: str) -> str | None:
    """Internal use only (HTTP clients). Environment first, then the connector's file."""
    _check_name(name)
    env = os.environ.get(name, "").strip()
    if env:
        return env
    v = _read_file(secret_file(connector_id)).get(name, "").strip()
    return v or None


META_TOKEN_VARS = ("META_AD_LIBRARY_TOKEN", "HYDRA_META_TOKEN")  # first set wins; HYDRA_META_TOKEN kept for old setups


def meta_token() -> str | None:
    """THE Meta Ad Library token provider (META-COLLECT): the Integrations test, the collector, the diagnostics and the
    legacy `hydra.enrich.adlibrary` paths all read the token here, so env / `HYDRA_SECRETS_DIR` / the default file
    can never disagree. Environment first (META_AD_LIBRARY_TOKEN, then HYDRA_META_TOKEN), then the connector file
    (`secret_file("meta-adlibrary")`). Internal use only: never logged, cached, returned or persisted."""
    for k in META_TOKEN_VARS:
        v = os.environ.get(k, "").strip()
        if v:
            return v
    vals = _read_file(secret_file(META_CONNECTOR))
    return next((vals[k].strip() for k in META_TOKEN_VARS if vals.get(k, "").strip()), None)


def meta_token_status() -> dict[str, Any]:
    """configured / source / last4 of the token `meta_token()` would use (never the value)."""
    for k in META_TOKEN_VARS:
        v = os.environ.get(k, "").strip()
        if v:
            return {"name": k, "configured": True, "source": "env", "last4": last4(v)}
    vals = _read_file(secret_file(META_CONNECTOR))
    for k in META_TOKEN_VARS:
        v = vals.get(k, "").strip()
        if v:
            return {"name": k, "configured": True, "source": "file", "last4": last4(v)}
    return {"name": META_TOKEN_VARS[0], "configured": False, "source": None, "last4": None}


def last4(value: str | None) -> str | None:
    """Last 4 characters, only for values long enough that 4 characters reveal little (>= 12)."""
    return value[-4:] if value and len(value) >= 12 else None


def status(connector_id: str, name: str) -> dict[str, Any]:
    _check_name(name)
    env = os.environ.get(name, "").strip()
    if env:
        return {"name": name, "configured": True, "source": "env", "last4": last4(env)}
    v = _read_file(secret_file(connector_id)).get(name, "").strip()
    if v:
        return {"name": name, "configured": True, "source": "file", "last4": last4(v)}
    return {"name": name, "configured": False, "source": None, "last4": None}


def secret_status(connector_id: str, names: tuple[str, ...] | list[str]) -> list[dict[str, Any]]:
    return [status(connector_id, n) for n in names]


def _write(path: Path, vals: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".env", dir=path.parent)  # mkstemp creates 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("# HYDRA NEXUS connector secrets (0600). Do not commit.\n")
            for k in sorted(vals):
                fh.write(f"{k}={vals[k]}\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def set_secret(connector_id: str, name: str, value: str) -> dict[str, Any]:
    """Store one secret in the connector's 0600 file. Returns the status (never the value)."""
    _check_name(name)
    value = (value or "").strip()
    if not value or len(value) > MAX_VALUE or any(c in value for c in "\r\n\x00"):
        raise SecretError("Nieprawidłowa wartość sekretu (pusta, za długa albo z niedozwolonymi znakami).")
    path = secret_file(connector_id)
    vals = _read_file(path)
    vals[name] = value
    _write(path, vals)
    return {"name": name, "configured": True, "source": "file", "last4": last4(value)}


def clear_secret(connector_id: str, name: str) -> dict[str, Any]:
    """Remove one secret from the file (an environment variable cannot be cleared from here)."""
    _check_name(name)
    path = secret_file(connector_id)
    vals = _read_file(path)
    if name in vals:
        vals.pop(name)
        if vals:
            _write(path, vals)
        else:
            path.unlink(missing_ok=True)
    return status(connector_id, name)
