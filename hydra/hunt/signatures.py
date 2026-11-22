"""Campaign signature (INNOWACJE §4): what stays stable across APPROVED members vs what is exchangeable decoration.

Per (kind, value) carried by the members:

    utility = stability x rarity x reliability          (heuristic priority, NOT a probability)

- stability   = members carrying the value / approved members
- rarity      = 1 / (1 + df) where df counts NON-member incidents carrying it. The background is CONDITIONAL when
                the kit is known: df is counted among "kit users" (non-members sharing >= 1 kit-tier value with the
                campaign), because a value rare in the corpus can be common among customers of one landing kit.
                Otherwise the whole revision corpus is the background (`background_basis` says which).
- reliability = OBS 1.0, INF 0.6, HYP 0.0 (a hypothesis never confirms itself, rule 2)

Roles: `invariant` (operator or kit tier, stable, df <= 1), `decoration` (kind seen with >= 2 values among members,
or a decoration-tier gene), `kit`/`unstable` (kept for display, never required), `boilerplate` (0 weight).
Brand is decoration by construction: it is never an invariant and never ground truth (rule 4).

Members come only from a published revision (hydra.review): analyst-approved members (must-link endpoints or
analyst-added incidents) when there are >= 2 of them, else all members of the published revision, and
`approval_basis` records which. Assignment-run candidates and HUNT observations never enter (rule 1).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable
from contextlib import closing
from typing import Any

from hydra.contracts import Gene
from hydra.hunt.hypotheses import parse_ts
from hydra.hunt.records import DatasetKind, Signature, SignatureGene, Tier, Trait

RELIABILITY = {"OBS": 1.0, "INF": 0.6, "HYP": 0.0}
STABLE_MIN = 0.66
INVARIANT_MAX_DF = 1
MAX_INVARIANTS_PER_GROUP = 5
CONTEXT_KINDS = {"scheme"}  # narrative type: a contradiction check (hydra.synapse), never a required invariant
DECORATION_KINDS = {"brand_mention", "brand", "brand_label", "person_named", "face", "headline", "title", "logo",
                    "persona", "ocr_line", "phrase_headline"}
MUTABLE_BY_RULE = ["domain", "host", "headline", "person_named", "brand_label"]  # used only with no observed variation
HARD_CONFLICTS = ["cannot_link_with_member", "different_operator_id", "scheme_contradiction"]
GROUP_OF_TIER = {Tier.OPERATOR: "operator", Tier.KIT: "page_structure", Tier.DECORATION: "decoration",
                 Tier.BOILERPLATE: "boilerplate"}


def _source_of(g: Gene) -> str:
    ev = g.evidence
    if ev.har_entry is not None:
        return "har"
    if ev.dom_path:
        return "dom"
    if ev.enrichment_key or str(g.family) == "META":
        return "metadata"
    if ev.bbox is not None and str(g.family) == "TXT":
        return "ocr"
    if str(g.family) in ("VIS", "ID"):
        return "image"
    if str(g.family) == "TXT":
        return "text"
    return "url"


def trait_of_gene(g: Gene) -> Trait | None:
    """Store gene -> hunt trait; tiers follow hydra.synapse.level (the same scoring policy, rule 8)."""
    from hydra.synapse import level

    if str(g.status) != "observed" or g.value is None:
        return None
    lv = level(g)
    tier = {"campaign": Tier.OPERATOR, "kit": Tier.KIT, "asset": Tier.DECORATION, "none": Tier.BOILERPLATE}[lv]
    if g.kind in DECORATION_KINDS:
        tier = Tier.DECORATION
    return Trait(kind=g.kind, value=str(g.value)[:200], tier=tier, group=GROUP_OF_TIER[Tier(tier)],
                 dep=g.dependency_group or f"{g.family}:{g.kind}", source=_source_of(g), provenance=str(g.provenance))


def build_signature(campaign_id: str, revision: str, members: dict[str, list[Trait]],
                    background: dict[str, list[Trait]], *, approval_basis: str,
                    dataset_kind: DatasetKind = DatasetKind.REAL) -> Signature:
    """Pure function: approved members' traits + non-member traits -> sealed Signature."""
    m = len(members)
    if m == 0:
        raise ValueError("a signature needs at least one approved member")
    carriers: dict[tuple[str, str], set[str]] = defaultdict(set)
    proto: dict[tuple[str, str], Trait] = {}
    values_of_kind: dict[str, set[str]] = defaultdict(set)
    for iid, traits in members.items():
        for t in traits:
            if t.provenance == "HYP":
                continue  # rule 2: hypothesis output never becomes a signature gene
            carriers[(t.kind, t.value)].add(iid)
            values_of_kind[t.kind].add(t.value)
            proto.setdefault((t.kind, t.value), t)
    member_kit = {k for k, t in proto.items() if t.tier == Tier.KIT}
    kit_users = sorted(iid for iid, ts in background.items() if any((t.kind, t.value) in member_kit for t in ts))
    bg_basis, bg_ids = ("kit_users", kit_users) if kit_users else ("corpus", sorted(background))
    df: Counter[tuple[str, str]] = Counter()
    for iid in bg_ids:
        for k in {(t.kind, t.value) for t in background[iid]}:
            df[k] += 1
    stable_min = 1.0 if m <= 2 else STABLE_MIN
    genes: list[SignatureGene] = []
    for k, t in proto.items():
        stab = len(carriers[k]) / m
        rar = 1.0 / (1.0 + df[k])
        rel = RELIABILITY.get(t.provenance, 0.0)
        varies = len(values_of_kind[t.kind]) >= 2
        if t.tier == Tier.BOILERPLATE:
            role = "boilerplate"
        elif t.tier == Tier.DECORATION or (varies and stab < 1.0):
            role = "decoration"
        elif stab >= stable_min and df[k] <= INVARIANT_MAX_DF and rel > 0 and t.kind not in CONTEXT_KINDS:
            role = "invariant"
        else:
            role = "kit" if t.tier == Tier.KIT else "unstable"
        genes.append(SignatureGene(kind=t.kind, value=t.value, tier=t.tier, group=t.group, dep=t.dep, source=t.source,
                                   role=role, members_with=len(carriers[k]), members=m, stability=round(stab, 3),
                                   background_df=df[k], background_n=len(bg_ids), background_basis=bg_basis,
                                   rarity=round(rar, 3), reliability=rel, utility=round(stab * rar * rel, 3)))
    genes.sort(key=lambda g: (-g.utility, g.kind, g.value))
    # one gene per dependency group (rule 6), at most N per coarse group
    invariants: list[str] = []
    seen_dep: set[str] = set()
    per_group: Counter[str] = Counter()
    for g in genes:
        if g.role != "invariant" or g.dep in seen_dep or per_group[g.group] >= MAX_INVARIANTS_PER_GROUP:
            continue
        seen_dep.add(g.dep)
        per_group[g.group] += 1
        invariants.append(f"{g.kind}={g.value}")
    observed_mut = sorted(k for k, vs in values_of_kind.items() if len(vs) >= 2)
    mutations, basis = (observed_mut, "observed") if observed_mut else (MUTABLE_BY_RULE, "experimental_rule")
    required = sorted(per_group)
    inv_utils = [g.utility for g in genes if f"{g.kind}={g.value}" in invariants]
    priority = round(sum(inv_utils) * len(required), 3)
    sig = Signature(campaign_id=campaign_id, campaign_revision=revision, approval_basis=approval_basis,
                    members=sorted(members), genes=genes, invariants=invariants, allowed_mutations=mutations,
                    mutations_basis=basis, required_evidence_groups=required, hard_conflicts=HARD_CONFLICTS,
                    kit_users=kit_users, priority=priority, dataset_kind=dataset_kind)
    return sig.seal()


