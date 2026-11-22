"""Benefit tests (G3-FEATURES, 2026-11-22). SYNTHETIC incidents only; see tests/benefit/helpers.py.

Each test states a decision the product should get right because of a feature and, where possible, compares it
with the same material without that feature. They run the real adapter, extractors and scorer. `world` has an
empty models directory (DINOv2, e5, YuNet/SFace unavailable); `world_m` uses the installed offline weights and
skips when they are absent. OCR reads no text in both (generated creatives carry none; see helpers).
Measured outcomes that contradict the intended claim are kept as strict xfails with the measured reason, not
weakened into passing assertions (listed in the internal feature-quality notes, not published).
"""

from __future__ import annotations

import pytest

from hydra import vis_geom
from tests.benefit.helpers import BACKGROUND_TEXTS, World, add_background, creative, crop, kinds, members_of

# Rare, campaign-specific copy (generated phrases, not quotes from any material).
TEXT_A = ("Syntetyczny bursztynowy kompas orbitalny podwaja oszczędności w trzy tygodnie.\n"
          "Lazurowa mapa hipotetycznej pracowni czeka na pierwszych inwestorów.\n"
          "Ostatnie miejsca w programie Kwarcowy Zegar Delta, zapisy tylko dziś.\n")
TEXT_B = ("Miedziany ogród paraboliczny leczy stawy bez tabletek i bez recepty.\n"
          "Profesor z fikcyjnego instytutu poleca Szmaragdową Kurację Omega.\n"
          "Zamów teraz, a druga paczka trafi do Ciebie gratis jeszcze w tym tygodniu.\n")
IBAN_A = "PL61 1090 1014 0000 0712 1981 2874"
IBAN_B = "PL27 1140 2004 0000 3002 0135 5387"


@pytest.fixture
def world(settings, tmp_path, monkeypatch):
    return World(settings, tmp_path / "inc", monkeypatch)


@pytest.fixture
def world_m(settings, tmp_path, monkeypatch):
    """Same, with the installed DINOv2 / e5 weights (skips when they are not installed)."""
    return World(settings, tmp_path / "inc", monkeypatch, real_models=True)


def _two_campaigns(world: World) -> None:
    """Campaign A and B, two members each, with an image, copy, an account and a landing domain each."""
    for k in (1, 2):
        world.add(f"A{k}", {"reklama.png": creative(1), "opis.txt": TEXT_A + f"Przelew: {IBAN_A}\n"
                            f"https://kompas-zysk{k}.example/start\n"})
        world.add(f"B{k}", {"reklama.png": creative(2), "opis.txt": TEXT_B + f"Konto: {IBAN_B}\n"
                            f"https://kuracja-omega{k}.example/zamow\n"})
    add_background(world)


def _brief(pe) -> list[tuple[str, str, float]]:
    return [(str(m.family), m.kind, round(m.bits, 1)) for m in pe.score.reasons]


# ------------------------------------------------------------------------------------------ (a)


def test_a_image_and_text_without_har_or_landing_link_to_the_right_campaign(world_m):
    """New material = the campaign creative cropped and rescaled + the ad copy. No HAR, no landing page, no URL,
    no account. It must join campaign A (not B) on image + text alone."""
    world = world_m
    _two_campaigns(world)
    world.add("N", {"reklama.png": crop(creative(1)), "reklama.txt": TEXT_A})
    to_a, to_b = world.pair("N", "A1"), world.pair("N", "B1")
    assert to_a.score.hypothesis == "same_campaign", _brief(to_a)
    assert to_b.score.hypothesis != "same_campaign", _brief(to_b)
    fams = {str(m.family) for m in to_a.score.reasons if m.bits > 0}
    assert {"VIS", "TXT"} <= fams, fams          # both modalities contribute
    assert "INF" not in fams                     # nothing infrastructural was available
    _pairs, cl = world.clustering()
    assert members_of(cl, "N") == {"N", "A1", "A2"}


