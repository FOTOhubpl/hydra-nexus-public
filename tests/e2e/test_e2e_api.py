"""HYDRA NEXUS end to end through the real HTTP API (one server process, synthetic corpus, temp state).

Order matters: the tests run top to bottom as one analyst session (import + analyse at session start, then
assign-batch, reviews, exports, graph, cards, search, scheduler, offline guard, audit). Known product defects are
`xfail(strict=True)` with their CR-E2E id from docs/CONTRACT_REQUESTS.md, so a fix turns them into a failure that
asks for the marker to be removed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import socket
import threading
import time
import zipfile
from typing import Any

import httpx
import pytest

from tests.e2e import corpus
from tests.e2e.server import wait_job

pytestmark = pytest.mark.e2e
POLISH = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
A_IDS = {f"nask-{i}" for i in corpus.CAMPAIGN_A}
B_IDS = {f"nask-{i}" for i in corpus.CAMPAIGN_B}
C_IDS = {f"nask-{i}" for i in corpus.CAMPAIGN_C}
NEG_IDS = {f"nask-{i}" for i in corpus.NEGATIVES}


def _campaigns(api: httpx.Client) -> list[dict[str, Any]]:
    r = api.get("/v1/campaigns", params={"limit": 500})
    r.raise_for_status()
    return r.json()["items"]


def _campaign_of(api: httpx.Client, members: set[str]) -> dict[str, Any] | None:
    for c in _campaigns(api):
        if members <= set(c["members"]):
            return c
    return None


def _table(t: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(zip(t["columns"], row, strict=False)) for row in t["rows"]]


def _incident(api: httpx.Client, iid: str) -> dict[str, Any]:
    r = api.get(f"/v1/incidents/{iid}")
    r.raise_for_status()
    return r.json()


# ----------------------------------------------------------------------------- 1. import + analyse


def test_corpus_zip_imported_and_analysed(srv, api):
    assert srv.info.get("analyse_status", "done") == "done"
    status = api.get("/v1/status").json()
    assert status["offline"] is True
    assert status["run_id"] and status["run_id"].startswith("analyse-")
    assert status["audit"]["ok"] is True and status["audit"]["events"] > 0
    inc = api.get("/v1/incidents", params={"limit": 500}).json()
    ids = {i["id"] for i in inc["items"]}
    assert ids == A_IDS | B_IDS | C_IDS | NEG_IDS, ids ^ (A_IDS | B_IDS | C_IDS | NEG_IDS)
    corpora = api.get("/v1/corpora").json()["items"]
    assert any(c["latest_run"] == status["run_id"] for c in corpora)


@pytest.mark.parametrize(("iid", "expected"), [
    ("nask-a1", {"image/png", "text/html", "application/json"}),  # screenshot, saved page, HAR
    ("nask-b1", {"text/html", "application/pdf"}),
    ("nask-b4", {"text/html", "application/pdf"}),  # the page arrives inside strona.zip
    ("nask-c1", {"image/png", "video/mp4"}),
])
def test_every_input_kind_became_an_artifact(api, iid, expected):
    d = _incident(api, iid)
    mimes = {a["mime"] for a in d["artifacts"]}
    assert expected <= mimes, (iid, mimes)
    roles = {a["role"] for a in d["artifacts"]}
    if iid == "nask-a1":
        assert {"feed_screenshot", "landing_html", "har"} <= roles
    if iid == "nask-c1":
        assert "screen_recording" in roles
    fams = {f["family"]: f["status"] for f in d["families"]}
    assert fams.get("TXT") == "observed" and fams.get("INF") == "observed", fams


def test_incident_source_ref_is_not_a_server_path(api, srv):
    for inc in api.get("/v1/incidents", params={"limit": 500}).json()["items"]:
        assert str(srv.state_dir) not in (inc.get("source_ref") or ""), inc["source_ref"]
        assert "/tmp/" not in (inc.get("source_ref") or ""), inc["source_ref"]


def test_chat_export_is_text_not_json(api):
    d = _incident(api, "nask-c1")
    chat = next(a for a in d["artifacts"] if a["path_in_incident"] == "czat.txt")
    assert chat["mime"].startswith("text/plain"), chat["mime"]


def test_campaigns_a_and_b_recovered_exactly(api, ctx):
    a, b = _campaign_of(api, A_IDS), _campaign_of(api, B_IDS)
    assert a is not None and b is not None and a["id"] != b["id"]
    assert set(a["members"]) == A_IDS and set(b["members"]) == B_IDS
    assert "PKO" in a["label"] and "Buty" in b["label"]
    ctx["A"], ctx["B"] = a["id"], b["id"]
    detail = api.get(f"/v1/campaigns/{a['id']}").json()
    assert detail["core_genome"], "campaign A has no core genome"
    kinds = {g["kind"] for g in detail["core_genome"]}
    assert kinds & {"landing_host", "registrable_domain", "tracker_id", "phone", "iban", "fb_pixel", "pixel_id"}, kinds
    assert all(g["bits"] > 0 for g in detail["core_genome"])
    assert any(POLISH.search(g.get("explanation_pl") or "") for g in detail["core_genome"])


def test_negatives_stay_unlinked(api):
    for c in _campaigns(api):
        members = set(c["members"])
        neg = members & NEG_IDS
        if neg:
            assert len(members) == 1, f"benign pages grouped into a campaign: {c['id']} {sorted(members)}"


def test_campaign_c_sms_lure_grouped(api):
    """CR-E2E-2 (fixed by SCORE-C1): one sender number, one short-link domain and one recording across four
    incidents; operator values are counted per pass-0 operator unit in analyse, as in assignment."""
    c = _campaign_of(api, C_IDS)
    assert c is not None, [x["members"] for x in _campaigns(api) if set(x["members"]) & C_IDS]


def test_pair_scores_and_polish_reasons(api):
    same = api.get("/v1/pairs/nask-a1/nask-a2").json()
    s = same["score"]
    assert s["hypothesis"] == "same_campaign" and same["same_campaign_in_run"] is True
    assert s["campaign_bits"] >= 10.0 and s["calibrated"] is False
    assert s["reasons"] and all(POLISH.search(r["explanation_pl"]) or "bit" in r["explanation_pl"]
                                for r in s["reasons"][:5])
    cross = api.get("/v1/pairs/nask-a1/nask-b1").json()
    assert cross["same_campaign_in_run"] is False
    assert cross["score"]["campaign_bits"] < s["campaign_bits"]
    neg = api.get("/v1/pairs/nask-n1/nask-n2").json()
    assert neg["same_campaign_in_run"] is False and neg["score"]["hypothesis"] != "same_campaign"


# ----------------------------------------------------------------------------- 2. assign-batch + SSE


class _Sse:
    """Minimal SSE reader on a thread (httpx stream), collecting (event, data) tuples."""

    def __init__(self, srv) -> None:  # noqa: ANN001
        self.events: list[tuple[str, Any]] = []
        self.hello = threading.Event()
        self._stop = threading.Event()
        self.error: BaseException | None = None
        self._t = threading.Thread(target=self._run, args=(srv,), daemon=True)
        self._t.start()

    def _run(self, srv) -> None:  # noqa: ANN001
        try:
            # Like the browser's EventSource: the session cookie, no bearer token (AUTH-LOGIN: SSE needs the session).
            with srv.session_client(timeout=httpx.Timeout(5.0, read=60.0)) as c, \
                    c.stream("GET", "/v1/stream", headers={"Accept": "text/event-stream"}) as r:
                assert r.status_code == 200, r.status_code
                assert r.headers["content-type"].startswith("text/event-stream")
                ev, data = "message", []
                for line in r.iter_lines():
                    if self._stop.is_set():
                        return
                    if line.startswith("event:"):
                        ev = line[6:].strip()
                    elif line.startswith("data:"):
                        data.append(line[5:].strip())
                    elif line == "":
                        if data:
                            payload = json.loads("\n".join(data))
                            self.events.append((ev, payload))
                            if ev == "hello":
                                self.hello.set()
                        ev, data = "message", []
        except BaseException as exc:  # noqa: BLE001 — reported by the test
            if not self._stop.is_set():
                self.error = exc

    def wait(self, pred, timeout: float = 30.0):  # noqa: ANN001, ANN201
        t_end = time.monotonic() + timeout
        while time.monotonic() < t_end:
            for ev in list(self.events):
                if pred(ev):
                    return ev
            time.sleep(0.2)
        return None

    def close(self) -> None:
        self._stop.set()


def test_assign_batch_new_campaign_duplicate_and_known(srv, api, ctx):
    sse = _Sse(srv)
    try:
        assert sse.hello.wait(15), f"no SSE hello: {sse.error}"
        hello = next(d for e, d in sse.events if e == "hello")
        status = api.get("/v1/status").json()
        assert hello["run_id"] == status["run_id"]
        r = api.post("/v1/assign-batch", json={"path": str(srv.new)})
        assert r.status_code == 202, r.text
        assert r.json()["items"] == 5
        body = wait_job(api, f"/v1/assign-batch/{r.json()['job_id']}", 600)
        rep = body["report"]
        ctx["batch"] = rep
        rows = {row["item"]: row for row in rep["items"]}
        assert set(rows) == {*corpus.NEW_D, corpus.NEW_DUP, corpus.NEW_A}
        # unseen campaign D: three items, one new pattern holding exactly them
        d_rows = [rows[i] for i in corpus.NEW_D]
        assert {x["decision"] for x in d_rows} == {"new_campaign"}, d_rows
        pids = {x["new_pattern_id"] for x in d_rows}
        assert len(pids) == 1 and None not in pids
        pattern = next(p for p in rep["new_patterns"] if p["id"] in pids)
        assert sorted(pattern["items"]) == sorted(corpus.NEW_D)
        assert "Energa" in pattern["label"]
        assert pattern["links"] and all(lk["hypothesis"] == "same_campaign" for lk in pattern["links"])
        # duplicate of a base incident: identified as such and attached to campaign A
        dup = rows[corpus.NEW_DUP]
        assert dup["duplicate_of"] == "nask-a1" and dup["campaign_id"] == ctx["A"]
        assert any(x["item"] == corpus.NEW_DUP and "nask-a1" in x["reason_pl"] for x in rep["duplicates"])
        # a fresh member of known campaign A
        assert rows[corpus.NEW_A]["decision"] == "assign" and rows[corpus.NEW_A]["campaign_id"] == ctx["A"]
        assert rep["decisions"] == {"assign": 2, "new_campaign": 3}
        new_ids = {row["incident_id"] for row in rep["items"]}
        ctx["new_ids"] = new_ids
        ctx["new_by_item"] = {k: v["incident_id"] for k, v in rows.items()}
        # SSE: deltas for the new material reached the open stream
        delta = sse.wait(lambda e: e[0] == "delta" and "assign" in (e[1].get("kinds") or [])
                         and {n["id"] for n in e[1].get("nodes") or []} & new_ids, timeout=30)
        assert delta is not None, [e for e, _ in sse.events]
        node = next(n for n in delta[1]["nodes"] if n["id"] in new_ids)
        assert node["new"] is True and node["decision"] in {"assign", "new_campaign"}
        assert delta[1]["run_id"] == status["run_id"] and delta[1]["seq"] > 0
        batch = sse.wait(lambda e: e[0] == "delta" and e[1].get("batch"), timeout=20)
        assert batch is not None, "no batch delta after assign-batch"
        assert batch[1]["batch"]["summary"]["items"] == 5
    finally:
        sse.close()


def test_assignments_listed_with_polish_reasons(api, ctx):
    items = api.get("/v1/assignments", params={"limit": 500}).json()["items"]
    by_inc = {a["incident_id"]: a for a in items}
    assert ctx["new_ids"] <= set(by_inc)
    a5 = by_inc[ctx["new_by_item"][corpus.NEW_A]]
    assert a5["decision"] == "assign" and a5["campaign_id"] == ctx["A"]
    assert POLISH.search(a5["reason_pl"]) and a5["candidates"]
    d1 = by_inc[ctx["new_by_item"]["new-d1"]]
    assert d1["decision"] == "new_campaign" and d1["campaign_id"] is None and POLISH.search(d1["reason_pl"])
    batches = api.get("/v1/assign-batches").json()["items"]
    assert batches and batches[0]["batch_run"] == ctx["batch"]["batch_run"]


def test_single_item_upload_assigns_with_polish_reason(srv, api, ctx):
    """Multipart upload of one incident's files (what the GUI drop zone sends) → one assignment."""
    item = srv.new / corpus.NEW_A
    files = [("files", (p.name, p.read_bytes())) for p in sorted(item.iterdir())]
    r = api.post("/v1/analyse", files=files)
    assert r.status_code == 202 and r.json()["mode"] == "files", r.text
    body = wait_job(api, f"/v1/analyse/{r.json()['job_id']}", 300)
    a = body["assignment"]
    assert a["decision"] == "assign" and a["campaign_id"] == ctx["A"], a.get("reason_pl")
    assert POLISH.search(a["reason_pl"])
    assert body["campaign"]["id"] == ctx["A"]
    report = body["result"]["input_report"]
    # the same files already arrived in the batch: reported per file as duplicates of that batch incident
    assert report["counts"] == {"duplicate": len(files)}, report["counts"]
    assert {f["duplicate_of"] for f in report["files"]} == {ctx["new_by_item"][corpus.NEW_A]}
    assert POLISH.search(report["summary_pl"]) or "duplikat" in report["summary_pl"]


