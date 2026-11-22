"""Link watch events to analysed campaigns: campaign gene index (for Stage A reasons), lightweight watch
incidents (INF genes only) and the existing open-set assigner (`hydra.assign.assign`).

Watch incidents are never written to the database: `hydra.corpus.corpus_of("watch:...")` would file them under
the protected NASK corpus, so they stay in memory (corpus "watch") and in the watch's own JSONL log.
The corpus flag (`HYDRA_WATCH_CORPUS`, default `nask`; `all`, `public`, `extended` or explicit names, see
`hydra.corpus.matches`) selects which analysed campaigns a watch event may join.
"""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from hydra.config import Settings
from hydra.contracts import Gene, GeneStatus
from hydra.corpus import corpus_of, matches
from hydra.enrich.domains import is_platform
from hydra.watch.names import alpha_tokens, family_key, fold, parts

CORPUS_WATCH = "watch"
_STOP = {
    "production", "studio", "store", "johnson", "coleman", "wydarzen", "oszczedzania", "emerytalnego", "aktywnego",
    "wiadomoci", "wiadomosci", "favorable", "formal", "sustain", "evelyn", "sharon", "polska", "polski", "platforma",
    "summit", "invest", "poland", "forte", "official", "online", "sklep", "gosc", "trwaly",
}
LANDING_KINDS = ("landing_host", "registrable_domain")
NAME_KINDS = ("product_name", "platform_page_name")
INDEX_KINDS = {"landing_host", "registrable_domain", "url_label_skeleton", "ns_set", "ns_host", "registrar_day",
               "registration_day", "dns_ip24", "server_ip24", "cert_san_sibling", "domain_contacted"}


SHAPE_KINDS = {"url_label_skeleton", "url_label_path_skeleton", "url_path_skeleton"}
# A name shape may support assignment only if it is rarer than 1 in 2^SHAPE_MIN_BITS names on the live feed.
SHAPE_MIN_BITS = 8.0
SHAPE_MIN_BACKGROUND = 500


def shape_is_rare(n_shape: int, n_all: int) -> bool:
    import math

    return n_all >= SHAPE_MIN_BACKGROUND and math.log2(n_all / (n_shape + 1)) >= SHAPE_MIN_BITS


def _slug(v: str) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(v)[0])


@dataclass
class CampaignIndex:
    """Known-campaign genes for Stage A and a (kind, value) inverted index for the assignment prefilter."""

    run_id: str | None = None
    corpus: str = "nask"
    campaigns: list[Any] = field(default_factory=list)
    groups: list[Any] = field(default_factory=list)
    genomes: dict[str, Any] = field(default_factory=dict)
    conformal: dict | None = None
    labels: dict[str, str] = field(default_factory=dict)
    skeletons: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    tokens: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    domains: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    lures: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    families: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))  # 'oferta-#.sbs' -> campaigns
    values: dict[tuple[str, str], set[str]] = field(default_factory=lambda: defaultdict(set))
    loaded_at: str | None = None
    error: str | None = None

    # ---- CampaignGenes protocol (score.py)
    def skeleton_campaigns(self, skeleton: str) -> list[str]:
        return sorted(self.skeletons.get(skeleton, ()))

    def token_campaigns(self, token: str) -> list[str]:
        return sorted(self.tokens.get(token, ())) if len(token) >= 6 else []

    def domain_campaigns(self, host: str, registrable: str) -> list[str]:
        return sorted(self.domains.get(host, set()) | self.domains.get(registrable, set()))

    def family_campaigns(self, family: str) -> list[str]:
        return sorted(self.families.get(family, ())) if "#" in family else []

    def lure_labels(self) -> dict[str, list[str]]:
        return {k: sorted(v) for k, v in self.lures.items()}

    def shares_value(self, genes: list[Gene]) -> bool:
        return any((g.kind, g.value) in self.values for g in genes if g.value is not None)

    def summary(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "corpus": self.corpus, "campaigns": len(self.campaigns),
                "incidents": len(self.genomes), "skeletons": len(self.skeletons), "tokens": len(self.tokens),
                "domains": len(self.domains), "loaded_at": self.loaded_at, "error": self.error}