def test_a_image_and_text_each_alone_measured(world_m):
    """Measured split (2026-11-22): the verified creative alone is standalone campaign evidence
    (visual_near_duplicate, 12 bit) and links; the copy alone is capped at the TXT family limit (8 bit < 10) and
    goes to review (`abstain`); image + text links with more support. So on this material VIS decides and TXT
    adds support; the claim is not that either modality alone is useless."""
    world = world_m
    _two_campaigns(world)
    shot = crop(creative(1))
    world.add("N_both", {"reklama.png": shot, "reklama.txt": TEXT_A})
    world.add("N_img", {"reklama.png": shot})
    world.add("N_txt", {"reklama.txt": TEXT_A})
    both, img, txt = (world.pair(n, "A1") for n in ("N_both", "N_img", "N_txt"))
    print({"both": (both.score.hypothesis, both.campaign_bits), "image_only": (img.score.hypothesis,
           img.campaign_bits), "text_only": (txt.score.hypothesis, txt.campaign_bits)})
    assert both.score.hypothesis == "same_campaign", _brief(both)
    assert img.score.hypothesis == "same_campaign", _brief(img)
    assert txt.score.hypothesis == "abstain", _brief(txt)
    assert both.campaign_bits > max(img.campaign_bits, txt.campaign_bits)


# ------------------------------------------------------------------------------------------ (b)


CROPS = [(0.12, 0.0, 0.0, 0.12), (0.15, 0.05, 0.05, 0.1), (0.18, 0.0, 0.0, 0.18), (0.2, 0.1, 0.0, 0.1),
         (0.1, 0.2, 0.1, 0.0), (0.22, 0.05, 0.0, 0.15), (0.0, 0.0, 0.3, 0.1)]  # (left, top, right, bottom)


def test_b_crop_geometry_links_cropped_rescaled_creatives_the_hashes_miss(settings, tmp_path, monkeypatch):
    """7 crops (keeping 52-81 % of the area) rescaled to 80 %, each with the campaign copy, one pair per corpus.
    Geometry on (HYDRA_ADV_CROP default): all 7 link through a verified visual near-duplicate. Geometry off:
    only the crops the pHash sub-windows catch link; the rest fall back to the capped text (8 bit, abstain).
    Measured 2026-11-22 on this fixture: on 7/7, off 3/7."""
    on_linked, off_linked = [], []
    for n, fx in enumerate(CROPS):
        world = World(settings, tmp_path / f"w{n}", monkeypatch, real_models=True)
        world.add("A1", {"reklama.png": creative(5), "opis.txt": TEXT_A})
        world.add("N", {"reklama.png": crop(creative(5), fx=fx), "opis.txt": TEXT_A})
        add_background(world)
        monkeypatch.delenv("HYDRA_ADV_CROP", raising=False)
        vis_geom._MEMO.clear()
        on = world.pair("N", "A1")
        monkeypatch.setenv("HYDRA_ADV_CROP", "0")
        vis_geom._MEMO.clear()
        off = world.pair("N", "A1")
        monkeypatch.delenv("HYDRA_ADV_CROP")
        on_linked.append(on.score.hypothesis == "same_campaign" and "visual_near_duplicate" in kinds(on))
        off_linked.append(off.score.hypothesis == "same_campaign")
        if not off_linked[-1]:
            assert "visual_near_duplicate" not in kinds(off) and off.score.hypothesis == "abstain", _brief(off)
    print({"geometry_on_linked": sum(on_linked), "geometry_off_linked": sum(off_linked), "n": len(CROPS)})
    assert all(on_linked), on_linked
    assert sum(off_linked) < sum(on_linked), off_linked


def test_b_exact_stock_image_in_many_unrelated_incidents_does_not_link(world_m):
    """The same stock photo (byte-identical) in 6 incidents with unrelated copy: it is common (near-df above
    STOCK_IMAGE_MAX_DF) and must not make one campaign."""
    world = world_m
    stock = creative(77)
    texts = [TEXT_A, TEXT_B, *BACKGROUND_TEXTS]
    for k in range(6):
        world.add(f"S{k}", {"stock.png": stock, "opis.txt": texts[k]})
    add_background(world, n=4)
    pairs, cl = world.clustering()
    stock_pairs = {(a, b): p for (a, b), p in pairs.items() if a.startswith("S") and b.startswith("S")}
    assert all(p.score.hypothesis != "same_campaign" for p in stock_pairs.values()), \
        {k: _brief(p) for k, p in stock_pairs.items() if p.score.hypothesis == "same_campaign"}
    assert all(len(members_of(cl, f"S{k}") & {f"S{j}" for j in range(6)}) == 1 for k in range(6))


