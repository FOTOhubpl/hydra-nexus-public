"""API-PERF: per-run response cache correctness and level-of-detail payload shapes (synthetic rows only)."""

from __future__ import annotations

import gzip
import json
import math
import sqlite3

import pytest
from fastapi.testclient import TestClient

from hydra import api_cache, events
from hydra.api import create_app
from hydra.config import Settings
from hydra.db import Writer
from hydra.graph_lod import EDGE_COLUMNS, NODE_COLUMNS, spiral_pack, sunflower
from ui_api.synthetic import ASSIGN_RUN_ID, RUN_ID, seed


@pytest.fixture
def client(settings: Settings):
    seed(settings.db_path, settings.cas_dir)
    api_cache.clear_memory()
    with TestClient(create_app(settings)) as c:
        yield c


def _rows(table: dict) -> list[dict]:
    return [dict(zip(table["columns"], r, strict=True)) for r in table["rows"]]


def _tx(settings: Settings, fn) -> None:
    w = Writer(settings.db_path)
    try:
        w.run(fn)
    finally:
        w.close()


# --------------------------------------------------------------------------------------- cache


def test_cache_miss_then_hit_and_persisted(client, settings):
    r1 = client.get("/v1/graph/explore")
    r2 = client.get("/v1/graph/explore")
    assert r1.headers["x-hydra-cache"] == "miss" and r2.headers["x-hydra-cache"] == "hit"
    assert r1.json() == r2.json()
    files = list(api_cache.run_dir(settings, RUN_ID).glob("graph-base-*.json.gz"))
    assert len(files) == 1
    api_cache.clear_memory()  # a new process reads the disk copy
    r3 = client.get("/v1/graph/explore")
    assert r3.headers["x-hydra-cache"] == "hit" and r3.json() == r1.json()


@pytest.mark.parametrize("path", ["/v1/graph/explore", "/v1/graph/overview", "/v1/system/flow", "/v1/dashboard",
                                  "/v1/analytics/overview", f"/v1/graph/campaign/syn-c1?run_id={RUN_ID}",
                                  "/v1/graph/neighbors/syn-1"])
def test_cached_equals_uncached(client, monkeypatch, path):
    cold = client.get(path).json()
    warm = client.get(path).json()
    monkeypatch.setenv("HYDRA_API_CACHE", "0")
    fresh = client.get(path)
    assert "x-hydra-cache" not in fresh.headers or fresh.headers["x-hydra-cache"] == "miss"
    assert cold == warm == fresh.json()


def test_explore_matches_legacy_uncached_loader(client):
    """The cached base gives exactly the legacy `_load` result (legacy path: top_reasons > 5)."""
    new = client.get("/v1/graph/explore", params={"top_reasons": 3}).json()
    old = client.get("/v1/graph/explore", params={"top_reasons": 6}).json()
    for e in old["edges"]:
        e["reasons"] = e["reasons"][:3]
    assert new["edges"] == old["edges"]
    assert new["nodes"] == old["nodes"] and new["campaigns"] == old["campaigns"] and new["stats"] == old["stats"]
    assert new["corpus"] == old["corpus"] and new["synthetic"] == old["synthetic"] is True


def test_reviews_overlay_live_on_cached_graph(client):
    q = {"run_id": RUN_ID}  # a review also writes a revision run, which becomes the default run
    client.get("/v1/graph/explore", params=q)
    client.get("/v1/graph/overview", params=q)
    assert client.post("/v1/reviews", json={"action": "reject_link", "target": {"a": "syn-1", "b": "syn-2"}}
                       ).status_code == 201
    assert client.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "syn-5", "b": "syn-6"}}
                       ).status_code == 201
    r = client.get("/v1/graph/explore", params=q)
    assert r.headers["x-hydra-cache"] == "hit"
    edges = {e["id"]: e for e in r.json()["edges"]}
    assert edges["syn-1|syn-2"]["review"] == "cannot_link"
    assert edges["syn-5|syn-6"]["review"] == "must_link" and edges["syn-5|syn-6"]["level"] == "review"
    ov = client.get("/v1/graph/overview", params=q).json()
    assert ov["stats"]["cannot_link"] == 1 and ov["stats"]["must_link"] == 1
    nb = client.get("/v1/graph/neighbors/syn-6", params=q).json()
    assert any(e[EDGE_COLUMNS.index("review")] == "must_link" for e in nb["edges"]["rows"])
    flow = client.get("/v1/system/flow", params=q)
    assert flow.headers["x-hydra-cache"] == "miss"  # first flow request for this run
    assert flow.json()["boxes"]["reviews"]["reviews"] == 2


