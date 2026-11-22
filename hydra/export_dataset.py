"""Enriched, organised dataset export (NASK task §10: "wzbogacony i uporządkowany zbiór danych").

    export_dataset(settings, "latest", out_dir) -> summary dict

Used by `hydra export-dataset` and `GET /v1/export/dataset` (ZIP of the same folder). Reads one analysis run from
the SQLite store (schema v2/v3, read-only connection) and writes:

| File | Content |
|---|---|
| incidents.parquet + .csv | per incident: dates, artifacts per role, campaign, group, assignment, genes/family |
| genes.parquet | every gene: kind, value (redacted/hashed), tier, group, status, provenance, evidence |
| enrichment.parquet | per host/domain/IP: registrar, dates, NS, MX, A, ASN, SAN siblings, CERT PL hit |
| links.parquet | incident pairs of the run: bits, probability, hypothesis, family bits, top reasons |
| assignments.parquet + .csv | newest open-set assignment per incident across the run and its `assign` runs, with
  top candidate, bits, Polish reason, `duplicate_of` and `new_pattern_id` (from `Assignment.meta`, null if absent) |
| campaigns.json, campaign_groups.json | members, core genome, infrastructure summary |
| README_DATASET.md | data dictionary, sources, licences/terms, redaction rules, distribution notice |
| manifest.json | sha256 and size of every file |
| SIGNED_MANIFEST.json + .sig | Ed25519-signed canonical manifest: paths, sha256, sizes, run, commit, audit head |

Every string leaving the store passes `redact_text` again (tokens, JWTs, local user names) as a second line of
defence; ground-truth labels (`incidents.label`, eval only) are never exported. The output contains organiser
material and is not for public distribution (task §8).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.contracts import CONTRACTS_VERSION
from hydra.enrich.domains import redact_text as _domain_redact
from hydra.export_safe import csv_cell
from hydra.redact import redact_text as _central_redact

FAMILIES = ("VIS", "TXT", "INF", "ID", "NAR", "META")
ROLES = (
    "ad_creative",
    "feed_screenshot",
    "screen_recording",
    "ad_video",
    "landing_html",
    "landing_asset",
    "landing_screenshot",
    "har",
    "platform_metadata",
    "text",
    "url",
    "other",
)
INFRA_KINDS = (
    "landing_host",
    "registrable_domain",
    "registrar",
    "registration_day",
    "ns_set",
    "asn",
    "as_org",
    "tracker_id",
    "operator_id",
    "cert_san_sibling",
    "cert_pl_listed_host",
    "tds_signature",
    "url_label_skeleton",
)


def _j(s: str | None, default: Any) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def redact_text(v: str) -> str:
    """Central redaction (`hydra.redact`, CR-ASTRA-SEC-13) plus the long-token rule of the INF extractors."""
    return _domain_redact(_central_redact(v) or "")


def _r(v: Any) -> Any:
    return redact_text(v) if isinstance(v, str) else v


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def resolve_run(conn: sqlite3.Connection, run: str) -> str:
    if run != "latest":
        if not conn.execute("SELECT 1 FROM runs WHERE id=?", (run,)).fetchone():
            raise ValueError(f"run {run!r} not found")
        return run
    row = conn.execute(
        "SELECT id FROM runs WHERE kind='analyse' ORDER BY (finished_at IS NULL), created_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if not row:
        raise ValueError("no analyse run in the store")
    return row[0]


# ------------------------------------------------------------------------------------------- builders


def _campaigns(conn, run_id, tables):
    camps = {}
    for r in conn.execute("SELECT id, label, confidence, status, detail FROM campaigns WHERE run_id=?", (run_id,)):
        d = _j(r["detail"], {})
        camps[r["id"]] = {
            "id": r["id"],
            "label": r["label"],
            "confidence": r["confidence"],
            "status": r["status"],
            "detail": d,
            "members": [],
        }
    for r in conn.execute("SELECT campaign_id, incident_id FROM campaign_members WHERE run_id=?", (run_id,)):
        camps.setdefault(r["campaign_id"], {"id": r["campaign_id"], "label": "", "detail": {}, "members": []})
        camps[r["campaign_id"]]["members"].append(r["incident_id"])
    groups = {}
    if "campaign_groups" in tables:
        for r in conn.execute("SELECT id, label, confidence, detail FROM campaign_groups WHERE run_id=?", (run_id,)):
            groups[r["id"]] = {
                "id": r["id"],
                "label": r["label"],
                "confidence": r["confidence"],
                "detail": _j(r["detail"], {}),
            }
    if not groups:  # CR-CORE-1 workaround: groups stored in runs.metrics.campaign_groups
        m = conn.execute("SELECT metrics FROM runs WHERE id=?", (run_id,)).fetchone()
        for g in _j(m["metrics"] if m else None, {}).get("campaign_groups") or []:
            if isinstance(g, dict) and g.get("id"):
                groups[g["id"]] = {
                    "id": g["id"],
                    "label": g.get("label", ""),
                    "confidence": g.get("confidence"),
                    "detail": g,
                }
    camp_group = {}
    for gid, g in groups.items():
        for cid in g["detail"].get("campaign_ids") or []:
            camp_group[cid] = gid
    for cid, c in camps.items():
        gid = c["detail"].get("group_id") or (c["detail"].get("meta") or {}).get("group_id")
        if gid:
            camp_group.setdefault(cid, gid)
    return camps, groups, camp_group


def _assignment_runs(conn, run_id) -> list[str]:
    """The analyse run plus every `assign` run scored against it (params.base_run), as in the API views."""
    from hydra.dataflow import assign_runs

    return [run_id, *assign_runs(conn, run_id)]  # CR-E2E-5: also against the runs it revises


def _latest_assignments(conn, run_id) -> list[sqlite3.Row]:
    """Newest assignment per incident across the run family (ties: later run id), ordered by incident id."""
    runs = _assignment_runs(conn, run_id)
    q = ",".join("?" * len(runs))
    return conn.execute(
        "SELECT * FROM (SELECT incident_id, run_id, decision, campaign_id, detail, created_at, ROW_NUMBER() OVER "
        "(PARTITION BY incident_id ORDER BY created_at DESC, run_id DESC) AS rn FROM assignments "
        f"WHERE run_id IN ({q})) WHERE rn=1 ORDER BY incident_id",
        runs,
    ).fetchall()


def _ref(v: Any, *keys: str) -> str | None:
    """A meta reference that may be a plain id or an object carrying one; anything else -> None."""
    if isinstance(v, dict):
        v = next((v[k] for k in keys if v.get(k)), None)
    if v is None or isinstance(v, bool) or v == "":
        return None
    return _r(str(v)) if isinstance(v, str | int) else None


def _assignment_rows(rows) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        d = _j(r["detail"], {})
        meta = d.get("meta") if isinstance(d.get("meta"), dict) else {}
        cands = [c for c in d.get("candidates") or [] if isinstance(c, dict)]
        top = cands[0] if cands else {}
        best = top.get("best_pair") if isinstance(top.get("best_pair"), dict) else {}
        out.append(
            {
                "incident_id": r["incident_id"],
                "run_id": r["run_id"],
                "created_at": r["created_at"],
                "decision": r["decision"],
                "campaign_id": r["campaign_id"],
                "group_id": d.get("group_id"),
                "top_candidate": top.get("campaign_id"),
                "top_probability": top.get("probability"),
                "top_support": top.get("support"),
                "top_bits": best.get("total_bits"),
                "top_hypothesis": best.get("hypothesis"),
                "n_candidates": len(cands),
                "reason_pl": _r(d.get("reason_pl")),
                "model": d.get("model"),
                "duplicate_of": _ref(meta.get("duplicate_of"), "incident_id", "id"),
                "new_pattern_id": _ref(meta.get("new_pattern_id"), "id")
                or _ref(meta.get("new_pattern"), "id", "pattern_id"),
            }
        )
    return out


ASSIGNMENT_COLUMNS = [
    "incident_id",
    "run_id",
    "created_at",
    "decision",
    "campaign_id",
    "group_id",
    "top_candidate",
    "top_probability",
    "top_support",
    "top_bits",
    "top_hypothesis",
    "n_candidates",
    "reason_pl",
    "model",
    "duplicate_of",
    "new_pattern_id",
]


def _run_scope(conn, run_id, camps, latest) -> list[str]:
    """Incidents of the run (CR-ASTRA-PIPE-301): campaign members, items assigned against it, ends of its pairs."""
    ids = {m for c in camps.values() for m in c["members"]} | {r["incident_id"] for r in latest}
    ids |= {x for r in conn.execute("SELECT a, b FROM pairs WHERE run_id=?", (run_id,)) for x in r}
    return sorted(ids)


def _scoped(conn, sql: str, ids: list[str], tail: str = ""):
    """`sql` + ` WHERE incident_id IN (...)` in chunks (SQLite variable limit); rows in chunk order."""
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        yield from conn.execute(f"{sql} IN ({','.join('?' * len(chunk))}){tail}", chunk)


def _incident_rows(conn, run_id, camps, camp_group, latest, scope):
    inc_camp = {m: cid for cid, c in camps.items() for m in c["members"]}
    assign = {r["incident_id"]: (r["decision"], r["campaign_id"]) for r in latest}
    roles: dict[str, Counter] = defaultdict(Counter)
    for r in _scoped(conn, "SELECT incident_id, role FROM incident_artifacts WHERE incident_id", scope):
        roles[r["incident_id"]][r["role"]] += 1
    cov: dict[str, Counter] = defaultdict(Counter)
    total: Counter = Counter()
    for r in _scoped(conn, "SELECT incident_id, family, status, COUNT(*) n FROM genes WHERE incident_id", scope,
                     " GROUP BY 1, 2, 3"):
        total[r["incident_id"]] += r["n"]
        if r["status"] == "observed":
            cov[r["incident_id"]][r["family"]] += r["n"]
    rows = []
    for r in _scoped(conn, "SELECT id, source, source_ref, title, observed_at, meta, created_at FROM incidents"
                     " WHERE id", scope, " ORDER BY id"):
        iid = r["id"]
        meta = _j(r["meta"], {})
        cid = inc_camp.get(iid)
        camp = camps.get(cid or "", {})
        dec, acid = assign.get(iid, (None, None))
        row = {
            "incident_id": iid,
            "source": r["source"],
            "source_ref": _r(r["source_ref"]),
            "title": _r(r["title"]),
            "scheme": (camp.get("detail") or {}).get("scheme") or meta.get("scheme"),
            "observed_at": r["observed_at"],
            "imported_at": r["created_at"],
            "campaign_id": cid,
            "campaign_label": _r(camp.get("label")) if camp else None,
            "group_id": camp_group.get(cid) if cid else None,
            "assignment_decision": dec,
            "assignment_campaign_id": acid,
            "n_artifacts": sum(roles[iid].values()),
            "n_genes": total[iid],
        }
        for role in ROLES:
            row[f"artifacts_{role}"] = roles[iid].get(role, 0)
        for f in FAMILIES:
            row[f"genes_{f}"] = cov[iid].get(f, 0)
        row["families_observed"] = ",".join(f for f in FAMILIES if cov[iid].get(f))
        rows.append(row)
    return rows


def _gene_rows(conn, scope):
    for r in _scoped(conn, "SELECT incident_id, family, kind, value, vector_ref, quality, status, provenance, "
                     "evidence, extractor, dependency_group FROM genes WHERE incident_id", scope,
                     " ORDER BY incident_id, id"):
        dg = r["dependency_group"]
        parts = (dg or "").split(":")
        tier = parts[1] if len(parts) >= 3 and parts[0] in ("inf", "vis", "txt", "id", "nar", "meta") else None
        yield {
            "incident_id": r["incident_id"],
            "family": r["family"],
            "kind": r["kind"],
            "value": _r(r["value"]),
            "tier": tier,
            "dependency_group": dg,
            "status": r["status"],
            "provenance": r["provenance"],
            "quality": r["quality"],
            "extractor": r["extractor"],
            "vector_ref": r["vector_ref"],
            "evidence": redact_text(r["evidence"] or "{}"),
        }


def _enrichment_entity(source: str, query: str) -> str:
    """Name the entity a stored enrichment record describes (same keys as `_enrichment_rows`)."""
    return query.rpartition("_")[0] if source == "dns" else query


def _scope_enrichment(conn, rows: list[dict[str, Any]], scope: list[str]) -> list[dict[str, Any]]:
    """Enrichment records about entities observed in the run's incidents (CR-ASTRA-PIPE-301): a host/domain/IP
    gene value of a scoped incident, a parent domain of one, or an IP a kept host resolved to."""
    values = {str(r[0]).strip().lower().rstrip(".") for r in _scoped(
        conn, "SELECT value FROM genes WHERE status='observed' AND value IS NOT NULL AND incident_id", scope)}

    names = {v.split(".", i)[-1] for v in values if len(v) < 256 for i in range(v.count(".") + 1)}  # + parents

    def wanted(name: str) -> bool:
        n = name.strip().lower().rstrip(".")
        return bool(n) and n in names

    keep = [wanted(_enrichment_entity(r["source"], r["query"] or "")) for r in rows]
    ips = {str(a) for r, k in zip(rows, keep, strict=True) if k and r["source"] == "dns"
           for a in (_j(r["summary"], {}).get("answers") or [])}
    return [r for r, k in zip(rows, keep, strict=True) if k or (r["source"] == "asn" and r["query"] in ips)]


def _enrichment_source_rows(conn, settings: Settings, scope: list[str]) -> tuple[list[dict[str, Any]], str]:
    """Rows of the `enrichment` table about the run's incidents; if the pipeline did not persist them, rebuild
    them from the replay cache (never live: export must not touch the network)."""
    rows = [dict(r) for r in conn.execute("SELECT source, query, fetched_at, status, summary FROM enrichment")]
    if rows:
        return _scope_enrichment(conn, rows, scope), "enrichment table"
    try:
        from hydra.contracts import Incident, IncidentArtifact
        from hydra.enrich import Enricher, records_for
        from hydra.genome.base import ResolvedArtifact, ResolvedIncident
        from hydra.storage import ContentStore
    except ImportError:
        return [], "none"
    store = ContentStore(settings.cas_dir)
    enr = Enricher(settings=settings, live=False)
    arts: dict[str, list] = defaultdict(list)
    for r in conn.execute(
        "SELECT ia.incident_id, ia.artifact_id, ia.role, ia.path_in_incident, a.mime, a.size "
        "FROM incident_artifacts ia JOIN artifacts a ON a.id = ia.artifact_id"
    ):
        arts[r["incident_id"]].append(r)
    out: dict[str, dict[str, Any]] = {}
    for r in _scoped(conn, "SELECT id, source, source_ref, observed_at FROM incidents WHERE id", scope):
        metas = [
            IncidentArtifact(
                artifact_id=a["artifact_id"],
                role=a["role"],
                path_in_incident=a["path_in_incident"],
                mime=a["mime"] or "",
                size=a["size"] or 0,
            )
            for a in arts[r["id"]]
        ]
        inc = Incident(
            id=r["id"], source=r["source"], source_ref=r["source_ref"], observed_at=r["observed_at"], artifacts=metas
        )
        res = ResolvedIncident(
            inc,
            [
                ResolvedArtifact(m, store.path_for(m.artifact_id))
                for m in metas
                if store.path_for(m.artifact_id).is_file()
            ],
        )
        try:
            recs = records_for(res, enr)
        except Exception:  # noqa: BLE001 — export degrades to fewer enrichment rows, never fails
            continue
        for rec in recs:
            out.setdefault(
                rec.key,
                {
                    "source": rec.source,
                    "query": rec.query,
                    "fetched_at": rec.fetched_at,
                    "status": rec.status,
                    "summary": json.dumps(rec.summary, default=str),
                },
            )
    return list(out.values()), "replay cache (enrichment table empty)"


def _enrichment_rows(source_rows):
    ent: dict[str, dict[str, Any]] = {}

    def e(name: str, typ: str) -> dict[str, Any]:
        x = ent.setdefault(name, {"entity": name, "entity_type": typ, "sources": set(), "fetched": []})
        return x

    host_ips: dict[str, list[str]] = defaultdict(list)
    asn: dict[str, dict[str, Any]] = {}
    for r in source_rows:
        s = _j(r["summary"], {})
        src, q = r["source"], r["query"]
        if src == "rdap":
            x = e(q, "domain")
            if r["status"] == "observed":
                x.update(
                    registrar=s.get("registrar"),
                    registrar_iana=s.get("registrar_iana"),
                    created=s.get("registration"),
                    expires=s.get("expiration"),
                    rdap_nameservers=",".join(s.get("nameservers") or []),
                )
        elif src == "crtsh":
            x = e(q, "domain")
            if r["status"] == "observed":
                x.update(
                    cert_count=s.get("n_certs"),
                    cert_issuers=",".join(s.get("issuers") or []),
                    cert_first=s.get("first_cert"),
                    cert_last=s.get("last_cert"),
                    cert_san_siblings=",".join(n for n in (s.get("sans") or []) if n != q and not n.endswith("." + q))[
                        :4000
                    ],
                )
        elif src == "dns":
            name, _, rtype = q.rpartition("_")
            if not name or name.endswith("cymru.com"):
                continue
            x = e(name, "host")
            ans = s.get("answers") or []
            if rtype in ("A", "AAAA"):
                host_ips[name] += ans
            x[f"dns_{rtype.lower()}"] = ",".join(ans)
            if s.get("status") in ("NXDOMAIN",):
                x["dns_rcode"] = "NXDOMAIN"
        elif src == "asn":
            asn[q] = s
            x = e(q, "ip")
            x.update(asn=s.get("asn"), as_name=s.get("as_name"), bgp_prefix=s.get("prefix"), country=s.get("cc"))
        elif src == "cert_pl_list":
            x = e(q, "host")
            x.update(
                cert_pl_listed=bool(s.get("host_listed")),
                cert_pl_insert=s.get("insert_date"),
                cert_pl_parent=s.get("parent_listed"),
                cert_pl_snapshot=s.get("snapshot"),
            )
        else:
            x = e(q, src)
        x["sources"].add(src)
        x["fetched"].append(r["fetched_at"])
    for name, ips in host_ips.items():
        a = [asn[i] for i in dict.fromkeys(ips) if i in asn]
        ent[name]["asns"] = ",".join(sorted({f"AS{x['asn']}" for x in a if x.get("asn")}))
        ent[name]["as_names"] = ",".join(sorted({str(x.get("as_name")) for x in a if x.get("as_name")}))
    rows = []
    for x in ent.values():
        f = sorted(t for t in x.pop("fetched") if t)
        x["sources"] = ",".join(sorted(x["sources"]))
        x["fetched_at_first"], x["fetched_at_last"] = (f[0], f[-1]) if f else (None, None)
        rows.append({k: _r(v) for k, v in x.items()})
    return sorted(rows, key=lambda r: (r["entity_type"], r["entity"]))


def _link_rows(conn, run_id):
    for r in conn.execute(
        "SELECT a, b, total_bits, probability, calibrated, hypothesis, model, detail FROM pairs "
        "WHERE run_id=? ORDER BY total_bits DESC",
        (run_id,),
    ):
        d = _j(r["detail"], {})
        reasons = d.get("reasons") or []
        top = [
            f"{x.get('family')}/{x.get('kind')}={_r(str(x.get('value_a') or ''))[:80]} ({x.get('bits', 0):+.1f})"
            for x in reasons[:5]
        ]
        yield {
            "a": r["a"],
            "b": r["b"],
            "total_bits": r["total_bits"],
            "probability": r["probability"],
            "calibrated": bool(r["calibrated"]),
            "hypothesis": r["hypothesis"],
            "model": r["model"],
            "family_bits": json.dumps(d.get("family_bits") or {}, sort_keys=True),
            "missing_families": ",".join(d.get("missing_families") or []),
            "n_contradictions": len(d.get("contradictions") or []),
            "n_reasons": len(reasons),
            "top_reasons": " | ".join(top),
            "reasons_pl": " | ".join(_r(str(x.get("explanation_pl") or "")) for x in reasons[:5]),
        }


def _infra_summary(conn, members: list[str]) -> dict[str, list[str]]:
    if not members:
        return {}
    q = ",".join("?" * len(members))
    kinds = ",".join("?" * len(INFRA_KINDS))
    out: dict[str, Counter] = defaultdict(Counter)
    for r in conn.execute(
        f"SELECT kind, value, dependency_group FROM genes WHERE status='observed' AND value IS NOT NULL "
        f"AND incident_id IN ({q}) AND kind IN ({kinds})",
        (*members, *INFRA_KINDS),
    ):
        if (r["dependency_group"] or "").startswith("inf:boilerplate"):
            continue
        out[r["kind"]][_r(r["value"])] += 1
    return {k: [v for v, _ in c.most_common(20)] for k, c in sorted(out.items())}


# ------------------------------------------------------------------------------------------- writers


def _write_parquet(rows: list[dict[str, Any]], path: Path, columns: list[str] | None = None) -> int:
    import pandas as pd

    df = pd.DataFrame(rows, columns=columns) if not rows and columns else pd.DataFrame(rows)
    for c in df.columns:  # mixed object columns -> string, so pyarrow never guesses wrong
        if df[c].dtype == object:
            vals = [v for v in df[c] if not (v is None or (isinstance(v, float) and v != v))]  # drop None/NaN
            if vals and all(isinstance(v, bool) for v in vals):
                df[c] = df[c].map(lambda v: v if isinstance(v, bool) else None).astype("boolean")
            elif vals and all(isinstance(v, int | float) and not isinstance(v, bool) for v in vals):
                df[c] = pd.to_numeric(df[c])
            else:
                df[c] = df[c].map(
                    lambda v: (
                        v
                        if v is None or isinstance(v, str) or (isinstance(v, float) and v != v)
                        else json.dumps(v, default=str)
                    )
                )
    df.to_parquet(path, index=False)
    return len(df)


def export_dataset(settings: Settings, run: str, out_dir: Path, campaign_id: str | None = None) -> dict[str, Any]:
    """Signed dataset of one run. With `campaign_id` (audit B15) only that campaign: its members, the material
    assigned to it later, their genes/enrichment, the pairs inside that scope and the groups that contain it;
    KeyError when the campaign is not in the run."""
    from hydra.db import connect

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path, readonly=True)
    try:
        tables = _tables(conn)
        run_id = resolve_run(conn, run)
        run_row = conn.execute(
            "SELECT id, kind, params, metrics, created_at, finished_at FROM runs WHERE id=?", (run_id,)
        ).fetchone()
        camps, groups, camp_group = _campaigns(conn, run_id, tables)
        latest = _latest_assignments(conn, run_id)
        if campaign_id is not None:
            if campaign_id not in camps:
                raise KeyError(f"campaign {campaign_id} not found in run {run_id}")
            camps = {campaign_id: camps[campaign_id]}
            groups = {k: g for k, g in groups.items() if campaign_id in (g["detail"].get("campaign_ids") or [])}
            members = set(camps[campaign_id]["members"])
            latest = [r for r in latest if r["incident_id"] in members or r["campaign_id"] == campaign_id]
            scope = sorted(members | {r["incident_id"] for r in latest})
        else:
            scope = _run_scope(conn, run_id, camps, latest)
        in_scope = set(scope)
        incidents = _incident_rows(conn, run_id, camps, camp_group, latest, scope)
        assignments = _assignment_rows(latest)
        import pandas as pd

        counts: dict[str, int] = {}
        counts["incidents"] = _write_parquet(incidents, out / "incidents.parquet")
        pd.DataFrame([{k: csv_cell(v) if isinstance(v, str) else v for k, v in row.items()} for row in incidents],
                     columns=list(incidents[0]) if incidents else None).to_csv(out / "incidents.csv", index=False)
        gene_cols = [
            "incident_id",
            "family",
            "kind",
            "value",
            "tier",
            "dependency_group",
            "status",
            "provenance",
            "quality",
            "extractor",
            "vector_ref",
            "evidence",
        ]
        counts["genes"] = _write_parquet(list(_gene_rows(conn, scope)), out / "genes.parquet", gene_cols)
        src_rows, enrich_from = _enrichment_source_rows(conn, settings, scope)
        counts["enrichment_records"] = len(src_rows)
        counts["enrichment_entities"] = _write_parquet(
            _enrichment_rows(src_rows), out / "enrichment.parquet", ["entity", "entity_type", "sources"]
        )
        counts["links"] = _write_parquet(
            [r for r in _link_rows(conn, run_id) if campaign_id is None or (r["a"] in in_scope and r["b"] in in_scope)],
            out / "links.parquet", ["a", "b", "total_bits", "probability", "hypothesis"]
        )
        counts["assignments"] = _write_parquet(assignments, out / "assignments.parquet", ASSIGNMENT_COLUMNS)
        pd.DataFrame([{k: csv_cell(v) if isinstance(v, str) else v for k, v in row.items()} for row in assignments],
                     columns=ASSIGNMENT_COLUMNS).to_csv(out / "assignments.csv", index=False)  # CR-ASTRA-SEC-10
        counts["duplicates"] = sum(1 for a in assignments if a["duplicate_of"])
        counts["new_patterns"] = sum(1 for a in assignments if a["new_pattern_id"])
        camp_out = []
        for c in camps.values():
            d = c["detail"]
            camp_out.append(
                {
                    "id": c["id"],
                    "label": _r(c["label"]),
                    "confidence": c.get("confidence"),
                    "status": c.get("status"),
                    "group_id": camp_group.get(c["id"]),
                    "scheme": d.get("scheme"),
                    "first_seen": d.get("first_seen"),
                    "last_seen": d.get("last_seen"),
                    "members": sorted(c["members"]),
                    "core_genome": json.loads(redact_text(json.dumps(d.get("core_genome") or [], ensure_ascii=False))),
                    "infra_summary": _infra_summary(conn, c["members"]),
                }
            )
        (out / "campaigns.json").write_text(json.dumps(camp_out, ensure_ascii=False, indent=1))
        grp_out = [
            {
                "id": g["id"],
                "label": _r(g["label"]),
                "confidence": g["confidence"],
                "campaign_ids": g["detail"].get("campaign_ids") or [],
                "members": sorted(
                    {m for cid in g["detail"].get("campaign_ids") or [] for m in camps.get(cid, {}).get("members", [])}
                ),
                "shared_genes": json.loads(
                    redact_text(json.dumps(g["detail"].get("shared_genes") or [], ensure_ascii=False))
                ),
            }
            for g in groups.values()
        ]
        (out / "campaign_groups.json").write_text(json.dumps(grp_out, ensure_ascii=False, indent=1))
        counts["campaigns"], counts["campaign_groups"] = len(camp_out), len(grp_out)
        run_info = {
            "run_id": run_id,
            "kind": run_row["kind"],
            "created_at": run_row["created_at"],
            "finished_at": run_row["finished_at"],
            "params": _j(run_row["params"], {}),
            "enrichment_from": enrich_from,
        }
        if campaign_id is not None:
            run_info["campaign_id"] = campaign_id  # scope recorded in the signed manifest
    finally:
        conn.close()
    (out / "README_DATASET.md").write_text(readme(run_info, counts))
    from hydra.evidence_sign import SIGNATURE_FILES  # written after this manifest, signed separately

    files = sorted(p for p in out.iterdir() if p.is_file() and p.name != "manifest.json"
                   and p.name not in SIGNATURE_FILES)
    manifest = {
        "dataset": "HYDRA NEXUS enriched dataset",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "contracts_version": CONTRACTS_VERSION,
        "run": run_info,
        "counts": counts,
        "notice": "Contains organiser material (NASK). Not for public distribution (task §8).",
        "files": [
            {"name": p.name, "size": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
            for p in files
        ],
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    from hydra.evidence_sign import sign_package

    signed = sign_package(settings, out, run_id)  # SIGNED_MANIFEST.json + .sig over every file (spec test 7)
    return {
        "signature": {"key_fingerprint": signed["key_fingerprint"], "files": len(signed["files"]),
                      "audit_chain_head": signed["audit_chain_head"]},
        "out_dir": str(out),
        "run_id": run_id,
        "counts": counts,
        "bytes": sum(p.stat().st_size for p in out.iterdir() if p.is_file()),
    }


def readme(run_info: dict[str, Any], counts: dict[str, int]) -> str:
    c = counts
    return f"""# HYDRA NEXUS: enriched incident dataset

