"""KONTRA K1 ("Laboratorium hipotez"): competing explanations and counterfactual re-scoring, in shadow mode.

For one stored incident the laboratory
1. freezes the context of the product decision (`freeze`): base run, campaigns, own copies of the base genomes
   and vectors, the effective background prior (mode + sha256), explicit prior, fusion model (sha256), analyst
   cannot-links, thresholds; the fingerprint covers all of them. Re-scoring uses only that materialised context;
   a product state that moved meanwhile marks the experiment `stale_context`;
2. enumerates competing hypotheses: the top candidate campaigns, "nowy wzorzec" and "wspólny kit/szablon";
3. runs REAL recomputations (`rescore`) through the same steps as `pipeline._assign_loaded` (the product
   re-decide path behind `assign_stored`): rarity rebuilt with the modified query, retrieval, `synapse.score_pair`,
   `assign.assign` with conformal, cannot-link and the no-features rule. Only removal and restriction scenarios
   exist: drop one information group (dependency group, gene family, source artefact, a broad removal of all
   VIS/TXT/AUD/VID groups) or remove a campaign from the hypothesis space. No value is ever invented and no bit
   subtraction formula replaces a re-score;
4. searches a bounded greedy flip set over the top reason groups (a SUFFICIENT set, not a globally minimal one);
5. ranks the HUNT action catalogue by whether its source family could separate the two leading hypotheses
   (heuristic recommendation without a demonstrated advantage, labelled as such); actions whose input artefact
   (HAR, HTML, image) the incident lacks are listed as unavailable, not ranked.

Nothing here writes to the database, the CAS, the genes or the campaigns: results go to
`<state>/kontra/experiments/` as JSON. Default scoring, thresholds and assignments are unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hydra.config import Settings
from hydra.contracts import Campaign, CampaignGroup, Gene, GeneMatch
from hydra.db import connect
from hydra.entropy import usable
from hydra.events import canonical_json, utcnow
from hydra.export_safe import safe_filename
from hydra.redact import redact_obj, redact_text
from hydra.synapse import MODEL_NAME, IncidentGenome, Thresholds, build_genome

SCHEMA = "hydra.kontra.v1"
MAX_GROUPS, MAX_SCENARIOS, MAX_ARTEFACTS, MAX_FLIP_CANDIDATES, FLIP_BUDGET = 12, 32, 4, 8, 12
MAX_HYPOTHESES = 5
CLASS_PL = {"baseline": "bazowy", "observation_uncertainty": "niepewność obserwacji",
            "reuse_hypothesis": "hipoteza ponownego użycia", "diagnostic_stress": "test diagnostyczny",
            "competitor_check": "weryfikacja konkurenta"}
DECISION_PL = {"assign": "przypisanie", "new_campaign": "nowy wzorzec", "abstain": "wstrzymanie"}
FAMILY_PL = {"INF": "infrastruktura", "TXT": "tekst", "VIS": "obraz", "META": "metadane", "ID": "tożsamość",
             "NAR": "narracja", "AUD": "dźwięk", "VID": "wideo", "PROV": "pochodzenie", "DFK": "manipulacja"}
# ASTRA-DELTA-05 (CR-KONTRA-05): the scenario that removes every information group of these families is a BROAD
# family-removal test, not a model of copied material (a family does not tell which fact was copied with which
# artefact). It is labelled as such in the API and UI and classed `diagnostic_stress`.
BROAD_FAMILIES = ("VIS", "TXT", "AUD", "VID")
BROAD_LABEL_PL = ("Szeroki test usunięcia rodzin (VIS/TXT/AUD/VID): bez wszystkich grup tych rodzin;"
                  " nie jest modelem kopiowania materiału")
# HUNT source → the query artefact it has to read; an action whose artefact is absent is not ranked (§7)
SOURCE_REQUIRES = {"har": "har", "dom": "html", "ocr": "image", "image": "image"}
ARTEFACT_PL = {"har": "HAR (log sieciowy)", "html": "zapisana strona HTML", "image": "obraz"}
STATUS_PL = {"ok": "kontekst zamrożony i aktualny",
             "stale_context": "kontekst nieaktualny: dane, prior lub model zmieniły się od zamrożenia"}
DRIFT_PL = {"corpus": "geny lub incydenty korpusu", "background_prior": "prior tła (tryb lub plik)",
            "fusion": "model fusion", "legacy": "eksperyment sprzed zamrażania kontekstu"}


class KontraError(LookupError):
    pass


class StaleContextError(RuntimeError):
    """The frozen context can no longer be established consistently (status `stale_context`)."""

    def __init__(self, reasons: list[str]):
        super().__init__("stale_context: " + ", ".join(reasons))
        self.reasons = reasons


# --------------------------------------------------------------------------------------- frozen context


@dataclass
class Context:
    settings: Settings
    incident_id: str
    base_run: str | None
    campaigns: list[Campaign]          # base campaigns without the query (leave-one-out, as the product path)
    groups: list[CampaignGroup]
    conformal_cal: dict | None
    base_params: dict
    root_params: dict
    prior: Any
    fusion: Any
    cannot_link: list[tuple[str, str]]
    corpus_ids: list[str]
    genome: IncidentGenome             # the query as stored (all genes)
    th: Thresholds = field(default_factory=Thresholds)
    stored: dict | None = None         # newest stored assignment row of the query
    fingerprint: str = ""
    corpus_signature: str = ""
    recomputations: int = 0
    # ASTRA-DELTA-02/10 (CR-KONTRA-02/10): materialised at freeze time and owned by this context; `rescore`
    # never re-reads the database, the prior file or the environment.
    base_genomes: dict[str, IncidentGenome] = field(default_factory=dict)
    footage: dict[tuple[str, str], Any] = field(default_factory=dict)  # base-only footage_df, restored per rescore
    shipped_prior: Any = None
    identity: dict[str, Any] = field(default_factory=dict)   # background prior / fusion / explicit prior
    artefacts: dict[str, bool] = field(default_factory=dict)  # which artefact kinds the query has
    lock: Any = field(default_factory=threading.RLock, repr=False)

    def scorer(self) -> dict[str, Any]:
        from hydra.assign import ABSTAIN_MIN, MARGIN, MAX_CANDIDATES, MODEL

        return {"assign_model": MODEL, "scorer": self.fusion.name if self.fusion else MODEL_NAME,
                "thresholds": {"link": self.th.link, "kit": self.th.kit, "low": self.th.low, "asset": self.th.asset},
                "margin": MARGIN, "abstain_min": ABSTAIN_MIN, "max_candidates": MAX_CANDIDATES,
                "prior": self.root_params.get("prior"), "fusion": self.root_params.get("fusion"),
                "background_prior": self.identity.get("background_prior"),
                "fusion_sha256": (self.identity.get("fusion") or {}).get("sha256"),
                "explicit_prior_sha256": self.identity.get("explicit_prior_sha256"),
                "cannot_links": len(self.cannot_link), "conformal": self.conformal_cal is not None,
                "conformal_sha256": (hashlib.sha256(canonical_json(self.conformal_cal).encode()).hexdigest()
                                     if self.conformal_cal is not None else None)}


_PRIOR_CACHE: dict[tuple[str, str], Any] = {}
_PRIOR_LOCK = threading.Lock()


def background_prior_identity() -> dict[str, Any]:
    """Mode and sha256 of the EFFECTIVE shipped background prior (`hydra.priors`), as the environment selects it
    now."""
    from hydra import priors

    m = priors.mode()
    f = priors.prior_file()
    on = m in ("mixed", "legit")
    sha = hashlib.sha256(f.read_bytes()).hexdigest() if on and f.is_file() else None
    return {"mode": m, "enabled": bool(on and sha), "sha256": sha,
            "source": "shipped" if f == priors.PRIOR_FILE else "HYDRA_BACKGROUND_PRIOR_FILE"}


def _freeze_background_prior() -> tuple[dict[str, Any], Any]:
    """(identity, prior object) of the effective shipped prior, parsed from the file whose sha256 is recorded
    (not through the path-keyed lru cache of `hydra.priors`, which would not notice a replaced file)."""
    from hydra import priors

    for _ in range(3):
        ident = background_prior_identity()
        if not ident["enabled"]:
            return ident, None
        key = (ident["mode"], ident["sha256"])
        with _PRIOR_LOCK:
            obj = _PRIOR_CACHE.get(key)
        if obj is None:
            obj = priors._load.__wrapped__(str(priors.prior_file()), ident["mode"])
            if background_prior_identity() != ident:  # replaced while parsing: retry
                continue
            with _PRIOR_LOCK:
                _PRIOR_CACHE.clear()
                _PRIOR_CACHE[key] = obj
        return ident, obj
    raise StaleContextError(["background_prior"])


def fusion_identity(root_params: dict) -> dict[str, Any] | None:
    """Spec and sha256 of the fusion model file the base run names (None when the run used none)."""
    spec = root_params.get("fusion")
    if not spec:
        return None
    p = Path(str(spec)).expanduser()
    sha = hashlib.sha256(p.read_bytes()).hexdigest() if str(spec).endswith(".pkl") and p.is_file() else None
    return {"spec": str(spec), "sha256": sha}


def _explicit_prior_sha(prior: Any) -> str | None:
    """Content hash of an explicit `--prior` corpus object (loaded once at freeze)."""
    if prior is None:
        return None
    df = sorted([list(k) if isinstance(k, tuple) else [k], v] for k, v in (getattr(prior, "df", None) or {}).items())
    return hashlib.sha256(canonical_json({"corpus": getattr(prior, "corpus", None), "n": getattr(prior, "n", None),
                                          "n_by_kind": getattr(prior, "n_by_kind", None), "df": df}
                                         ).encode()).hexdigest()


def _query_artefacts(conn: Any, inc_id: str) -> dict[str, bool]:
    rows = conn.execute("SELECT ia.role, a.mime FROM incident_artifacts ia JOIN artifacts a ON a.id = ia.artifact_id"
                        " WHERE ia.incident_id = ?", (inc_id,)).fetchall()
    roles = {str(r["role"]) for r in rows}
    mimes = {str(r["mime"]) for r in rows}
    return {"har": "har" in roles, "html": "landing_html" in roles or "text/html" in mimes,
            "image": any(m.startswith("image/") for m in mimes)}


def _stored_assignment(conn: Any, inc_id: str) -> dict | None:
    r = conn.execute("SELECT run_id, decision, campaign_id, detail, created_at FROM assignments WHERE incident_id=?"
                     " ORDER BY created_at DESC, run_id DESC LIMIT 1", (inc_id,)).fetchone()
    if r is None:
        return None
    try:
        detail = json.loads(r["detail"] or "{}")
    except ValueError:
        detail = {}
    return {"run_id": r["run_id"], "decision": r["decision"], "campaign_id": r["campaign_id"],
            "group_id": detail.get("group_id"), "base_run": (detail.get("meta") or {}).get("base_run"),
            "created_at": r["created_at"], "meta": detail.get("meta") or {}}


def freeze(inc_id: str, settings: Settings, base_run: str | None = None, th: Thresholds | None = None) -> Context:
    """The context of the product decision for `inc_id`, read-only. The base run is the one the stored
    assignment was made against (else its analyse run, else the newest analyse run)."""
    from hydra.pipeline import (
        _corpus_signature,
        active_cannot_links,
        latest_run,
        load_clustering,
        load_genomes,
        scoring_context,
    )

    with closing(connect(settings.db_path, readonly=True)) as conn:
        if conn.execute("SELECT 1 FROM incidents WHERE id=?", (inc_id,)).fetchone() is None:
            raise KontraError(f"unknown incident {inc_id}")
        stored = _stored_assignment(conn, inc_id)
        if base_run is None and stored:
            base_run = stored.get("base_run")
            if base_run is None:
                kind = conn.execute("SELECT kind FROM runs WHERE id=?", (stored["run_id"],)).fetchone()
                base_run = stored["run_id"] if kind and kind["kind"] == "analyse" else None
        base_run = base_run or latest_run(conn)
        campaigns, groups = load_clustering(conn, base_run) if base_run else ([], [])
        mrow = conn.execute("SELECT metrics, params FROM runs WHERE id = ?", (base_run,)).fetchone() \
            if base_run else None
    conformal_cal = json.loads(mrow["metrics"]).get("conformal") if mrow else None
    base_params = json.loads(mrow["params"]) if mrow else {}
    corpus_ids = sorted({m for c in campaigns for m in c.members} - {inc_id})
    # Own copies (ASTRA-DELTA-02): `load_genomes` builds fresh objects (genes + vectors), never the shared
    # `_BASE_MEMO` genomes that product assignments keep mutating; the base-only footage annotation is snapshotted
    # so that each `rescore` starts from the same state.
    with closing(connect(settings.db_path, readonly=True)) as conn:
        genomes = load_genomes(conn, corpus_ids)
        new = load_genomes(conn, [inc_id])[inc_id]
        sig = _corpus_signature(conn, [*corpus_ids, inc_id])
        artefacts = _query_artefacts(conn, inc_id)
    footage = {(iid, uid): d.get("footage_df") for iid, g in genomes.items() for uid, d in g.units.items()}
    campaigns = [c.model_copy(update={"members": [m for m in c.members if m != inc_id]}) for c in campaigns]
    campaigns = [c for c in campaigns if c.members]
    bg_ident, shipped = _freeze_background_prior()
    root_params, prior, fusion = scoring_context(settings, base_run, base_params, set(genomes) | {inc_id})
    identity = {"background_prior": bg_ident, "fusion": fusion_identity(root_params),
                "explicit_prior_sha256": _explicit_prior_sha(prior)}
    ctx = Context(settings=settings, incident_id=inc_id, base_run=base_run, campaigns=campaigns, groups=groups,
                  conformal_cal=conformal_cal, base_params=base_params, root_params=root_params, prior=prior,
                  fusion=fusion, cannot_link=active_cannot_links(settings, base_params, set(genomes)),
                  corpus_ids=corpus_ids, genome=new, th=th or Thresholds(), stored=stored, corpus_signature=sig,
                  base_genomes=genomes, footage=footage, shipped_prior=shipped, identity=identity,
                  artefacts=artefacts)
    ctx.fingerprint = hashlib.sha256(canonical_json({"scorer": ctx.scorer(), "base_run": base_run,
                                                     "corpus": sig, "query": genes_sha(new)}).encode()).hexdigest()
    return ctx


def genes_sha(g: IncidentGenome) -> str:
    return hashlib.sha256(canonical_json([x.model_dump(mode="json") for x in g.genes]).encode()).hexdigest()


def context_drift(settings: Settings, incident_id: str, corpus_ids: list[str], corpus_signature: str,
                  identity: dict[str, Any]) -> list[str]:
    """What changed since the freeze (empty = still current): corpus genes / incidents, the effective background
    prior (mode + sha256), the fusion model file. Cheap: no genome is loaded."""
    from hydra.pipeline import _corpus_signature

    out: list[str] = []
    with closing(connect(settings.db_path, readonly=True)) as conn:
        if _corpus_signature(conn, [*corpus_ids, incident_id]) != corpus_signature:
            out.append("corpus")
    if background_prior_identity() != identity.get("background_prior"):
        out.append("background_prior")
    fz = identity.get("fusion")
    if fz and fusion_identity({"fusion": fz.get("spec")}) != fz:
        out.append("fusion")
    return out


def drift_of(ctx: Context) -> list[str]:
    return context_drift(ctx.settings, ctx.incident_id, ctx.corpus_ids, ctx.corpus_signature, ctx.identity)


def experiment_status(settings: Settings, exp: dict[str, Any]) -> dict[str, Any]:
    """`status` / `stale_reasons` of a stored experiment against the current state (an experiment written before
    the frozen context existed is `stale_context` with reason `legacy`)."""
    c = exp.get("context") or {}
    fr = c.get("frozen")
    if not fr:
        reasons = ["legacy"]
    else:
        reasons = context_drift(settings, exp["incident_id"], fr.get("corpus_ids") or [],
                                fr.get("corpus_sha256") or "", fr.get("identity") or {})
    return _status(reasons)


def _status(reasons: list[str]) -> dict[str, Any]:
    st = "stale_context" if reasons else "ok"
    return {"status": st, "status_pl": STATUS_PL[st], "stale_reasons": reasons,
            "stale_reasons_pl": [DRIFT_PL.get(r, r) for r in reasons]}


# --------------------------------------------------------------------------------------- re-scoring


def _genome_without(g: IncidentGenome, drop: frozenset[int]) -> IncidentGenome:
    """The query genome rebuilt from its genes minus the dropped indices (vectors carried over unchanged)."""
    vecs: dict[str, tuple[str, Any]] = {}
    for (_kind, model), (mat, genes) in g.vectors.items():
        for i, gene in enumerate(genes):
            if gene.vector_ref is not None:
                vecs[gene.vector_ref] = (model, mat[i])
    kept = [x for i, x in enumerate(g.genes) if i not in drop]
    return build_genome(g.incident_id, kept, vecs, label=g.label, title=g.title, observed_at=g.observed_at)


def rescore(ctx: Context, drop: frozenset[int] = frozenset(), exclude_campaigns: frozenset[str] = frozenset()
            ) -> Any:
    """One recomputation: the steps of `pipeline._assign_loaded` with the query's genes `drop` removed and the
    campaigns `exclude_campaigns` removed from the hypothesis space (their members stay in the corpus and in the
    rarity background). Uses only the context materialised by `freeze` (base genomes and vectors, background
    prior, explicit prior, fusion, conformal calibration); the database, prior file and environment are not
    read again. Returns the `assign.AssignResult`."""
    from hydra.assign import assign as assign_open_set
    from hydra.contracts import AssignmentDecision
    from hydra.entropy import CompositePrior
    from hydra.genome.vid_seq import annotate_footage
    from hydra.pipeline import NO_FEATURES_PL, _informative, rarity_for

    ctx.recomputations += 1
    with ctx.lock:
        genomes = ctx.base_genomes
        for (iid, uid), v in ctx.footage.items():  # base-only footage frequencies, as frozen
            if v is None:
                genomes[iid].units[uid].pop("footage_df", None)
            else:
                genomes[iid].units[uid]["footage_df"] = v
        new = _genome_without(ctx.genome, drop)
        annotate_footage({new.incident_id: new})  # as `load_genomes` does for the query alone
        annotate_footage({**genomes, ctx.incident_id: new})
        rarity = rarity_for({**genomes, ctx.incident_id: new}, prior=ctx.prior)
        # the background prior frozen at `freeze` (ASTRA-DELTA-10), combined as `pipeline.with_shipped_prior`
        # does, instead of the one the current environment would select
        sp = ctx.shipped_prior
        rarity.prior = ctx.prior if sp is None else (sp if ctx.prior is None else CompositePrior([sp, ctx.prior]))
        rarity.kit_unit_of = {m: (c.group_id or c.id) for c in ctx.campaigns for m in c.members}
        camps = [c for c in ctx.campaigns if c.id not in exclude_campaigns]
        res = assign_open_set(new, genomes, camps, ctx.groups, rarity, ctx.th, conformal=ctx.conformal_cal,
                              fusion=ctx.fusion, cannot_link=ctx.cannot_link)
    if ctx.base_run is not None and not _informative(new) and res.assignment.decision != "assign":
        res.assignment.decision = AssignmentDecision.ABSTAIN
        res.assignment.campaign_id, res.assignment.group_id = None, None
        res.assignment.reason_pl = NO_FEATURES_PL
        res.assignment.meta["no_features"] = True
    return res


# --------------------------------------------------------------------------------------- information groups


def _group_key(g: Gene) -> str:
    return g.dependency_group or f"{g.family}:{g.kind}"


def _fam(g: Gene | GeneMatch) -> str:
    return str(getattr(g.family, "value", g.family))


def _query_side(m: GeneMatch, score: Any, inc_id: str) -> tuple[str | None, Any]:
    return (m.value_a, m.evidence_a) if score.a == inc_id else (m.value_b, m.evidence_b)


def _reason_genes(ctx: Context, score: Any, m: GeneMatch) -> list[int]:
    """Indices of the query genes behind one reason: same kind and the query-side value, else the same family
    and the query-side evidence pointer (near-duplicate / vector rows carry the evidence, not the value)."""
    val, ev = _query_side(m, score, ctx.incident_id)
    fam = _fam(m)
    hits = [i for i, g in enumerate(ctx.genome.genes)
            if usable(g) and g.kind == m.kind and val is not None and g.value == val]
    if not hits and ev is not None:
        hits = [i for i, g in enumerate(ctx.genome.genes)
                if usable(g) and _fam(g) == fam and g.evidence == ev]
    if not hits and ev is not None and ev.artifact_id:
        hits = [i for i, g in enumerate(ctx.genome.genes)
                if usable(g) and _fam(g) == fam and g.kind == m.kind and g.evidence.artifact_id == ev.artifact_id]
    return hits


@dataclass
class Unit:
    unit_id: str
    unit: str          # dependency_group | family | artefact
    family: str | None
    genes: frozenset[int]
    relevance: float
    label_pl: str
    kinds: list[str]


def information_units(ctx: Context, base: Any) -> tuple[list[Unit], list[Unit], list[Unit], dict[int, float]]:
    """Dependency groups, families and source artefacts of the query that carry the reasons of the top
    candidates, ranked by the absolute bits of those reasons. Returns (groups, families, artefacts, bits per
    gene index)."""
    per_gene: dict[int, float] = {}
    for cand in base.assignment.candidates[:2]:
        sc = cand.best_pair
        for m in sc.reasons:
            if not m.bits:
                continue
            for i in _reason_genes(ctx, sc, m):
                per_gene[i] = per_gene.get(i, 0.0) + abs(float(m.bits))
    genes = ctx.genome.genes

    def unit_of(key_fn, unit: str, label_fn) -> list[Unit]:
        buckets: dict[str, set[int]] = {}
        for i, g in enumerate(genes):
            if usable(g):
                k = key_fn(g)
                if k:
                    buckets.setdefault(k, set()).add(i)
        out = []
        for k, idx in buckets.items():
            rel = round(sum(per_gene.get(i, 0.0) for i in idx), 3)
            if rel <= 0:
                continue
            fams = {_fam(genes[i]) for i in idx}
            out.append(Unit(unit_id=f"{unit}:{k}", unit=unit, family=fams.pop() if len(fams) == 1 else None,
                            genes=frozenset(idx), relevance=rel, label_pl=label_fn(k, idx),
                            kinds=sorted({genes[i].kind for i in idx})))
        out.sort(key=lambda u: (-u.relevance, u.unit_id))
        return out

    def grp_label(k: str, idx: set[int]) -> str:
        kinds = sorted({genes[i].kind for i in idx})
        return f"grupa {k} ({', '.join(kinds[:3])}{'…' if len(kinds) > 3 else ''}; {len(idx)} cech)"

    groups = unit_of(_group_key, "dependency_group", grp_label)
    families = unit_of(_fam, "family", lambda k, idx: f"rodzina {k} ({FAMILY_PL.get(k, k)}; {len(idx)} cech)")
    arts = unit_of(lambda g: g.evidence.artifact_id, "artefact",
                   lambda k, idx: f"artefakt {k[:12]} ({len(idx)} cech)")
    return groups, families, arts, per_gene


# --------------------------------------------------------------------------------------- hypotheses


def winner_of(a: Any) -> str:
    if a.decision == "assign" and a.campaign_id:
        return f"campaign:{a.campaign_id}"
    if a.decision == "abstain":
        return "abstain"
    return f"shared_kit:{a.group_id}" if a.group_id else "new_pattern"


def _reason_row(ctx: Context, sc: Any, m: GeneMatch, gene_group: dict[int, str]) -> dict[str, Any]:
    val, ev = _query_side(m, sc, ctx.incident_id)
    idx = _reason_genes(ctx, sc, m)
    return {"family": _fam(m), "kind": m.kind, "bits": round(float(m.bits), 3),
            "explanation_pl": redact_text(m.explanation_pl), "value": redact_text((val or "")[:160]) or None,
            "group": gene_group.get(idx[0]) if idx else None,
            "evidence": ev.model_dump(mode="json") if ev is not None else None}


def hypotheses(ctx: Context, base: Any, gene_group: dict[int, str]) -> list[dict[str, Any]]:
    a = base.assignment
    top = {t["campaign_id"]: t for t in a.meta.get("top_candidates") or []}
    group_of = {c.id: c.group_id for c in ctx.campaigns}
    win = winner_of(a)
    out: list[dict[str, Any]] = []
    link = ctx.th.link
    shown = [c for i, c in enumerate(a.candidates) if i == 0 or (top.get(c.campaign_id) or {}).get("support", 0) > 0
             or c.best_pair.total_bits > 0]
    for rank, cand in enumerate(shown[:MAX_HYPOTHESES - 2], start=1):
        t = top.get(cand.campaign_id) or {}
        sc = cand.best_pair
        support = t.get("support", sc.total_bits)
        hid = f"campaign:{cand.campaign_id}"
        out.append({
            "id": hid, "kind": "campaign", "campaign_id": cand.campaign_id, "group_id": group_of.get(cand.campaign_id),
            "label_pl": f"Kampania {cand.campaign_id}", "rank": rank, "leading": hid == win,
            "support_bits": round(float(support), 3), "campaign_bits": t.get("campaign_bits"),
            "kit_bits": t.get("kit_bits"), "operator_bits": t.get("operator_bits"),
            "supporting_members": cand.support, "pair_hypothesis": str(sc.hypothesis),
            "families": {k: round(float(v), 3) for k, v in (sc.family_bits or {}).items()},
            "gap_to_link_bits": round(max(0.0, link - float(support)), 3),
            "note_pl": ("najlepszy kandydat" if rank == 1 else "konkurent") + f"; para {sc.a}–{sc.b}",
            "reasons": [_reason_row(ctx, sc, m, gene_group) for m in sc.reasons[:6]]})
    kit = max((t for t in top.values()), key=lambda t: t.get("kit_bits") or 0.0, default=None)
    kit_bits = float(kit.get("kit_bits") or 0.0) if kit else 0.0
    kit_gid = group_of.get(kit["campaign_id"]) if kit else None
    out.append({"id": f"shared_kit:{kit_gid}" if kit_gid else "shared_kit", "kind": "shared_kit",
                "campaign_id": kit["campaign_id"] if kit else None, "group_id": kit_gid,
                "label_pl": "Wspólny kit / szablon" + (f" (grupa {kit_gid})" if kit_gid else ""),
                "rank": len(out) + 1, "leading": win.startswith("shared_kit"), "support_bits": None,
                "campaign_bits": None, "kit_bits": round(kit_bits, 3), "operator_bits": None,
                "supporting_members": None, "pair_hypothesis": None, "families": {},
                "gap_to_link_bits": round(max(0.0, ctx.th.kit - kit_bits), 3),
                "note_pl": ("dowody poziomu zestawu narzędzi (kit/szablon) nigdy nie przypisują do kampanii; próg kit"
                            f" {ctx.th.kit:g} bit"), "reasons": []})
    out.append({"id": "new_pattern", "kind": "new_pattern", "campaign_id": None, "group_id": None,
                "label_pl": "Nowy wzorzec", "rank": len(out) + 1, "leading": win == "new_pattern",
                "support_bits": None, "campaign_bits": None, "kit_bits": None, "operator_bits": None,
                "supporting_members": None, "pair_hypothesis": None, "families": {}, "gap_to_link_bits": None,
                "note_pl": "żadna znana kampania nie spełnia reguły linku; brak obserwacji nie jest dowodem przeciw",
                "reasons": []})
    return out


# --------------------------------------------------------------------------------------- scenarios


def _scenario_row(sid: str, typ: str, cls: str, label: str, removed: dict, base: Any, res: Any, ms: float,
                  th: Thresholds) -> dict[str, Any]:
    a, b = res.assignment, base.assignment
    top = a.meta.get("top_candidates") or []
    support = float(top[0]["support"]) if top else 0.0
    base_c = next((t for t in top if b.campaign_id and t["campaign_id"] == b.campaign_id), None)
    return {"scenario_id": sid, "type": typ, "class": cls, "class_pl": CLASS_PL[cls], "label_pl": label,
            "removed": removed, "decision": str(a.decision), "campaign_id": a.campaign_id, "group_id": a.group_id,
            "winner": winner_of(a), "leading_campaign": top[0]["campaign_id"] if top else None,
            "support_bits": round(support, 3),
            "base_campaign_support_bits": round(float(base_c["support"]), 3) if base_c else None,
            "link_threshold": th.link, "margin_bits": a.meta.get("margin_bits"),
            "link_rule_met": a.meta.get("link_rule_met"),
            "decision_changed": str(a.decision) != str(b.decision),
            "campaign_changed": a.campaign_id != b.campaign_id, "group_changed": a.group_id != b.group_id,
            "runtime_ms": round(ms, 1), "reason_pl": redact_text(a.reason_pl)}


def _flips(row: dict[str, Any]) -> bool:
    return bool(row["decision_changed"] or row["campaign_changed"])


def _removed(units: list[Unit], ctx: Context, campaigns: list[str] | None = None) -> dict[str, Any]:
    genes: set[int] = set().union(*(u.genes for u in units)) if units else set()
    return {"groups": [u.unit_id for u in units if u.unit == "dependency_group"],
            "families": [u.unit_id.split(":", 1)[1] for u in units if u.unit == "family"],
            "artefacts": [u.unit_id.split(":", 1)[1] for u in units if u.unit == "artefact"],
            "campaigns": campaigns or [], "n_genes": len(genes)}


def run_experiment(inc_id: str, settings: Settings, *, base_run: str | None = None, max_groups: int = MAX_GROUPS,
                   max_scenarios: int = MAX_SCENARIOS, flip_budget: int = FLIP_BUDGET) -> dict[str, Any]:
    """The full K1 experiment for one incident (shadow: nothing is written to the analysis)."""
    t_all = time.perf_counter()
    ctx = freeze(inc_id, settings, base_run=base_run)
    t0 = time.perf_counter()
    base = rescore(ctx)
    base_ms = (time.perf_counter() - t0) * 1000
    groups, families, arts, _per_gene = information_units(ctx, base)
    gene_group = {i: u.unit_id for u in groups for i in u.genes}
    rows: list[dict[str, Any]] = [_scenario_row("baseline", "baseline", "baseline", "Bez zmian (decyzja produktu)",
                                                _removed([], ctx), base, base, base_ms, ctx.th)]
    planned: list[tuple[str, str, str, str, list[Unit], list[str]]] = []
    for u in groups[:max_groups]:
        planned.append((f"drop-{u.unit_id}", "remove_group", "observation_uncertainty",
                        f"Bez {u.label_pl}", [u], []))
    for u in families:
        planned.append((f"drop-{u.unit_id}", "remove_family", "diagnostic_stress", f"Bez {u.label_pl}", [u], []))
    for u in arts[:MAX_ARTEFACTS]:
        planned.append((f"drop-{u.unit_id}", "remove_artefact", "diagnostic_stress", f"Bez pliku: {u.label_pl}",
                        [u], []))
    broad = [u for u in groups if u.family in BROAD_FAMILIES]
    if broad:  # ASTRA-DELTA-05: a broad family-removal test, no copy-model interpretation
        planned.append(("broad-family-removal", "broad_family_removal", "diagnostic_stress", BROAD_LABEL_PL,
                        broad, []))
    lead = base.assignment.candidates[0].campaign_id if base.assignment.candidates else None
    if lead:
        planned.append((f"without-campaign:{lead}", "exclude_campaign", "competitor_check",
                        f"Hipoteza „kampania {lead}” wyłączona: kto wygrywa bez niej", [], [lead]))
    budget_main = max(0, max_scenarios - 1 - flip_budget)
    skipped = max(0, len(planned) - budget_main)
    for sid, typ, cls, label, units, camps in planned[:budget_main]:
        drop = frozenset().union(*(u.genes for u in units)) if units else frozenset()
        t0 = time.perf_counter()
        res = rescore(ctx, drop=drop, exclude_campaigns=frozenset(camps))
        rows.append(_scenario_row(sid, typ, cls, label, _removed(units, ctx, camps), base, res,
                                  (time.perf_counter() - t0) * 1000, ctx.th))
    flip = flip_search(ctx, base, groups, rows, flip_budget)
    rows.extend(flip.pop("_rows"))
    counter = [r for r in rows if r["type"] != "baseline" and r["class"] != "competitor_check"]
    stable = sum(1 for r in counter if not _flips(r))
    a = base.assignment
    hyps = hypotheses(ctx, base, gene_group)
    exp: dict[str, Any] = {
        "schema_version": SCHEMA, "incident_id": inc_id, "mode": "shadow", "created_at": utcnow(),
        "context": {"base_run": ctx.base_run, "fingerprint": ctx.fingerprint, "corpus_signature": ctx.corpus_signature,
                    "product_commit": _git_commit(), "scorer": ctx.scorer(),
                    "query_genes": len(ctx.genome.genes), "query_genes_sha256": genes_sha(ctx.genome),
                    "frozen": {"materialised": True, "corpus_ids": ctx.corpus_ids, "identity": ctx.identity,
                               "corpus_sha256": ctx.corpus_signature,  # `*_signature` keys are redacted
                               "base_genomes": len(ctx.base_genomes), "query_artefacts": ctx.artefacts,
                               "note_pl": ("genomy i wektory korpusu, prior tła, prior jawny, model fusion i"
                                           " kalibracja zamrożone w chwili startu; przeliczenia nie czytają"
                                           " ponownie bazy ani ustawień")}},
        "baseline": {"decision": str(a.decision), "campaign_id": a.campaign_id, "group_id": a.group_id,
                     "reason_pl": redact_text(a.reason_pl), "support_bits": rows[0]["support_bits"],
                     "link_threshold": ctx.th.link, "margin_bits": a.meta.get("margin_bits"),
                     "winner": winner_of(a), "link_rule_met": a.meta.get("link_rule_met"),
                     "stored": ({k: ctx.stored[k] for k in ("decision", "campaign_id", "group_id", "run_id")}
                                if ctx.stored else None),
                     "matches_stored": (None if not ctx.stored else
                                        (ctx.stored["decision"], ctx.stored["campaign_id"])
                                        == (str(a.decision), a.campaign_id))},
        "hypotheses": hyps,
        "groups": [{"group_id": u.unit_id, "unit": u.unit, "family": u.family, "label_pl": u.label_pl,
                    "n_genes": len(u.genes), "kinds": u.kinds, "relevance_bits": u.relevance}
                   for u in [*groups[:max_groups], *families, *arts[:MAX_ARTEFACTS]]],
        "scenarios": rows,
        "flip_set": flip,
        "next_observations": next_observations(ctx, base, hyps),
        "retrieval_scope": a.meta.get("retrieval_scope"),
        "candidates": a.meta.get("candidates"),
        "conformal": a.meta.get("conformal"),
        "limits": {"max_groups": max_groups, "max_scenarios": max_scenarios, "flip_budget": flip_budget,
                   "groups_total": len(groups), "groups_tested": min(len(groups), max_groups),
                   "scenarios_run": len(rows), "scenarios_skipped": skipped,
                   "scenario_space_exhausted": skipped == 0 and len(groups) <= max_groups,
                   "retrieval_complete": None},
        "stats": {"recomputations": ctx.recomputations,
                  "runtime_ms_total": round((time.perf_counter() - t_all) * 1000, 1),
                  "scenario_stability_fraction": round(stable / len(counter), 3) if counter else None,
                  "scenario_stability_note": ("udział sprawdzonych scenariuszy (bez weryfikacji konkurenta), które"
                                              " zachowują decyzję; statystyka skończonego katalogu, nie"
                                              " prawdopodobieństwo")},
    }
    exp.update(_status(drift_of(ctx)))  # the product state moved while the experiment ran → stale_context
    exp["experiment_id"] = experiment_id(exp)
    return redact_obj(exp)


VOLATILE = ("created_at", "runtime_ms", "runtime_ms_total", "experiment_id", "product_commit", "status", "status_pl",
            "stale_reasons", "stale_reasons_pl")


def canonical_view(obj: Any) -> Any:
    """The experiment without job identity, times and runtimes (the reproducible part)."""
    if isinstance(obj, dict):
        return {k: canonical_view(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [canonical_view(v) for v in obj]
    return obj


def experiment_id(exp: dict[str, Any]) -> str:
    return "kx-" + hashlib.sha256(canonical_json(canonical_view(exp)).encode()).hexdigest()[:20]


def flip_search(ctx: Context, base: Any, groups: list[Unit], rows: list[dict[str, Any]], budget: int
                ) -> dict[str, Any]:
    """Greedy bounded search for a set of reason groups whose joint removal changes the decision or campaign:
    single-group scenarios first (already computed), then cumulative removal in relevance order, then pruning.
    The result is a SUFFICIENT set within the budget, not a globally minimal one."""
    cand = groups[:MAX_FLIP_CANDIDATES]
    note = "zestaw wystarczający (zachłanny, w budżecie przeliczeń), nie globalnie najmniejszy"
    out: dict[str, Any] = {"found": False, "groups": [], "labels_pl": [], "decision": None, "campaign_id": None,
                           "searched": 0, "budget": budget, "candidates": [u.unit_id for u in cand],
                           "note_pl": note, "_rows": []}
    by_id = {u.unit_id: u for u in cand}
    single = [r for r in rows if r["type"] == "remove_group" and _flips(r)
              and r["removed"]["groups"] and r["removed"]["groups"][0] in by_id]
    if single:
        r = single[0]
        out.update(found=True, groups=r["removed"]["groups"], labels_pl=[by_id[r["removed"]["groups"][0]].label_pl],
                   decision=r["decision"], campaign_id=r["campaign_id"], scenario_id=r["scenario_id"])
        return out
    used = 0
    found: list[Unit] | None = None
    found_row: dict[str, Any] | None = None

    def trial(units: list[Unit], tag: str) -> dict[str, Any]:
        nonlocal used
        used += 1
        t0 = time.perf_counter()
        res = rescore(ctx, drop=frozenset().union(*(u.genes for u in units)))
        label = "Bez łącznie: " + "; ".join(u.label_pl for u in units)
        return _scenario_row(f"flip-{tag}-{len(units)}", "flip_search", "diagnostic_stress", label,
                             _removed(units, ctx), base, res, (time.perf_counter() - t0) * 1000, ctx.th)

    for k in range(2, len(cand) + 1):
        if used >= budget:
            break
        r = trial(cand[:k], "prefix")
        out["_rows"].append(r)
        if _flips(r):
            found, found_row = list(cand[:k]), r
            break
    if found is not None:
        for u in list(reversed(found)):
            if used >= budget or len(found) <= 1:
                break
            rest = [x for x in found if x is not u]
            r = trial(rest, "prune")
            out["_rows"].append(r)
            if _flips(r):
                found, found_row = rest, r
        assert found_row is not None
        out.update(found=True, groups=[u.unit_id for u in found], labels_pl=[u.label_pl for u in found],
                   decision=found_row["decision"], campaign_id=found_row["campaign_id"],
                   scenario_id=found_row["scenario_id"])
    out["searched"] = used
    return out


# --------------------------------------------------------------------------------------- next observation

# HUNT action source → gene families its result could add (assumed mapping, documented in KONTRA_METHOD.md)
SOURCE_FAMILIES = {"har": ["INF"], "dom": ["INF", "TXT"], "ocr": ["TXT"], "image": ["VIS"], "metadata": ["META"],
                   "analyst": []}


def _hyp_families(ctx: Context, hyp: dict[str, Any]) -> dict[str, set[str]]:
    """Family → campaign-level values the hypothesis's campaign members share (Campaign.core_genome)."""
    camp = next((c for c in ctx.campaigns if c.id == hyp.get("campaign_id")), None)
    out: dict[str, set[str]] = {}
    for m in (camp.core_genome if camp else []):
        out.setdefault(_fam(m), set()).add(f"{m.kind}={m.value_a or m.value_b}")
    return out