def _assign_syn_new(settings) -> None:
    now = events.utcnow()

    def tx(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO incidents (id, source, source_ref, title, observed_at, label, meta, created_at)"
                     " VALUES ('syn-new', 'synthetic', 'synthetic/syn-new', 'Nowy', '2026-11-01', NULL, '{}', ?)",
                     (now,))
        conn.execute("INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES"
                     " ('syn-assign-2', 'assign', ?, '{}', ?, ?)",
                     (json.dumps({"base_run": RUN_ID}), now, now))
        conn.execute("INSERT INTO assignments (incident_id, run_id, decision, campaign_id, detail, created_at)"
                     " VALUES ('syn-new', 'syn-assign-2', 'new_campaign', NULL, '{}', ?)", (now,))
        # as hydra.pipeline._write_run: the run and its audit event in one transaction
        events.append(conn, "RUN_FINISHED", {"run_id": "syn-assign-2", "kind": "assign", "pairs": 0, "campaigns": 0})

    _tx(settings, tx)


def test_new_assignment_keeps_base_cache_and_shows_live(client, settings):
    """DATAFLOW (ADR-0009): new material is a delta over the cached base; the base is not recomputed."""
    client.get("/v1/graph/explore")
    assert client.get("/v1/graph/explore").headers["x-hydra-cache"] == "hit"
    _assign_syn_new(settings)
    r = client.get("/v1/graph/explore")
    assert r.headers["x-hydra-cache"] == "hit"
    assert "syn-new" in {n["id"] for n in r.json()["nodes"]}
    ov = client.get("/v1/graph/overview").json()
    assert "syn-new" in {n["id"] for n in ov["live"]["nodes"]}


def test_new_assignment_changes_fingerprint_without_dataflow(client, settings, monkeypatch):
    monkeypatch.setenv("HYDRA_DATAFLOW", "0")
    client.get("/v1/graph/explore")
    assert client.get("/v1/graph/explore").headers["x-hydra-cache"] == "hit"
    _assign_syn_new(settings)
    r = client.get("/v1/graph/explore")
    assert r.headers["x-hydra-cache"] == "miss"
    assert "syn-new" in {n["id"] for n in r.json()["nodes"]}


def test_unfinished_run_is_never_cached(client, settings):
    now = events.utcnow()
    _tx(settings, lambda conn: conn.execute(
        "INSERT INTO runs (id, kind, params, metrics, created_at, finished_at) VALUES ('syn-open', 'analyse', '{}',"
        " '{}', ?, NULL)", (now,)))
    for _ in range(2):
        assert client.get("/v1/graph/explore", params={"run_id": "syn-open"}).headers["x-hydra-cache"] == "miss"
    assert not api_cache.run_dir(settings, "syn-open").exists()


def test_invalidate_drops_run(client, settings):
    client.get("/v1/graph/explore")
    assert api_cache.run_dir(settings, RUN_ID).exists()
    api_cache.invalidate(settings, RUN_ID)
    assert not api_cache.run_dir(settings, RUN_ID).exists()
    assert client.get("/v1/graph/explore").headers["x-hydra-cache"] == "miss"


def test_corrupt_cache_file_is_recomputed(client, settings):
    good = client.get("/v1/graph/explore").json()
    for f in api_cache.run_dir(settings, RUN_ID).glob("*.json.gz"):
        f.write_bytes(b"not gzip")
    api_cache.clear_memory()
    r = client.get("/v1/graph/explore")
    assert r.headers["x-hydra-cache"] == "miss" and r.json() == good


def test_dashboard_jobs_and_assignments_live(client):
    d1 = client.get("/v1/dashboard").json()
    assert d1["kpis"]["assignments"] == 1 and isinstance(d1["jobs"], list)
    assert {p["id"] for p in d1["previews"]} >= {"syn-1", "syn-6"}
    assert all(p["thumbnail"] for p in d1["previews"])
    assert "label" not in json.dumps(d1["previews"])  # ground truth never leaves the API


