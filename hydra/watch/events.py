"""Normalised HYDRA Watch events: one discovered host/URL from a public source.

A recording line is `{"rec_ts", "source", "raw"}` (hydra.watch.recorder). `normalise` turns it into a
`WatchEvent` (domain/URL, first seen, source, redacted raw). Sources:
- `certpl`: CERT Polska Warning List action (`DomainAddress`, `ActionTime`, `ActionType` block/unblock);
- `ct`: certstream-style certificate update (`data.leaf_cert.all_domains`, `data.seen`), one event per name.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from hydra.redact import redact_obj, redact_url
from hydra.watch.names import defang, normalise_host

SOURCES = {"certpl": "watch:certpl", "ct": "watch:ct"}


@dataclass
class WatchEvent:
    event_id: str
    source: str                 # watch:certpl | watch:ct
    host: str
    url: str | None
    first_seen: str             # ISO-8601 UTC, from the source (CERT ActionTime / CT seen)
    received_at: str            # when the recorder (or live tail) received it
    action: str = "block"
    raw: dict[str, Any] = field(default_factory=dict)
    seq: int = 0
    replay: bool = False
    # filled by the engine
    score: dict[str, Any] | None = None
    live_campaign: str | None = None
    assignment: dict[str, Any] | None = None
    enrichment: dict[str, Any] | None = None
    alerts: list[dict[str, Any]] | None = None  # fingerprint alerts (candidates), strongest first

    @property
    def host_defanged(self) -> str:
        return defang(self.host)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["host_defanged"] = self.host_defanged
        d["url_defanged"] = defang(self.url) if self.url else None
        return d


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="seconds")
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    d = d.replace(tzinfo=UTC) if d.tzinfo is None else d.astimezone(UTC)
    return d.isoformat(timespec="seconds")


def _eid(source: str, host: str, ref: str) -> str:
    return "w-" + hashlib.sha256(f"{source}|{host}|{ref}".encode()).hexdigest()[:12]


class MalformedRecord(ValueError):
    """A recording line whose JSON structure is not a watch record (CR-ASTRA-PIPE-310)."""


def _obj(value: Any, where: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MalformedRecord(f"{where}: oczekiwano obiektu JSON, jest {type(value).__name__}")
    return value


def _names(value: Any) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise MalformedRecord(f"lista nazw certyfikatu: oczekiwano listy, jest {type(value).__name__}")
    return value


def normalise(rec: dict[str, Any]) -> list[WatchEvent]:
    """One recording line → zero or more events (CT certificates carry several names). Every structural level is
    checked before it is dereferenced: a wrong shape raises `MalformedRecord` (a ValueError), never AttributeError."""
    rec = _obj(rec, "rekord")
    src = str(rec.get("source") or "")
    raw = _obj(rec.get("raw") or None, "pole raw")
    received = _iso(rec.get("rec_ts")) or datetime.now(UTC).isoformat(timespec="seconds")
    if src == "certpl":
        host = normalise_host(str(raw.get("DomainAddress") or ""))
        if not host or "." not in host:
            return []
        first = _iso(raw.get("ActionTime")) or received
        ref = str(raw.get("RegisterPositionId") or first)
        return [WatchEvent(event_id=_eid("certpl", host, ref), source=SOURCES["certpl"], host=host, url=None,
                           first_seen=first, received_at=received, action=str(raw.get("ActionType") or "block"),
                           raw=redact_obj(dict(raw)))]
    if src == "ct":
        data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        leaf = _obj(data.get("leaf_cert") or None, "pole leaf_cert")
        names = _names(leaf.get("all_domains") or raw.get("names") or None)
        first = _iso(data.get("seen")) or received
        out: list[WatchEvent] = []
        seen: set[str] = set()
        for n in names[:50]:
            host = normalise_host(str(n))
            if not host or "." not in host or host in seen:
                continue
            seen.add(host)
            issuer = _obj(leaf.get("issuer") or None, "pole issuer").get("O")
            out.append(WatchEvent(event_id=_eid("ct", host, str(leaf.get("fingerprint") or first)),
                                  source=SOURCES["ct"], host=host, url=None, first_seen=first, received_at=received,
                                  action="cert", raw={"issuer": issuer, "san_count": len(names)}))
        return out
    if src == "url":  # generic: {"raw": {"url": ...}}
        url = redact_url(str(raw.get("url") or "")) or ""
        host = normalise_host(url)
        if not host:
            return []
        first = _iso(raw.get("seen")) or received
        return [WatchEvent(event_id=_eid("url", host, url), source="watch:url", host=host, url=url,
                           first_seen=first, received_at=received, raw={})]
    return []
