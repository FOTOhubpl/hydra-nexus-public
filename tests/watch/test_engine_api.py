"""Engine + API on a synthetic recording (fake names, no network, no NASK data)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi.testclient import TestClient

from hydra.api import create_app
from hydra.watch.engine import WatchEngine, recordings_dir
from hydra.watch.events import normalise


def _rec(i: int, host: str, ts: str, action: str = "block") -> str:
    raw = {"RegisterPositionId": i, "DomainAddress": host, "ActionTime": ts, "ActionType": action}
    return json.dumps({"rec_ts": "2026-11-21T15:00:00Z", "source": "certpl", "raw": raw})


def _write(settings, monkeypatch) -> Path:
    monkeypatch.delenv("HYDRA_WATCH_REC_DIR", raising=False)
    d = recordings_dir(settings)
    d.mkdir(parents=True, exist_ok=True)
    hist = [_rec(i, f"oferta-{1000 + i}.sbs", f"2026-11-20T1{i}:00:00+00:00") for i in range(4)]
    (d / "certpl-backfill-2026-11-21.jsonl").write_text("\n".join(hist) + "\n")
    live = [
        _rec(10, "allegro.zakup-oferta987654.sbs", "2026-11-21T14:00:00+00:00"),
        _rec(11, "olx.zakup-oferta987654.sbs", "2026-11-21T14:00:05+00:00"),
        _rec(12, "vinted.zakup-oferta987654.sbs", "2026-11-21T14:00:09+00:00"),
        _rec(13, "oferta-7777.sbs", "2026-11-21T14:01:00+00:00"),
        _rec(14, "spokojny-ogrod.com", "2026-11-21T14:02:00+00:00"),
        _rec(15, "oferta-7777.sbs", "2026-11-21T14:03:00+00:00", action="unblock"),
    ]
    (d / "certpl-2026-11-21.jsonl").write_text("\n".join(live) + "\n")
    return d


def test_normalise_certpl_and_ct():
    ev = normalise(json.loads(_rec(1, "WWW.Example-Shop.com.", "2026-11-21T14:00:00+00:00")))[0]
    assert ev.host == "www.example-shop.com" and ev.source == "watch:certpl" and ev.first_seen.startswith("2026")
    ct = {"rec_ts": "2026-11-21T15:00:00Z", "source": "ct",
          "raw": {"data": {"seen": 1763737200.0, "leaf_cert": {"all_domains": ["*.a-shop.com", "a-shop.com"],
                                                                "issuer": {"O": "Some CA"}}}}}
    evs = normalise(ct)
    assert [e.host for e in evs] == ["a-shop.com"] and evs[0].source == "watch:ct"


def test_replay_block_scores_links_and_flags(settings, monkeypatch):
    _write(settings, monkeypatch)
    eng = WatchEngine(settings, enrich=False)
    st = eng.start_replay("certpl-2026-11-21.jsonl", speed=36000, block=True)
    assert st["mode"] == "replay" and st["replay"]["done"] and len(st["replay"]["sha256"]) == 64
    assert st["seeded_history"] == 4  # backfill older than the first replayed event
    rows = eng.events_since(0)
    assert len(rows) == 5 and all(r["replay"] for r in rows)  # unblock skipped
    by = {r["host"]: r for r in rows}
    a = by["allegro.zakup-oferta987654.sbs"]
    assert a["score"]["bits"] >= 6 and a["host_defanged"] == "allegro[.]zakup-oferta987654[.]sbs"
    # three listed hosts under one registrable domain = live campaign; shown retroactively on the first one
    assert a["live_campaign"] and a["live_campaign"] == by["vinted.zakup-oferta987654.sbs"]["live_campaign"]
    fam = by["oferta-7777.sbs"]
    assert any(r["code"] == "family" for r in fam["score"]["reasons"])  # 4 seeded oferta-# domains
    assert fam["live_campaign"]
    assert by["spokojny-ogrod.com"]["live_campaign"] is None
    # assignment ran (no analysed corpus in the temp state → unavailable, never an error)
    assert all(r["assignment"]["decision"] == "unavailable" for r in rows)
    assert st["sparkline"] and st["events"] == 5


def test_api_endpoints(settings, monkeypatch):
    _write(settings, monkeypatch)
    with TestClient(create_app(settings)) as c:
        recs = c.get("/v1/watch/recordings").json()["recordings"]
        assert {r["file"] for r in recs} == {"certpl-2026-11-21.jsonl", "certpl-backfill-2026-11-21.jsonl"}
        assert c.post("/v1/watch/replay", json={"file": "../etc/passwd.jsonl"}).status_code == 404
        r = c.post("/v1/watch/replay", json={"file": "certpl-2026-11-21.jsonl", "speed": 36000})
        assert r.status_code == 200 and r.json()["mode"] == "replay"
        for _ in range(100):
            st = c.get("/v1/watch/status").json()
            if st["replay"]["done"] and st["pending"] == 0:
                break
            time.sleep(0.05)
        ev = c.get("/v1/watch/events", params={"since": 0}).json()
        assert ev["replay"] is True and len(ev["events"]) == 5
        assert c.get("/v1/watch/events", params={"since": ev["seq"]}).json()["events"] == []
        # analyst click: on a machine without HYDRA_ENRICH the page is never fetched
        f = c.post("/v1/watch/fetch", json={"event_id": ev["events"][0]["event_id"]}).json()
        assert f["status"] == "disabled" and "[.]" in f["url_defanged"]
        assert c.post("/v1/watch/fetch", json={"event_id": "w-unknown"}).status_code == 404
        assert c.post("/v1/watch/stop").json()["mode"] == "idle"


def test_fingerprint_build_and_match():
    from hydra.contracts import Campaign, Family, Gene
    from hydra.synapse import build_genome
    from hydra.watch import fingerprints as F
    from hydra.watch.link import CampaignIndex

    def g(iid, kind, value, grp=None):
        return Gene(incident_id=iid, family=Family.INF, kind=kind, value=value, extractor="t@1",
                    dependency_group=grp or f"inf:operator:{kind}")

    genes = {
        "i1": [g("i1", "landing_host", "allegro.zakup-oferta111111.sbs"), g("i1", "registrable_domain",
               "zakup-oferta111111.sbs"), g("i1", "ns_set", "ns1.zly-dns.com,ns2.zly-dns.com"),
               g("i1", "registrar_day", "Some Registrar|2026-11-18")],
        "i2": [g("i2", "registrable_domain", "zakup-oferta222222.sbs")],
    }
    idx = CampaignIndex(run_id="r", corpus="nask")
    idx.campaigns = [Campaign(id="C-1", label="Testowa", members=["i1", "i2"])]
    idx.genomes = {k: build_genome(k, v, {}) for k, v in genes.items()}
    fs = F.build(idx)
    kinds = {c["kind"] for c in fs.fingerprints["C-1"].clauses}
    assert {"domain", "family", "ns_set", "registrar_day", "registrar_week", "brand_placement"} <= kinds
    assert fs.sha256 and fs.summary()["fingerprints"] == 1
    # a new burst of the same template: campaign-level candidate
    a = F.match(fs, "olx.zakup-oferta333333.sbs")
    assert a and a[0]["campaign_id"] == "C-1" and a[0]["level"] == "campaign" and a[0]["status"] == "kandydat"
    assert a[0]["matched"][0]["kind"] == "family"
    # infra after enrichment raises confidence
    enr = {"ns": ["ns1.zly-dns.com", "ns2.zly-dns.com"], "rdap": {"registrar": "Some Registrar",
                                                                   "registration": "2026-11-18T10:00:00Z"}}
    b = F.match(fs, "zupelnie-inna.com", enr)
    assert b[0]["campaign_bits"] >= 10 and b[0]["confidence"] == "wysoka"
    assert F.match(fs, "spokojny-ogrod.com") == []
    # held-out member is excluded from its own fingerprint
    fs2 = F.build(idx, exclude={"i1"})
    assert all(c["value"] != "allegro.zakup-oferta111111.sbs" for c in fs2.fingerprints["C-1"].clauses)