def test_live_counts_include_the_batch(api, ctx):
    camp = next(c for c in _campaigns(api) if c["id"] == ctx["A"])
    assert camp["size"] >= 6 and camp.get("new", 0) >= 2, camp  # 4 base + new-a5 + duplicate
    dash = api.get("/v1/dashboard").json()
    assert dash["kpis"], dash.keys()


# ----------------------------------------------------------------------------- 3. reviews


def _same_campaign(api: httpx.Client, a: str, b: str) -> bool:
    return bool(api.get(f"/v1/pairs/{a}/{b}").json()["same_campaign_in_run"])


def test_review_reject_splits_and_undo_restores(api, ctx):
    assert _same_campaign(api, "nask-b1", "nask-b2")
    r = api.post("/v1/reviews", json={"action": "reject_link", "target": {"a": "nask-b2", "b": "nask-b1"},
                                      "note": "e2e: różne sklepy"})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["effect"] == "applied", out
    assert out["review"]["target"] == {"a": "nask-b1", "b": "nask-b2"}  # sorted pair
    assert re.fullmatch(r"[0-9a-f]{64}", out["event_hash"])
    assert not _same_campaign(api, "nask-b1", "nask-b2"), "cannot-link did not separate the pair"
    undo = api.post("/v1/reviews", json={"action": "undo", "undoes": out["review"]["id"]})
    assert undo.status_code == 201 and undo.json()["effect"] == "applied", undo.text
    assert _same_campaign(api, "nask-b1", "nask-b2"), "undo did not restore the grouping"
    again = api.post("/v1/reviews", json={"action": "undo", "undoes": out["review"]["id"]})
    assert again.status_code == 409
    reviews = api.get("/v1/reviews").json()
    assert reviews["total"] >= 2
    orig = next(x for x in reviews["items"] if x["id"] == out["review"]["id"])
    assert orig.get("undone") is True, orig


