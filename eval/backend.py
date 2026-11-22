"""Backends the harness can evaluate.

- `StubBackend(scorer)`: eval stub genes (`eval.stubgenes`) + one of the ladder scorers (`eval.scorers`).
- `HydraBackend`: CORE's `hydra.pipeline` as it is (import/extract into an isolated eval DB, `score_and_cluster`,
  `hydra.assign.assign`). `auto` falls back to the stub only when `hydra.pipeline` is absent; every result is
  labelled with the backend that produced it.

Gene extraction is cached on disk (keyed by the sha256 of every file in the incident + extractor name) so
leave-one-out rebuilds never re-extract.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from eval import scorers as S
from eval.incidents import IncidentFiles, materialise
from hydra.contracts import Assignment, Gene


@dataclass
class IncidentRef:
    iid: str
    path: Path
    label: str | None
    group_label: str | None
    synthetic: bool = False
    meta: dict | None = None


def cache_dir() -> Path:
    root = Path(os.environ.get("HYDRA_CACHE_DIR", Path.home() / "HydraNexus" / "cache")) / "eval-genes"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _key(inc: IncidentFiles, extractor: str) -> str:
    h = hashlib.sha256(extractor.encode())
    for f in sorted(inc.files, key=lambda f: f.rel):
        h.update(f"{f.rel}\0{f.sha256}\0{f.role}\n".encode())
    return h.hexdigest()


class Backend:
    name = "base"
    supports_family_drop = True

    def extract(self, ref: IncidentRef) -> list[Gene]:
        raise NotImplementedError

    def build(self, genes: dict[str, list[Gene]], labels: dict[str, str] | None = None,
              drop_families: frozenset[str] = frozenset()) -> S.Build:
        raise NotImplementedError

    def assign(self, b: S.Build, iid: str, genes: list[Gene], drop_families: frozenset[str] = frozenset()
               ) -> Assignment:
        raise NotImplementedError


class StubBackend(Backend):
    extractor = "evalstub@1"

    def __init__(self, scorer_name: str = "B3_rarity_bits", threshold: float | None = None,
                 group_threshold: float | None = None) -> None:
        self.scorer_name = scorer_name
        self.threshold = threshold
        self.group_threshold = group_threshold
        self.name = f"stub+{scorer_name}"
        self._scratch = Path(tempfile.mkdtemp(prefix="hydra-eval-x-"))
        self._prof: dict = {}

    def new_scorer(self, drop: frozenset[str] = frozenset()) -> S.Scorer:
        cls = S.SCORERS[self.scorer_name]
        sc = cls(drop) if cls is S.RarityBits else cls()
        if self.threshold is not None:
            sc.threshold = self.threshold
        if self.group_threshold is not None:
            sc.group_threshold = self.group_threshold
        return sc

    def extract(self, ref: IncidentRef) -> list[Gene]:
        from eval import stubgenes

        inc = materialise(ref.path, ref.iid, self._scratch / ref.iid)
        key = _key(inc, self.extractor)
        cp = cache_dir() / f"{key}.json"
        if cp.exists():
            rows = json.loads(cp.read_text())
            return [Gene.model_validate({**r, "incident_id": ref.iid}) for r in rows]
        genes = stubgenes.extract(inc)
        cp.write_text(json.dumps([g.model_dump(mode="json") for g in genes]))
        return genes

    def _profile(self, iid: str, genes: list[Gene], drop: frozenset[str]) -> S.Profile:
        key = (iid, id(genes), drop)
        hit = self._prof.get(key)
        if hit is None or hit[0] is not genes:
            hit = (genes, S.profile(iid, genes, drop))
            self._prof[key] = hit
        return hit[1]

    def build(self, genes, labels=None, drop_families=frozenset()):
        profiles = {i: self._profile(i, g, frozenset(drop_families)) for i, g in genes.items()}
        return S.build(self.new_scorer(drop_families), profiles, labels)

    def assign(self, b, iid, genes, drop_families=frozenset()):
        return S.assign(b.ctx, b, self._profile(iid, genes, frozenset(drop_families)))


def default_eval_state() -> Path:
    """Isolated eval state (never the real ~/HydraNexus/state). Persistent so CORE's per-incident gene cache
    (fingerprint of artifacts + extractor tags) survives between eval runs; `--fresh-state` uses a tmp dir."""
    return Path(os.environ.get("HYDRA_EVAL_STATE", Path.home() / "HydraNexus" / "cache" / "eval-state"))


class HydraBackend(Backend):
    """CORE pipeline as it is (hydra.pipeline): import + extract genes into an isolated eval DB, then
    `score_and_cluster` on exactly the incidents of each build and `hydra.assign.assign` for new material
    (rarity rebuilt from the build corpus + the new item, campaign-group units, as `pipeline._assign_incident`).
    Labels are never written to the eval DB, so CORE's supervised fusion path cannot see them (no leakage)."""

    def __init__(self, state_dir: Path | None = None, thresholds: dict | None = None,
                 enrich_cache: Path | None = None, only: list[str] | None = None,
                 fusion: Path | None = None) -> None:
        from hydra import pipeline as P
        from hydra.config import Settings
        from hydra.db import Writer
        from hydra.storage import ContentStore
        from hydra.synapse import Thresholds

        self.P = P
        self.state_dir = Path(state_dir or default_eval_state())
        kw = {"enrich_cache": enrich_cache} if enrich_cache else {}
        self.settings = Settings(state_dir=self.state_dir, enrich=False, **kw)
        self.settings.ensure_dirs()
        self.writer = Writer(self.settings.db_path)
        self.store = ContentStore(self.settings.cas_dir)
        self.th = Thresholds(**(thresholds or {}))
        self.only = only
        self._data: dict[str, tuple[list[Gene], dict]] = {}
        self._genomes: dict = {}
        self._preloaded: set[str] = set()
        self.observed_at: dict[str, str | None] = {}
        self.abstain_min: float | None = None  # None = CORE's ABSTAIN_MIN
        self.conformal = True  # product parity: calibrate per build and pass it to assign, as `analyse` does
        self.extract_stats: dict[str, dict] = {}
        self.name = f"hydra-core(link={self.th.link},kit={self.th.kit})"
        # Optional pretrained fusion (`hydra train` on external corpora, TRAIN): loaded from the store of the state it
        # was trained in (`<state>/models/fusion/*.pkl`, sha256-checked by `load_fusion`), never copied.
        self.fusion = None
        if fusion is not None:
            from hydra.train import load_fusion

            fp = Path(fusion).expanduser().resolve()
            self.fusion = load_fusion(fp, Settings(state_dir=fp.parents[2], enrich=False))
            self.name += f"+fusion({fp.stem})"

    def close(self) -> None:
        self.writer.close()

    def _load(self, iid: str) -> tuple[list[Gene], dict]:
        import numpy as np

        from hydra.db import connect

        conn = connect(self.settings.db_path, readonly=True)
        try:
            genes = [self.P._row_gene(r) for r in
                     conn.execute("SELECT * FROM genes WHERE incident_id = ? ORDER BY id", (iid,))]
            vecs = {r["key"]: (r["model"], np.frombuffer(r["data"], dtype="<f4").astype(np.float32))
                    for r in conn.execute("SELECT key, model, data FROM gene_vectors WHERE key LIKE ?", (f"{iid}|%",))}
            row = conn.execute("SELECT observed_at FROM incidents WHERE id = ?", (iid,)).fetchone()
            self.observed_at[iid] = row["observed_at"] if row else None
        finally:
            conn.close()
        return genes, vecs

    def extract(self, ref: IncidentRef) -> list[Gene]:
        import zipfile

        from hydra.adapters.nask_folder import import_incident, import_single, load_incident
        from hydra.db import connect

        if ref.iid in self._preloaded:
            return self._data[ref.iid][0]
        path = ref.path
        if path.is_file() and zipfile.is_zipfile(path):
            # the product's content-addressed safe unpack (CORE A11, c4f7ba8+); older CORE: pipeline._unpack_zip
            try:
                from hydra.adapters.nask_folder import unpack_zip
            except ImportError:
                unpack_zip = self.P._unpack_zip
            path = unpack_zip(path, self.settings)
        # re-import replaces the artifact list (same id + same bytes keeps CORE's gene cache valid)
        self.writer.run(lambda c: c.execute("DELETE FROM incident_artifacts WHERE incident_id = ?", (ref.iid,)))
        if path.is_dir():
            import_incident(path, self.writer, self.store, self.settings, incident_id=ref.iid, source="eval")
        else:
            import_single(path, self.writer, self.store, self.settings, incident_id=ref.iid, source="eval")
        conn = connect(self.settings.db_path, readonly=True)
        try:
            inc = load_incident(conn, ref.iid)
        finally:
            conn.close()
        self.extract_stats.update(self.P.extract([inc], self.writer, self.store, self.settings, only=self.only))
        genes, vecs = self._load(ref.iid)
        self._data[ref.iid] = (genes, vecs)
        self._genomes = {k: v for k, v in self._genomes.items() if k[0] != ref.iid}
        return genes

    def preload(self, db_path: Path, iids: list[str]) -> list[str]:
        """Read genes + vectors extracted by a shard (its own state DB). Returns ids that had no genes."""
        saved = self.settings
        missing = []
        try:
            from hydra.config import Settings

            self.settings = Settings(state_dir=db_path.parent)
            for iid in iids:
                genes, vecs = self._load(iid)
                if not genes:
                    missing.append(iid)
                self._data[iid] = (genes, vecs)
                self._preloaded.add(iid)
                self._genomes = {k: v for k, v in self._genomes.items() if k[0] != iid}
        finally:
            self.settings = saved
        return missing

    def adopt(self, other: HydraBackend, src: str, dst: str) -> None:
        """Use another backend's extraction of `src` under the id `dst` (e.g. the no-enrichment re-extraction)."""
        genes, vecs = other._data[src]
        self._data[dst] = ([g.model_copy(update={"incident_id": dst}) for g in genes],
                           {k.replace(f"{src}|", f"{dst}|", 1): v for k, v in vecs.items()})
        self._genomes = {k: v for k, v in self._genomes.items() if k[0] != dst}

    def genome(self, iid: str, drop: frozenset[str] = frozenset()):
        from hydra.synapse import build_genome

        key = (iid, drop)
        if key not in self._genomes:
            genes, vecs = self._data[iid]
            if drop:
                genes = [g for g in genes if str(g.family) not in drop]
                refs = {g.vector_ref for g in genes if g.vector_ref}
                vecs = {k: v for k, v in vecs.items() if k in refs}
            self._genomes[key] = build_genome(iid, genes, vecs)
        return self._genomes[key]

    def build(self, genes, labels=None, drop_families=frozenset()):
        drop = frozenset(drop_families)
        genomes = {i: self.genome(i, drop) for i in sorted(genes)}
        rarity, pairs, clustering, info = self.P.score_and_cluster(genomes, self.th, fusion=self.fusion)
        cal = None
        if self.conformal:
            from hydra.conformal import calibrate

            cal = calibrate(genomes, clustering.campaigns, self.th)
        member_of = dict(clustering.membership)
        for i in genomes:
            member_of.setdefault(i, f"single-{i}")
        group_of = {m: (c.group_id or f"g-{c.id}") for c in clustering.campaigns for m in c.members}
        for i in genomes:
            group_of.setdefault(i, f"g-{member_of[i]}")
        return S.Build(ids=sorted(genomes), pairs={k: pe.score for k, pe in pairs.items()},
                       campaigns=list(clustering.campaigns), member_of=member_of, model=info.get("scorer", "core"),
                       group_of=group_of, ctx={"clustering": clustering, "genomes": genomes, "drop": drop,
                                               "info": info, "conformal": cal, "rarity": rarity,
                                               "pair_evals": pairs})

    def assign(self, b, iid, genes, drop_families=frozenset()):
        """Same steps as the product path `hydra.pipeline._assign_incident` (CORE c4f7ba8): campaign-level rarity
        over build corpus + new item (incident units), kit units = campaign groups, conformal calibration of the
        base build, then `hydra.assign.assign`. Only the DB reads/writes around it are skipped."""
        from hydra.assign import assign as assign_open_set

        drop = frozenset(drop_families)
        genomes = b.ctx["genomes"]
        new = self.genome(iid, drop)
        campaigns = [c for c in b.ctx["clustering"].campaigns if c.members]
        rarity = self.P.rarity_for({**genomes, iid: new})
        rarity.kit_unit_of = {m: (c.group_id or c.id) for c in campaigns for m in c.members}
        kw = {"abstain_min": self.abstain_min} if getattr(self, "abstain_min", None) is not None else {}
        res = assign_open_set(new, genomes, campaigns, b.ctx["clustering"].groups, rarity, self.th,
                              conformal=b.ctx.get("conformal"), **kw)
        a = res.assignment
        if res.conformal is not None:
            a.meta["eval_conformal"] = {k: res.conformal.get(k) for k in ("decided_by", "set_size", "n_calibration",
                                                                         "prediction_set", "usable")}
        return a


def make_backend(kind: str, scorer: str = "B3_rarity_bits", **kw) -> Backend:
    if kind == "stub":
        return StubBackend(scorer)
    if kind == "hydra":
        return HydraBackend(**kw)
    try:
        return HydraBackend(**kw)
    except ImportError:  # auto: CORE pipeline not present in this checkout
        return StubBackend(scorer)
