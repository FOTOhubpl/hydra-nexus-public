"""Synthetic unit tests for normalisation and entity extraction (no organiser data)."""

from __future__ import annotations

from hydra.genome.txt_entities import (
    find_amounts,
    find_brands,
    find_emails,
    find_hosts,
    find_ibans,
    find_persons,
    find_phones,
    find_products,
    ocr_fix,
)
from hydra.genome.txt_norm import detect_obfuscation, fold, guess_language, light, tokens


def test_fold_is_length_preserving_and_aggressive():
    s = "Zażółć GĘŚLĄ jaźń — Łódź!"
    f = fold(s)
    assert len(f) == len(s)
    assert " ".join(f.split()) == "zazolc gesla jazn lodz"


def test_light_folds_homoglyphs_only_in_mixed_words_and_nfkc():
    assert light("Pоlska") == "Polska"  # Cyrillic о inside a Latin word
    assert light("Схуднення") == "Схуднення"  # pure Cyrillic word untouched
    assert light("Pobｉerz​ aplikację") == "Pobierz aplikację"


def test_obfuscation_detection():
    techs = {o.technique for o in detect_obfuscation("Zаrabiaj​ z k0ntem fr33 ｂonus")}
    assert {"homoglyph_mixed_script", "zero_width", "leetspeak", "fullwidth"} <= techs
    # OCR engines emit fullwidth letters as artefacts: not reported for OCR text
    assert "fullwidth" not in {o.technique for o in detect_obfuscation("Pobｉerz", ocr=True)}
    assert not detect_obfuscation("Kontakt: jan@example.com, tvn24, PL78T")


def test_language_guess():
    assert guess_language("To jest bardzo dobra oferta, która się nie powtórzy i nie czekaj")[0] == "pl"
    assert guess_language("This is the best offer and you can win it with your friends")[0] == "en"
    assert guess_language("krótko") == (None, 0.0)


def test_tokens_offsets_point_into_source():
    s = "Źródłowa Zyskomatówka, 1 000 zł"
    for t in tokens(s):
        assert fold(s[t.start:t.end]) == t.text


def test_ocr_fix():
    assert ocr_fix("BYć MOŽE Ô TYM") == "BYĆ MOŻE Ó TYM"
    assert ocr_fix("bonus 5000zt w 2O26") == "bonus 5000 zł w 2026"


def test_phones():
    vals = {e.value for e in find_phones("Zadzwoń: +48 511 222 333 lub 22 444 01 88, tel. 601234567")}
    assert vals == {"+48511222333", "+48224440188", "+48601234567"}
    assert not find_phones("Konto 009988776 i saldo 123456789 zł")


def test_iban_checksum():
    good = "PL61 1090 1014 0000 0712 1981 2874"
    es = find_ibans(f"Przelew na {good} teraz")
    assert es and es[0].value == good.replace(" ", "") and es[0].quality == 1.0
    bad = find_ibans("PL61 1090 1014 0000 0712 1981 2875")
    assert bad and bad[0].quality < 1.0


def test_emails_hosts_urls():
    t = "Pisz: kontakt@fake-bank.pl albo wejdź na https://zus.pl/program-inwestycyjny oraz Gazeta.pl. Fot. Policja"
    assert {e.value for e in find_emails(t)} == {"kontakt@fake-bank.pl"}
    hosts = {(e.kind, e.value) for e in find_hosts(t)}
    assert ("domain_in_text", "zus.pl") in hosts and ("domain_in_text", "gazeta.pl") in hosts
    assert ("url_in_text", "https://zus.pl/program-inwestycyjny") in hosts
    assert ("domain_in_text", "fake-bank.pl") not in hosts  # inside the email
    assert not [e for e in find_hosts("Fot. Policja / PAP") if e.value.startswith("fot")]


def test_amounts_discounts_promo():
    vals = {e.value for e in find_amounts("Wpłać 1 150 zł, saldo 12 345,67 PLN, bonus $5,000, -50% i kod: WIOSNA24")}
    assert {"1150 PLN", "12345.67 PLN", "5000 USD", "50%", "WIOSNA24"} <= vals


def test_persons_gazetteer_inflection_and_heuristic():
    t = ("Wywiad z Rafałem Trzaskowskim. Hołownia milczy, a Szymona Hołowni pytano o to. Duda to częste nazwisko. "
         "Ekspert Krzysztof Wiśniak.")
    es = find_persons(t)
    named = {e.value for e in es if e.kind == "person_named"}
    assert "Rafał Trzaskowski" in named and "Szymon Hołownia" in named
    assert "Andrzej Duda" not in named  # surname-only "Duda" is ambiguous
    assert any(e.value == "Krzysztof Wiśniak" and e.quality < 1 for e in es)
    assert {e.kind for e in find_persons("Jan Kowalski")} == {"commenter_name"}  # generic persona line


def test_brands_and_products():
    t = "Materiał TVN24 i Gazeta.pl o platformie inwestycyjnej Zyskomat Prime oraz aplikacji Sweet Bonanza"
    b = {e.value for e in find_brands(t)}
    assert {"tvn24", "gazeta_pl", "sweet_bonanza"} <= b
    assert "zyskomat prime" in {e.value for e in find_products(t)}  # invented names: context heuristic
    assert "onet" not in {e.value for e in find_brands("to jest monet onet")}
    p = {e.value for e in find_products("Zainwestuj w platformie Nova Kapitał Plus już dziś")}
    assert "nova kapital plus" in p
