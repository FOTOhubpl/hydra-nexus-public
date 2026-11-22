"""Synthetic genomes with explicit provenance and controlled frequency backgrounds."""

from __future__ import annotations

import hashlib
from itertools import combinations

import numpy as np
from hypothesis import settings

from hydra.contracts import EvidencePointer, Family, Gene
from hydra.entropy import build_rarity
from hydra.nexus import cluster
from hydra.synapse import build_genome, score_pair

PROPERTY = settings(max_examples=40, derandomize=True, database=None, deadline=None)


def gene(iid, kind, value, group=None, family=Family.INF, **kwargs):
    return Gene(
        incident_id=iid, family=family, kind=kind, value=value, extractor="astra_synthetic@1",
        dependency_group=group,
        evidence=EvidencePointer(artifact_id=hashlib.sha256(f"{iid}:{kind}".encode()).hexdigest(),
                                 note="Synthetic ASTRA fixture"),
        **kwargs,
    )


def corpus(rows, vectors=None, background=8):
    rows = {iid: list(gs) for iid, gs in rows.items()}
    for k in range(background):
        iid = f"background-{k:03}"
        rows[iid] = [gene(iid, "host", f"{iid}.invalid", "INF:operator:host")]
    vectors = vectors or {}
    genomes = {iid: build_genome(iid, gs, vectors) for iid, gs in rows.items()}
    per_kind = {}
    for iid, genome in genomes.items():
        for key, (matrix, _genes) in genome.vectors.items():
            per_kind.setdefault(key, {})[iid] = matrix
    return genomes, build_rarity(rows, per_kind)


def scored_cluster(genomes, rarity):
    pairs = {(a, b): score_pair(genomes[a], genomes[b], rarity)
             for a, b in combinations(sorted(genomes), 2)}
    return pairs, cluster(genomes, pairs, rarity)


def image_unit(iid, uid, phash, image_class="creative", vector=None):
    """Synthetic embedding contract fixture, NOT a DINOv2 measurement."""
    vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32) if vector is None else vector
    ref = f"{iid}:{uid}:synthetic-vector"
    evidence = EvidencePointer(
        artifact_id=hashlib.sha256(f"{iid}:{uid}".encode()).hexdigest(),
        note=f"unit={uid}; class={image_class}; synthetic embedding, not model output",
    )
    common = dict(incident_id=iid, family=Family.VIS, extractor="astra_synthetic@1",
                  dependency_group=f"vis_unit:{uid}", evidence=evidence)
    return [Gene(kind="phash", value=phash, **common),
            Gene(kind="dinov2", vector_ref=ref, **common)], {ref: ("astra-synthetic-dinov2-contract@1", vector)}
