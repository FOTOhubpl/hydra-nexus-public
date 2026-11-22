"""STIX 2.1 bundle per campaign or per run (library `stix2` 3.0.2, BSD-3-Clause, OASIS Open).

Objects per campaign:
- `campaign` (label, first/last seen, scheme, member incidents as external references);
- `indicator` per reported domain (`[domain-name:value = '…']`), entry URL pattern (host + path, query removed) and
  core file hash (`[file:hashes.'SHA-256' = '…']`), each with `x_hydra_bits` / `x_hydra_doc_freq` when known;
- `infrastructure` per domain (`infrastructure_types: ["phishing"]`, the closest STIX vocabulary term for a scam
  landing host), with `consists-of` → `domain-name` (and `ipv4-addr`) observables;
- `observed-data` per incident (its domains and files; first/last observed = incident date);
- relationships: indicator `indicates` campaign, campaign `uses` infrastructure, observed-data `related-to` campaign.
Ids are UUIDv5 of stable keys, so re-exports are identical. Values are redacted; nothing leaves the machine unless
the analyst downloads it. Validation: the bundle is rebuilt with `stix2.parse` (allow_custom for x_hydra_*).
"""

from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from hydra.campaign_facts import CampaignView, campaign_hosts, load_campaign, run_known
from hydra.config import Settings
from hydra.db import connect
from hydra.redact import redact_text

NS = uuid.UUID("6f1c2e3a-8b47-4d55-9a0e-6d7c1b2a3f40")
IDENTITY_NAME = "HYDRA NEXUS"


def _id(kind: str, key: str) -> str:
    return f"{kind}--{uuid.uuid5(NS, kind + '|' + key)}"


def _ts(date: str | None) -> datetime:
    """Source timestamp as a UTC instant. The full string is parsed first so an offset (`+02:00`) shifts the
    instant correctly (CR-ASTRA-SEC-15); naive values are taken as UTC; sub-seconds are dropped."""
    if date:
        text = str(date).strip().replace("Z", "+00:00").replace("z", "+00:00")
        for candidate in (text, text[:19]):
            try:
                d = datetime.fromisoformat(candidate)
            except ValueError:
                continue
            d = d if d.tzinfo else d.replace(tzinfo=UTC)
            return d.astimezone(UTC).replace(microsecond=0)
    return datetime(2026, 1, 1, tzinfo=UTC)


def _q(v: str) -> str:
    return v.replace("\\", "\\\\").replace("'", "\\'")


