"""URL/text feed adapters (certpl-warnings, phishtank, phiusiil, smishing-imc25) on synthetic fixtures."""

from __future__ import annotations

import json
from pathlib import Path

from hydra.adapters.datasets import certpl_warnings, phishtank, phiusiil, smishing_imc25


def test_certpl_warnings(tmp_path: Path) -> None:
    root = tmp_path / "certpl-warnings"
    root.mkdir()
    (root / "domains-20260101T000000Z.json").write_text(json.dumps([
        {"RegisterPositionId": 1, "DomainAddress": "allegro-login.sbs", "InsertDate": "2026-01-01T10:00:00+00:00",
         "DeleteDate": None},
        {"RegisterPositionId": 2, "DomainAddress": "inpost-paczka.top", "InsertDate": "2026-01-02T10:00:00+00:00",
         "DeleteDate": None},
        {"RegisterPositionId": 3, "DomainAddress": "deleted.example", "InsertDate": "2026-01-01T10:00:00+00:00",
         "DeleteDate": "2026-01-03T00:00:00+00:00"},
    ]))
    rows = certpl_warnings.index(root)
    assert {r.key for r in rows} == {"allegro-login.sbs", "inpost-paczka.top"}
    assert all(r.group == "phish" and r.label is None and r.extra["lang"] == "pl" for r in rows)
    incs = list(certpl_warnings.load(root, rows))
    one = next(i for i in incs if i.key == "allegro-login.sbs")
    assert one.files["url.txt"] == b"http://allegro-login.sbs/\n"
    assert one.source == "ext:certpl-warnings"
    assert one.group_label == "ext:certpl-warnings:phish"
    assert one.meta["lang"] == "pl" and one.meta["url_synthesised"] is True


def test_phishtank(tmp_path: Path) -> None:
    root = tmp_path / "phishtank"
    root.mkdir()
    (root / "online-valid-20260101T000000Z.csv").write_text(
        "phish_id,url,submission_time,verified,online,target\n"
        "1,https://login.allegro.pl/x,2026-01-01T10:00:00+00:00,yes,yes,Allegro\n"
        "2,http://allegrolokalnie.cfd/y,2026-01-01T11:00:00+00:00,yes,yes,Allegro\n"
        "3,https://foo.example/z,2026-01-01T12:00:00+00:00,yes,yes,Other\n"
        "4,not-a-url,2026-01-01T13:00:00+00:00,yes,yes,Allegro\n")
    rows = phishtank.index(root)
    assert len(rows) == 3  # the non-URL row is dropped
    allegro = [r for r in rows if r.label == "allegro"]
    assert len(allegro) == 2
    other = next(r for r in rows if r.extra["url"].endswith("/z"))
    assert other.label is None  # target 'Other' -> no brand
    pl = next(r for r in rows if r.key == "1")
    assert pl.extra["lang"] == "pl"
    incs = {i.key: i for i in phishtank.load(root, rows)}
    assert incs["1"].files["url.txt"] == b"https://login.allegro.pl/x\n"
    assert incs["1"].label == "ext:phishtank:allegro"
    assert incs["1"].group_label == "ext:phishtank:phish"


def test_phiusiil(tmp_path: Path) -> None:
    root = tmp_path / "phiusiil"
    root.mkdir()
    (root / "PhiUSIIL_Phishing_URL_Dataset.csv").write_text(
        "FILENAME,URL,TLD,label\n"
        "a,https://bank.pl/login,pl,1\n"
        "b,http://phish.sbs/go,sbs,0\n"
        "c,https://legit.com/,com,1\n"
        "d,bad,com,0\n")
    rows = phiusiil.index(root)
    assert len(rows) == 3
    by = {r.key: r for r in rows}
    assert by["https://bank.pl/login"].group == "legit"
    assert by["https://bank.pl/login"].extra["lang"] == "pl"
    assert by["http://phish.sbs/go"].group == "phish"
    assert by["https://legit.com/"].extra["lang"] is None
    incs = {i.key: i for i in phiusiil.load(root, rows)}
    assert incs["http://phish.sbs/go"].files["url.txt"] == b"http://phish.sbs/go\n"
    assert incs["http://phish.sbs/go"].group_label == "ext:phiusiil:phish"
    assert incs["http://phish.sbs/go"].label is None


def test_smishing_imc25(tmp_path: Path) -> None:
    root = tmp_path / "smishing-imc25"
    root.mkdir()
    (root / "final_dataset_output.csv").write_text(
        "text,language,named_entity,scam_type\n"
        "Twoja paczka InPost czeka,Polish,InPost,delivery\n"
        "Dear SBI user verify,English,State Bank of India,banking\n"
        ",English,,others\n")
    rows = smishing_imc25.index(root)
    assert len(rows) == 2  # empty text dropped
    pl = next(r for r in rows if r.extra["lang"] == "pl")
    assert pl.label == "inpost" and pl.group == "delivery"
    assert smishing_imc25.where("pl")(pl) is True
    incs = list(smishing_imc25.load(root, rows))
    one = next(i for i in incs if i.label == "ext:smishing-imc25:inpost")
    assert one.files["ad_text.txt"] == b"Twoja paczka InPost czeka"
    assert one.group_label == "ext:smishing-imc25:delivery"
    assert one.meta["lang"] == "pl"


def test_unique_key_survives_slug_truncation() -> None:
    from hydra.adapters.datasets._common import slug, unique_key

    a, b = "x" * 60 + "/2024-01-01", "x" * 60 + "/2024-01-02"
    assert slug(a) == slug(b)  # plain keys collide after the 48-char truncation
    assert slug(unique_key(a)) != slug(unique_key(b))
    assert unique_key(a).endswith(a)
