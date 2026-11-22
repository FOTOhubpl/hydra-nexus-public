"""Public-feed adapter: synthetic rows only (fictional hosts under reserved .example / .test names or made-up
lure patterns). No network."""

from __future__ import annotations

import json
from pathlib import Path

from hydra.adapters import public_feeds as pf
from hydra.config import Settings
from hydra.contracts import ArtifactRole
from hydra.db import Writer, connect
from hydra.genome.base import ExtractContext
from hydra.storage import ContentStore


def test_brand_guess_and_pl_filter():
    assert pf.brand_guess("allegrolokalnie.oferta-12345.example") == "allegro"
    assert pf.brand_guess("olx-pl.delivery-4421.example") == "olx"
    assert pf.brand_guess("inpost-paczka.example") == "inpost"
    assert pf.brand_guess("ing-bank-logowanie.example") == "ing"
    assert pf.brand_guess("shopping-online.example") is None  # 'ing' only as a whole token
    assert pf.brand_guess("dhlexpress.example") is None or pf.brand_guess("dhlexpress.example") == "dhl"
    assert pf.is_pl_targeted("sklep.example.pl")
    assert pf.is_pl_targeted("doplata-paczka.example")
    assert pf.is_pl_targeted("x.example", target="Allegro")
    assert not pf.is_pl_targeted("bank-login.example", target="Other")


def test_digit_skeleton_masks_rotation():
    assert pf.digit_skeleton("allegrolokalnie.pe-7492104.example.com") == pf.digit_skeleton(
        "allegrolokalnie.pe-11.example.com"
    )
    assert pf.digit_skeleton("www.shop77.example.com") == "shop#.example.com"


def _feeds(tmp: Path) -> tuple[Path, Path, Path]:
    cert = tmp / "domains.json"
    cert.write_text(
        json.dumps(
            [
                {
                    "RegisterPositionId": 1,
                    "DomainAddress": "inpost-odbior-101.example.com",
                    "InsertDate": "2026-11-19T10:00:00+00:00",
                    "DeleteDate": None,
                },
                {
                    "RegisterPositionId": 2,
                    "DomainAddress": "inpost-odbior-202.example.com",
                    "InsertDate": "2026-11-19T11:00:00+00:00",
                    "DeleteDate": None,
                },
                {
                    "RegisterPositionId": 3,
                    "DomainAddress": "old-entry.example.com",
                    "InsertDate": "2026-06-01T00:00:00+00:00",
                    "DeleteDate": "2026-07-01T00:00:00+00:00",
                },
            ]
        )
    )
    pt = tmp / "pt.csv"
    pt.write_text(
        "phish_id,url,phish_detail_url,submission_time,verified,verification_time,online,target\n"
        "11,https://allegro.pl-oferta-555.example.com/x,d,2026-11-20T10:00:00+00:00,yes,t,yes,Allegro\n"
        "12,https://allegro.pl-oferta-555.example.com/y,d,2026-11-20T09:00:00+00:00,yes,t,yes,Allegro\n"
        "13,https://bank-login.example.com/,d,2026-11-20T09:00:00+00:00,yes,t,yes,Other\n"
        "14,https://inpost-odbior-101.example.com/pay,d,2026-11-20T09:00:00+00:00,yes,t,yes,Other\n"
    )
    ph = tmp / "ph.csv"
    ph.write_text(
        "﻿FILENAME,URL,Domain,label\n"
        "a.txt,https://sklep.example.pl/login,sklep.example.pl,0\n"
        "b.txt,https://legit.example.pl/,legit.example.pl,1\n"
    )
    return cert, pt, ph


def test_loaders_group_and_import(tmp_path: Path):
    cert, pt, ph = _feeds(tmp_path)
    c = pf.load_cert_pl(cert, since="2026-11-01")
    assert [r.host for r in c] == ["inpost-odbior-101.example.com", "inpost-odbior-202.example.com"]
    assert all(r.url_synthesised for r in c)
    p = pf.load_phishtank(pt)
    assert {r.host for r in p} == {
        "allegro.pl-oferta-555.example.com",
        "inpost-odbior-101.example.com",
    }  # 'Other' bank dropped
    u = pf.load_phiusiil(ph)
    assert [r.host for r in u] == ["sklep.example.pl"]  # label 1 (legitimate) dropped
    groups = pf.group_by_host([*c, *p, *u])
    by = {g.host: g for g in groups}
    assert len(groups) == 4
    assert by["inpost-odbior-101.example.com"].sources == ["cert_pl", "phishtank"]
    assert by["allegro.pl-oferta-555.example.com"].first_seen == "2026-11-20T09:00:00+00:00"

    s = Settings(state_dir=tmp_path / "state", enrich_cache=tmp_path / "cache", offline=True)
    s.ensure_dirs()
    store = ContentStore(s.cas_dir)
    w = Writer(s.db_path)
    try:
        incs = pf.import_groups(groups, w, store)
    finally:
        w.close()
    inc = next(i for i in incs if i.meta["feed_sources"] == ["cert_pl", "phishtank"])
    assert inc.artifacts[0].role == ArtifactRole.URL
    # the real PhishTank URL wins over the synthesised CERT one
    assert (
        store.path_for(inc.artifacts[0].artifact_id).read_text().strip() == "https://inpost-odbior-101.example.com/pay"
    )
    assert inc.meta["brand_guess"] == "inpost" and inc.observed_at == "2026-11-19T10:00:00+00:00"
    with connect(s.db_path, readonly=True) as conn:
        assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 4


def test_inf_feed_genes(tmp_path: Path):
    from hydra.genome.base import ResolvedArtifact, ResolvedIncident

    cert, pt, ph = _feeds(tmp_path)
    g = pf.group_by_host(pf.load_cert_pl(cert, since="2026-11-01"))[0]
    store = ContentStore(tmp_path / "cas")
    inc = pf.to_incident(g, store)
    res = pf.inf_feed(
        ResolvedIncident(inc, [ResolvedArtifact(a, store.path_for(a.artifact_id)) for a in inc.artifacts]),
        ExtractContext(settings=Settings(state_dir=tmp_path), work_dir=tmp_path),
    )
    kinds = {(x.kind, x.value, x.dependency_group) for x in res.genes}
    assert ("url_brand", "inpost", "inf:kit:brand") in kinds
    assert ("url_digit_skeleton", "inpost-odbior-#.example.com", "inf:operator:url_skeleton") in kinds


def test_source_tags_and_iter_incidents(tmp_path: Path):
    cert, pt, ph = _feeds(tmp_path)
    groups = pf.group_by_host([*pf.load_cert_pl(cert, since="2026-11-01"), *pf.load_phishtank(pt),
                               *pf.load_phiusiil(ph)])
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "groups.jsonl").write_text("".join(
        json.dumps({"host": g.host, "rows": [r.__dict__ for r in g.rows]}) + "\n" for g in groups))
    incs = list(pf.iter_incidents(corpus))
    assert {i.source for i in incs} == {"public:certpl", "public:phishtank", "public:phiusiil"}
    merged = next(i for i in incs if i.meta["feed_sources"] == ["cert_pl", "phishtank"])
    assert merged.source == "public:certpl" and merged.meta["corpus"] == "pl-feeds"
    assert len(list(pf.iter_incidents(corpus, limit=2))) == 2