def has_operator_invariant(sig: Signature) -> bool:
    """Rule 4: a shared kit / template alone never names an operator, so it cannot carry a hypothesis."""
    return any(g.role == "invariant" and g.tier == Tier.OPERATOR for g in sig.genes)


# ------------------------------------------------------------------------------------------- store side


def traits_by_incident(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, list[Trait]]:
    from hydra.pipeline import _row_gene

    out: dict[str, list[Trait]] = {}
    for iid in ids:
        ts = [trait_of_gene(_row_gene(r)) for r in conn.execute("SELECT * FROM genes WHERE incident_id = ?", (iid,))]
        out[iid] = [t for t in ts if t is not None]
    return out


def trait_timeline(conn: sqlite3.Connection, ids: Iterable[str]
                   ) -> dict[str, list[tuple[Trait, str | None, str | None]]]:
    """Per incident: (trait, available_from, retired_at) for the current genes AND the value genes retired by a
    later correction (`incidents.meta.evidence_retired`). `available_from` is `genes.created_at` (when HYDRA first
    held the gene, CR-ASTRA-RES-4) or None when that time is unknown; None is never replaced by an earlier time."""
    from hydra.pipeline import _row_gene

    def _time(s: Any) -> str | None:
        try:
            return s if s and parse_ts(str(s)) else None
        except ValueError:
            return None

    out: dict[str, list[tuple[Trait, str | None, str | None]]] = {}
    for iid in ids:
        items: list[tuple[Trait, str | None, str | None]] = []
        for r in conn.execute("SELECT * FROM genes WHERE incident_id = ? ORDER BY id", (iid,)):
            t = trait_of_gene(_row_gene(r))
            if t is not None:
                items.append((t, _time(r["created_at"]), None))
        row = conn.execute("SELECT meta FROM incidents WHERE id = ?", (iid,)).fetchone()
        for g in (json.loads(row["meta"] or "{}").get("evidence_retired") or []) if row is not None else []:
            try:
                gene = Gene(incident_id=iid, family=g["family"], kind=g["kind"], value=g["value"],
                            status=g["status"], provenance=g["provenance"], evidence=g.get("evidence") or {},
                            extractor=g["extractor"], dependency_group=g.get("dependency_group"))
            except (KeyError, ValueError):
                continue
            t = trait_of_gene(gene)
            if t is not None:
                items.append((t, _time(g.get("available_from")), _time(g.get("retired_at"))))
        out[iid] = items
    return out