@pytest.mark.xfail(strict=True, reason=(
    "CR-G3-1 MEASURED LIMITATION (G3-FEATURES 2026-11-22): stock detection counts near-identical pHash/PDQ holders "
    "(Rarity.near_df, STOCK_IMAGE_MAX_DF=2). When each incident carries a differently cropped/rescaled copy of "
    "the same stock photo, the hashes differ, the photo is not recognised as common, and the crop geometry "
    "confirms every pair: visual_near_duplicate (12 bit) links unrelated incidents. Scoring code unchanged."))
def test_b_cropped_variants_of_one_stock_image_do_not_link(world_m):
    world = world_m
    stock = creative(77)
    texts = [TEXT_A, TEXT_B, *BACKGROUND_TEXTS]
    for k in range(6):
        img = crop(stock, fx=(0.02 * k, 0.01 * k, 0.03, 0.02), scale=0.7 + 0.05 * k)
        world.add(f"S{k}", {"stock.png": img, "opis.txt": texts[k]})
    add_background(world, n=4)
    pairs, _cl = world.clustering()
    linked = [k for k, p in pairs.items() if k[0].startswith("S") and k[1].startswith("S")
              and p.score.hypothesis == "same_campaign"]
    assert not linked, linked


# ------------------------------------------------------------------------------------------ (c)


def _landing(host: str, copy: str, iban: str, phone: str | None = None) -> str:
    contact = f"<p>Infolinia: {phone}</p>" if phone else ""
    return (f'<!doctype html><html lang="pl"><head><meta charset="utf-8"><title>{copy[:30]}</title>'
            f'<link rel="canonical" href="https://{host}/start"></head><body><h1>{copy}</h1>'
            f"<p>Wpłaty wyłącznie na rachunek: {iban}</p>{contact}</body></html>")


PHONE_A, PHONE_B = "+48 512 908 364", "+48 698 120 447"


def test_c_changed_domain_same_rare_operator_contact_links(world):
    """Saved landing pages on two different domains with rewritten copy, sharing the operator's phone and
    account: they link, and not through the domain. The same page template with another phone and account
    does not link."""
    add_background(world)
    world.add("A1", {"strona.html": _landing("zysk-kompas.example", "Inwestuj z nami bez ryzyka", IBAN_A, PHONE_A)})
    world.add("A2", {"strona.html": _landing("inna-domena-x.example", "Nowa platforma i inne zasady", IBAN_A,
                                             PHONE_A)})
    world.add("C1", {"strona.html": _landing("inna-domena-y.example", "Nowa platforma i inne zasady", IBAN_B,
                                             PHONE_B)})
    same, other = world.pair("A1", "A2"), world.pair("A2", "C1")
    print({"same_operator": (same.score.hypothesis, same.campaign_bits, _brief(same)),
           "other_operator": (other.score.hypothesis, other.campaign_bits, _brief(other))})
    assert same.score.hypothesis == "same_campaign", _brief(same)
    assert any(m.kind == "entity_phone" and m.bits > 0 for m in same.score.reasons)
    assert not any(m.kind in ("domain_in_text", "url_in_text", "landing_host", "canonical_host") and m.bits > 0
                   for m in same.score.reasons)  # the link is not carried by the (changed) domain
    assert other.score.hypothesis != "same_campaign", _brief(other)


def test_c_shared_account_alone_does_not_auto_link(world):
    """Measured boundary (2026-11-22): one shared IBAN and nothing else rare counts in one family (TXT, capped
    at 8 bit < 10): the pair is not linked automatically (`abstain` for plain notes, `shared_template` when the
    page template is also shared). A single account is a review lead here, not a link."""
    add_background(world)
    world.add("A1", {"opis.txt": "Inwestuj z nami. Wpłaty: " + IBAN_A + "\nhttps://zysk-kompas.example/start\n"})
    world.add("A2", {"opis.txt": "Nowa platforma, nowe zasady. Konto do wpłat: " + IBAN_A +
                     "\nhttps://inna-domena-x.example/rejestracja\n"})
    world.add("L1", {"strona.html": _landing("zysk-kompas.example", "Inwestuj z nami bez ryzyka", IBAN_B)})
    world.add("L2", {"strona.html": _landing("inna-domena-x.example", "Nowa platforma i inne zasady", IBAN_B)})
    note, page = world.pair("A1", "A2"), world.pair("L1", "L2")
    print({"note": (note.score.hypothesis, note.campaign_bits), "page": (page.score.hypothesis, page.campaign_bits)})
    for pe in (note, page):
        assert any(m.kind == "entity_iban" and m.bits > 0 for m in pe.score.reasons), _brief(pe)
    assert note.score.hypothesis == "abstain", _brief(note)
    assert page.score.hypothesis in ("abstain", "shared_template"), _brief(page)