def test_review_accept_links_pair(api):
    r = api.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "nask-c1", "b": "nask-c2"}})
    assert r.status_code == 201 and r.json()["effect"] == "applied", r.text
    assert _same_campaign(api, "nask-c1", "nask-c2"), "must-link did not group the pair"
    edge = api.get("/v1/graph/edge/nask-c1/nask-c2").json()
    assert edge["review"], edge.get("review")
    undo = api.post("/v1/reviews", json={"action": "undo", "undoes": r.json()["review"]["id"]})
    assert undo.status_code == 201


def test_batch_items_survive_reviews(api, ctx):
    camp = next(c for c in _campaigns(api) if set(c["members"]) >= A_IDS)
    assert camp["size"] >= 6, camp
    assert api.get("/v1/dashboard").json()["kpis"]["incidents"] >= 25
    assert api.get("/v1/graph/overview").json()["stats"]["nodes"] >= 25


def _views_agree(api: httpx.Client, ctx: dict, step: str, extra: set[str] = frozenset()) -> dict[str, Any]:
    """Campaign list, campaign card, lineage, graph, dashboard, assignments and the dataset export describe the same
    selected version: every post-analysis item is counted and points at a campaign of that version."""
    camps = {c["id"]: c for c in _campaigns(api)}
    total = api.get("/v1/incidents", params={"limit": 500}).json()["total"]
    kpis = api.get("/v1/dashboard").json()["kpis"]
    nodes = api.get("/v1/graph/overview").json()["stats"]["nodes"]
    assert kpis["incidents"] == nodes == total, (step, kpis["incidents"], nodes, total)
    asg = {a["incident_id"]: a for a in api.get("/v1/assignments", params={"limit": 500}).json()["items"]}
    for iid in set(ctx["new_ids"]) | set(extra):
        assert iid in asg, (step, iid)
        a = asg[iid]
        if a["decision"] == "assign":
            assert a["campaign_id"] in camps, (step, iid, a["campaign_id"])
    for cid in {asg[i]["campaign_id"] for i in ctx["new_ids"] if asg[i]["decision"] == "assign"}:
        card = api.get(f"/v1/campaigns/{cid}")
        assert card.status_code == 200, (step, cid)
        cj = card.json()
        # B15 (QA audit): the list carries clustered + assigned members; the card keeps them apart.
        assigned = {m if isinstance(m, str) else m.get("id") for m in cj.get("assigned_members", [])}
        card_all = set(cj["members"]) | assigned
        assert card_all == set(camps[cid]["members"]) or set(cj["members"]) == set(camps[cid]["members"]), step
        n_new = sum(1 for i in asg.values() if i["decision"] == "assign" and i["campaign_id"] == cid
                    and i["incident_id"] not in camps[cid]["members"])
        assert camps[cid]["size"] == len(camps[cid]["members"]) + n_new, (step, cid, camps[cid]["size"], n_new)
        assert api.get(f"/v1/campaigns/{cid}/lineage").status_code == 200, (step, cid)
    z = zipfile.ZipFile(io.BytesIO(api.get("/v1/export/dataset").content))
    root = z.namelist()[0].split("/")[0]
    exported = {c["id"]: c for c in json.loads(z.read(f"{root}/campaigns.json"))}
    assert set(exported) == set(camps), step
    rows = {r["incident_id"]: r for r in csv.DictReader(io.StringIO(z.read(f"{root}/assignments.csv").decode()))}
    for iid in ctx["new_ids"]:
        assert rows[iid]["decision"] == asg[iid]["decision"], (step, iid)
        if asg[iid]["decision"] == "assign":
            assert rows[iid]["campaign_id"] == asg[iid]["campaign_id"], (step, iid)
    return {"camps": camps, "asg": asg, "export": exported}