def test_entropy_map_persisted(client, settings):
    from hydra import api_task

    api_task._ENTROPY.clear()  # in-process memo shared with other tests (same synthetic images)
    client.get("/v1/dashboard")
    m = json.loads((api_cache.cache_root(settings) / "image_entropy.json").read_text())
    assert m and all(isinstance(v, float) and v >= 0 for v in m.values())


def test_analytics_sankey_matches_full_scan(client, settings):
    d = client.get("/v1/analytics/overview").json()
    conn = sqlite3.connect(settings.db_path)
    members = dict(conn.execute("SELECT incident_id, campaign_id FROM campaign_members WHERE run_id=?", (RUN_ID,)))
    ref: dict[tuple[str, str], float] = {}
    for a, b, detail in conn.execute("SELECT a, b, detail FROM pairs WHERE run_id=?", (RUN_ID,)):
        if members.get(a) and members.get(a) == members.get(b):
            for f, v in (json.loads(detail).get("family_bits") or {}).items():
                if v and v > 0:
                    ref[(f, members[a])] = ref.get((f, members[a]), 0.0) + v
    conn.close()
    got = {(x["family"], x["campaign_id"]): x["bits"] for x in d["sankey"]["links"]}
    assert got == {k: round(v, 2) for k, v in ref.items()}
    bits = [t["total_bits"] for t in d["tiers"]["links"]]
    assert bits == sorted(bits, reverse=True) and all(t["hypothesis"] != "independent" for t in d["tiers"]["links"])


# --------------------------------------------------------------------------------------- payload shape


def test_overview_shape(client):
    d = client.get("/v1/graph/overview").json()
    assert d["run_id"] == RUN_ID and d["stats"]["nodes"] == 6
    camps = {c["id"]: c for c in _rows(d["campaigns"])}
    assert camps["syn-c1"]["size"] == 3 and camps["syn-c1"]["internal_edges"] >= 1 and not camps["syn-c1"]["solo"]
    assert camps["solo:syn-6"]["solo"] is True and camps["solo:syn-6"]["size"] == 1
    for c in camps.values():
        assert c["r"] > 0 and math.isfinite(c["x"]) and math.isfinite(c["y"])
    links = _rows(d["links"])
    assert all(lk["source"] < lk["target"] and lk["pairs"] >= 1 for lk in links)
    assert {(lk["source"], lk["target"]) for lk in links} == {("syn-c1", "syn-c2")}  # kit link syn-3|syn-4
    assert links[0]["level"] == "kit"
    assert d["bounds"]["x0"] < d["bounds"]["x1"]


def test_campaign_expand_shape(client):
    d = client.get("/v1/graph/campaign/syn-c1").json()
    assert d["nodes"]["columns"] == NODE_COLUMNS and d["edges"]["columns"] == EDGE_COLUMNS
    nodes = _rows(d["nodes"])
    assert {n["id"] for n in nodes} == {"syn-1", "syn-2", "syn-3"}
    inside = {n["id"] for n in nodes}
    assert all(e["source"] in inside and e["target"] in inside for e in _rows(d["edges"]))
    ext = _rows(d["external"])
    assert ext and all((e["source"] in inside) != (e["target"] in inside) for e in ext)
    far = {x for e in ext for x in (e["source"], e["target"])} - inside
    assert {n["id"] for n in _rows(d["external_nodes"])} == far
    disc = d["campaigns"][0]
    for n in nodes:  # members sit inside their campaign disc
        assert math.hypot(n["x"] - disc["x"], n["y"] - disc["y"]) <= disc["r"] + 1e-6
    e = _rows(d["edges"])[0]
    assert len(e["reasons"]) <= 3 and all(len(r) == 6 for r in e["reasons"])
    two = client.get("/v1/graph/campaign/syn-c1,syn-c2").json()
    assert len(_rows(two["nodes"])) == 5 and "syn-3" in {x for e in _rows(two["edges"]) for x in (e["source"],)}
    assert client.get("/v1/graph/campaign/solo:syn-6").status_code == 200
    assert client.get("/v1/graph/campaign/nope").status_code == 404


