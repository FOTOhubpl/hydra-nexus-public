"""HYDRA_SELF_ASSIGN_MAX: the leave-one-out self-assignment pass on a deterministic stratified subset."""

from __future__ import annotations

import json

from hydra.config import Settings
from hydra.contracts import Campaign
from hydra.db import connect
from hydra.pipeline import analyse, self_assign_cap, self_assign_sample
from tests.core.test_pipeline import N_INC, corpus, test_extractors  # noqa: F401 — fixture


def _camp(cid: str, members: list[str]) -> Campaign:
    return Campaign(id=cid, members=members, label=cid)


def test_cap_env(monkeypatch):
    monkeypatch.delenv("HYDRA_SELF_ASSIGN_MAX", raising=False)
    assert self_assign_cap() is None
    for v, want in (("", None), ("0", None), ("-3", None), ("x", None), ("1500", 1500)):
        monkeypatch.setenv("HYDRA_SELF_ASSIGN_MAX", v)
        assert self_assign_cap() == want


def test_sample_is_stratified_and_deterministic():
    camps = [_camp("c1", ["a1", "a2", "a3", "a4"]), _camp("c2", ["b1", "b2"])] + [
        _camp(f"s{k}", [f"x{k}"]) for k in range(14)]
    ids = sorted(m for c in camps for m in c.members)
    assert self_assign_sample(ids, camps, None) == ids
    assert self_assign_sample(ids, camps, 100) == ids
    s = self_assign_sample(ids, camps, 10)
    assert len(s) == 10 and s == sorted(s) and set(s) <= set(ids)
    assert s == self_assign_sample(list(reversed(ids)), list(reversed(camps)), 10)
    members = [i for i in s if i[0] in "ab"]
    assert members == ["a1", "a2", "b1"]  # 6 of 20 are members -> round(10 * 6 / 20) = 3, round-robin c1, c2, c1
    only_single = self_assign_sample(ids, [], 5)
    assert len(only_single) == 5


def test_analyse_honours_the_cap(settings: Settings, make_png, tmp_path, test_extractors, monkeypatch):  # noqa: F811
    monkeypatch.setenv("HYDRA_SELF_ASSIGN_MAX", "4")
    res = analyse([corpus(tmp_path, make_png)], settings, only=test_extractors)
    s = res.metrics["assignment"]
    assert s["assignments"] == 4 and s["capped"] == {
        "cap": 4, "incidents": N_INC, "sampled": 4,
        "strategy": "stratified: campaign members round-robin, singletons sha256 order"}
    assert s["members"] >= 1 and s["singletons"] >= 1
    with connect(settings.db_path, readonly=True) as conn:
        rows = conn.execute("SELECT detail FROM assignments WHERE run_id=?", (res.run_id,)).fetchall()
    assert len(rows) == 4 and all(json.loads(r[0])["meta"]["mode"] == "leave_one_out" for r in rows)
    monkeypatch.delenv("HYDRA_SELF_ASSIGN_MAX")
    res2 = analyse([corpus(tmp_path / "again", make_png)], settings, only=test_extractors)
    assert res2.metrics["assignment"]["assignments"] == N_INC and res2.metrics["assignment"]["capped"] is None
