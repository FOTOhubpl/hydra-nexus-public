"""Non-secret integration state: `<state_dir>/integrations/state.json`.

Holds per-connector enabled flags, the last test and last run ({at, ok, status, message_pl, ms}) and request
timestamps for the rolling 60-minute usage counter. Never holds a secret value (the store is
`hydra.integrations.secrets`); messages are passed through `hydra.redact.redact_text` before they are saved.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from hydra.redact import redact_text

_LOCK = threading.RLock()
WINDOW_S = 3600.0


def state_path(settings: Any) -> Path:
    return Path(settings.state_dir) / "integrations" / "state.json"


def load(settings: Any) -> dict[str, Any]:
    p = state_path(settings)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"connectors": {}}
    except (OSError, ValueError):
        return {"connectors": {}}


def _save(settings: Any, data: dict[str, Any]) -> None:
    p = state_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=p.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, p)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _entry(data: dict[str, Any], cid: str) -> dict[str, Any]:
    return data.setdefault("connectors", {}).setdefault(cid, {})


def connector_state(settings: Any, cid: str) -> dict[str, Any]:
    return dict(load(settings).get("connectors", {}).get(cid, {}))


def is_enabled(settings: Any, cid: str) -> bool:
    return bool(connector_state(settings, cid).get("enabled"))


def set_enabled(settings: Any, cid: str, enabled: bool) -> None:
    with _LOCK:
        data = load(settings)
        _entry(data, cid)["enabled"] = bool(enabled)
        _save(settings, data)


def _result(r: dict[str, Any]) -> dict[str, Any]:
    return {"at": r.get("at"), "ok": bool(r.get("ok")), "status": str(r.get("status") or ""),
            "message_pl": redact_text(str(r.get("message_pl") or ""))[:500], "ms": r.get("ms")}


def record_test(settings: Any, cid: str, result: dict[str, Any]) -> None:
    with _LOCK:
        data = load(settings)
        _entry(data, cid)["last_test"] = _result(result)
        _save(settings, data)


def record_run(settings: Any, cid: str, result: dict[str, Any]) -> None:
    with _LOCK:
        data = load(settings)
        e = _entry(data, cid)
        e["last_run"] = _result(result)
        e["runs"] = ([e["last_run"]] + list(e.get("runs") or []))[:10]
        _save(settings, data)


def note_request(settings: Any, cid: str, now: float | None = None) -> None:
    now = time.time() if now is None else now
    with _LOCK:
        data = load(settings)
        e = _entry(data, cid)
        e["requests"] = [t for t in e.get("requests", []) if now - t < WINDOW_S] + [now]
        _save(settings, data)


def requests_since(settings: Any, cid: str, seconds: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return sum(1 for t in connector_state(settings, cid).get("requests", []) if now - t < seconds)


def usage(settings: Any, cid: str, limit_per_min: float | None) -> dict[str, Any]:
    return {"requests_60m": requests_since(settings, cid, WINDOW_S), "limit_per_min": limit_per_min}