def test_neighbors_and_viewport(client):
    d = client.get("/v1/graph/neighbors/syn-3").json()
    assert d["incident"]["id"] == "syn-3" and d["total"] == len(d["edges"]["rows"])
    bits = [e[EDGE_COLUMNS.index("bits")] for e in d["edges"]["rows"]]
    assert bits == sorted(bits, reverse=True)
    assert client.get("/v1/graph/neighbors/nope").status_code == 404
    lim = client.get("/v1/graph/neighbors/syn-3", params={"limit": 1}).json()
    assert len(lim["edges"]["rows"]) == 1 and lim["truncated"] == d["total"] - 1
    ov = client.get("/v1/graph/overview").json()
    b = ov["bounds"]
    vp = client.get("/v1/graph/viewport", params=b).json()
    assert len(vp["nodes"]["rows"]) == 6
    empty = client.get("/v1/graph/viewport", params={"x0": 1e6, "y0": 1e6, "x1": 1e6 + 1, "y1": 1e6 + 1}).json()
    assert empty["nodes"]["rows"] == [] and empty["edges"]["rows"] == []


def test_edge_detail_waterfall(client):
    d = client.get("/v1/graph/edge/syn-2/syn-1").json()
    assert (d["source"], d["target"]) == ("syn-1", "syn-2") and d["level"] == "campaign"
    assert d["waterfall"][-1]["cumulative"] == pytest.approx(sum(r["bits"] for r in d["reasons"]), abs=1e-3)
    assert d["campaigns"] == {"a": "syn-c1", "b": "syn-c1"}
    ind = client.get("/v1/graph/edge/syn-5/syn-6").json()
    assert ind["hypothesis"] == "independent" and ind["level"] is None
    assert client.get("/v1/graph/edge/syn-1/syn-1").status_code == 400
    assert client.get("/v1/graph/edge/syn-1/nope").status_code == 404


def test_explore_filters(client):
    full = client.get("/v1/graph/explore").json()
    kit = client.get("/v1/graph/explore", params={"levels": "kit"}).json()
    assert kit["edges"] and all(e["level"] == "kit" for e in kit["edges"])
    strong = client.get("/v1/graph/explore", params={"min_bits": 15}).json()
    assert all(e["bits"] >= 15 for e in strong["edges"]) and len(strong["edges"]) < len(full["edges"])
    assert len(strong["nodes"]) == len(full["nodes"])  # filters prune links, never incidents


def test_gzip_for_large_json(client):
    r = client.get("/v1/dashboard", headers={"accept-encoding": "gzip"})
    assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
    raw = client.get("/v1/dashboard", headers={"accept-encoding": "identity"})
    assert "content-encoding" not in raw.headers and int(r.headers["content-length"]) < len(raw.content)
    assert json.loads(gzip.decompress(gzip.compress(raw.content))) == r.json()


def test_runs_summary_and_detail(client):
    d = client.get("/v1/runs").json()
    by = {r["id"]: r for r in d["items"]}
    assert "metrics" not in by[RUN_ID] and "params" not in by[RUN_ID]
    assert by[RUN_ID]["counts"]["pairs"] >= 1 and by[RUN_ID]["corpus"]
    assert by[ASSIGN_RUN_ID]["base_run"] == RUN_ID
    full = client.get("/v1/runs", params={"full": "true"}).json()
    assert all("metrics" in r for r in full["items"])
    one = client.get(f"/v1/runs/{RUN_ID}").json()
    assert one["id"] == RUN_ID and "metrics" in one and one["counts"]["campaigns"] == 2
    assert client.get("/v1/runs/nope").status_code == 404
    assert client.get("/v1/runs/latest").json()["run"]["id"] == RUN_ID  # not shadowed by /runs/{id}


def test_task_graph_sql_filters(client):
    full = client.get("/v1/graph").json()
    f = client.get("/v1/graph", params={"min_bits": 10, "links_only": "true"}).json()
    assert all(e["total_bits"] >= 10 and e["hypothesis"] != "independent" for e in f["edges"])
    assert len(f["edges"]) < len(full["edges"])