def load_index(settings: Settings, corpus: str = "nask") -> CampaignIndex:
    from hydra.db import connect
    from hydra.pipeline import latest_run, load_clustering, load_genomes

    idx = CampaignIndex(corpus=corpus, loaded_at=datetime.now(UTC).isoformat(timespec="seconds"))
    if not settings.db_path.exists():
        idx.error = "no database"
        return idx
    with closing(connect(settings.db_path, readonly=True)) as conn:
        run = latest_run(conn)
        if run is None:
            idx.error = "no analysed run"
            return idx
        campaigns, groups = load_clustering(conn, run)
        src = {r["id"]: r["source"] for r in conn.execute("SELECT id, source FROM incidents")}
        keep = []
        for c in campaigns:
            members = [m for m in c.members if matches(corpus_of(src.get(m)), corpus)]
            if members:
                keep.append(c.model_copy(update={"members": members}))
        ids = sorted({m for c in keep for m in c.members})
        genomes = load_genomes(conn, ids)
        mrow = conn.execute("SELECT metrics FROM runs WHERE id = ?", (run,)).fetchone()
        try:
            idx.conformal = json.loads(mrow["metrics"]).get("conformal") if mrow else None
        except (TypeError, ValueError):
            idx.conformal = None
    idx.run_id, idx.campaigns, idx.groups, idx.genomes = run, keep, groups, genomes
    member_of = {m: c.id for c in keep for m in c.members}
    for c in keep:
        idx.labels[c.id] = c.label
    for iid, g in genomes.items():
        cid = member_of.get(iid)
        if cid is None:
            continue
        for gene in g.genes:
            if gene.status != GeneStatus.OBSERVED or gene.value is None:
                continue
            v = str(gene.value)
            if gene.kind in INDEX_KINDS:
                idx.values[(gene.kind, v)].add(iid)
            if gene.kind == "url_label_skeleton":
                idx.skeletons[v].add(cid)
            elif gene.kind in LANDING_KINDS and not is_platform(v):
                idx.domains[v.lower()].add(cid)
                if gene.kind == "registrable_domain":
                    hp = parts(v)
                    lab = hp.f_domain
                    if len(lab) >= 6:
                        idx.lures[lab].add(cid)
                    fk = family_key(hp)
                    if "#" in fk:
                        idx.families[fk].add(cid)
            elif gene.kind in NAME_KINDS:
                whole = _slug(v)
                if len(whole) >= 6 and whole not in _STOP:
                    idx.tokens[whole].add(cid)
                for t in alpha_tokens(fold(v.replace("_", " "))[0]):
                    if len(t) >= 7 and t not in _STOP:
                        idx.tokens[t].add(cid)
    return idx


# --------------------------------------------------------------------------------------- watch incident


def watch_genes(incident_id: str, host: str, enrichment: dict[str, Any] | None) -> list[Gene]:
    """INF genes of a watch event with the same kinds/tiers as the batch extractors (har.url_genes, inf_enrich)."""
    from hydra.genome.har import url_genes
    from hydra.genome.webparse import GeneSink

    sink = GeneSink(incident_id, "watch_inf@1")
    url_genes(sink, f"http://{host}/", "watch", None)
    e = enrichment or {}
    rd = e.get("rdap") or {}
    reg = rd.get("registration")
    if rd.get("registrar"):
        sink.add("registrar", str(rd["registrar"])[:120], None, note="rdap", quality=0.3)
    if reg:
        day = str(reg)[:10]
        sink.add("registration_day", day, None, note="rdap", quality=0.6)
        if rd.get("registrar"):
            sink.add("registrar_day", f"{rd['registrar']}|{day}"[:150], None, note="rdap")
    from hydra.genome.inf import SHARED_NS

    ns = sorted(set(e.get("ns") or rd.get("nameservers") or []))
    for n in ns:
        shared = any(x in n for x in SHARED_NS)
        sink.add("ns_host", n, "boilerplate" if shared else None, note="dns", quality=0.15 if shared else 1.0)
    if ns:
        all_shared = all(any(x in n for x in SHARED_NS) for n in ns)
        sink.add("ns_set", ",".join(ns), "platform" if all_shared else None, note="dns",
                 quality=0.3 if all_shared else 0.8)
    for ip in e.get("a") or []:
        p = ip.split(".")
        if len(p) == 4:
            sink.add("dns_ip", ip, None, note="dns")
            sink.add("dns_ip24", ".".join(p[:3]) + ".0/24", None, note="dns", quality=0.6)
    return sink.genes


