"""KAD filter-list adapter on a synthetic fixture (no real list)."""

from __future__ import annotations

from pathlib import Path

from hydra.adapters.datasets import kad_pl


def _root(tmp_path: Path) -> Path:
    sec = tmp_path / "kad-pl" / "sections"
    sec.mkdir(parents=True)
    (sec / "przekrety.txt").write_text(
        "! comment\nfiranyines.example,millenium.example##^responseheader(location)\n"
        "||fake-shop.example^$all\n||Promo-Sklep.example^$all\n||*fejs-colors.blogspot.com^$all\n")
    (sec / "przekrety-sciezki-parametry.txt").write_text(
        ".com/c/*?s1=$doc,to=com\n||about.example/finansowanie24$all\n")
    (sec / "podejrzane_inne_oszustwa.txt").write_text("||fake-shop.example^$all\n||casino77.example^$all\n")
    (sec / "przekrety_CERT.txt").write_text("||cert-only.example^$all\n")  # mirror of CERT: never read
    (sec / "natretne_lub_wprowadzajace_w_blad.txt").write_text("###humanFrame\n/^https?:\\/\\/x/$doc\n")
    return tmp_path / "kad-pl"


def test_index_sections_dedupe_and_skip(tmp_path: Path) -> None:
    rows = kad_pl.index(_root(tmp_path))
    by = {r.key: r for r in rows}
    assert set(by) == {"fake-shop.example", "promo-sklep.example", "fejs-colors.blogspot.com",
                       "about.example/finansowanie24", "casino77.example"}
    assert by["fake-shop.example"].group == "scam"  # first section in precedence wins
    assert by["casino77.example"].group == "suspicious_other_fraud"
    assert by["about.example/finansowanie24"].extra["has_path"] is True
    assert all(r.label is None and r.extra["lang"] == "pl" for r in rows)
    assert len([r for r in rows if kad_pl.where(hosts_only=True)(r)]) == 4


def test_load_url_file(tmp_path: Path) -> None:
    root = _root(tmp_path)
    incs = {i.key: i for i in kad_pl.load(root, kad_pl.index(root))}
    assert incs["fake-shop.example"].files == {"url.txt": b"http://fake-shop.example/\n"}
    assert incs["about.example/finansowanie24"].files["url.txt"] == b"http://about.example/finansowanie24\n"
    one = incs["casino77.example"]
    assert one.source == "ext:kad-pl" and one.group_label == "ext:kad-pl:suspicious_other_fraud"
    assert one.meta["url_synthesised"] is True and one.meta["lang"] == "pl"