def test_review_replay_merge_split_undo_keeps_views_consistent(api, ctx):
    """CR-E2E-5 / Astra U11 replay: merge -> late event -> split -> re-export -> undo split -> undo merge; after every
    step all views agree on the selected version and the post-analysis items stay attached."""
    a5 = ctx["new_by_item"][corpus.NEW_A]
    base = _views_agree(api, ctx, "start")
    camp_a = _campaign_of(api, A_IDS)["id"]
    camp_b = _campaign_of(api, B_IDS)["id"]
    assert base["asg"][a5]["campaign_id"] == camp_a
    merge = api.post("/v1/reviews", json={"action": "merge_campaigns", "target": {"campaign_ids": [camp_a, camp_b]}})
    assert merge.status_code == 201 and merge.json()["effect"] == "applied", merge.text
    merged = _campaign_of(api, A_IDS | B_IDS)
    assert merged is not None
    v = _views_agree(api, ctx, "merge")
    assert v["asg"][a5]["campaign_id"] == merged["id"], "batch item not carried into the merged campaign"
    # late event: material analysed against the revision
    r = api.post("/v1/analyse", json={"url": "https://late-event.example/promo"})
    late = wait_job(api, f"/v1/analyse/{r.json()['job_id']}", 120)["result"]["incident_id"]
    _views_agree(api, ctx, "late", {late})
    split = api.post("/v1/reviews", json={"action": "split_campaign",
                                          "target": {"campaign_id": merged["id"], "incident_ids": sorted(B_IDS)}})
    assert split.status_code == 201 and split.json()["effect"] == "applied", split.text
    v1 = _views_agree(api, ctx, "split", {late})
    v2 = _views_agree(api, ctx, "re-export", {late})  # same version, same export
    assert v1["export"] == v2["export"]
    assert v1["asg"][a5]["campaign_id"] == camp_a
    for rid in (split.json()["review"]["id"], merge.json()["review"]["id"]):
        u = api.post("/v1/reviews", json={"action": "undo", "undoes": rid})
        assert u.status_code == 201 and u.json()["effect"] == "applied", u.text
        _views_agree(api, ctx, f"undo {rid}", {late})
    end = _views_agree(api, ctx, "end", {late})
    assert set(end["camps"]) == set(base["camps"]), "undo did not restore the campaigns of the start version"
    assert end["asg"][a5]["campaign_id"] == camp_a


def test_review_validation(api):
    assert api.post("/v1/reviews", json={"action": "accept_link", "target": {"a": "nask-a1"}}).status_code == 400
    assert api.post("/v1/reviews", json={"action": "accept_link",
                                         "target": {"a": "nask-a1", "b": "nask-a1"}}).status_code == 400
    assert api.post("/v1/reviews", json={"action": "undo", "undoes": "nope"}).status_code == 404
    assert api.post("/v1/reviews", json={"action": "bogus"}).status_code == 422