def next_observations(ctx: Context, base: Any, hyps: list[dict[str, Any]]) -> dict[str, Any]:
    """Heuristic ranking of the HUNT catalogue (`hydra.hunt.actions.ACTIONS`) for the two leading hypotheses.

        separates = the two hypotheses' campaign core genomes differ in a family the action's source yields
                    (campaign vs new pattern: the campaign has core values in that family)
        priority  = (2 if separates else 0) + (1 if the query has no observed gene of those families) / cost

    A missing observation stays unknown; this ranking promises no accuracy gain and reads no hidden result."""
    from hydra.hunt.actions import ACTIONS, COST

    win = winner_of(base.assignment)
    camps = [h for h in hyps if h["kind"] == "campaign"]
    first = next((h for h in hyps if h["id"] == win), camps[0] if camps else hyps[-1])
    second = next((h for h in camps if h["id"] != first["id"]), None)
    if second is None or (first["kind"] == "campaign" and (second.get("support_bits") or 0) <= 0):
        second = next(h for h in hyps if h["kind"] == "new_pattern") if first["kind"] != "new_pattern" else \
            (camps[0] if camps else first)
    fa, fb = _hyp_families(ctx, first), _hyp_families(ctx, second)
    observed = {_fam(g) for g in ctx.genome.genes if usable(g)}
    ranking, unavailable = [], []
    for a in ACTIONS:
        fams = SOURCE_FAMILIES.get(a.source, [])
        need = SOURCE_REQUIRES.get(a.source)
        if need and not ctx.artefacts.get(need):  # §7: never rank an action whose input artefact is absent
            unavailable.append({"action": a.id, "label_pl": a.label_pl, "source": a.source, "families": fams,
                                "cost": a.cost, "requires": need, "status": "artefact_missing",
                                "why_pl": f"brak wymaganego artefaktu w incydencie: {ARTEFACT_PL[need]}"})
            continue
        if a.source == "analyst":
            sep, obs, why = True, False, "analityk ocenia most między hipotezami bezpośrednio (odpowiedź = review)"
        else:
            diff = [f for f in fams if (fa.get(f) or set()) != (fb.get(f) or set())]
            sep = bool(diff)
            obs = bool(set(fams) & observed)
            why = (f"wartości rdzeniowe hipotez różnią się w rodzinie {', '.join(diff)}" if sep else
                   "hipotezy nie mają różnych wartości rdzeniowych w tej rodzinie")
            why += "; zapytanie ma już cechy tej rodziny" if obs else "; brak takiej obserwacji w zapytaniu (nieznane)"
        prio = ((2 if sep else 0) + (0 if obs else 1)) / COST[a.cost]
        ranking.append({"action": a.id, "label_pl": a.label_pl, "source": a.source, "families": fams,
                        "separates": sep, "observed_in_query": obs, "cost": a.cost, "priority": round(prio, 3),
                        "status": "requires_input", "why_pl": why})
    order = {a.id: i for i, a in enumerate(ACTIONS)}
    ranking.sort(key=lambda r: (-r["priority"], order[r["action"]]))
    return {"heuristic": True, "rule": "priority = (2·rozróżnia + 1·nieobserwowane) / koszt",
            "label_pl": "rekomendacja heurystyczna, bez wykazanej przewagi",
            "note_pl": ("Rekomendacja heurystyczna, bez wykazanej przewagi nad stałą kolejnością: wskazuje rodzaj"
                        " obserwacji, który mógłby rozróżnić dwie wiodące hipotezy. Nie jest oczekiwaną poprawą"
                        " dokładności; brak odczytu nie eliminuje hipotezy. Pomijane są akcje bez wymaganego"
                        " artefaktu (HAR, HTML, obraz)."),
            "pair": [first["id"], second["id"]], "ranking": ranking, "unavailable": unavailable}


# --------------------------------------------------------------------------------------- storage


def _git_commit() -> str | None:
    from hydra.train import _git_commit as gc

    return gc()


def _exp_dir(settings: Settings, inc_id: str) -> Path:
    return Path(settings.state_dir) / "kontra" / "experiments" / safe_filename(inc_id, "incident")


def save_experiment(settings: Settings, exp: dict[str, Any]) -> Path:
    d = _exp_dir(settings, exp["incident_id"])
    d.mkdir(parents=True, exist_ok=True)
    body = json.dumps(exp, ensure_ascii=False, indent=1)
    p = d / f"{safe_filename(exp['experiment_id'])}.json"
    p.write_text(body, encoding="utf-8")
    (d / "latest.json").write_text(body, encoding="utf-8")
    return p


def load_experiment(settings: Settings, inc_id: str, experiment: str | None = None) -> dict[str, Any] | None:
    d = _exp_dir(settings, inc_id)
    p = d / (f"{safe_filename(experiment)}.json" if experiment else "latest.json")
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")


def valid_id(x: str) -> bool:
    return bool(_ID_RE.match(x)) and ".." not in x