def test_layout_discs_do_not_overlap():
    items = [(f"c{i}", sunflower(n)[1]) for i, n in enumerate([1, 1, 2, 3, 5, 8, 13, 40, 1, 2])]
    at, radius = spiral_pack(items, 0.6)
    r = dict(items)
    ids = list(at)
    for i, a in enumerate(ids):
        assert math.hypot(*at[a]) + r[a] <= radius + 1e-6
        for b in ids[i + 1:]:
            assert math.hypot(at[a][0] - at[b][0], at[a][1] - at[b][1]) >= r[a] + r[b] + 0.6 - 1e-6


def test_memo_serves_gzip_and_identity_identically(client):
    a = client.get("/v1/graph/explore", headers={"accept-encoding": "gzip"})
    b = client.get("/v1/graph/explore", headers={"accept-encoding": "gzip"})
    c = client.get("/v1/graph/explore", headers={"accept-encoding": "identity"})
    assert a.headers["content-encoding"] == "gzip" and b.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in c.headers and b.headers["x-hydra-cache"] == "hit"
    assert a.json() == b.json() == c.json()
    t1 = client.get("/v1/graph", params={"min_bits": 10, "links_only": "true"}).json()
    assert client.get("/v1/graph", params={"min_bits": 10, "links_only": "true"}).json() == t1


def test_precompute_run_and_all_runs_warm_are_disk_only(client, settings, monkeypatch):
    t = api_cache.precompute_run(settings, RUN_ID)
    assert t and all(v >= 0 for v in t.values())
    files = sorted(api_cache.run_dir(settings, RUN_ID).glob("*.json.gz"))
    assert len(files) == 4 and not api_cache._MEM and not api_cache._RESP
    api_cache.invalidate(settings, RUN_ID)
    api_cache.warm(client.app.state.hydra, [], all_runs=True)  # every finished analyse run, disk only
    assert len(list(api_cache.run_dir(settings, RUN_ID).glob("*.json.gz"))) == 4 and not api_cache._MEM
    again = api_cache.warm(client.app.state.hydra, [], all_runs=True)  # files exist: nothing is read or computed
    assert all(v < 1.0 for v in again.values())
    assert client.get("/v1/graph/overview").headers["x-hydra-cache"] == "hit"
    monkeypatch.setenv("HYDRA_API_PRECOMPUTE", "0")
    assert api_cache.precompute_run(settings, RUN_ID) is None


def test_warm_precomputes_default_views(client, settings):
    timings = api_cache.warm(client.app.state.hydra, [RUN_ID])
    assert set(timings) == {f"{RUN_ID}:graph", f"{RUN_ID}:flow", f"{RUN_ID}:dashboard"}
    names = {f.name.split("-")[0] for f in api_cache.run_dir(settings, RUN_ID).glob("*.json.gz")}
    assert names == {"graph", "system", "dashboard", "analytics"}
    for path in ("/v1/graph/explore", "/v1/graph/overview", "/v1/system/flow", "/v1/dashboard"):
        assert client.get(path).headers["x-hydra-cache"] == "hit", path
    warm_tile = client.get("/v1/graph", params={"min_p": 0, "min_bits": 10, "links_only": "true"}).json()
    api_cache.clear_memory()
    assert client.get("/v1/graph", params={"min_p": 0, "min_bits": 10, "links_only": "true"}).json() == warm_tile


def test_run_fingerprint_changes_with_a_batch_run(settings: Settings):
    """merge-batch3: a variants-pipe batch run rewrites assignment details of its base run in place (count and
    created_at unchanged), so the base run's cache key must change when a batch run against it is written."""
    seed(settings.db_path, settings.cas_dir)
    with sqlite3.connect(settings.db_path) as conn:
        before = api_cache.run_fingerprint(conn, RUN_ID)
    w = Writer(settings.db_path)
    try:
        w.run(lambda conn: conn.execute(
            "INSERT INTO runs(id, kind, params, metrics, created_at, finished_at) VALUES ('assign_batch-x',"
            " 'assign_batch', ?, '{}', '2026-11-21T20:00:00Z', '2026-11-21T20:00:01Z')",
            (json.dumps({"base_run": RUN_ID}),)))
    finally:
        w.close()
    with sqlite3.connect(settings.db_path) as conn:
        after = api_cache.run_fingerprint(conn, RUN_ID)
    assert before and after and before != after