def test_review_unsure_and_undo(api):
    r = api.post("/v1/review/unsure", json={"target": {"a": "nask-n1", "b": "nask-n2"}, "note": "nie wiem"})
    assert r.status_code in (200, 201), r.text
    mark = r.json().get("mark") or r.json()
    mid = mark.get("id") or (r.json().get("item") or {}).get("id")
    assert mid, r.json()
    listed = api.get("/v1/review/unsure").json()["items"]
    assert any(x["id"] == mid for x in listed)
    u = api.post("/v1/review/unsure/undo", json={"id": mid})
    assert u.status_code in (200, 201), u.text
    assert not any(x["id"] == mid for x in api.get("/v1/review/unsure").json()["items"])
    q = api.get("/v1/review/queue").json()
    assert q["calibrated"] is False and q["threshold_bits"] > 0 and isinstance(q["items"], list)


# ----------------------------------------------------------------------------- 4. exports


def test_export_campaign_json_and_csv(api, ctx):
    j = api.get(f"/v1/campaigns/{ctx['A']}/export")
    assert j.status_code == 200
    data = j.json()
    assert data["id"] == ctx["A"] and A_IDS <= set(data["members"])
    assert data["core_genome"] and data["run_id"]
    c = api.get(f"/v1/campaigns/{ctx['A']}/export", params={"format": "csv"})
    assert c.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(c.text)))
    assert rows and {"run_id", "campaign_id", "incident_id", "family", "kind", "value"} <= set(rows[0])
    assert {r["campaign_id"] for r in rows} == {ctx["A"]}
    assert A_IDS <= {r["incident_id"] for r in rows}
    assert any(r["kind"] == "landing_host" and r["value"] == corpus.A["host"] for r in rows)
    for r in rows:  # spreadsheet formula injection guard
        assert not any(str(v).startswith(("=", "+", "@")) for v in r.values() if v and not str(v)[1:2].isdigit())


def test_export_neo4j(api):
    from tests.security.cypher import statements

    r = api.get("/v1/export/neo4j")
    assert r.status_code == 200
    text = r.text
    head = text.splitlines()[0]
    m = re.search(r"incidents (\d+), campaigns (\d+)", head)
    assert m, head
    assert int(m.group(1)) >= 20
    stmts = statements(text)
    assert len(stmts) >= 5  # constraints + UNWIND batches
    assert any("Incident" in s for s in stmts) and any("Campaign" in s for s in stmts)
    assert any("MEMBER_OF" in s for s in stmts) and any("HAS_GENE" in s for s in stmts)
    assert "nask-a1" in text and corpus.A["host"] in text


def test_export_stix(api, ctx):
    import stix2

    bundle = api.get(f"/v1/campaigns/{ctx['A']}/stix").json()
    assert bundle["type"] == "bundle"
    stix2.parse(json.dumps(bundle), version="2.1", allow_custom=True)
    ids = {o["id"] for o in bundle["objects"]}
    assert len(ids) == len(bundle["objects"])
    for o in bundle["objects"]:
        for k, v in o.items():
            if k.endswith("_ref"):
                assert v in ids, (k, v)
            elif k.endswith("_refs"):
                assert set(v) <= ids, k
    types = {o["type"] for o in bundle["objects"]}
    assert {"identity", "campaign", "indicator", "relationship"} <= types, types
    camp = next(o for o in bundle["objects"] if o["type"] == "campaign")
    assert "PKO" in camp["name"]
    patterns = " ".join(o.get("pattern", "") for o in bundle["objects"] if o["type"] == "indicator")
    assert corpus.A["host"] in patterns


def test_export_xarf_abuse_package(api, ctx):
    from hydra.export_abuse import validate_xarf

    r = api.get(f"/v1/campaigns/{ctx['A']}/abuse-package")
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    reports = [n for n in names if n.startswith("reports/") and n.endswith(".xarf.json")]
    assert reports and "manifest.json" in names
    rep = json.loads(z.read(reports[0]))
    assert validate_xarf(rep) == [], validate_xarf(rep)
    assert rep["xarf_version"].startswith("4.") and rep["category"] == "content"
    assert rep["source_identifier"] == corpus.A["host"]
    assert f"campaign:{ctx['A']}" in rep["tags"]
    manifest = json.loads(z.read("manifest.json"))
    entry = next(e for e in manifest["reports"] if e["report"] == reports[0])
    assert entry["sha256"] == hashlib.sha256(z.read(reports[0])).hexdigest() and entry["xarf_valid"] is True
    assert set(entry["incidents"]) == A_IDS
    assert entry["evidence"] and all(re.fullmatch(r"sha256:[0-9a-f]{64}", e["hash"]) for e in entry["evidence"])


def test_xarf_url_keeps_observed_scheme(api, ctx):
    r = api.get(f"/v1/campaigns/{ctx['A']}/abuse-package")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    rep = json.loads(z.read(next(n for n in z.namelist() if n.endswith(".xarf.json"))))
    assert rep["url"].startswith("https://"), rep["url"]


