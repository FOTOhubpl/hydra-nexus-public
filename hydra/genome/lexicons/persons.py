"""Public-figure gazetteer for person-name genes (TXT family, kind `person_named`).

Names are matched in folded text (lowercase, no diacritics). Polish inflects surnames ("Kowalski",
"Kowalskiego", "Kowalskim"), so each entry carries a surname stem plus allowed endings length.

The repository ships `origin="prior"` entries (public figures often impersonated in Polish scam ads per public
CERT Polska / NASK warnings, not individually sourced) and `origin="documented"` entries (`pl_public_figures.json`:
persons a primary public source documents as abused in scam ads; source URLs in `PERSON_SOURCES`). Persons named
in the organiser sample (`origin="material"`) are not distributed with the repository; they are prepended at
import time from the local overlay `$HYDRA_DATA_DIR/nask/lexicons/persons.json` (see `lexicons/local.py`).
The material is never evidence that an impersonated person took part. Persons identified only visually are
deliberately not listed: names come from text only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Person:
    canonical: str
    surname_stems: tuple[str, ...]  # folded stems; a token matches if it starts with a stem and adds <= 3 chars
    given_stems: tuple[str, ...] = ()
    role: str = "public_figure"
    origin: str = "prior"


def P(canonical, surnames, given=(), role="public_figure", origin="prior") -> Person:  # noqa: N802
    return Person(canonical, tuple(surnames), tuple(given), role, origin)


_PRIOR_PERSONS: tuple[Person, ...] = (
    # --- frequently impersonated (prior knowledge, public warnings)
    P("Robert Lewandowski", ["lewandowsk"], ["robert"], "athlete"),
    P("Anna Lewandowska", ["lewandowsk"], ["ann"], "celebrity"),
    P("Andrzej Duda", ["dud"], ["andrz"], "politician"),
    P("Karol Nawrocki", ["nawrock"], ["karol"], "politician"),
    P("Rafał Trzaskowski", ["trzaskowsk"], ["rafa"], "politician"),
    P("Szymon Hołownia", ["holowni"], ["szymon"], "politician"),
    P("Jarosław Kaczyński", ["kaczynsk"], ["jarosla"], "politician"),
    P("Sławomir Mentzen", ["mentzen"], ["slawomir"], "politician"),
    P("Jerzy Owsiak", ["owsiak"], ["jerz"], "public_figure"),
    P("Kuba Wojewódzki", ["wojewodzk"], ["kub", "jakub"], "celebrity"),
    P("Magda Gessler", ["gessler"], ["magd"], "celebrity"),
    P("Zenon Martyniuk", ["martyniuk"], ["zen"], "celebrity"),
    P("Krzysztof Ibisz", ["ibisz"], ["krzysztof"], "celebrity"),
    P("Tomasz Kammel", ["kammel"], ["tomasz"], "celebrity"),
    P("Dorota Wellman", ["wellman"], ["dorot"], "journalist"),
    P("Marcin Prokop", ["prokop"], ["marcin"], "journalist"),
    P("Piotr Kraśko", ["krask"], ["piotr"], "journalist"),
    P("Michał Sołowow", ["solowow"], ["micha"], "businessman"),
    P("Dominika Kulczyk", ["kulczyk"], ["dominik"], "businessman"),
    P("Zbigniew Jakubas", ["jakubas"], ["zbigniew"], "businessman"),
    P("Leszek Czarnecki", ["czarneck"], ["leszek", "leszk"], "businessman"),
    P("Elon Musk", ["musk"], ["elon"], "businessman"),
    P("Iga Świątek", ["swiatek", "swiatk"], ["ig"], "athlete"),
    P("Marcin Najman", ["najman"], ["marcin"], "celebrity"),
    # --- added 2026-11-21 for the likeness triage card (names only, prior knowledge of public Polish scam-ad
    #     warnings and press reports; [verify] before citing a specific case)
    P("Robert Makłowicz", ["maklowicz"], ["robert"], "celebrity"),
    P("Krzysztof Stanowski", ["stanowsk"], ["krzysztof"], "journalist"),
    P("Ewa Chodakowska", ["chodakowsk"], ["ew"], "celebrity"),
    P("Małgorzata Rozenek-Majdan", ["rozenek"], ["malgorzat"], "celebrity"),
    P("Adam Małysz", ["malysz"], ["adam"], "athlete"),
    P("Wojciech Szczęsny", ["szczesn"], ["wojciech"], "athlete"),
)


def _documented() -> tuple[tuple[Person, ...], dict[str, list[dict]]]:
    """Persons documented in a primary public source as abused in scam ads (`pl_public_figures.json`, origin
    "documented"). A name already in the prior list keeps its entry and only gains the source list."""
    from hydra.genome.lexicons import public

    prior = {p.canonical for p in _PRIOR_PERSONS}
    new, src = [], {}
    for e in public.figures():
        src[e["canonical"]] = list(e.get("sources") or [])
        if e["canonical"] not in prior:
            new.append(P(e["canonical"], e["surnames"], e.get("given", ()), e.get("role", "public_figure"),
                         "documented"))
    return tuple(new), src


_DOCUMENTED, PERSON_SOURCES = _documented()


def _local_persons() -> tuple[Person, ...]:
    from hydra.genome.lexicons import local

    return tuple(P(e["canonical"], e["surnames"], e.get("given", ()), e.get("role", "public_figure"),
                   e.get("origin", "material")) for e in (local.load("persons") or []))


PERSONS: tuple[Person, ...] = _local_persons() + _PRIOR_PERSONS + _DOCUMENTED

# Common Polish given names (folded stems) for the capitalised-name heuristic (quality < 1).
FIRST_NAMES: frozenset[str] = frozenset("""
adam adrian agata agnieszka aleksander aleksandra alicja andrzej aneta anita anna antoni arkadiusz artur
barbara bartosz beata bogdan bozena cezary czeslaw damian daniel danuta dariusz dawid dominik dominika
dorota edyta elzbieta emilia ewa ewelina filip franciszek grazyna grzegorz halina hanna henryk ireneusz
irena iwona izabela jacek jadwiga jakub jan janina janusz jaroslaw jerzy joanna jolanta jozef julia julian
justyna kacper kamil kamila karol karolina katarzyna kazimierz kinga klaudia konrad krystyna krzysztof
kuba leszek lucyna lukasz maciej magdalena malgorzata marcin marek maria marian mariusz marta marzena
mateusz michal mieczyslaw mikolaj milena miroslaw monika natalia norbert oliwia patrycja patryk pawel
piotr przemyslaw rafal robert roman ryszard sebastian slawomir stanislaw stefan sylwia szymon tadeusz
teresa tomasz urszula wanda weronika wieslaw wiktor wiktoria witold wladyslaw wojciech zbigniew zdzislaw
zofia zuzanna zygmunt sara maria
""".split())