**NOTICE: this dataset contains organiser material (NASK PIB, "Kolekcjoner fejków"). It is provided to the
organiser only and is NOT for public distribution (task description §8).**

Run `{run_info["run_id"]}` (kind {run_info["kind"]}, created {run_info["created_at"]}, finished
{run_info["finished_at"]}). Contracts {CONTRACTS_VERSION}. Generated by `hydra export-dataset` or
`GET /v1/export/dataset` (`hydra/export_dataset.py`). Counts: {c.get("incidents", 0)} incidents,
{c.get("genes", 0)} genes, {c.get("enrichment_entities", 0)} enriched hosts/domains/IPs, {c.get("links", 0)} scored
pairs, {c.get("assignments", 0)} assignments ({c.get("duplicates", 0)} duplicates, {c.get("new_patterns", 0)} new
patterns), {c.get("campaigns", 0)} campaigns, {c.get("campaign_groups", 0)} campaign groups. File hashes:
`manifest.json`; Ed25519 signature over every file: `SIGNED_MANIFEST.json` + `SIGNED_MANIFEST.sig`, check
with `hydra verify-export <zip|folder>`. Scope: only this run's incidents (its campaign members, items assigned
against it and the ends of its scored pairs), their genes, and enrichment of hosts/domains/IPs observed in them.

