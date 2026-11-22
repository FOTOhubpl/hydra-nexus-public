"""Payload builders for outbound integrations, built from the run / campaign views (read-only).

`collect()` reduces a run (or one campaign) to indicators only: domains, URLs (query secrets redacted), IPs,
SHA-256 of shared files, campaign ids, decision (campaign status), score bits, member counts, first/last seen.
Never artefact contents, OCR text, file names, incident titles, campaign labels, organiser files or secrets.

Builders (`BUILDERS[format](collected) -> Payload`): misp, csv, ndjson, es_bulk, siem_json, sql, webhook_json.
`stix21`, `neo4j`, `xarf` and `evidence` wrap the existing exports (hydra.export_stix, export_neo4j, export_abuse,
export_dataset + evidence_sign) and need the settings, so they go through `build()`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra.export_safe import csv_row
from hydra.redact import redact_url

INDICATOR_FIELDS = ["type", "value", "campaign_id", "decision", "score_bits", "count", "first_seen", "last_seen"]
CAMPAIGN_FIELDS = ["campaign_id", "decision", "confidence", "count", "indicators", "first_seen", "last_seen"]
MISP_TYPES = {"domain": ("domain", "Network activity"), "url": ("url", "Network activity"),
              "ip": ("ip-dst", "Network activity"), "sha256": ("sha256", "Payload delivery")}
CONTENT_TYPES = {"misp": "application/json", "csv": "text/csv; charset=utf-8", "ndjson": "application/x-ndjson",
                 "es_bulk": "application/x-ndjson", "siem_json": "application/json", "sql": "application/sql",
                 "webhook_json": "application/json", "stix21": "application/stix+json;version=2.1",
                 "neo4j": "text/plain; charset=utf-8", "xarf": "application/zip", "evidence": "application/zip"}
EXTENSIONS = {"misp": "misp.json", "csv": "csv", "ndjson": "ndjson", "es_bulk": "es-bulk.ndjson",
              "siem_json": "hec.json", "sql": "sql", "webhook_json": "webhook.json", "stix21": "stix.json",
              "neo4j": "cypher", "xarf": "xarf.zip", "evidence": "evidence.zip"}
ES_INDEX = "hydra-indicators"


@dataclass
class Collected:
    run_id: str
    campaign_id: str | None
    campaigns: list[dict[str, Any]]
    indicators: list[dict[str, Any]]
    redactions: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))


@dataclass
class Payload:
    format: str
    content_type: str
    body: bytes
    preview: Any
    fields_sent: list[str]
    redactions: list[str]
    sha256: str = ""

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.body).hexdigest()

    def filename(self, stem: str) -> str:
        return f"{stem}.{EXTENSIONS.get(self.format, 'bin')}"


def _bits(view: Any) -> dict[tuple[str, str], float]:
    out: dict[tuple[str, str], float] = {}
    for g in view.campaign.core_genome:
        if g.value_a and g.bits is not None:
            out[(g.kind, g.value_a)] = round(float(g.bits), 3)
    return out


def collect(settings: Any, run_id: str | None = None, campaign_id: str | None = None, *, limit: int | None = None,
            multi_only: bool = True) -> Collected:
    """Indicators of one campaign or every (multi-member) campaign of a run. KeyError for unknown run/campaign."""
    from hydra.campaign_facts import campaign_hosts, load_campaign, run_known
    from hydra.db import connect
    from hydra.pipeline import latest_run

    with closing(connect(settings.db_path, readonly=True)) as conn:
        if run_id and not run_known(conn, run_id):
            raise KeyError(f"run {run_id} not found")
        run_id = run_id or latest_run(conn)
        if run_id is None:
            raise KeyError("no analysed run")
        if campaign_id:
            ids = [campaign_id]
        else:
            ids = [r["id"] for r in conn.execute("SELECT id, detail FROM campaigns WHERE run_id=? ORDER BY id",
                                                  (run_id,))
                   if not multi_only or len(json.loads(r["detail"]).get("members", [])) > 1]
        views = [v for cid in ids if (v := load_campaign(conn, cid, run_id)) is not None]
    if campaign_id and not views:
        raise KeyError(f"campaign {campaign_id} not found")
    redactions: set[str] = set()
    campaigns, indicators = [], []
    for v in views:
        c = v.campaign
        bits = _bits(v)
        seen_at = sorted(str(i.get("observed_at")) for i in v.incidents.values() if i.get("observed_at"))
        base = {"campaign_id": c.id, "decision": c.status,
                "first_seen": c.first_seen or (seen_at[0] if seen_at else None),
                "last_seen": c.last_seen or (seen_at[-1] if seen_at else None)}
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()

        def add(kind: str, value: str, count: int, score: float | None, _rows=rows, _seen=seen, _base=base) -> None:
            if (kind, value) in _seen:
                return
            _seen.add((kind, value))
            _rows.append({"type": kind, "value": value, **_base, "score_bits": score, "count": count})

        for h in campaign_hosts(v):
            n = len(h["incidents"])
            add("domain", h["host"], n, bits.get(("landing_host", h["host"])) or bits.get(("registrable_domain",
                                                                                          h["domain"])))
            for u in h["urls"]:
                ru = redact_url(u) or ""
                if ru != u:
                    redactions.add("Parametry URL wyglądające na sekrety zastąpione [REDACTED].")
                add("url", ru, n, None)
            for ip in h["ips"]:
                add("ip", ip, n, None)
        for g in c.core_genome:
            if g.kind in ("asset_sha256", "artifact_sha256") and g.value_a and len(g.value_a) == 64:
                add("sha256", g.value_a.lower(), int(g.doc_freq or len(c.members)), g.bits)
        campaigns.append({**base, "confidence": round(float(c.confidence), 4), "count": len(c.members),
                          "indicators": len(rows)})
        indicators.extend(rows)
    if limit is not None and len(indicators) > limit:
        indicators = indicators[:limit]
        redactions.add(f"Lista skrócona do {limit} wskaźników (parametr limit).")
    return Collected(run_id, campaign_id, campaigns, indicators, sorted(redactions))


# --------------------------------------------------------------------------------------------- builders


def _fields(col: Collected, extra: tuple[str, ...] = ()) -> list[str]:
    kinds = sorted({i["type"] for i in col.indicators})
    base = ["campaign_id", "decision", "score_bits", "count", "first_seen", "last_seen"]
    return [*kinds, *base, *extra]


def _json(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False).encode()


def misp_event(col: Collected) -> Payload:
    attrs = []
    for i in col.indicators:
        typ, cat = MISP_TYPES[i["type"]]
        attrs.append({"type": typ, "category": cat, "value": i["value"], "to_ids": i["type"] != "url",
                      "comment": f"HYDRA NEXUS kampania {i['campaign_id']}; decyzja {i['decision']}; "
                                 f"incydenty {i['count']}" + (f"; bity {i['score_bits']}" if i["score_bits"]
                                                              is not None else "")})
    scope = f"kampania {col.campaign_id}" if col.campaign_id else f"przebieg {col.run_id}"
    event = {"Event": {"info": f"HYDRA NEXUS: {scope} ({len(col.campaigns)} kampanii, {len(attrs)} wskaźników)",
                       "threat_level_id": "2", "analysis": "1", "distribution": "0",
                       "date": col.generated_at[:10], "Attribute": attrs,
                       "Tag": [{"name": "tlp:amber"}, {"name": "hydra-nexus"}]}}
    return Payload("misp", CONTENT_TYPES["misp"], _json(event), event, _fields(col), col.redactions)


def indicators_csv(col: Collected) -> Payload:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(INDICATOR_FIELDS)
    for i in col.indicators:
        w.writerow(csv_row(i.get(k) for k in INDICATOR_FIELDS))
    text = buf.getvalue()
    return Payload("csv", CONTENT_TYPES["csv"], text.encode("utf-8"), text, _fields(col),
                   [*col.redactions, "Komórki zabezpieczone przed formułami arkusza (=, +, -, @)."])


def _doc(i: dict[str, Any], col: Collected) -> dict[str, Any]:
    return {"@timestamp": col.generated_at, "source": "hydra-nexus", "run_id": col.run_id,
            "indicator": {"type": i["type"], "value": i["value"]},
            **{k: i[k] for k in INDICATOR_FIELDS if k not in ("type", "value")}}


def ndjson(col: Collected) -> Payload:
    docs = [_doc(i, col) for i in col.indicators]
    body = "".join(json.dumps(d, ensure_ascii=False, sort_keys=True) + "\n" for d in docs)
    return Payload("ndjson", CONTENT_TYPES["ndjson"], body.encode(), docs, _fields(col), col.redactions)


def es_bulk(col: Collected) -> Payload:
    lines: list[str] = []
    for i in col.indicators:
        doc_id = hashlib.sha256(f"{col.run_id}|{i['campaign_id']}|{i['type']}|{i['value']}".encode()).hexdigest()
        lines.append(json.dumps({"index": {"_index": ES_INDEX, "_id": doc_id}}))
        lines.append(json.dumps(_doc(i, col), ensure_ascii=False, sort_keys=True))
    body = "".join(line + "\n" for line in lines)
    return Payload("es_bulk", CONTENT_TYPES["es_bulk"], body.encode(), [json.loads(x) for x in lines],
                   _fields(col), col.redactions)


def siem_json(col: Collected) -> Payload:
    try:
        t = datetime.fromisoformat(col.generated_at).timestamp()
    except ValueError:
        t = datetime.now(UTC).timestamp()
    events = [{"time": round(t, 3), "host": "hydra-nexus", "source": "hydra", "sourcetype": "hydra:indicator",
               "event": _doc(i, col)} for i in col.indicators]
    body = "\n".join(json.dumps(e, ensure_ascii=False, sort_keys=True) for e in events)
    return Payload("siem_json", CONTENT_TYPES["siem_json"], body.encode(), events, _fields(col), col.redactions)


def _sql_lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int | float):
        return repr(float(v)) if isinstance(v, float) else str(v)
    s = str(v).replace("\x00", "")
    return "'" + s.replace("'", "''") + "'"


def sql(col: Collected) -> Payload:
    lines = ["-- HYDRA NEXUS indicators export (SQLite / PostgreSQL compatible)",
             f"-- run {col.run_id}; generated {col.generated_at}; indicators only, no artefact contents",
             "CREATE TABLE IF NOT EXISTS hydra_campaigns (run_id TEXT NOT NULL, campaign_id TEXT NOT NULL, "
             "decision TEXT, confidence REAL, member_count INTEGER, indicator_count INTEGER, first_seen TEXT, "
             "last_seen TEXT, PRIMARY KEY (run_id, campaign_id));",
             "CREATE TABLE IF NOT EXISTS hydra_indicators (run_id TEXT NOT NULL, campaign_id TEXT NOT NULL, "
             "type TEXT NOT NULL, value TEXT NOT NULL, decision TEXT, score_bits REAL, member_count INTEGER, "
             "first_seen TEXT, last_seen TEXT, PRIMARY KEY (run_id, campaign_id, type, value));"]
    for c in col.campaigns:
        vals = [col.run_id, c["campaign_id"], c["decision"], c["confidence"], c["count"], c["indicators"],
                c["first_seen"], c["last_seen"]]
        lines.append("INSERT INTO hydra_campaigns VALUES (" + ", ".join(_sql_lit(v) for v in vals) + ");")
    for i in col.indicators:
        vals = [col.run_id, i["campaign_id"], i["type"], i["value"], i["decision"], i["score_bits"], i["count"],
                i["first_seen"], i["last_seen"]]
        lines.append("INSERT INTO hydra_indicators VALUES (" + ", ".join(_sql_lit(v) for v in vals) + ");")
    text = "\n".join(lines) + "\n"
    return Payload("sql", CONTENT_TYPES["sql"], text.encode(), text, _fields(col), col.redactions)


def webhook_json(col: Collected) -> Payload:
    body = {"event": "export", "created_at": col.generated_at, "source": "hydra-nexus",
            "data": {"run_id": col.run_id, "campaign_id": col.campaign_id, "campaigns": col.campaigns,
                     "indicators": [{k: i[k] for k in INDICATOR_FIELDS} for i in col.indicators]}}
    return Payload("webhook_json", CONTENT_TYPES["webhook_json"], _json(body), body, _fields(col), col.redactions)


def slack_message(col: Collected) -> Payload:
    """Short Slack incoming-webhook text: campaign ids, decisions, counts and at most 5 domains each."""
    lines = [f"HYDRA NEXUS: {len(col.campaigns)} kampanii, {len(col.indicators)} wskaźników (przebieg "
             f"{col.run_id})."]
    for c in col.campaigns[:10]:
        doms = [i["value"] for i in col.indicators if i["campaign_id"] == c["campaign_id"] and i["type"] ==
                "domain"][:5]
        lines.append(f"• {c['campaign_id']} ({c['decision']}, {c['count']} incydentów): " + (", ".join(doms) or "-"))
    body = {"text": "\n".join(lines)}
    return Payload("webhook_json", "application/json", _json(body), body,
                   ["campaign_id", "decision", "count", "domain"], col.redactions)


BUILDERS: dict[str, Callable[[Collected], Payload]] = {
    "misp": misp_event, "csv": indicators_csv, "ndjson": ndjson, "es_bulk": es_bulk, "siem_json": siem_json,
    "sql": sql, "webhook_json": webhook_json,
}
WRAPPED = ("stix21", "neo4j", "xarf", "evidence")


def _stix(settings: Any, run_id: str | None, campaign_id: str | None) -> Payload:
    from hydra.export_stix import export_stix

    bundle = export_stix(settings, campaign_id, run_id)
    return Payload("stix21", CONTENT_TYPES["stix21"], _json(bundle), bundle,
                   ["domain", "url", "ip", "sha256", "campaign_id", "score_bits", "first_seen", "last_seen"],
                   ["Wartości po hydra.redact; wzorce URL bez zapytań."])


def _neo4j(settings: Any, run_id: str | None, campaign_id: str | None) -> Payload:
    from hydra.db import connect
    from hydra.export_neo4j import build_graph, inline_cypher
    from hydra.pipeline import latest_run

    with closing(connect(settings.db_path, readonly=True)) as conn:
        run = run_id or latest_run(conn)
        if run is None:
            raise KeyError("no analysed run")
        from hydra.campaign_facts import run_known

        if not run_known(conn, run):
            raise KeyError(f"run {run} not found")
        text = inline_cypher(build_graph(conn, run))
    return Payload("neo4j", CONTENT_TYPES["neo4j"], text.encode(), text[:200_000],
                   ["domain", "url", "ip", "sha256", "phash", "campaign_id", "score_bits", "count"],
                   ["Wartości genów po hydra.redact (istniejący eksport Neo4j)."])


def _xarf(settings: Any, run_id: str | None, campaign_id: str | None) -> Payload:
    from hydra.export_abuse import abuse_package

    if not campaign_id:
        raise ValueError("Format XARF wymaga campaign_id.")
    data, manifest = abuse_package(settings, campaign_id, run_id)
    return Payload("xarf", CONTENT_TYPES["xarf"], data, manifest,
                   ["domain", "url", "ip", "campaign_id", "first_seen", "last_seen"],
                   ["Pakiet nie jest wysyłany; zawiera wyciągi dowodów (base64) do ręcznej weryfikacji."])


def _evidence(settings: Any, run_id: str | None, campaign_id: str | None) -> Payload:
    from hydra.db import connect
    from hydra.export_dataset import export_dataset, resolve_run

    with closing(connect(settings.db_path, readonly=True)) as conn:
        from hydra.pipeline import latest_run

        run = run_id or latest_run(conn)
        if run is None:
            raise KeyError("no analysed run")
        try:
            run = resolve_run(conn, run)
        except ValueError as exc:
            raise KeyError(str(exc)) from exc
    Path(settings.tmp_dir).mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=settings.tmp_dir, prefix="integ-evidence-"))
    try:
        res = export_dataset(settings, run, tmp / "dataset", campaign_id=campaign_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted((tmp / "dataset").iterdir()):
                zf.write(f, f"hydra-evidence/{f.name}")
        data = buf.getvalue()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    preview = {"run_id": res.get("run_id"), "counts": res.get("counts"),
               "key_fingerprint": (res.get("signature") or {}).get("key_fingerprint"),
               "verify": "hydra verify-export <plik.zip>"}
    return Payload("evidence", CONTENT_TYPES["evidence"], data, preview,
                   ["domain", "url", "ip", "sha256", "phash", "campaign_id", "decision", "score_bits", "first_seen",
                    "last_seen", "count"], ["Zbiór danych po hydra.redact, podpis Ed25519."])


_WRAPPERS = {"stix21": _stix, "neo4j": _neo4j, "xarf": _xarf, "evidence": _evidence}


def build(settings: Any, fmt: str, *, run_id: str | None = None, campaign_id: str | None = None,
          limit: int | None = None) -> Payload:
    """Any format. KeyError: unknown run/campaign; ValueError: bad format / missing parameter."""
    if fmt in _WRAPPERS:
        return _WRAPPERS[fmt](settings, run_id, campaign_id)
    if fmt not in BUILDERS:
        raise ValueError(f"Nieznany format: {fmt}")
    return BUILDERS[fmt](collect(settings, run_id, campaign_id, limit=limit))