class Linker:
    """Thread-safe holder of the campaign index + open-set assignment of watch incidents."""

    def __init__(self, settings: Settings, corpus: str = "nask") -> None:
        self.settings, self.corpus = settings, corpus
        self._lock = threading.Lock()
        self.index: CampaignIndex = CampaignIndex(corpus=corpus)

    def reload(self) -> CampaignIndex:
        try:
            idx = load_index(self.settings, self.corpus)
        except Exception as exc:  # noqa: BLE001 - linking is optional, the ticker must keep running
            idx = CampaignIndex(corpus=self.corpus, error=f"{type(exc).__name__}: {exc}"[:200])
        with self._lock:
            self.index = idx
        return idx

    def assign(self, event_id: str, host: str, enrichment: dict[str, Any] | None,
               skeleton_ok: bool = True) -> dict[str, Any]:
        """`skeleton_ok=False` drops the name-shape genes (url_label_skeleton & co.): the corpus rarity of a
        shape like 'L7.com' is computed on a small corpus and says nothing about how common the shape is on the
        live feed; the engine passes False when the stream background makes the shape common."""
        from hydra.assign import MODEL, assign
        from hydra.synapse import build_genome

        idx = self.index
        iid = f"watch-{event_id}"
        genes = watch_genes(iid, host, enrichment)
        if not skeleton_ok:
            genes = [g for g in genes if g.kind not in SHAPE_KINDS]
        base = {"incident_id": iid, "corpus": CORPUS_WATCH, "base_run": idx.run_id, "genes": len(genes),
                "campaign_corpus": idx.corpus}
        if not idx.campaigns:
            return {**base, "decision": "unavailable", "reason_pl": "Brak przeanalizowanych kampanii w korpusie."}
        if not idx.shares_value(genes):
            return {**base, "decision": "new_campaign", "model": f"{MODEL}+prefilter", "campaign_id": None,
                    "reason_pl": "Nowy wzorzec: brak wspólnej cechy infrastruktury z żadną znaną kampanią."}
        new = build_genome(iid, genes, {})
        with self._lock:  # assign rebuilds rarity over the corpus; one at a time
            res = assign(new, idx.genomes, idx.campaigns, idx.groups, conformal=idx.conformal)
        a = res.assignment
        best = a.candidates[0] if a.candidates else None
        reasons = []
        if best and best.best_pair:
            reasons = [{"kind": r.kind, "bits": round(r.bits, 2), "pl": r.explanation_pl}
                       for r in best.best_pair.reasons[:4]]
        decision, cid = str(a.decision), a.campaign_id
        reason_pl = a.reason_pl
        positive = {r["kind"] for r in reasons if r["bits"] > 0}
        if decision == "assign" and positive and positive <= SHAPE_KINDS:
            # name shape alone (letter/digit counts) is not campaign evidence on an open feed: candidate only
            decision, cid = "candidate", None
            reason_pl = (f"Kandydat: z kampanią {a.campaign_id} wspólny jest tylko kształt nazwy (szkielet etykiet); "
                         "to za mało, aby przypisać. Do weryfikacji przez analityka.")
        return {**base, "decision": decision, "campaign_id": cid, "group_id": a.group_id if cid else None,
                "campaign_label": idx.labels.get(cid or ""), "reason_pl": reason_pl, "model": a.model,
                "open_set_decision": str(a.decision),
                "best_candidate": best.campaign_id if best else None,
                "best_bits": round(best.best_pair.total_bits, 2) if best and best.best_pair else None,
                "reasons": reasons, "conformal": res.conformal}