## Files and data dictionary

### incidents.parquet / incidents.csv (one row per incident)
| Column | Meaning |
|---|---|
| incident_id, source, source_ref | stable id (e.g. `nask-61`), input channel, origin reference (user names redacted) |
| title, scheme | incident title; scheme of its campaign (investment, health, ...) if known |
| observed_at, imported_at | earliest date from the material (UTC ISO-8601); import time |
| campaign_id, campaign_label, group_id | campaign of the run and its group (similar campaigns: shared kit) |
| assignment_decision, assignment_campaign_id | newest open-set decision: assign / new_campaign / abstain |
| n_artifacts, artifacts_<role> | artifact counts per role (ad_creative, landing_html, har, ...) |
| n_genes, genes_<FAMILY>, families_observed | all genes; observed genes per family (VIS, TXT, INF, ID, NAR, META) |

### genes.parquet (one row per gene)
| Column | Meaning |
|---|---|
| incident_id, family, kind | owner incident; family; gene kind (e.g. `landing_host`, `tracker_id`, `pdq`, `phrase`) |
| value | value / hash / normalised text; personal data and secrets: salted hash `h:<hex>` or `[REDACTED]` |
| tier, dependency_group | INF evidence tier (operator/platform/kit/boilerplate/label) and the bit-budget group |
| status | observed / unavailable / failed / not_applicable (missing data is unknown, never evidence against) |
| provenance, quality, extractor | OBS / INF / HYP; prior quality 0–1; `name@version` of the extractor |
| vector_ref | key of an embedding vector (vectors are not exported) |
| evidence | JSON pointer: artifact sha256, bbox, char range, timecode, HAR entry, DOM path, enrichment key |