def test_export_dataset_zip(api, ctx):
    r = api.get("/v1/export/dataset")
    assert r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    root = names[0].split("/")[0]
    for f in ("manifest.json", "incidents.csv", "campaigns.json", "assignments.csv", "README_DATASET.md",
              "genes.parquet", "links.parquet"):
        assert f"{root}/{f}" in names, f
    manifest = json.loads(z.read(f"{root}/manifest.json"))
    blob = json.dumps(manifest)
    for n in names:
        if not n.endswith(("manifest.json", "SIGNED_MANIFEST.json", "SIGNED_MANIFEST.sig")):
            assert hashlib.sha256(z.read(n)).hexdigest() in blob, n
    incidents = list(csv.DictReader(io.StringIO(z.read(f"{root}/incidents.csv").decode("utf-8-sig"))))
    assert {r.get("incident_id") or r.get("id") for r in incidents} >= A_IDS | B_IDS
    camps = json.loads(z.read(f"{root}/campaigns.json"))
    camp_list = camps if isinstance(camps, list) else camps.get("campaigns", camps.get("items", []))
    assert any(set(c.get("members", [])) >= A_IDS for c in camp_list)
    import pyarrow.parquet as pq

    genes = pq.read_table(io.BytesIO(z.read(f"{root}/genes.parquet")))
    assert genes.num_rows > 100 and "kind" in genes.column_names
    # spec test 7: the downloaded ZIP verifies offline; one changed byte fails and names the file
    import tempfile
    from pathlib import Path

    from hydra.evidence_sign import verify_package

    with tempfile.TemporaryDirectory() as td:
        zpath = Path(td) / "export.zip"
        zpath.write_bytes(r.content)
        ok = verify_package(zpath)
        assert ok["ok"], ok["errors"]
        assert ok["key_fingerprint"] == r.headers["x-hydra-signing-key"]
        bad = Path(td) / "bad.zip"
        with zipfile.ZipFile(zpath) as src, zipfile.ZipFile(bad, "w") as dst:
            for n in src.namelist():
                data = src.read(n)
                if n.endswith("/campaigns.json"):
                    data = data[:-1] + bytes([data[-1] ^ 1])
                dst.writestr(n, data)
        res = verify_package(bad)
        assert not res["ok"] and any("campaigns.json" in e for e in res["errors"])


def test_export_subgraph_json_and_cypher(api, ctx):
    ids = sorted(A_IDS)
    j = api.post("/v1/graph/export", json={"ids": ids, "format": "json"})
    assert j.status_code == 200
    g = j.json()
    assert {n["id"] for n in g["nodes"]} == A_IDS
    assert g["edges"] and all(e["source"] in A_IDS and e["target"] in A_IDS for e in g["edges"])
    assert [c["id"] for c in g["campaigns"]] == [ctx["A"]]
    cy = api.post("/v1/graph/export", json={"ids": ids, "format": "cypher"})
    assert cy.status_code == 200 and "nask-a1" in cy.text and "nask-b1" not in cy.text


# ----------------------------------------------------------------------------- 5. graph LOD


def test_graph_overview_and_campaign_expand(api, ctx):
    ov = api.get("/v1/graph/overview").json()
    assert ov["stats"]["nodes"] >= 20 and ov["stats"]["campaigns"] >= 2  # >= 25 once CR-E2E-5 is fixed
    assert ov["thresholds"]["link_bits"] > 0
    camps = ov["campaigns"]
    rows = _table(camps) if isinstance(camps, dict) else camps
    by_id = {r["id"]: r for r in rows}
    assert ctx["A"] in by_id and ctx["B"] in by_id
    b = ov["bounds"]
    assert b["x0"] < b["x1"] and b["y0"] < b["y1"]
    exp = api.get(f"/v1/graph/campaign/{ctx['A']}").json()
    nodes = _table(exp["nodes"])
    assert A_IDS <= {n["id"] for n in nodes}
    edges = _table(exp["edges"])
    assert edges and all(e["bits"] > 0 for e in edges)
    assert {e["level"] for e in edges} <= {"campaign", "kit", "asset", "weak", "review"}
    disc = exp["campaigns"][0]
    for n in nodes:  # members sit inside their campaign disc
        if n["campaign_id"] == ctx["A"]:
            assert (n["x"] - disc["x"]) ** 2 + (n["y"] - disc["y"]) ** 2 <= (disc["r"] + 1e-6) ** 2
    vp = api.get("/v1/graph/viewport", params=b).json()
    assert {n["id"] for n in _table(vp["nodes"])} >= A_IDS | B_IDS
    nb = api.get("/v1/graph/neighbors/nask-a1").json()
    assert nb["incident"]["campaign_id"] == ctx["A"]
    assert (A_IDS - {"nask-a1"}) <= {n["id"] for n in _table(nb["nodes"])}


def test_graph_edge_waterfall_and_path(api):
    e = api.get("/v1/graph/edge/nask-a1/nask-a2").json()
    assert e["level"] == "campaign" and e["bits"] >= 10
    assert e["waterfall"], "edge has no bits waterfall"
    p = api.get("/v1/graph/path", params={"a": "nask-a1", "b": "nask-a4"}).json()
    assert p["found"] is True and p["hops"] >= 1 and p["steps"][0]["level_pl"]
    none = api.get("/v1/graph/path", params={"a": "nask-a1", "b": "nask-n3"}).json()
    assert none["found"] is False or none["weakest_bits"] < 10


# ----------------------------------------------------------------------------- 6. cards


def test_likeness_card(api):
    card = api.get("/v1/incidents/nask-a1/likeness").json()
    assert card["incident_id"] == "nask-a1" and card["triggered"] is False
    assert POLISH.search(card["disclaimer_pl"]) and "deepfake" in card["disclaimer_pl"]
    assert 0 <= card["present"] <= card["total"] and card["signals"]
    assert api.get("/v1/incidents/nope/likeness").status_code == 404


