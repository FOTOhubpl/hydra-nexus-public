"""AUDIT B15: after assign / assign-batch, a campaign's live `size` counts the assigned material, so its member
list must show it too (list: `members` + `assigned_members`; card: `size`, `base_size`, `assigned_members`, and the
assigned incident in `member_incidents` flagged `membership: assigned`). Synthetic rows only (DATAFLOW world)."""

from __future__ import annotations

import random

from fastapi.testclient import TestClient

from hydra import dataflow
from hydra.api import create_app
from hydra.db import Writer
from tests.core.dataflow_world import RUN, World
from tests.core.test_dataflow import _settings


def test_campaign_size_matches_members_after_assignment(tmp_path):
    s = _settings(tmp_path)
    w = Writer(s.db_path)
    try:
        world = World(w, random.Random(7))
        world.build_base()
        dataflow.build(s.db_path, w, RUN)
        for _ in range(12):  # the world picks a random (sometimes missing) campaign per decision
            world.assign(decision="assign")
    finally:
        w.close()
    with TestClient(create_app(s)) as c:
        items = c.get("/v1/campaigns", params={"run_id": RUN}).json()["items"]
        grown = [it for it in items if it["assigned_members"]]
        assert grown, "the synthetic world assigned nothing to a known campaign"
        for it in items:
            assert it["size"] == len(it["members"]), it["id"]
            assert it["base_size"] + len(it["assigned_members"]) == it["size"]
            assert set(it["assigned_members"]) <= set(it["members"])
        it = grown[0]
        card = c.get(f"/v1/campaigns/{it['id']}", params={"run_id": RUN}).json()
        assert card["size"] == it["size"] == card["base_size"] + len(card["assigned_members"])
        assert card["assigned_members"] == it["assigned_members"]
        flagged = [m["id"] for m in card["member_incidents"] if m.get("membership") == "assigned"]
        assert flagged == sorted(it["assigned_members"])
        assert not set(card["assigned_members"]) & set(card["members"])  # clustered members stay the base list