### enrichment.parquet (one row per host, domain or IP)
entity, entity_type (domain / host / ip); RDAP: registrar, registrar_iana, created, expires, rdap_nameservers;
DNS: dns_a, dns_aaaa, dns_cname, dns_ns, dns_mx, dns_txt, dns_rcode, asns, as_names (via the host's IPs);
IP: asn, as_name, bgp_prefix, country; crt.sh: cert_count, cert_issuers, cert_first, cert_last, cert_san_siblings
(other names on the same certificates); CERT Polska: cert_pl_listed (exact host), cert_pl_insert, cert_pl_parent,
cert_pl_snapshot; sources, fetched_at_first, fetched_at_last (results depend on the lookup date).

### links.parquet (one row per scored pair of the run)
a, b, total_bits (evidence weight, log2 likelihood-ratio contributions; not a probability), probability
(calibrated only if `calibrated` is true), hypothesis (same_campaign / shared_template / shared_asset /
independent / abstain), model, family_bits (JSON), missing_families, n_contradictions, top_reasons
(`FAMILY/kind=value (bits)`), reasons_pl (Polish explanations).

### assignments.parquet / assignments.csv (one row per assigned incident)
Newest assignment per incident across the run and the `assign` runs scored against it (`params.base_run`).
incident_id, run_id (run that holds the assignment), created_at, decision (assign / new_campaign / abstain),
campaign_id, group_id (known group a new campaign still fits), top_candidate, top_probability, top_support (members
with a supporting pair), top_bits and top_hypothesis (best pair of the top candidate), n_candidates, reason_pl (Polish
explanation), model, duplicate_of (incident this one duplicates, `Assignment.meta.duplicate_of`), new_pattern_id
(new pattern opened by this incident, `Assignment.meta.new_pattern_id` / `new_pattern`); both null when absent.

### campaigns.json, campaign_groups.json
Campaigns: id, label, confidence, status, group_id, scheme, first_seen, last_seen, members, core_genome (genes shared
by most members, with bits and explanations), infra_summary (non-boilerplate landing hosts, domains, registrars,
registration days, NS, ASNs, tracker and operator IDs, SAN siblings, CERT Polska hits of the members).
Groups: id, label, confidence, campaign_ids, members, shared_genes.

## Sources and terms
- Organiser material (ad creatives, screen recordings, saved pages, HAR logs, platform metadata): NASK, for this
  challenge only, no public distribution.
- Public infrastructure metadata, cached with timestamps (ADR-007): RDAP via rdap.org (registry public data;
  registry notices forbid bulk harvesting), DNS via public resolvers 1.1.1.1 / 8.8.8.8, Team Cymru IP-to-ASN
  (free community service), crt.sh certificate transparency (Sectigo, public CT logs, RFC 6962), CERT Polska
  warning list snapshot (published for public use; snapshot date in each row), Public Suffix List (MPL-2.0).
- Details, limits and licences: `docs/genes/INF.md` §3 and `docs/DATA_SOURCES.md` in the repository.

## Redaction rules
1. Access tokens (FB `EAA...`), JWTs, API keys, long mixed-case secrets and local user names in paths are replaced
   by `[REDACTED]`; secret URL parameter values are removed from stored URLs.
2. Values that identify a campaign but are secret or personal (TDS keys, CAPI tokens, phone numbers, e-mail
   addresses, IBANs) are stored only as salted sha256 prefixes `h:<16 hex>`; equal values stay linkable.
3. Every string is redacted a second time at export. Ground-truth labels and embedding vectors are not exported.
4. Bits are evidence weights, not probabilities; probabilities are calibrated only where marked.
"""
