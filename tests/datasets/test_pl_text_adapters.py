"""POLfake, MIPD and Polish Political Advertising adapters on synthetic fixtures."""

from __future__ import annotations

import csv
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from hydra.adapters.datasets import mipd, polfake, political_ads_pl


def _csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_polfake(tmp_path: Path) -> None:
    root = tmp_path / "polfake"
    base = root / "repo"
    _csv(base / polfake.CLAIMS, [
        {"id": "1", "content": "Podatki wzrosły o 50%", "source": "demagog_org", "label": "Fałsz", "tags": "podatki",
         "publication_date": "2025-01-02", "author": "X", "link": "", "label_enc": "1"},
        {"id": "2", "content": "Szczepionki działają", "source": "fake_news_pl", "label": "Prawda", "tags": "",
         "publication_date": "2025-01-03", "author": "", "link": "", "label_enc": "0"}])
    _csv(base / polfake.TWEETS, [
        {"id": "10", "claim_id": "1", "content": "Znowu podatki w górę o połowę!", "shares": "1", "likes": "2",
         "replies": "0", "link": "", "publication_date": "2025-01-05", "label": "Fałsz", "label_enc": "1"},
        {"id": "11", "claim_id": "", "content": "", "shares": "", "likes": "", "replies": "", "link": "",
         "publication_date": "", "label": "Prawda", "label_enc": "0"}])
    rows = polfake.index(root)
    assert {r.key for r in rows} == {"claim-1", "claim-2", "tweet-10"}  # empty tweet dropped
    by = {r.key: r for r in rows}
    assert by["tweet-10"].label == by["claim-1"].label == "claim:1"
    assert by["claim-1"].group == "fałsz"
    incs = {i.key: i for i in polfake.load(root, rows)}
    t = incs["tweet-10"]
    assert t.label == "ext:polfake:claim:1" and t.group_label == "ext:polfake:fałsz"
    assert t.files["ad_text.txt"].decode() == "Znowu podatki w górę o połowę!"
    assert t.meta["lang"] == "pl" and t.meta["kind"] == "tweet"
    assert polfake.where("claim")(by["claim-2"]) and not polfake.where("claim")(by["tweet-10"])
    c = incs["claim-1"]
    assert c.source == "ext:polfake" and c.meta["origin"] == "demagog_org" and "source" not in c.meta


def test_meta_of_keeps_corpus_flag() -> None:
    from hydra.adapters.datasets import meta_of
    from hydra.adapters.datasets._common import ExtIncident

    x = ExtIncident(dataset="polfake", key="k", files={}, label="ext:polfake:claim:1",
                    meta={"source": "demagog_org", "label": "fake", "id": "x", "origin": "demagog_org"})
    m = meta_of(x)
    assert m["source"] == "ext:polfake" and m["label"] == "ext:polfake:claim:1" and m["id"] == "ext-polfake-k"
    assert m["origin"] == "demagog_org"


def test_mipd(tmp_path: Path) -> None:
    root = tmp_path / "mipd"
    d = root / "data"
    art_a, art_b = "Artykuł A o spisku.", "Artykuł B rzetelny."
    _csv(d / "raw_articles.csv", [{"article_id": "1", "full_article": art_a},
                                  {"article_id": "2", "full_article": art_b}])
    _csv(d / "articles_categories.csv", [{"article_id": "1", "category": "zdrowie"},
                                         {"article_id": "2", "category": "polityka"}])
    _csv(d / "disinformation" / "train.csv", [{"article": art_a, "disinformation": "1"}])
    _csv(d / "disinformation" / "test.csv", [{"article": art_b, "disinformation": "0"}])
    _csv(d / "manipulation" / "train.csv", [{"article": art_a, "STRAWMAN": "1", "WHATABOUTISM": "0"}])
    _csv(d / "intention" / "train.csv", [{"article": art_a, "CAUSE_PANIC": "1"}])
    rows = mipd.index(root)
    assert sorted(r.group for r in rows) == ["credible", "disinformation"]
    incs = {i.group_label: i for i in mipd.load(root, rows)}
    dis = incs["ext:mipd:disinformation"]
    assert dis.label is None and dis.files["ad_text.txt"].decode() == art_a
    assert dis.meta["techniques"] == ["strawman"] and dis.meta["intentions"] == ["cause_panic"]
    assert dis.meta["category"] == "zdrowie" and dis.meta["split"] == "train" and dis.meta["lang"] == "pl"
    assert incs["ext:mipd:credible"].meta["split"] == "test"
    assert [r.group for r in rows if mipd.where(disinformation_only=True)(r)] == ["disinformation"]


def test_political_ads_pl(tmp_path: Path) -> None:
    root = tmp_path / "political-ads-pl"
    root.mkdir()
    tab = pa.Table.from_pylist([
        {"tokens": str(["Więcej", "lekarzy", "w", "szpitalach", "!"]), "tags": str(["O", "B-HEALHCARE", "O",
                                                                                  "B-HEALHCARE", "O"])},
        {"tokens": str(["Dzień", "dobry", "."]), "tags": str(["O", "O", "O"])}])
    pq.write_table(tab, root / "train.parquet")
    rows = political_ads_pl.index(root)
    assert [r.group for r in rows] == ["healhcare", "none"]
    incs = list(political_ads_pl.load(root, rows))
    assert incs[0].files["ad_text.txt"].decode() == "Więcej lekarzy w szpitalach!"
    assert incs[1].files["ad_text.txt"].decode() == "Dzień dobry."
    assert incs[0].group_label == "ext:political-ads-pl:healhcare" and incs[0].label is None
    assert incs[0].meta["categories"] == ["healhcare"] and incs[0].meta["split"] == "train"