def test_lineage(api, ctx):
    lin = api.get(f"/v1/campaigns/{ctx['A']}/lineage").json()
    nodes = {n["incident_id"]: n for n in lin["nodes"]}
    assert A_IDS <= set(nodes)
    assert lin["roots"] == ["nask-a1"] and nodes["nask-a1"]["root"] is True
    dated = sorted((n for n in nodes.values() if n["observed_at"]), key=lambda n: n["order"])
    assert [n["observed_at"] for n in dated] == sorted(n["observed_at"] for n in dated)
    assert all(nodes[n["parent"]]["order"] < n["order"] for n in nodes.values() if n["parent"])
    assert lin["edges"] and POLISH.search(lin["direction_pl"])


def test_similar_cases(api, ctx):
    sim = api.get("/v1/incidents/nask-a1/similar").json()
    assert sim["calibrated"] is False and sim["operator_values"] > 0
    top = [i["incident_id"] for i in sim["items"]][:3]
    assert top and set(top) <= A_IDS | ctx["new_ids"], sim["items"][:3]
    assert all(i["incident_id"] not in NEG_IDS for i in sim["items"])


# ----------------------------------------------------------------------------- 7. search + hint


def test_search_and_hint(api, ctx):
    s = api.get("/v1/search", params={"q": corpus.A["host"]}).json()
    host = next(g for g in s["genes"] if g["kind"] == "landing_host" and g["value"] == corpus.A["host"])
    assert A_IDS <= set(host["incidents"])
    r = api.post("/v1/analyse", json={"hint": "PKO Bonus"})
    assert r.status_code == 202 and r.json()["mode"] == "hint"
    res = wait_job(api, f"/v1/analyse/{r.json()['job_id']}", 120)["result"]
    assert res["kind"] == "hint"
    camps = res["search"]["campaigns"]
    assert camps and camps[0]["campaign_id"] == ctx["A"] and set(camps[0]["incidents"]) >= A_IDS, camps[:2]
    assert res["search"]["ad_library"]["status"] in ("not_configured", "offline", "replay")


# ----------------------------------------------------------------------------- 8. offline: scheduler, URL, guard


def _netlog(srv) -> list[str]:  # noqa: ANN001
    return srv.netlog.read_text().splitlines() if srv.netlog.exists() else []


def test_scheduler_run_now_offline(srv, api):
    assert api.post("/v1/watchlist", json={"add": [{"kind": "keyword", "value": "PKO Bonus"},
                                                   {"kind": "domain", "value": corpus.A["host"]}]}).status_code == 200
    t0 = time.time()
    r = api.post("/v1/scheduler/run").json()
    assert r["available"] is True and r["started"] is True
    jobs: dict[str, Any] = {}
    t_end = time.monotonic() + 60
    while time.monotonic() < t_end:
        st = api.get("/v1/scheduler/status").json()
        jobs = st["jobs"]
        if not st["running"] and jobs and all(j.get("last_finished") for j in jobs.values()):
            break
        time.sleep(0.5)
    assert set(jobs) >= {"inbox", "keyword_watch", "reenrich_domains", "adlibrary_pull"}, jobs
    for name, j in jobs.items():
        assert j["status"] != "error", (name, j)
    assert jobs["reenrich_domains"]["status"] == "skipped"
    assert "OFFLINE" in json.dumps(jobs["reenrich_domains"]["detail"])
    assert st["live_enrichment"] is False
    assert time.time() >= t0
    assert _netlog(srv) == []


def test_url_input_offline_no_fetch(srv, api):
    r = api.post("/v1/analyse", json={"url": f"{corpus.A['host']}/oferta/999"})  # typed without a scheme
    assert r.status_code == 202 and r.json()["mode"] == "url"
    res = wait_job(api, f"/v1/analyse/{r.json()['job_id']}", 120)["result"]
    f = res["fetch"]
    assert f["url"].startswith("https://") and f["hops"] == 0 and f["status"] == "unavailable"
    assert "offline" in f["status_pl"].lower()
    assert res["assignment"]["reason_pl"]
    assert _netlog(srv) == []


def test_url_on_campaign_landing_host_assigns(api, ctx):
    r = api.post("/v1/analyse", json={"url": f"https://{corpus.A['host']}/oferta/123?utm_source=fb"})
    res = wait_job(api, f"/v1/analyse/{r.json()['job_id']}", 120)["result"]
    assert res["assignment"]["decision"] == "assign" and res["assignment"]["campaign_id"] == ctx["A"]


