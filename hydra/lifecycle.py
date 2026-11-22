"""Infrastructure lifecycle per campaign and lead times over a corpus (timeline UI data). Read-only, replay only.

Events per reported host, each with its source and fetch time:
- `registration`: RDAP `events[registration]` of the registrable domain (cache `rdap`);
- `first_certificate` / `last_certificate`: earliest / latest `not_before` in Certificate Transparency (cache `crtsh`);
- `first_observed` / `last_seen`: earliest / latest date of the incidents holding the host (ad, screenshot and
  capture dates from the adapters; HAR/page capture dates as META genes when present);
- `cert_pl_listed` / `cert_pl_removed`: CERT Polska warning list InsertDate / DeleteDate (offline snapshot).
Lead times in days (later − earlier) for registration→first observation, first certificate→first observation,
registration→listing, first certificate→listing and first observation→listing; corpus medians report n and the
coverage (share of hosts with both dates). Dates that are missing stay missing: no imputation.
"""

from __future__ import annotations

import statistics
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from hydra.campaign_facts import (
    CampaignView,
    campaign_hosts,
    crtsh_domain,
    enricher,
    load_campaign,
    rdap_domain,
    run_known,
)
from hydra.config import Settings
from hydra.db import connect

LEADS = [("registration", "first_observed"), ("first_certificate", "first_observed"),
         ("registration", "cert_pl_listed"), ("first_certificate", "cert_pl_listed"),
         ("first_observed", "cert_pl_listed")]
DATE_GENE_KINDS = {"capture_date", "capture_time", "ad_start_date", "collected_date", "upload_date"}


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).strip().replace("Z", "+00:00").replace(" ", "T")
    for cand in (s, s[:19], s[:10]):
        try:
            d = datetime.fromisoformat(cand)
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _ev(kind: str, when: datetime | None, source: str, fetched_at: str | None = None, **extra: Any) -> dict | None:
    if when is None:
        return None
    return {"type": kind, "date": when.isoformat(timespec="seconds"), "source": source, "fetched_at": fetched_at,
            **extra}


def host_events(enr, host: dict[str, Any], incident_dates: dict[str, list[datetime]]) -> list[dict[str, Any]]:
    from hydra.enrich import cert_pl

    rd = rdap_domain(enr, host["domain"])
    ct = crtsh_domain(enr, host["domain"])
    seen = sorted(d for i in host["incidents"] for d in incident_dates.get(i, []))
    cert = cert_pl.lookup(host["host"]) if cert_pl.available() else {}
    evs = [
        _ev("registration", _dt(rd["summary"].get("registration")), "rdap", rd["fetched_at"]),
        _ev("first_certificate", _dt(ct["summary"].get("first_cert")), "crt.sh", ct["fetched_at"],
            n_certs=ct["summary"].get("n_certs")),
        _ev("last_certificate", _dt(ct["summary"].get("last_cert")), "crt.sh", ct["fetched_at"]),
        _ev("first_observed", seen[0] if seen else None, "incident"),
        _ev("last_seen", seen[-1] if seen else None, "incident"),
        _ev("cert_pl_listed", _dt(cert.get("insert_date")), "cert_pl", cert.get("retrieved_at"),
            snapshot=cert.get("snapshot")),
        _ev("cert_pl_removed", _dt(cert.get("delete_date")), "cert_pl", cert.get("retrieved_at")),
    ]
    return sorted((e for e in evs if e), key=lambda e: e["date"])


def lead_days(events: list[dict[str, Any]]) -> dict[str, float | None]:
    by = {e["type"]: _dt(e["date"]) for e in events}
    out: dict[str, float | None] = {}
    for a, b in LEADS:
        out[f"{a}->{b}"] = round((by[b] - by[a]).total_seconds() / 86400, 2) if by.get(a) and by.get(b) else None
    return out


def incident_dates(view: CampaignView) -> dict[str, list[datetime]]:
    out: dict[str, list[datetime]] = {}
    for iid, inc in view.incidents.items():
        ds = [d for d in [_dt(inc.get("observed_at"))] if d]
        ds += [d for g in view.genes.get(iid, []) if g.kind in DATE_GENE_KINDS and (d := _dt(g.value))]
        out[iid] = ds
    return out


def campaign_lifecycle(settings: Settings, campaign_id: str, run_id: str | None = None) -> dict[str, Any]:
    with closing(connect(settings.db_path, readonly=True)) as conn:
        view = load_campaign(conn, campaign_id, run_id)
    if view is None:
        raise KeyError(f"campaign {campaign_id} not found")
    enr = enricher(settings)
    dates = incident_dates(view)
    domains = []
    for h in campaign_hosts(view):
        evs = host_events(enr, h, dates)
        domains.append({"host": h["host"], "domain": h["domain"], "incidents": h["incidents"], "events": evs,
                        "lead_days": lead_days(evs)})
    first: dict[str, dict] = {}
    for d in domains:
        for e in d["events"]:
            if e["type"] not in first or e["date"] < first[e["type"]]["date"]:
                first[e["type"]] = {**e, "host": d["host"]}
    return {"campaign_id": view.campaign.id, "run_id": view.run_id, "label": view.campaign.label,
            "domains": domains, "campaign_first_events": sorted(first.values(), key=lambda e: e["date"]),
            "lead_times": summarise_leads([d["lead_days"] for d in domains])}


def summarise_leads(rows: list[dict[str, float | None]]) -> dict[str, dict[str, Any]]:
    out = {}
    for a, b in LEADS:
        k = f"{a}->{b}"
        vals = sorted(r[k] for r in rows if r.get(k) is not None)
        out[k] = {"n": len(vals), "hosts": len(rows), "coverage": round(len(vals) / len(rows), 3) if rows else None,
                  "median_days": round(statistics.median(vals), 2) if vals else None,
                  "p25_days": round(vals[len(vals) // 4], 2) if vals else None,
                  "p75_days": round(vals[(3 * len(vals)) // 4], 2) if vals else None}
    return out


def corpus_lead_times(settings: Settings, run_id: str | None = None, max_campaigns: int | None = None
                      ) -> dict[str, Any]:
    """Lead-time medians over every host of every campaign of a run (e.g. the public corpus)."""
    from hydra.pipeline import latest_run

    with closing(connect(settings.db_path, readonly=True)) as conn:
        if run_id and not run_known(conn, run_id):
            raise KeyError(f"run {run_id} not found")
        run_id = run_id or latest_run(conn)
        if run_id is None:
            raise KeyError("no analysed run")
        ids = [r["id"] for r in conn.execute("SELECT id FROM campaigns WHERE run_id = ? ORDER BY id", (run_id,))]
        views = [v for cid in ids[:max_campaigns] if (v := load_campaign(conn, cid, run_id))]
    enr = enricher(settings)
    rows, seen_hosts = [], set()
    for v in views:
        dates = incident_dates(v)
        for h in campaign_hosts(v):
            if h["host"] in seen_hosts:
                continue
            seen_hosts.add(h["host"])
            rows.append(lead_days(host_events(enr, h, dates)))
    return {"run_id": run_id, "campaigns": len(views), "hosts": len(rows), "lead_times": summarise_leads(rows),
            "computed_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "note": "Median days later − earlier; negative = the later event came first (e.g. listed before our"
                    " observation). Dates from the replay cache and the CERT snapshot; missing dates are excluded."}
