"""Shipped background prior (hydra.priors) and its one-sided use in Rarity (synthetic data only)."""

from __future__ import annotations

import gzip
import json

import pytest

from hydra import priors
from hydra.contracts import Gene
from hydra.entropy import BackgroundPrior, CompositePrior, build_rarity


def _g(iid: str, kind: str, value: str) -> Gene:
    return Gene(
        incident_id=iid, family="INF", kind=kind, value=value, status="observed", provenance="OBS", extractor="test"
    )


def _corpus() -> dict[str, list[Gene]]:
    return {f"i{n}": [_g(f"i{n}", "phrase", f"p{n}")] for n in range(10)} | {
        "a": [_g("a", "phrase", "polityka prywatności"), _g("a", "phrase", "rare-kit-line")],
        "b": [_g("b", "phrase", "polityka prywatności"), _g("b", "phrase", "rare-kit-line")],
    }


def _write_prior(tmp_path, rows, n_kind=5000, legit=4000, fraud=1000):
    d = {
        "schema": priors.SCHEMA,
        "units": {"legit": legit, "fraud": fraud},
        "incidents": legit + fraud,
        "n_by_kind": {"phrase": {"legit": int(n_kind * 0.8), "fraud": int(n_kind * 0.2)}},
        "sources": [{"source": "ext:test", "incidents": 5000, "licence": "CC0"}],
        "columns": ["key", "df_legit", "df_fraud"],
        "df": [[priors.value_key("phrase", v), lg, fr] for v, lg, fr in rows],
    }
    p = tmp_path / "bg.json.gz"
    p.write_bytes(gzip.compress(json.dumps(d).encode()))
    priors._load.cache_clear()
    return p


def test_common_web_value_loses_bits_unseen_value_keeps_local(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    r = build_rarity(_corpus(), {})
    base_common, _ = r.discrete_bits("phrase", "polityka prywatności", ("a", "b"))
    base_rare, _ = r.discrete_bits("phrase", "rare-kit-line", ("a", "b"))
    r.prior = priors.default_prior(_write_prior(tmp_path, [("polityka prywatności", 900, 100)]))
    common, _ = r.discrete_bits("phrase", "polityka prywatności", ("a", "b"))
    rare, _ = r.discrete_bits("phrase", "rare-kit-line", ("a", "b"))
    assert common < base_common - 3  # 1000 of 5000 background units hold it
    assert rare == base_rare  # never seen in the background: local rarity unchanged


def test_background_never_makes_a_value_rarer(tmp_path, monkeypatch):
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    r = build_rarity(_corpus(), {})
    before, _ = r.discrete_bits("phrase", "rare-kit-line", ("a", "b"))
    r.prior = priors.default_prior(_write_prior(tmp_path, [("rare-kit-line", 1, 2)]))
    after, _ = r.discrete_bits("phrase", "rare-kit-line", ("a", "b"))
    assert after <= before


def test_modes_and_thin_kinds(tmp_path, monkeypatch):
    p = _write_prior(tmp_path, [("x", 0, 40)])
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "legit")
    assert priors.default_prior(p).lookup("phrase", "x") == (0, 4000)
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    assert priors.default_prior(p).lookup("phrase", "x") == (40, 5000)
    assert priors.default_prior(p).lookup("unknown_kind", "x") == (0, 0)
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "0")
    assert priors.default_prior(p) is None
    p2 = _write_prior(tmp_path, [("x", 5, 5)], n_kind=100)
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    assert priors.default_prior(p2).lookup("phrase", "x") == (0, 0)  # < MIN_KIND_UNITS observing the kind


def test_fingerprint_tracks_mode_and_file(tmp_path, monkeypatch):
    """Stage caches key the score on it: off, legit, mixed and a different file must all differ."""
    p = _write_prior(tmp_path, [("x", 3, 0)])
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", str(p))
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "0")
    assert priors.fingerprint() is None
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "legit")
    a = priors.fingerprint()
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    b = priors.fingerprint()
    q = tmp_path / "other"
    q.mkdir()
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", str(_write_prior(q, [("y", 4, 1)])))
    c = priors.fingerprint()
    assert None not in (a, b, c) and len({a, b, c}) == 3


def test_prior_file_override_is_used_and_reported(tmp_path, monkeypatch):
    p = _write_prior(tmp_path, [("x", 3, 0)])
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR", "mixed")
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", str(p))
    assert priors.prior_file() == p
    assert priors.default_prior() is not None
    d = priors.describe()
    assert d["enabled"] and d["file"] == str(p)
    monkeypatch.setenv("HYDRA_BACKGROUND_PRIOR_FILE", str(tmp_path / "missing.json.gz"))
    assert priors.default_prior() is None
    assert priors.describe()["file_present"] is False


@pytest.mark.parametrize(
    "kind",
    ["entity_email", "entity_phone", "person_named", "commenter_name", "capture_date", "fake_comment", "meta_author"],
)
def test_personal_kinds_never_used(kind):
    assert priors.excluded_kind(kind)


def test_composite_adds_counts():
    a = BackgroundPrior("a", 10, {"phrase": 10}, {("phrase", "v"): 2})
    b = BackgroundPrior("b", 30, {"phrase": 30}, {("phrase", "v"): 3})
    c = CompositePrior([a, b])
    assert c.lookup("phrase", "v") == (5, 40) and c.n == 40 and c.corpus == "a+b"


def test_shipped_file_if_present_is_well_formed():
    if not priors.PRIOR_FILE.is_file():
        pytest.skip("no shipped prior in this checkout")
    d = json.loads(gzip.decompress(priors.PRIOR_FILE.read_bytes()))
    assert d["schema"] == priors.SCHEMA and d["columns"] == ["key", "df_legit", "df_fraud"]
    assert all(len(r[0]) == 16 for r in d["df"][:1000])
    assert d["sources"] and all(s["verdict"] == "SHIP-SAFE" for s in d["sources"])
    assert not any(priors.excluded_kind(k) for k in d["n_by_kind"])
    assert priors.PRIOR_FILE.stat().st_size < 15_000_000