def test_offline_socket_guard_in_process(srv, monkeypatch):
    """HYDRA_OFFLINE=1 kill switch: with a recording socket layer below it, a public connect / DNS lookup never
    reaches the layer (OfflineBlocked), loopback does (the e2e API answers)."""
    from hydra import netguard
    from hydra.config import Settings

    seen: list[object] = []
    below_connect = socket.socket.connect
    below_gai = socket.getaddrinfo

    def rec_connect(self, address):  # type: ignore[no-untyped-def]
        seen.append(address)
        return below_connect(self, address)

    def rec_gai(host, *a, **k):  # type: ignore[no-untyped-def]
        seen.append(host)
        return below_gai(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", rec_connect)
    monkeypatch.setattr(socket, "getaddrinfo", rec_gai)
    monkeypatch.setenv("HYDRA_SOCKET_GUARD", "1")
    netguard.uninstall_offline_guard()
    assert netguard.guard_if_offline(Settings(offline=True)) is True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s, pytest.raises(netguard.OfflineBlocked):
            s.connect(("203.0.113.9", 443))
        with pytest.raises(netguard.OfflineBlocked):
            socket.getaddrinfo("example.org", 443)
        with pytest.raises(netguard.OfflineBlocked):
            netguard.require_online("e2e probe", Settings(offline=True))
        assert seen == []
        with socket.create_connection(("127.0.0.1", srv.port), timeout=5):
            pass
        assert ("127.0.0.1", srv.port) in seen
    finally:
        netguard.uninstall_offline_guard()


# ----------------------------------------------------------------------------- 9. artefacts, audit


def test_artifact_endpoints(api):
    d = _incident(api, "nask-c1")
    vid = next(a for a in d["artifacts"] if a["mime"] == "video/mp4")
    png = next(a for a in d["artifacts"] if a["mime"] == "image/png")
    raw = api.get(f"/v1/artifacts/{png['artifact_id']}/raw")
    assert raw.status_code == 200 and raw.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert hashlib.sha256(raw.content).hexdigest() == png["artifact_id"]
    th = api.get(f"/v1/artifacts/{png['artifact_id']}/thumb")
    assert th.status_code == 200 and th.headers["content-type"].startswith("image/")
    fr = api.get(f"/v1/artifacts/{vid['artifact_id']}/frame", params={"t": 0.5})
    assert fr.status_code == 200 and fr.headers["content-type"].startswith("image/")
    a1 = _incident(api, "nask-a1")
    html = next(a for a in a1["artifacts"] if a["mime"] == "text/html")
    info = api.get(f"/v1/artifacts/{html['artifact_id']}/info")
    assert info.status_code == 200
    txt = api.get(f"/v1/artifacts/{html['artifact_id']}/text")
    assert txt.status_code == 200 and "PKO" in txt.text
    har = next(a for a in a1["artifacts"] if a["role"] == "har")
    h = api.get(f"/v1/artifacts/{har['artifact_id']}/har")
    assert h.status_code == 200 and corpus.A["host"] in h.text
    raw_html = api.get(f"/v1/artifacts/{html['artifact_id']}/raw")
    assert raw_html.status_code == 200
    csp = raw_html.headers.get("content-security-policy", "")
    assert "text/html" not in raw_html.headers.get("content-type", "") or "sandbox" in csp, raw_html.headers


def test_auth_gate_end_to_end(srv, api):
    """AUTH-LOGIN on the real server: SSE needs the session, roles hold, audit events carry no secrets."""
    with srv.client(auth=False) as anon:
        assert anon.get("/v1/stream", headers={"Accept": "text/event-stream"}).status_code == 401
        assert anon.get("/v1/campaigns").status_code == 401
        assert anon.get("/v1/auth/session").json()["authenticated"] is False
    r = api.post("/v1/auth/users", json={"username": "e2e-viewer", "role": "viewer"})
    assert r.status_code == 201, r.text
    viewer_pw = r.json()["password"]
    assert r.json()["user"]["must_change_password"] is True
    from tests.e2e.server import login_session

    with login_session(srv.base_url, "e2e-viewer", viewer_pw) as v:
        assert v.get("/v1/campaigns").json()["detail"]["code"] == "password_change_required"
        new_pw = "Viewer-e2e-Passw0rd!"
        r = v.post("/v1/auth/password", json={"current_password": viewer_pw, "new_password": new_pw})
        assert r.status_code == 200, r.text
        assert v.get("/v1/campaigns").status_code == 200
        r = v.post("/v1/reviews", json={"a": "x", "b": "y", "decision": "accept"})
        assert r.status_code == 403 and r.json()["detail"]["code"] == "forbidden"
        assert v.get("/v1/auth/users").status_code == 403
        assert v.post("/v1/auth/logout").status_code == 204
        assert v.get("/v1/campaigns").status_code == 401
    import sqlite3

    conn = sqlite3.connect(f"file:{srv.state_dir / 'hydra.sqlite'}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT type, payload FROM events WHERE type IN ('LOGIN_OK','LOGIN_FAIL','LOGOUT',"
                            "'PASSWORD_CHANGED','USER_CREATED','TOKEN_CREATED')").fetchall()
    finally:
        conn.close()
    types = {t for t, _ in rows}
    assert {"LOGIN_OK", "LOGIN_FAIL", "LOGOUT", "PASSWORD_CHANGED", "USER_CREATED", "TOKEN_CREATED"} <= types
    blob = "\n".join(p for _, p in rows)
    for secret in (viewer_pw, new_pw, srv.info["admin_password"], srv.info["api_token"]):
        assert secret not in blob


def test_audit_chain_verifies_and_counts_session(api, srv):
    v = api.get("/v1/audit/verify").json()
    assert v["ok"] is True and v["first_bad_seq"] is None
    assert v["events"] > 50
    assert _netlog(srv) == []


def test_cli_verify_rehashes_originals(srv):
    """`hydra verify` on the e2e state (read-only): every CAS original re-hashes, the audit chain holds."""
    import os
    import subprocess
    import sys

    from tests.e2e.server import REPO, server_env

    env = server_env(srv.root, srv.port, None)
    env.pop("HYDRA_E2E_NETLOG", None)
    p = subprocess.run([sys.executable, "-c", "from hydra.cli import app; app()", "verify"], cwd=REPO, env=env,
                       capture_output=True, text=True, timeout=300, check=False)
    assert p.returncode == 0, p.stdout + p.stderr
    out = json.loads(p.stdout[p.stdout.index("{"):])
    assert out["cas_mismatches"] == [] and out["audit_ok"] is True and out["audit_events"] > 50
    assert os.path.realpath(env["HYDRA_STATE_DIR"]) == os.path.realpath(srv.state_dir)