# ------------------------------------------------------------------------------------------ (d)


def test_d_same_logo_and_person_across_operators_is_not_one_campaign(world_m):
    """Two operators reuse the same impersonated public person and the same brand logo image, each with its own
    account, domain and copy. They must not become one campaign."""
    world = world_m
    add_background(world)
    logo = creative(33, size=(160, 160))
    person = "Robert Lewandowski poleca tę metodę."
    world.add("P1", {"logo.png": logo, "opis.txt": person + "\n" + TEXT_A + f"Konto: {IBAN_A}\n"
                     "https://kompas-zysk.example/\n"})
    world.add("P2", {"logo.png": logo, "opis.txt": person + "\n" + TEXT_B + f"Konto: {IBAN_B}\n"
                     "https://kuracja-omega.example/\n"})
    pe = world.pair("P1", "P2")
    print({"hypothesis": pe.score.hypothesis, "campaign_bits": pe.campaign_bits, "reasons": _brief(pe)})
    assert any(m.kind == "person_named" for m in pe.score.reasons) or \
        any(g.kind == "person_named" for g in world.genes["P1"])  # the person was read
    assert pe.score.hypothesis != "same_campaign", _brief(pe)
    assert pe.operator_bits == 0, pe.operator_bits


# ------------------------------------------------------------------------------------------ (e)


BACKGROUND_TEXT_LONG = {
    1: "Sklep ogrodniczy zaprasza na kiermasz sadzonek w sobotę od godziny dziewiątej.",
    2: "Biblioteka miejska wydłuża godziny otwarcia czytelni w okresie sesji egzaminacyjnej.",
}


def test_e_missing_models_and_corrupt_files_are_unavailable_with_a_reason(world):
    """Empty models dir (DINOv2, e5, face models missing) + a corrupt JPEG: every affected gene is
    `unavailable`/`failed` with a non-empty reason and no value, and two such incidents get no evidence."""
    for k in (1, 2):
        world.add(f"E{k}", {"zepsuty.jpg": b"\xff\xd8\xff\xe0" + bytes([k]) * 64 + b" not a jpeg",
                            "reklama.png": creative(40 + k), "opis.txt": BACKGROUND_TEXT_LONG[k]})
    for iid in ("E1", "E2"):
        bad = [g for g in world.genes[iid] if str(g.status) in ("unavailable", "failed")]
        by_kind = {g.kind: g for g in bad}
        assert "dinov2" in by_kind and str(by_kind["dinov2"].status) == "unavailable"
        assert "ModelUnavailable" in (by_kind["dinov2"].evidence.note or "")
        assert any(g.kind.startswith("emb_") and str(g.status) == "unavailable" for g in bad)
        assert any(g.kind == "pdq" and str(g.status) == "failed" for g in bad)  # the corrupt file
        for g in bad:
            assert g.value is None and g.vector_ref is None, g
            assert (g.evidence.note or "").strip(), g  # a reason is always recorded
    pe = world.pair("E1", "E2")
    unavailable_kinds = {g.kind for iid in ("E1", "E2") for g in world.genes[iid]
                         if str(g.status) in ("unavailable", "failed")}
    assert not [m for m in pe.score.reasons if m.bits > 0 and m.kind in unavailable_kinds]
    assert pe.score.hypothesis != "same_campaign"


def test_e_identical_material_with_models_missing_still_links_on_what_was_observed(world):
    """Missing models are unknown, not a penalty: the same copy + creative still links with DINOv2/e5 absent."""
    add_background(world)
    for k in (1, 2):
        world.add(f"M{k}", {"reklama.png": creative(1), "opis.txt": TEXT_A + f"Konto: {IBAN_A}\n"})
    pe = world.pair("M1", "M2")
    assert pe.score.hypothesis == "same_campaign", _brief(pe)
    assert not any(m.kind in ("dinov2", "emb_text") and m.bits > 0 for m in pe.score.reasons)