def campaign_objects(view: CampaignView, identity_id: str) -> list[Any]:
    import stix2

    c = view.campaign
    created = _ts(c.first_seen)
    camp_id = _id("campaign", c.id)
    refs = [{"source_name": "hydra-nexus", "external_id": m, "description": redact_text(
        (view.incidents.get(m) or {}).get("title") or "") or None} for m in c.members]
    objs: list[Any] = [stix2.Campaign(
        id=camp_id, name=redact_text(c.label)[:250], created=created, modified=created, created_by_ref=identity_id,
        description=redact_text(f"Schemat: {c.scheme or 'nieznany'}; {len(c.members)} incydentów; pewność"
                                f" {c.confidence}"), first_seen=_ts(c.first_seen), last_seen=_ts(c.last_seen),
        external_references=[{k: v for k, v in r.items() if v} for r in refs], allow_custom=True,
        x_hydra_group_id=c.group_id, x_hydra_run_id=view.run_id)]
    bits = {(g.kind, g.value_a): g for g in c.core_genome}
    for h in campaign_hosts(view):
        dom = h["host"]
        dn = stix2.DomainName(value=dom)
        objs.append(dn)
        ips = [stix2.IPv4Address(value=ip) for ip in h["ips"] if ip.count(".") == 3]
        objs.extend(ips)
        infra_id = _id("infrastructure", f"{c.id}|{dom}")
        objs.append(stix2.Infrastructure(id=infra_id, name=dom, infrastructure_types=["phishing"], created=created,
                                         modified=created, created_by_ref=identity_id,
                                         description=f"Host strony docelowej kampanii {c.id}"))
        for target in [dn, *ips]:
            objs.append(stix2.Relationship(id=_id("relationship", f"{infra_id}|{target.id}"), relationship_type=
                                           "consists-of", source_ref=infra_id, target_ref=target.id, created=created,
                                           modified=created, created_by_ref=identity_id))
        objs.append(stix2.Relationship(id=_id("relationship", f"{camp_id}|uses|{infra_id}"), relationship_type="uses",
                                       source_ref=camp_id, target_ref=infra_id, created=created, modified=created,
                                       created_by_ref=identity_id))
        g = bits.get(("landing_host", dom)) or bits.get(("registrable_domain", h["domain"]))
        patterns = [(f"[domain-name:value = '{_q(dom)}']", f"Domena {dom}", f"domain|{dom}")]
        for u in h["urls"][:3]:
            p = urlsplit(u)
            if p.hostname and p.path not in ("", "/"):
                patterns.append((f"[url:value LIKE '{_q(p.scheme)}://{_q(p.hostname)}{_q(p.path)}%']",
                                 f"Wzorzec URL {p.hostname}{p.path}", f"url|{p.hostname}{p.path}"))
        for pat, name, key in patterns:
            ind_id = _id("indicator", f"{c.id}|{key}")
            objs.append(stix2.Indicator(id=ind_id, name=name, pattern=pat, pattern_type="stix", valid_from=created,
                                        created=created, modified=created, created_by_ref=identity_id,
                                        indicator_types=["malicious-activity"], allow_custom=True,
                                        x_hydra_bits=g.bits if g else None,
                                        x_hydra_doc_freq=g.doc_freq if g else None))
            objs.append(stix2.Relationship(id=_id("relationship", f"{ind_id}|indicates|{camp_id}"),
                                           relationship_type="indicates", source_ref=ind_id, target_ref=camp_id,
                                           created=created, modified=created, created_by_ref=identity_id))
    for g in c.core_genome:
        if g.kind in ("asset_sha256", "artifact_sha256") and g.value_a and len(g.value_a) == 64:
            ind_id = _id("indicator", f"{c.id}|sha256|{g.value_a}")
            objs.append(stix2.Indicator(id=ind_id, name=f"Plik wspólny kampanii ({g.kind})",
                                        pattern=f"[file:hashes.'SHA-256' = '{g.value_a}']", pattern_type="stix",
                                        valid_from=created, created=created, modified=created,
                                        created_by_ref=identity_id, indicator_types=["malicious-activity"],
                                        allow_custom=True, x_hydra_bits=g.bits, x_hydra_doc_freq=g.doc_freq))
            objs.append(stix2.Relationship(id=_id("relationship", f"{ind_id}|indicates|{camp_id}"),
                                           relationship_type="indicates", source_ref=ind_id, target_ref=camp_id,
                                           created=created, modified=created, created_by_ref=identity_id))
    host_set = {h["host"]: h for h in campaign_hosts(view)}
    for m in c.members:
        doms = [stix2.DomainName(value=d) for d, h in host_set.items() if m in h["incidents"]]
        if not doms:
            continue
        t = _ts((view.incidents.get(m) or {}).get("observed_at") or c.first_seen)
        od_id = _id("observed-data", f"{c.id}|{m}")
        objs.append(stix2.ObservedData(id=od_id, first_observed=t, last_observed=t, number_observed=1,
                                       object_refs=[d.id for d in doms], created=created, modified=created,
                                       created_by_ref=identity_id,
                                       external_references=[{"source_name": "hydra-nexus", "external_id": m}]))
        objs.append(stix2.Relationship(id=_id("relationship", f"{od_id}|related-to|{camp_id}"), relationship_type=
                                       "related-to", source_ref=od_id, target_ref=camp_id, created=created,
                                       modified=created, created_by_ref=identity_id))
    return objs


def export_stix(settings: Settings, campaign_id: str | None = None, run_id: str | None = None,
                multi_only: bool = True) -> dict[str, Any]:
    """STIX 2.1 bundle (dict) for one campaign, or every (multi-member) campaign of the run."""
    import stix2

    from hydra.pipeline import latest_run

    with closing(connect(settings.db_path, readonly=True)) as conn:
        if run_id and not run_known(conn, run_id):
            raise KeyError(f"run {run_id} not found")
        run_id = run_id or latest_run(conn)
        if run_id is None:
            raise KeyError("no analysed run")
        ids = [campaign_id] if campaign_id else [
            r["id"] for r in conn.execute("SELECT id, detail FROM campaigns WHERE run_id = ? ORDER BY id", (run_id,))
            if not multi_only or len(json.loads(r["detail"]).get("members", [])) > 1]
        views = [v for cid in ids if (v := load_campaign(conn, cid, run_id)) is not None]
    if campaign_id and not views:
        raise KeyError(f"campaign {campaign_id} not found")
    ident = stix2.Identity(id=_id("identity", IDENTITY_NAME), name=IDENTITY_NAME, identity_class="system",
                           created=datetime(2026, 11, 21, tzinfo=UTC), modified=datetime(2026, 11, 21, tzinfo=UTC))
    objs: dict[str, Any] = {ident.id: ident}
    for v in views:
        for o in campaign_objects(v, ident.id):
            objs.setdefault(o.id, o)
    bundle = stix2.Bundle(objects=list(objs.values()), id=_id("bundle", f"{run_id}|{campaign_id or 'run'}"),
                          allow_custom=True)
    text = bundle.serialize(sort_keys=True)
    stix2.parse(text, allow_custom=True)  # validation: every object re-parses as STIX 2.1
    return json.loads(text)