def approved_members(conn: sqlite3.Connection, run_id: str, campaign_id: str) -> tuple[list[str], str]:
    from hydra.review import active_constraints

    members = sorted(r["incident_id"] for r in conn.execute(
        "SELECT incident_id FROM campaign_members WHERE run_id = ? AND campaign_id = ?", (run_id, campaign_id)))
    _root, constraints, added, _applied = active_constraints(conn, run_id)
    ms = set(members)
    analyst = {x for c in constraints if c.get("kind") == "must" for x in (c["a"], c["b"]) if x in ms}
    analyst |= {a for a in added if a in ms}
    if len(analyst) >= 2:
        return sorted(analyst), "analyst"
    return members, "published_revision"


def cannot_links(conn: sqlite3.Connection, run_id: str) -> list[tuple[str, str]]:
    from hydra.review import active_constraints

    return [(c["a"], c["b"]) for c in active_constraints(conn, run_id)[1] if c.get("kind") == "cannot"]


def from_revision(db_path: Any, campaign_id: str, run_id: str | None = None) -> tuple[Signature, dict[str, Any]]:
    """Signature of `campaign_id` in published revision `run_id` (default: the latest analyse run)."""
    from hydra.db import connect
    from hydra.pipeline import latest_run

    with closing(connect(db_path, readonly=True)) as conn:
        run_id = run_id or latest_run(conn)
        if run_id is None:
            raise LookupError("no published revision")
        row = conn.execute("SELECT detail FROM campaigns WHERE run_id = ? AND id = ?", (run_id, campaign_id)).fetchone()
        if row is None:
            raise LookupError(f"campaign {campaign_id} not in revision {run_id}")
        members, basis = approved_members(conn, run_id, campaign_id)
        all_ids = [r["incident_id"] for r in conn.execute(
            "SELECT DISTINCT incident_id FROM campaign_members WHERE run_id = ?", (run_id,))]
        traits = traits_by_incident(conn, all_ids)
        campaign_all = {r["incident_id"] for r in conn.execute(
            "SELECT incident_id FROM campaign_members WHERE run_id = ? AND campaign_id = ?", (run_id, campaign_id))}
        meta = {"run_id": run_id, "label": json.loads(row["detail"]).get("label"),
                "cannot_link": cannot_links(conn, run_id),
                "corpus_ids": sorted(all_ids)}
    mem = {i: traits.get(i, []) for i in members}
    # unapproved members of the same campaign are neither members nor background
    bg = {i: ts for i, ts in traits.items() if i not in campaign_all}
    sig = build_signature(campaign_id, f"{campaign_id}@{run_id}", mem, bg, approval_basis=basis)
    return sig, meta
