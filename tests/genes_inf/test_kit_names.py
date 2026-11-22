"""Kit file-name-set fingerprint (docs/cleanroom/kit_filetree_fingerprint.md acceptance tests). Synthetic only."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from hydra.contracts import Incident
from hydra.entropy import build_rarity
from hydra.genome.base import ExtractContext, ResolvedIncident, run_all
from hydra.genome.kit_names import distinctive, jaccard
from hydra.synapse import build_genome, level, score_pair

sys.path.insert(0, os.path.dirname(__file__))
from fixtures import _art, _synthetic_warninglists  # noqa: E402,F401

KIT = ["zysk-panel.css", "inwestor-form.js", "krok2-weryfikacja.js", "baner-prezes-wywiad.webp",
       "licznik-miejsc.js", "potwierdzenie-wplaty.png", "formularz-lead.css", "pasek-zaufania.svg"]


def _incident(root: Path, iid: str, names: list[str], salt: bytes) -> ResolvedIncident:
    arts = [_art(root / iid, f"Strona_files/{n}.pobrane" if n.endswith(".js") else f"Strona_files/{n}",
                 hashlib.sha256(salt + n.encode()).digest() * 40, "landing_asset") for n in names]
    inc = Incident(id=iid, source="test", source_ref=iid, artifacts=[a.meta for a in arts])
    return ResolvedIncident(inc, arts)


def _genes(tmp_path, inc):
    from hydra.config import Settings

    ctx = ExtractContext(settings=Settings(root=tmp_path, state_dir=tmp_path / "s"), work_dir=tmp_path / "w")
    return run_all(inc, ctx, only=["inf_kit_names"]).genes


def test_filters():
    assert distinctive("krok2-weryfikacja.js.pobrane") == "krok2-weryfikacja.js"
    for generic in ("index.html", "style.css", "favicon.ico", "jquery-3.6.0.min.js", "12345.png",
                    "3f9a2c1b7d4e8f00aa11.js", "app.3f9a2c1b7d4e8f.js", "wp-content", "Xk9qL2vR8tZ0pW3mN7bY.png"):
        assert distinctive(generic) is None, generic


def test_same_kit_resaved_shares_nameset_but_not_bytes(tmp_path):
    a = _genes(tmp_path, _incident(tmp_path, "a", KIT, b"v1"))
    b = _genes(tmp_path, _incident(tmp_path, "b", KIT, b"v2-reminified"))
    ha = {g.value for g in a if g.kind == "asset_nameset_hash"}
    assert ha and ha == {g.value for g in b if g.kind == "asset_nameset_hash"}
    g0 = next(g for g in a if g.kind == "asset_nameset_hash")
    assert g0.dependency_group == "inf:kit:asset_names" and level(g0) == "kit"
    assert sum(g.kind == "asset_name_token" for g in a) == len(KIT)


def test_variant_jaccard_and_capped_kit_link(tmp_path):
    variant = KIT[:-1] + ["inny-baner.webp"]
    assert abs(jaccard(set(KIT), set(variant)) - 7 / 9) < 1e-9
    genes = {"a": _genes(tmp_path, _incident(tmp_path, "a", KIT, b"1")),
             "b": _genes(tmp_path, _incident(tmp_path, "b", variant, b"2"))}
    for k in range(8):
        genes[f"f{k}"] = _genes(tmp_path, _incident(tmp_path, f"f{k}", [f"plik{k}-{j}-unikat.css" for j in range(5)],
                                                    b"f"))
    r = build_rarity(genes, {})
    gen = {i: build_genome(i, g, {}) for i, g in genes.items()}
    pe = score_pair(gen["a"], gen["b"], r)
    assert pe.kit_bits > 0 and pe.campaign_bits == 0  # kit evidence only: a campaign group, never a campaign


def test_unavailable_without_enough_names(tmp_path):
    genes = _genes(tmp_path, _incident(tmp_path, "c", ["index.html", "style.css", "a1b2c3d4e5f6a7b8.png"], b"x"))
    assert [g.status for g in genes] == ["unavailable"]
